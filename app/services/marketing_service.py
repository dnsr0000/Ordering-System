"""
會員分群 (RFM) 與自動行銷 (#9)。

分群：以最近一次消費距今天數 (R)、近 365 天消費次數 (F)、近 365 天消費金額 (M) 各給 1~5 分 (依全部有消費會員的排名五等分)，
再對應成冠軍 / 忠實 / 新客 / 需要關注 / 快流失 / 沉睡；註冊後還沒消費的是「未消費」。已取消、未付款的訂單不計。

自動行銷 (只在線上端或單機執行：會員資料在線上端最完整，避免每家分店各發一次；發出的會員券照常同步到各分店)：
- 久未消費自動發券 (winback)：每天 10:00 檢查最後一次消費已超過 N 天的會員，發券並以 LINE 推播 (同一位會員冷卻期內只發一次)。
- 排程推播 (broadcast)：指定時間對某個分群發一次。
- 拒絕行銷的會員 (user.marketing_opt_out，會員自己在紅利商城關閉) 一律不發。
- 成效：每次發送後 30 天內回來消費的次數與營業額、券的使用張數 (同一位會員收到多次時每次分別計算)。
- 推播在資料庫交易之外進行 (發券與紀錄先提交)，避免 SQLite 門市推播期間鎖住點餐機 / POS 的寫入。
"""
import bisect
import logging
import math
import time as _time
from datetime import date, datetime, timedelta

from sqlalchemy import case, func, update

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
    since = now - timedelta(days=WINDOW_DAYS)
    last = dict(db.session.query(Order.user_id, func.max(Order.created_at)).filter(*base).group_by(Order.user_id).all())
    window = {uid: (cnt, int(total or 0)) for uid, cnt, total in db.session.query(
        Order.user_id, func.count(Order.id), func.sum(Order.total_price))
        .filter(*base, Order.created_at >= since).group_by(Order.user_id).all()}
    # 已封存的舊訂單 (已從主資料庫刪除) 也要算：否則老顧客被當成「未消費」，久未消費活動永遠發不到他們
    archived_last, archived_window = _archived_orders(tenant_id, since)
    for uid, at in archived_last.items():
        if at is not None and (last.get(uid) is None or at > last[uid]):
            last[uid] = at
    for uid, (cnt, total) in archived_window.items():
        cnt0, total0 = window.get(uid, (0, 0))
        window[uid] = (cnt0 + cnt, total0 + total)
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


def _archived_orders(tenant_id, since):
    """封存檔 (舊資料封存) 裡的會員消費：({user_id: 最後消費時間}, {user_id: (since 之後的次數, 金額)})；沒有封存檔時都是空的"""
    import os

    import sqlalchemy as sa

    from app.services import archive_service
    path = archive_service.archive_path(tenant_id)
    if not os.path.exists(path):
        return {}, {}
    t = sa.table('order', sa.column('tenant_id', sa.Integer), sa.column('user_id', sa.Integer),
                 sa.column('status', sa.String), sa.column('payment_status', sa.String),
                 sa.column('created_at', sa.DateTime), sa.column('total_price', sa.Integer))
    cond = (t.c.tenant_id == tenant_id, t.c.user_id.is_not(None), t.c.status != OrderStatus.CANCELLED,
            func.coalesce(t.c.payment_status, '').in_(REVENUE_STATUSES))
    engine = sa.create_engine('sqlite:///' + path)
    try:
        with engine.connect() as conn:
            last = dict(conn.execute(sa.select(t.c.user_id, func.max(t.c.created_at)).where(*cond)
                                     .group_by(t.c.user_id)).all())
            window = {uid: (cnt, int(total or 0)) for uid, cnt, total in conn.execute(
                sa.select(t.c.user_id, func.count(), func.sum(t.c.total_price)).where(*cond, t.c.created_at >= since)
                .group_by(t.c.user_id)).all()}
        return last, window
    except sa.exc.SQLAlchemyError:   # 封存檔損毀 / 版本不符：分群照常 (只看主資料庫)
        logger.warning('讀取封存檔的會員消費失敗', exc_info=True)
        return {}, {}
    finally:
        engine.dispose()


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
    if coupon is None and '{coupon}' in campaign.message:
        raise AppError('訊息使用了 {coupon}，請選擇要發送的優惠券')
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
            # 券尚未開始時，N 天從活動開始日起算 (Coupon.expires_for)：只寫「N 天內」會讓會員以為今天就能用
            when = f'{coupon.valid_from:%m/%d} 起 ' if coupon.valid_from and coupon.valid_from > date.today() else ''
            text = (text + '\n' if text else '') + f'送您一張「{coupon.title}」({coupon.discount_label})，' \
                f'{when}{campaign.coupon_valid_days} 天內結帳時選用！'
    else:   # 沒有券 (只推播，或券已被刪除)：不可把「{coupon}」原樣送給會員
        text = text.replace('{coupon}', '')
    return text.strip()


