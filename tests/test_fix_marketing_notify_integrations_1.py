"""修正小組 marketing-notify-integrations-1：Webhook 與自動行銷的回歸測試"""
import json
import sqlite3
import zlib
from datetime import datetime, timedelta

import pytest

from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon
from app.models.integration import WebhookDelivery, WebhookEndpoint
from app.models.marketing import CampaignSend, MarketingCampaign
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.user import User
from app.services import line_service, marketing_service, monitor_service, webhook_service
from tests.support import get_or_create_tenant, tenant_request


@pytest.fixture
def alerts(monkeypatch):
    out = []
    monkeypatch.setattr(monitor_service, 'alert', lambda msg, **kw: out.append(msg))
    return out


def _sqlite_path():
    url = db.engine.url
    return url.database if url.get_backend_name() == 'sqlite' else None


def _write_lock_free(path):
    """另開連線搶寫入鎖：搶得到代表目前沒有交易持有 SQLite 寫入鎖"""
    conn = sqlite3.connect(path, timeout=0.1)
    try:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('ROLLBACK')
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()


def _endpoint(tid, created_at=None, events=None):
    ep = WebhookEndpoint(tenant_id=tid, name='ERP', url='https://erp.example.test/h',
                         events=','.join(events or webhook_service.WEBHOOK_EVENTS),
                         created_at=created_at or datetime.now() - timedelta(days=3))
    db.session.add(ep)
    db.session.commit()
    return ep


def _events(ep_id):
    return [(json.loads(d.payload)['data']['id'], d.event) for d in
            WebhookDelivery.query.filter_by(endpoint_id=ep_id).order_by(WebhookDelivery.id).all()]


def _order(tid, **kw):
    o = Order(tenant_id=tid, total_price=kw.pop('total_price', 80), order_type='外帶', pickup_number=0, **kw)
    db.session.add(o)
    db.session.commit()
    return o


# ---------------------------------------------------------------------------
# B-349：雙協定目標 IPv4 優先，連不上時改試下一個 IP
# ---------------------------------------------------------------------------
def test_webhook_prefers_ipv4_and_falls_back_to_next_ip(app, monkeypatch):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    with app.app_context():
        monkeypatch.setattr(webhook_service, 'resolve_ips', lambda host: ['2600:1f18::1', '52.1.2.3'])
        assert webhook_service.check_public_host('hook.example.test') == '52.1.2.3'
        assert webhook_service.check_public_all('https://hook.example.test/x') == ['52.1.2.3', '2600:1f18::1']
    got = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got['body'] = self.rfile.read(int(self.headers['Content-Length'])).decode()
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass
    server = HTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.handle_request, daemon=True).start()
    port = server.server_address[1]
    # 第一個 IP 連不上 (IPv6 迴路沒有服務 / 主機沒有 IPv6)：改連第二個
    code = webhook_service._post(f'http://hook.example.test:{port}/x', '{"a": 1}', {}, ip=['::1', '127.0.0.1'])
    server.server_close()
    assert code == 200 and got['body'] == '{"a": 1}'


# ---------------------------------------------------------------------------
# B-350：送出期間不持有 SQLite 寫入鎖
# ---------------------------------------------------------------------------
def test_webhook_delivery_does_not_hold_write_lock_while_posting(app, monkeypatch):
    tid = get_or_create_tenant(app, 'fix-mni1-hook-lock')
    monkeypatch.setattr(webhook_service, 'resolve_ips', lambda host: ['93.184.216.34'])
    with tenant_request(app, tid):
        path = _sqlite_path()
        if path is None:
            pytest.skip('只有 SQLite 有整個資料庫的寫入鎖')
        ep = _endpoint(tid)
        webhook_service.queue_ping(ep)
        db.session.commit()
        seen = []

        def post(url, body, headers):
            seen.append(_write_lock_free(path))
            status = db.session.execute(db.select(WebhookDelivery.status).where(
                WebhookDelivery.endpoint_id == ep.id)).scalar()
            seen.append(status)
            return 200
        assert webhook_service.deliver_due(tid, post=post) == 1
        assert seen == [True, 'sending']                     # 送出時：沒有寫入鎖、這筆仍是 sending (不會被重複搶)
        d = WebhookDelivery.query.filter_by(endpoint_id=ep.id).one()
        assert (d.status, d.attempts) == ('sent', 1)
        webhook_service.queue_ping(ep)
        db.session.commit()
        webhook_service.deliver_due(tid, post=lambda u, b, h: 500)
        d = WebhookDelivery.query.filter_by(endpoint_id=ep.id).order_by(WebhookDelivery.id.desc()).first()
        assert (d.status, d.attempts, d.last_error) == ('pending', 1, 'HTTP 500')


