"""
會員分群 (RFM) 與自動行銷 (第三輪 #9)。

分群：以最近一次消費距今天數 (R)、近 365 天消費次數 (F)、近 365 天消費金額 (M) 各給 1~5 分 (依全部有消費會員的排名五等分)，
再對應成冠軍 / 忠實 / 新客 / 需要關注 / 快流失 / 沉睡；註冊後還沒消費的是「未消費」。已取消、未付款的訂單不計。

自動行銷 (只在線上端或單機執行：會員資料在線上端最完整，避免每家分店各發一次；發出的會員券照常同步到各分店)：
- 久未消費自動發券 (winback)：每天 10:00 檢查最後一次消費已超過 N 天的會員，發券並以 LINE 推播 (同一位會員冷卻期內只發一次)。
- 排程推播 (broadcast)：指定時間對某個分群發一次。
- 拒絕行銷的會員 (user.marketing_opt_out，會員自己在紅利商城關閉) 一律不發。
- 成效：收到後 30 天內回來消費的人數與營業額、券的使用張數。
"""
import bisect
import logging
import math
from datetime import datetime, timedelta

from sqlalchemy import func, update

from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.enums import OrderStatus
from app.models.marketing import CampaignSend, MarketingCampaign
from app.models.order import Order
from app.models.user import User
from app.services import coupon_service
from app.services.report_service import REVENUE_STATUSES
from app.sync import runtime as sync_runtime
from app.utils import safe_int

logger = logging.getLogger(__name__)

SEGMENTS = {
    'champion': {'label': '冠軍', 'desc': '最近常來、花得多', 'advice': '專屬禮遇、新品搶先試吃，不必再折價'},
    'loyal': {'label': '忠實', 'desc': '來店次數多', 'advice': '推薦寄杯 / 儲值 / 集點，維持習慣'},
    'promising': {'label': '新客', 'desc': '最近才開始消費', 'advice': '回訪券鼓勵第二、三次消費'},
    'need_attention': {'label': '需要關注', 'desc': '消費頻率與金額中等', 'advice': '推播新品、限時活動'},
    'at_risk': {'label': '快流失', 'desc': '以前常來、最近很少來', 'advice': '較高額的久未消費券把人找回來'},
    'hibernating': {'label': '沉睡', 'desc': '很久沒來、次數少', 'advice': '低成本喚回，沒有反應就不用再發'},
    'new': {'label': '未消費', 'desc': '註冊後還沒有消費', 'advice': '首購券'},
}
CONVERSION_DAYS = 30
WINDOW_DAYS = 365


def can_run():
    """自動行銷只在線上端或單機執行"""
    return not sync_runtime.is_node()


def _score(value, values, higher_is_better=True):
    """依中位排名 (同值取平均名次) 五等分給 1~5 分 (values 已排序)"""
    n = len(values)
    if n == 0:
        return 0
    pct = (bisect.bisect_left(values, value) + bisect.bisect_right(values, value)) / 2 / n
    if not higher_is_better:
        pct = 1 - pct
    return min(5, max(1, math.ceil(pct * 5)))


def segment_of(r, f, m, orders):
    if not orders:
        return 'new'
    if r >= 4 and f >= 4 and m >= 4:
        return 'champion'
    if f >= 4 and r >= 3:
        return 'loyal'
    if r >= 4 and f <= 2:
        return 'promising'
    if r <= 2 and f >= 3:
        return 'at_risk'
    if r <= 2:
        return 'hibernating'
    return 'need_attention'