class CampaignBusy(AppError):
    """活動正在由其他人 / 其他行程執行"""


def run_campaign(campaign, now=None, staff='system'):
    """
    執行一次：發券 + LINE 推播 + 記錄。回傳發送人數。由這裡 commit。
    先以條件式更新把狀態改為 running (同時按「立即執行」與排程到點只會發一次)，結束後改回原狀態 (排程推播改為 done)。
    """
    previous, campaign_id = campaign.status, campaign.id   # except 裡 rollback 後物件已過期 (可能已被刪除)，不可再讀屬性
    if previous not in ('active', 'paused'):
        raise CampaignBusy('此活動已發送完畢或正在執行中', 409)
    claimed = db.session.execute(update(MarketingCampaign).where(MarketingCampaign.id == campaign.id,
                                                                 MarketingCampaign.status == previous)
                                 .values(status='running', previous_status=previous)
                                 .execution_options(synchronize_session=False)).rowcount
    db.session.commit()
    if not claimed:
        raise CampaignBusy('此活動正在執行中 (可能剛被其他人執行)，請稍後再看結果', 409)
    db.session.refresh(campaign)
    try:
        return _run_claimed(campaign, previous, now, staff)
    except Exception:
        db.session.rollback()
        db.session.execute(update(MarketingCampaign).where(MarketingCampaign.id == campaign_id)
                           .values(status=previous).execution_options(synchronize_session=False))
        db.session.commit()
        raise