# ---------------------------------------------------------------------------
# B-355：網址的主機名稱 / 埠號不合法：儲存時拒絕；舊資料送出時記為失敗而不是卡在 sending
# ---------------------------------------------------------------------------
def test_webhook_invalid_host_or_port(app):
    with app.app_context():
        for url in ('https://a..b/hook', 'https://example.com:99999/hook', 'https://example.com:84a/hook',
                    'https://' + 'a' * 64 + '.com/hook', 'https://127.0.0.1:8080\\@example.com/hook'):
            with pytest.raises(AppError):
                webhook_service.validate_url(url)
        with pytest.raises(AppError, match='無法解析主機'):
            webhook_service.check_public_host('a..b')
    tid = get_or_create_tenant(app, 'fix-mni1-hook-bad')
    with tenant_request(app, tid):
        ep = _endpoint(tid)
        ep.url = 'https://a..b/hook'   # 舊資料 (修正前存進去的)
        db.session.commit()
        webhook_service.queue_ping(ep)
        db.session.commit()
        assert webhook_service.deliver_due(tid) == 0
        d = WebhookDelivery.query.filter_by(endpoint_id=ep.id).one()
        assert d.status == 'pending' and d.attempts == 1 and '無法解析主機' in d.last_error
        assert db.session.get(WebhookEndpoint, ep.id).fail_streak == 1


# ---------------------------------------------------------------------------
# R2-123：檢查實際連線的主機 (反斜線繞過)
# ---------------------------------------------------------------------------
def test_check_public_uses_host_requests_connects_to(app):
    with app.app_context():
        assert webhook_service.connect_host('http://127.0.0.1:8080\\@example.com/x') == '127.0.0.1'
        with pytest.raises(AppError, match='不可送到內網'):
            webhook_service.check_public('http://127.0.0.1:8080\\@example.com/bucket/x')


# ---------------------------------------------------------------------------
# B-356 / R2-130 / R2-126：事件
# ---------------------------------------------------------------------------
def test_webhook_first_scan_skips_old_orders_entirely(app):
    tid = get_or_create_tenant(app, 'fix-mni1-hook-first')
    with tenant_request(app, tid):
        old = _order(tid, status='Cancelled', payment_status='', created_at=datetime.now() - timedelta(days=1))
        ep = _endpoint(tid, created_at=datetime.now())
        webhook_service.scan(tid)
        assert _events(ep.id) == []                          # 端點建立前就取消的舊訂單：不送 order.cancelled
        assert old.id


def test_webhook_paid_at_checkout_sends_paid(app):
    tid = get_or_create_tenant(app, 'fix-mni1-hook-paid')
    with tenant_request(app, tid):
        ep = _endpoint(tid, events=['order.paid'])
        cash = _order(tid, status='Pending', payment_status='', payment_method='Cash')
        counter = _order(tid, status='Pending', payment_status='unpaid', payment_method='Cash')
        webhook_service.scan(tid)
        assert _events(ep.id) == [(cash.id, 'order.paid')]
        counter.payment_status = ''
        db.session.commit()
        webhook_service.scan(tid)
        assert _events(ep.id) == [(cash.id, 'order.paid'), (counter.id, 'order.paid')]


def test_webhook_cancelled_then_paid_sends_cancelled_not_paid(app):
    tid = get_or_create_tenant(app, 'fix-mni1-hook-refund')
    with tenant_request(app, tid):
        ep = _endpoint(tid)
        o = _order(tid, status='Pending', payment_status='pending', payment_method='Credit Card')
        assert webhook_service.scan(tid) == 0
        o.status, o.payment_status = 'Cancelled', 'expired'      # 逾時取消
        db.session.commit()
        assert webhook_service.scan(tid) == 0
        o.payment_status = 'refund_required'                     # 之後才收到付款
        db.session.commit()
        webhook_service.scan(tid)
        assert _events(ep.id) == [(o.id, 'order.created'), (o.id, 'order.cancelled')]
        assert webhook_service.scan(tid) == 0