def member_stats(tenant_id, now=None):
    """每位會員的 R / F / M、分數與分群：[{id, name, phone, line, opt_out, last_at, days, orders, spend, r, f, m, segment}]"""
    now = now or datetime.now()
    base = (Order.tenant_id == tenant_id, Order.user_id.is_not(None), Order.status != OrderStatus.CANCELLED,
            func.coalesce(Order.payment_status, '').in_(REVENUE_STATUSES))
    last = dict(db.session.query(Order.user_id, func.max(Order.created_at)).filter(*base).group_by(Order.user_id).all())
    window = {uid: (cnt, int(total or 0)) for uid, cnt, total in db.session.query(
        Order.user_id, func.count(Order.id), func.sum(Order.total_price))
        .filter(*base, Order.created_at >= now - timedelta(days=WINDOW_DAYS)).group_by(Order.user_id).all()}
    users = User.query.filter(User.tenant_id == tenant_id).order_by(User.id).all()
    rows = []
    for u in users:
        last_at = last.get(u.id)
        cnt, spend = window.get(u.id, (0, 0))
        rows.append({'id': u.id, 'name': u.name, 'phone': u.phone, 'line': bool(u.line_user_id),
                     'opt_out': bool(u.marketing_opt_out), 'last_at': last_at,
                     'days': (now - last_at).days if last_at else None, 'orders': cnt, 'spend': spend,
                     'has_orders': last_at is not None})
    buyers = [r for r in rows if r['has_orders']]
    days_sorted = sorted(r['days'] for r in buyers)
    freq_sorted = sorted(r['orders'] for r in buyers)
    spend_sorted = sorted(r['spend'] for r in buyers)
    for r in rows:
        if r['has_orders']:
            r['r'] = _score(r['days'], days_sorted, higher_is_better=False)
            r['f'] = _score(r['orders'], freq_sorted)
            r['m'] = _score(r['spend'], spend_sorted)
        else:
            r['r'] = r['f'] = r['m'] = 0
        r['segment'] = segment_of(r['r'], r['f'], r['m'], r['has_orders'])
    return rows


def summary(rows):
    out = {k: {'count': 0, 'spend': 0, 'line': 0, 'opt_out': 0} for k in SEGMENTS}
    for r in rows:
        s = out[r['segment']]
        s['count'] += 1
        s['spend'] += r['spend']
        s['line'] += 1 if r['line'] else 0
        s['opt_out'] += 1 if r['opt_out'] else 0
    for s in out.values():
        s['avg_spend'] = round(s['spend'] / s['count']) if s['count'] else 0
    return out


# ---------------------------------------------------------------------------
# 活動
# ---------------------------------------------------------------------------
def save_campaign(campaign, form, staff=''):
    """後台表單 → 活動 (由呼叫端 commit)"""
    name = str(form.get('name') or '').strip()[:60]
    if not name:
        raise AppError('請輸入活動名稱')
    kind = form.get('kind')
    if kind not in ('winback', 'broadcast'):
        raise AppError('請選擇活動種類')
    campaign.name, campaign.kind = name, kind
    campaign.message = str(form.get('message') or '').strip()[:500]
    coupon_id = safe_int(form.get('coupon_id'), 0)
    coupon = db.session.get(Coupon, coupon_id) if coupon_id else None
    if coupon_id and (coupon is None or coupon.tenant_id != campaign.tenant_id):
        raise AppError('查無此優惠券')
    if coupon is None and not campaign.message:
        raise AppError('請選擇要發的優惠券或輸入推播訊息')
    campaign.coupon_id = coupon.id if coupon else None
    campaign.coupon_valid_days = min(365, max(1, safe_int(form.get('coupon_valid_days'), 14)))
    if kind == 'winback':
        campaign.inactive_days = min(730, max(7, safe_int(form.get('inactive_days'), 60)))
        campaign.cooldown_days = min(730, max(7, safe_int(form.get('cooldown_days'), campaign.inactive_days)))
        campaign.segment, campaign.send_at = 'all', None
    else:
        segment = form.get('segment') or 'all'
        if segment != 'all' and segment not in SEGMENTS:
            raise AppError('請選擇分群')
        try:
            send_at = datetime.fromisoformat(str(form.get('send_at') or ''))
        except ValueError:
            raise AppError('請輸入推播時間')
        campaign.segment, campaign.send_at = segment, send_at
    campaign.created_by = campaign.created_by or staff
    campaign.status = 'active' if campaign.status in (None, '', 'done') else campaign.status
    return campaign