def _run_claimed(campaign, previous, now, staff):
    """
    資料庫異動先提交、再推播 (推播是逐一 / 分批的 HTTP 請求，可能很久)：SQLite 整個資料庫同時只能有一個寫入交易，
    推播期間持有寫入鎖會讓點餐機 / POS 結帳 database is locked；先提交也避免「訊息已送出、券卻因交易失敗沒發出」。
    """
    from app.models.tenant import Tenant
    from app.services import line_service
    now = now or datetime.now()
    tenant_id, campaign_id, name = campaign.tenant_id, campaign.id, campaign.name
    shop = db.session.get(Tenant, tenant_id)
    shop_name = shop.name if shop else ''
    coupon = campaign.coupon
    if coupon is None and not (campaign.message or '').replace('{coupon}', '').strip():
        # 券已被刪除、活動又沒有推播訊息：不可送出空白訊息，也不可維持啟用 (每天都空跑)
        logger.warning('自動行銷「%s」的優惠券已刪除且沒有推播訊息，已暫停', name)
        campaign.status, campaign.last_run_at = 'paused', now
        db.session.commit()
        return 0
    if coupon is not None and coupon.valid_until and now.date() > coupon.valid_until:
        raise AppError(f'「{coupon.title}」的活動已於 {coupon.valid_until:%Y/%m/%d} 結束')
    ids = targets(campaign, now)
    users = User.query.filter(User.id.in_(ids)).all() if ids else []
    batches = {}
    if users and coupon is not None:
        for i in range(0, len(users), coupon_service.MAX_RECIPIENTS):
            chunk = users[i:i + coupon_service.MAX_RECIPIENTS]
            result = coupon_service.issue(tenant_id, coupon, chunk, valid_days=campaign.coupon_valid_days,
                                          staff=staff, source='campaign', now=now)
            for uid in result['user_ids']:
                batches[uid] = result['batch_id']
        users = [u for u in users if u.id in batches]   # 已達每人張數上限的會員略過
    # 推播對象與內容 (提交前先取出：提交後物件會過期、逐一重新讀取)
    per_user = '{name}' in (campaign.message or '')
    recipients = []
    if users and line_service.messaging_enabled(tenant_id):
        common = None if per_user else _message(campaign, shop_name)
        recipients = [(u.id, u.line_user_id, _message(campaign, shop_name, u.name) if per_user else common)
                      for u in users if u.line_user_id]
    user_ids = [u.id for u in users]
    if coupon is not None:
        # 發券的活動：券、發送紀錄 (先記為只發券) 與活動結果一起提交，推播成功的再改為 line
        for uid in user_ids:
            db.session.add(CampaignSend(tenant_id=tenant_id, campaign_id=campaign_id, user_id=uid, sent_at=now,
                                        channel='coupon', batch_id=batches[uid]))
        _finish(campaign, previous, now, len(user_ids))
        db.session.commit()
        try:
            pushed, error = _push(tenant_id, campaign_id, recipients, per_user)
            _mark_line(campaign_id, pushed, set(batches.values()))
            db.session.commit()
        except Exception as exc:  # noqa: BLE001 —— 券已發出並提交：推播出錯不可讓活動回到執行前狀態 (排程推播會再發一次券)
            db.session.rollback()
            logger.exception('自動行銷推播失敗')
            error = f'推播時發生錯誤：{exc}'
        if error:
            _push_alert(name, campaign_id, tenant_id, f'券已發出，但 LINE 推播未全部送達：{error}')
        return len(user_ids)
    # 只推播的活動：推播期間不持有交易；送達的會員才記錄
    db.session.commit()
    pushed, error = _push(tenant_id, campaign_id, recipients, per_user)
    if recipients and not pushed:
        # 一則都沒送出 (額度用完、Token 過期、連不上 LINE)：往外拋，活動回到執行前狀態 (可重送)，排程由 _safe_run 暫停並通知
        raise AppError(f'LINE 推播失敗，沒有會員收到：{error}')
    if error:
        _push_alert(name, campaign_id, tenant_id, f'LINE 推播只送達部分會員：{error}')
    sent = 0
    for uid in user_ids:
        if uid in pushed:   # 沒有 LINE / 沒送達的會員不算送達
            db.session.add(CampaignSend(tenant_id=tenant_id, campaign_id=campaign_id, user_id=uid, sent_at=now,
                                        channel='line', batch_id=''))
            sent += 1
    _finish(campaign, previous, now, sent)
    db.session.commit()
    return sent


def _finish(campaign, previous, now, sent):
    campaign.sent_count = (campaign.sent_count or 0) + sent
    campaign.last_run_at = now
    campaign.status = 'done' if campaign.kind == 'broadcast' else previous


HEARTBEAT_SECONDS = 60


def _heartbeat(campaign_id):
    """長時間推播時定期更新 updated_at：run_due 才不會把執行中的活動當成卡住 (改回啟用後再執行一次)"""
    db.session.execute(update(MarketingCampaign).where(MarketingCampaign.id == campaign_id,
                                                       MarketingCampaign.status == 'running')
                       .values(updated_at=datetime.now()).execution_options(synchronize_session=False))
    db.session.commit()


def _push(tenant_id, campaign_id, recipients, per_user):
    """
    LINE 推播 (不可在寫入交易中呼叫)；recipients = [(會員 id, LINE user id, 訊息)]。回傳 (送達的會員 id, 錯誤訊息)。
    訊息含 {name} 時逐一推播，否則群發；群發後面的批次失敗時只算前面已送出的會員。
    """
    from app.services import line_service
    if not recipients:
        return set(), ''
    if per_user:
        pushed, beat = set(), _time.monotonic()
        for uid, line_user_id, text in recipients:
            if line_service.push_text(tenant_id, line_user_id, text):
                pushed.add(uid)
            if _time.monotonic() - beat >= HEARTBEAT_SECONDS:
                _heartbeat(campaign_id)
                beat = _time.monotonic()
        return pushed, ('' if pushed else 'LINE 推播全部失敗 (請檢查 LINE 官方帳號 Token 與當月訊息額度)')
    line_ids = list(dict.fromkeys(line_user_id for _uid, line_user_id, _text in recipients))
    try:
        result = line_service.multicast_text(tenant_id, line_ids, recipients[0][2])
    except AppError as exc:   # 第一批就失敗：什麼都沒送出
        return set(), exc.message
    error = getattr(result, 'error', '') or ''
    delivered = set(line_ids[:int(result)]) if error else set(line_ids)   # 依送出順序，前 int(result) 位已送出
    return {uid for uid, line_user_id, _text in recipients if line_user_id in delivered}, error