# ---------------------------------------------------------------------------
# 自動行銷
# ---------------------------------------------------------------------------
def _members(tid, n, prefix):
    users = []
    for i in range(n):
        u = User(tenant_id=tid, name=f'{prefix}{i}', phone=f'09{zlib.crc32(prefix.encode()) % 10000:04d}{i:04d}', photo_path='',
                 line_user_id=f'U{prefix}{i}')
        db.session.add(u)
        users.append(u)
    db.session.commit()
    return users


def _line_on(tid):
    from app.services import settings_service
    settings_service.put('line', {'messaging_enabled': True, 'messaging_access_token': 'bot'}, tid)
    db.session.commit()


def test_campaign_push_outside_transaction_and_marks_line(app, monkeypatch):
    """B-342：推播時沒有持有寫入鎖；券先提交，推播成功的改記為 line"""
    tid = get_or_create_tenant(app, 'fix-mni1-mkt-lock')
    with tenant_request(app, tid):
        path = _sqlite_path()
        _line_on(tid)
        users = _members(tid, 2, 'lock')
        coupon = Coupon(tenant_id=tid, code='LOCKFREE', title='推播券', discount_value=10, is_reward=False)
        db.session.add(coupon)
        db.session.commit()
        seen = []

        def fake_post(url, **kw):
            if path:
                seen.append(_write_lock_free(path))
            return 200, {}
        monkeypatch.setattr(line_service, '_post', fake_post)
        c = MarketingCampaign(tenant_id=tid, name='全體推播', kind='broadcast', segment='all', coupon_id=coupon.id,
                              message='{name} 送您 {coupon}', send_at=datetime.now() - timedelta(minutes=1))
        db.session.add(c)
        db.session.commit()
        assert marketing_service.run_campaign(c) == 2
        assert len(seen) == 2 and all(seen) if path else True
        sends = CampaignSend.query.filter_by(campaign_id=c.id).all()
        assert sorted(s.user_id for s in sends) == sorted(u.id for u in users)
        assert {s.channel for s in sends} == {'line'}
        assert db.session.get(MarketingCampaign, c.id).status == 'done'


def test_push_only_campaign_failure_pauses_and_partial_counts(app, monkeypatch, alerts):
    """B-344：只推播的活動全部失敗 → 暫停並通知、可重送；群發部分失敗只記錄已送出的會員"""
    tid = get_or_create_tenant(app, 'fix-mni1-mkt-fail')
    with tenant_request(app, tid):
        _line_on(tid)
        users = _members(tid, 2, 'fail')
        monkeypatch.setattr(line_service, '_post', lambda url, **kw: (429, {'message': 'quota'}))
        c = MarketingCampaign(tenant_id=tid, name='只推播', kind='broadcast', segment='all', message='新品上市',
                              send_at=datetime.now() - timedelta(minutes=1))
        db.session.add(c)
        db.session.commit()
        assert marketing_service.run_due(tid) == 1
        c = db.session.get(MarketingCampaign, c.id)
        assert c.status == 'paused' and CampaignSend.query.filter_by(campaign_id=c.id).count() == 0
        assert any('只推播' in a for a in alerts)

        result = line_service.MulticastResult(1)
        result.error = '後面的批次失敗'
        monkeypatch.setattr(line_service, 'multicast_text', lambda tenant_id, ids, text: result)
        assert marketing_service.run_campaign(c) == 1
        sends = CampaignSend.query.filter_by(campaign_id=c.id).all()
        assert [s.user_id for s in sends] == [users[0].id]
        assert db.session.get(MarketingCampaign, c.id).status == 'done'
        assert any('部分' in a for a in alerts)


def test_campaign_heartbeat_keeps_running_campaign(app):
    """B-354：執行中定期更新 updated_at，run_due 不會把它當成卡住"""
    tid = get_or_create_tenant(app, 'fix-mni1-mkt-beat')
    with tenant_request(app, tid):
        c = MarketingCampaign(tenant_id=tid, name='執行中', kind='broadcast', segment='all', message='hi',
                              send_at=datetime.now() - timedelta(hours=2), status='running')
        db.session.add(c)
        db.session.commit()
        db.session.execute(db.update(MarketingCampaign).where(MarketingCampaign.id == c.id)
                           .values(updated_at=datetime.now() - timedelta(hours=1)))
        db.session.commit()
        marketing_service._heartbeat(c.id)
        assert marketing_service.run_due(tid) == 0
        assert db.session.get(MarketingCampaign, c.id).status == 'running'