def targets(campaign, now=None, rows=None):
    """這次要發的會員 id (排除拒絕行銷、冷卻期內已發過的)"""
    now = now or datetime.now()
    rows = rows if rows is not None else member_stats(campaign.tenant_id, now)
    if campaign.kind == 'winback':
        picked = [r for r in rows if r['has_orders'] and r['days'] is not None and r['days'] >= campaign.inactive_days]
        recent = {uid for (uid,) in db.session.query(CampaignSend.user_id).filter(
            CampaignSend.campaign_id == campaign.id,
            CampaignSend.sent_at >= now - timedelta(days=campaign.cooldown_days)).all()} if campaign.id else set()
        picked = [r for r in picked if r['id'] not in recent]
    else:
        picked = [r for r in rows if campaign.segment == 'all' or r['segment'] == campaign.segment]
    return [r['id'] for r in picked if not r['opt_out']]


def _message(campaign, shop_name, user_name=''):
    coupon = campaign.coupon
    text = (campaign.message or '').replace('{shop}', shop_name).replace('{name}', user_name)
    if coupon is not None:
        text = text.replace('{coupon}', f'「{coupon.title}」({coupon.discount_label})')
        if '{coupon}' not in (campaign.message or ''):
            text = (text + '\n' if text else '') + f'送您一張「{coupon.title}」({coupon.discount_label})，' \
                f'{campaign.coupon_valid_days} 天內結帳時選用！'
    return text.strip()


class CampaignBusy(AppError):
    """活動正在由其他人 / 其他行程執行"""


def run_campaign(campaign, now=None, staff='system'):
    """
    執行一次：發券 + LINE 推播 + 記錄。回傳發送人數。由這裡 commit。
    先以條件式更新把狀態改為 running (同時按「立即執行」與排程到點只會發一次)，結束後改回原狀態 (排程推播改為 done)。
    """
    previous = campaign.status
    if previous not in ('active', 'paused'):
        raise CampaignBusy('此活動已發送完畢或正在執行中', 409)
    claimed = db.session.execute(update(MarketingCampaign).where(MarketingCampaign.id == campaign.id,
                                                                 MarketingCampaign.status == previous)
                                 .values(status='running').execution_options(synchronize_session=False)).rowcount
    db.session.commit()
    if not claimed:
        raise CampaignBusy('此活動正在執行中 (可能剛被其他人執行)，請稍後再看結果', 409)
    db.session.refresh(campaign)
    try:
        return _run_claimed(campaign, previous, now, staff)
    except Exception:
        db.session.rollback()
        db.session.execute(update(MarketingCampaign).where(MarketingCampaign.id == campaign.id)
                           .values(status=previous).execution_options(synchronize_session=False))
        db.session.commit()
        raise


def _run_claimed(campaign, previous, now, staff):
    from app.models.tenant import Tenant
    from app.services import line_service
    now = now or datetime.now()
    tenant_id = campaign.tenant_id
    shop = db.session.get(Tenant, tenant_id)
    coupon = campaign.coupon
    if coupon is not None and coupon.valid_until and now.date() > coupon.valid_until:
        raise AppError(f'「{coupon.title}」的活動已於 {coupon.valid_until:%Y/%m/%d} 結束')
    ids = targets(campaign, now)
    users = User.query.filter(User.id.in_(ids)).all() if ids else []
    sent = 0
    batches = {}
    if users and campaign.coupon is not None:
        for i in range(0, len(users), coupon_service.MAX_RECIPIENTS):
            chunk = users[i:i + coupon_service.MAX_RECIPIENTS]
            result = coupon_service.issue(tenant_id, campaign.coupon, chunk, valid_days=campaign.coupon_valid_days,
                                          staff=staff, source='campaign', now=now)
            for uid in result['user_ids']:
                batches[uid] = result['batch_id']
        users = [u for u in users if u.id in batches]   # 已達每人張數上限的會員略過
    pushed = set()
    if users and line_service.messaging_enabled(tenant_id):
        with_line = [u for u in users if u.line_user_id]
        if '{name}' in (campaign.message or ''):
            for u in with_line:
                if line_service.push_text(tenant_id, u.line_user_id, _message(campaign, shop.name if shop else '', u.name)):
                    pushed.add(u.id)
        elif with_line:
            try:
                line_service.multicast_text(tenant_id, [u.line_user_id for u in with_line],
                                            _message(campaign, shop.name if shop else ''))
                pushed = {u.id for u in with_line}
            except AppError as exc:
                logger.warning('自動行銷推播失敗：%s', exc.message)
    for u in users:
        if campaign.coupon is None and u.id not in pushed:
            continue   # 只推播的活動：沒有 LINE 的會員不算送達
        db.session.add(CampaignSend(tenant_id=tenant_id, campaign_id=campaign.id, user_id=u.id, sent_at=now,
                                    channel='line' if u.id in pushed else 'coupon', batch_id=batches.get(u.id, '')))
        sent += 1
    campaign.sent_count = (campaign.sent_count or 0) + sent
    campaign.last_run_at = now
    campaign.status = 'done' if campaign.kind == 'broadcast' else previous
    db.session.commit()
    return sent