def _mark_line(campaign_id, pushed, batch_ids):
    """發券活動推播成功的會員：發送紀錄改為 line"""
    pushed = sorted(pushed)
    for i in range(0, len(pushed), 500):
        db.session.execute(update(CampaignSend).where(CampaignSend.campaign_id == campaign_id,
                                                      CampaignSend.batch_id.in_(batch_ids),
                                                      CampaignSend.user_id.in_(pushed[i:i + 500]))
                           .values(channel='line').execution_options(synchronize_session=False))


def _push_alert(name, campaign_id, tenant_id, message):
    from app.services import monitor_service
    logger.warning('自動行銷「%s」%s', name, message)
    monitor_service.alert(f'自動行銷「{name}」{message}', key=f'campaign-push-{campaign_id}', tenant_id=tenant_id,
                          level='warning', source='marketing')


def run_due(tenant_id, now=None):
    """排程呼叫：時間已到的排程推播 (每分鐘)；回傳執行的活動數"""
    if not can_run():
        return 0
    now = now or datetime.now()
    # 執行到一半行程中止 (停電、重啟) 而卡在 running 超過 30 分鐘：恢復執行前的狀態 (原本暫停的維持暫停，其餘改回啟用)
    db.session.execute(update(MarketingCampaign).where(
        MarketingCampaign.tenant_id == tenant_id, MarketingCampaign.status == 'running',
        MarketingCampaign.updated_at < now - timedelta(minutes=30))
        .values(status=case((MarketingCampaign.previous_status == 'paused', 'paused'), else_='active'))
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
    campaign_id = campaign.id
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
    except Exception:  # noqa: BLE001 —— 例如執行中活動被刪除 (StaleDataError / IntegrityError)：不可中斷後面的店家與每日工作
        db.session.rollback()
        logger.exception('自動行銷 (活動 #%s) 執行失敗', campaign_id)
        return 0


def stats(campaign):
    """
    成效：{sent, members, returned, revenue, rate, coupons_used, coupons_issued}。
    以每一次發送計算 (久未消費活動過了冷卻期會再發給同一位會員)：sent 是發送次數 (與發券張數同一基準)，
    members 是不重複人數；訂單算在它之前最近一次發送，落在該次發送後 30 天內就算這次回購 (同一張訂單只算一次)。
    """
    sends = CampaignSend.query.filter_by(campaign_id=campaign.id).all()
    if not sends:
        return {'sent': 0, 'members': 0, 'returned': 0, 'revenue': 0, 'rate': 0, 'coupons_used': 0,
                'coupons_issued': 0}
    times = {}
    for s in sends:
        times.setdefault(s.user_id, []).append(s.sent_at)
    for v in times.values():
        v.sort()
    start = min(v[0] for v in times.values())
    orders = Order.query.filter(Order.user_id.in_(list(times)), Order.created_at >= start,
                                Order.status != OrderStatus.CANCELLED,
                                func.coalesce(Order.payment_status, '').in_(REVENUE_STATUSES)).all()
    converted, revenue = set(), 0
    for o in orders:
        user_times = times.get(o.user_id) or []
        i = bisect.bisect_right(user_times, o.created_at) - 1   # 這張訂單之前最近一次發送
        if i >= 0 and o.created_at <= user_times[i] + timedelta(days=CONVERSION_DAYS):
            converted.add((o.user_id, i))
            revenue += o.total_price or 0
    batch_ids = {s.batch_id for s in sends if s.batch_id}
    issued = used = 0
    if batch_ids:
        issued = UserCoupon.query.filter(UserCoupon.batch_id.in_(batch_ids)).count()
        used = UserCoupon.query.filter(UserCoupon.batch_id.in_(batch_ids), UserCoupon.is_used.is_(True)).count()
    return {'sent': len(sends), 'members': len(times), 'returned': len(converted), 'revenue': revenue,
            'rate': round(len(converted) / len(sends) * 100, 1), 'coupons_used': used, 'coupons_issued': issued}