def test_stuck_running_campaign_restores_previous_status(app, monkeypatch):
    """B-354：搶下執行權時記錄原狀態；卡住超過 30 分鐘恢復時，原本暫停的維持暫停，原本啟用的改回啟用"""
    tid = get_or_create_tenant(app, 'fix-mni1-mkt-stuck')
    with tenant_request(app, tid):
        paused = MarketingCampaign(tenant_id=tid, name='暫停中', kind='winback', segment='all', message='hi',
                                   status='paused')
        db.session.add(paused)
        db.session.commit()
        seen = []
        monkeypatch.setattr(marketing_service, '_run_claimed',
                            lambda campaign, previous, now, staff: seen.append(
                                (campaign.status, campaign.previous_status)) or 0)
        marketing_service.run_campaign(paused)
        assert seen == [('running', 'paused')]   # 執行中，原狀態已記錄

        active = MarketingCampaign(tenant_id=tid, name='排程', kind='broadcast', segment='all', message='hi',
                                   send_at=datetime.now() + timedelta(days=1), status='running',
                                   previous_status='active')
        db.session.add(active)
        db.session.commit()
        old = datetime.now() - timedelta(hours=1)
        db.session.execute(db.update(MarketingCampaign).where(MarketingCampaign.id.in_([paused.id, active.id]))
                           .values(status='running', updated_at=old))
        db.session.commit()
        marketing_service.run_due(tid)
        assert db.session.get(MarketingCampaign, paused.id).status == 'paused'
        assert db.session.get(MarketingCampaign, active.id).status == 'active'


def test_message_coupon_placeholder_requires_coupon(app):
    """R2-128"""
    tid = get_or_create_tenant(app, 'fix-mni1-mkt-coupon')
    with tenant_request(app, tid):
        c = MarketingCampaign(tenant_id=tid)
        form = {'name': '想念你', 'kind': 'winback', 'message': '{name} 好久不見！送您 {coupon}'}
        with pytest.raises(AppError, match='coupon'):
            marketing_service.save_campaign(c, form)
        c.message, c.coupon_id = '送您 {coupon}！', None
        assert '{coupon}' not in marketing_service._message(c, '本店')


def test_stats_count_every_send(app):
    """R2-129：第二次發送後的回購也算；發送次數與券數同一基準"""
    tid = get_or_create_tenant(app, 'fix-mni1-mkt-stats')
    with tenant_request(app, tid):
        u = _members(tid, 1, 'stats')[0]
        c = MarketingCampaign(tenant_id=tid, name='久未消費', kind='winback', message='hi')
        db.session.add(c)
        db.session.commit()
        t0 = datetime.now() - timedelta(days=120)
        for at in (t0, t0 + timedelta(days=100)):
            db.session.add(CampaignSend(tenant_id=tid, campaign_id=c.id, user_id=u.id, sent_at=at, channel='line'))
        _order(tid, status='PickedUp', payment_status='', user_id=u.id, total_price=150,
               created_at=t0 + timedelta(days=105))
        st = marketing_service.stats(c)
        assert (st['sent'], st['members'], st['returned'], st['revenue']) == (2, 1, 1, 150)


def test_member_stats_include_archived_orders(app):
    """R2-125：訂單全部封存的老顧客不是「未消費」，久未消費活動挑得到"""
    from app.services import archive_service, settings_service
    tid = get_or_create_tenant(app, 'fix-mni1-mkt-archive')
    with tenant_request(app, tid):
        settings_service.put('archive', {'months': 6}, tid)
        u = _members(tid, 1, 'arch')[0]
        item = MenuItem(tenant_id=tid, name='封存行銷餐', price=100, stock=99, total_stock=99)
        db.session.add(item)
        db.session.commit()
        old = datetime.now() - timedelta(days=300)
        _order(tid, status='PickedUp', payment_status='', user_id=u.id, total_price=200, created_at=old)
        archive_service.run(tid, archive_service.cutoff_for(tid))
        assert Order.query.filter_by(user_id=u.id).count() == 0
        row = next(r for r in marketing_service.member_stats(tid) if r['id'] == u.id)
        assert row['has_orders'] and row['segment'] != 'new' and row['days'] == 300
        assert row['orders'] == 1 and row['spend'] == 200
        wb = MarketingCampaign(tenant_id=tid, name='喚回', kind='winback', inactive_days=180, cooldown_days=60,
                               message='hi')
        db.session.add(wb)
        db.session.commit()
        assert u.id in marketing_service.targets(wb)