def run_due(tenant_id, now=None):
    """排程呼叫：時間已到的排程推播 (每分鐘)；回傳執行的活動數"""
    if not can_run():
        return 0
    now = now or datetime.now()
    # 執行到一半行程中止 (停電、重啟) 而卡在 running 超過 30 分鐘：改回啟用
    db.session.execute(update(MarketingCampaign).where(
        MarketingCampaign.tenant_id == tenant_id, MarketingCampaign.status == 'running',
        MarketingCampaign.updated_at < now - timedelta(minutes=30)).values(status='active')
        .execution_options(synchronize_session=False))
    db.session.commit()
    due = MarketingCampaign.query.filter(MarketingCampaign.tenant_id == tenant_id, MarketingCampaign.kind == 'broadcast',
                                         MarketingCampaign.status == 'active', MarketingCampaign.send_at <= now) \
        .execution_options(skip_tenant_filter=True).all()
    for c in due:
        _safe_run(c, now)
    return len(due)


def run_winbacks(tenant_id, now=None):
    """每天一次：所有啟用中的久未消費活動"""
    if not can_run():
        return 0
    now = now or datetime.now()
    rows = MarketingCampaign.query.filter(MarketingCampaign.tenant_id == tenant_id, MarketingCampaign.kind == 'winback',
                                          MarketingCampaign.status == 'active') \
        .execution_options(skip_tenant_filter=True).all()
    return sum(_safe_run(c, now) for c in rows)


def _safe_run(campaign, now):
    from app.services import monitor_service
    try:
        return run_campaign(campaign, now)
    except CampaignBusy:
        return 0
    except AppError as exc:   # 例如券的活動期間已結束：暫停並通知
        db.session.rollback()
        campaign = db.session.get(MarketingCampaign, campaign.id)
        campaign.status = 'paused'
        db.session.commit()
        monitor_service.alert(f'自動行銷「{campaign.name}」無法執行，已暫停：{exc.message}', key=f'campaign-{campaign.id}',
                              tenant_id=campaign.tenant_id, level='warning', source='marketing')
        return 0


def stats(campaign):
    """成效：{sent, returned, revenue, coupons_used, coupons_issued}"""
    sends = CampaignSend.query.filter_by(campaign_id=campaign.id).all()
    if not sends:
        return {'sent': 0, 'returned': 0, 'revenue': 0, 'rate': 0, 'coupons_used': 0, 'coupons_issued': 0}
    first = {}
    for s in sends:
        if s.user_id not in first or s.sent_at < first[s.user_id]:
            first[s.user_id] = s.sent_at
    start = min(first.values())
    orders = Order.query.filter(Order.user_id.in_(list(first)), Order.created_at >= start,
                                Order.status != OrderStatus.CANCELLED,
                                func.coalesce(Order.payment_status, '').in_(REVENUE_STATUSES)).all()
    returned, revenue = set(), 0
    for o in orders:
        sent_at = first.get(o.user_id)
        if sent_at and sent_at <= o.created_at <= sent_at + timedelta(days=CONVERSION_DAYS):
            returned.add(o.user_id)
            revenue += o.total_price or 0
    batch_ids = {s.batch_id for s in sends if s.batch_id}
    issued = used = 0
    if batch_ids:
        issued = UserCoupon.query.filter(UserCoupon.batch_id.in_(batch_ids)).count()
        used = UserCoupon.query.filter(UserCoupon.batch_id.in_(batch_ids), UserCoupon.is_used.is_(True)).count()
    return {'sent': len(first), 'returned': len(returned), 'revenue': revenue,
            'rate': round(len(returned) / len(first) * 100, 1) if first else 0, 'coupons_used': used,
            'coupons_issued': issued}