# ---------------------------------------------------------------------------
# 跨檔請求：B-493 / B-496 / R2-124 / R2-127
# ---------------------------------------------------------------------------
def test_message_mentions_start_date_when_coupon_not_started(app):
    """B-493：券尚未開始時，訊息寫出開始日 (N 天從開始日起算)"""
    from datetime import date
    tid = get_or_create_tenant(app, 'fix-mni1-req-msg')
    with tenant_request(app, tid):
        start = date.today() + timedelta(days=10)
        coupon = Coupon(tenant_id=tid, code='LATER493', title='晚開始', discount_value=10, is_reward=False,
                        valid_from=start)
        db.session.add(coupon)
        db.session.commit()
        c = MarketingCampaign(tenant_id=tid, name='預告', kind='broadcast', segment='all', coupon_id=coupon.id,
                              coupon_valid_days=14, message='')
        db.session.add(c)
        db.session.commit()
        assert f'{start:%m/%d} 起 14 天內結帳時選用' in marketing_service._message(c, '本店')
        coupon.valid_from = None
        db.session.commit()
        text = marketing_service._message(c, '本店')
        assert '14 天內結帳時選用' in text and ' 起 ' not in text


def test_campaign_with_deleted_coupon_and_no_message_pauses(app, monkeypatch):
    """B-496：券已刪除且沒有訊息：暫停，不送空白推播"""
    tid = get_or_create_tenant(app, 'fix-mni1-req-blank')
    with tenant_request(app, tid):
        _line_on(tid)
        _members(tid, 1, 'blank')
        calls = []
        monkeypatch.setattr(line_service, '_post', lambda url, **kw: calls.append(kw) or (200, {}))
        c = MarketingCampaign(tenant_id=tid, name='空白活動', kind='broadcast', segment='all', coupon_id=None,
                              message='{coupon}', send_at=datetime.now() - timedelta(minutes=1))
        db.session.add(c)
        db.session.commit()
        assert marketing_service.run_campaign(c) == 0
        c = db.session.get(MarketingCampaign, c.id)
        assert c.status == 'paused' and c.last_run_at is not None
        assert calls == [] and CampaignSend.query.filter_by(campaign_id=c.id).count() == 0


def test_campaign_deleted_while_running_does_not_break_schedule(app, monkeypatch):
    """R2-124：執行中被刪除 → 不丟 ObjectDeletedError；排程的 _safe_run 攔下非 AppError 的例外"""
    tid = get_or_create_tenant(app, 'fix-mni1-req-del')
    with tenant_request(app, tid):
        c = MarketingCampaign(tenant_id=tid, name='被刪除', kind='broadcast', segment='all', message='hi',
                              send_at=datetime.now() - timedelta(minutes=1))
        db.session.add(c)
        db.session.commit()
        cid = c.id

        def boom(campaign, previous, now, staff):
            db.session.execute(db.delete(MarketingCampaign).where(MarketingCampaign.id == cid))
            db.session.commit()
            raise RuntimeError('flush 失敗')
        monkeypatch.setattr(marketing_service, '_run_claimed', boom)
        with pytest.raises(RuntimeError):   # 原本的例外往外拋 (不是 rollback 後讀屬性的 ObjectDeletedError)
            marketing_service.run_campaign(c)

        c2 = MarketingCampaign(tenant_id=tid, name='排程', kind='broadcast', segment='all', message='hi',
                               send_at=datetime.now() - timedelta(minutes=1))
        db.session.add(c2)
        db.session.commit()
        cid = c2.id
        assert marketing_service.run_due(tid) == 1   # 沒有往外拋到每日排程
        assert db.session.get(MarketingCampaign, cid) is None


def test_inactive_endpoint_delivery_is_postponed_not_failed(app, alerts):
    """R2-127：停用中的端點不累加次數與連續失敗，只延後"""
    tid = get_or_create_tenant(app, 'fix-mni1-req-hookoff')
    with tenant_request(app, tid):
        ep = _endpoint(tid)
        webhook_service.queue_ping(ep)
        ep.is_active = False
        db.session.commit()
        posted = []
        now = datetime.now()
        assert webhook_service.deliver_due(tid, now=now, post=lambda *a: posted.append(a) or 200) == 0
        d = WebhookDelivery.query.filter_by(endpoint_id=ep.id).one()
        assert (d.status, d.attempts, d.last_error) == ('pending', 0, '端點已停用')
        assert d.next_attempt_at >= now + timedelta(minutes=59)
        assert db.session.get(WebhookEndpoint, ep.id).fail_streak == 0
        assert posted == [] and alerts == []
