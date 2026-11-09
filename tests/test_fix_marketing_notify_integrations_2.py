"""
修正小組 marketing-notify-integrations-2 的回歸測試：
Web Push 的 SSRF / 訂閱保留 / 外送文字 / 線上端補發、行銷頁權限與分群參數、執行中活動不可刪除、
停用中的 Webhook 不可測試、裝置配對網址帶店家、線上端寄信固定連線 IP。
"""
import base64

import pytest

from tests.support import default_tenant_id, flashes, login_staff, make_member

FCM = 'https://fcm.googleapis.com/fcm/send/fix-mni2-'


@pytest.fixture
def push_on(store_settings):
    store_settings('push', enabled=True, contact_email='push@example.com', vapid_private_key='', vapid_public_key='')
    yield


def _keys():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    raw = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    b64 = lambda b: base64.urlsafe_b64encode(b).rstrip(b'=').decode()  # noqa: E731
    return {'p256dh': b64(raw), 'auth': b64(b'0123456789abcdef')}


def _order(tid, **kw):
    from app.extensions import db
    from app.models.order import Order
    o = Order(tenant_id=tid, total_price=50, status=kw.pop('status', 'Pending'), order_type=kw.pop('order_type', '外帶'),
              payment_method='Cash', order_source='KIOSK', pickup_number=7, **kw)
    db.session.add(o)
    db.session.commit()
    return o


class _Resp:
    def __init__(self, code):
        self.status_code = code


# ---------------------------------------------------------------------------
# B-345：endpoint 只接受瀏覽器推播服務，送出時不跟隨轉址
# ---------------------------------------------------------------------------
def test_push_endpoint_must_be_browser_push_service_and_no_redirects(app, push_on, monkeypatch):
    from app.errors import AppError
    from app.models.integration import PushSubscription
    from app.services import push_service
    with app.app_context():
        tid = default_tenant_id(app)
        o = _order(tid)
        for bad in ('https://169.254.169.254/latest/meta-data/', 'https://attacker.example.com/x',
                    'https://fcm.googleapis.com.evil.example/x', 'https://fcm.googleapis.com:8443/x',
                    'https://user:pw@fcm.googleapis.com/x', 'http://fcm.googleapis.com/x'):
            with pytest.raises(AppError):
                push_service.subscribe(o, {'endpoint': bad, 'keys': _keys()})
        for good in (FCM + 'a', 'https://updates.push.services.mozilla.com/wpush/v2/x',
                     'https://wns2-sg2p.notify.windows.com/w/?token=x', 'https://web.push.apple.com/QGx'):
            push_service.subscribe(o, {'endpoint': good, 'keys': _keys()})
        from app.extensions import db
        db.session.commit()
        # 舊版存進去的非推播服務網址：送出時刪除、不發出請求
        db.session.add(PushSubscription(tenant_id=tid, endpoint='https://attacker.example.com/old', order_uuid=o.uuid,
                                        **_keys()))
        db.session.commit()
        calls = []

        def fake_post(url, **kw):
            calls.append((url, kw))
            return _Resp(201)
        monkeypatch.setattr(push_service.requests, 'post', fake_post)
        assert push_service.send_to_order(o.uuid, {'title': 't', 'body': 'b'}, tid) == 4
        assert all(kw.get('allow_redirects') is False for _u, kw in calls)
        assert not any('attacker' in u for u, _kw in calls)
        left = {s.endpoint for s in PushSubscription.query.filter_by(order_uuid=o.uuid).all()}
        assert 'https://attacker.example.com/old' not in left and len(left) == 4


# ---------------------------------------------------------------------------
# B-346：送出成功後保留訂閱 (之後的外送出發 / 店家回覆評價也送得到)
# ---------------------------------------------------------------------------
def test_push_subscription_kept_after_success_for_later_events(app, push_on, monkeypatch):
    from app.extensions import db
    from app.models.integration import PushSubscription
    from app.services import push_service
    with app.app_context():
        tid = default_tenant_id(app)
        o = _order(tid, status='Completed')
        push_service.subscribe(o, {'endpoint': FCM + 'keep', 'keys': _keys()})
        db.session.commit()
        sent = []
        monkeypatch.setattr(push_service.requests, 'post', lambda url, **kw: sent.append(url) or _Resp(201))
        assert push_service._deferred_send(o.id, 'ready') == 1
        assert push_service._deferred_send(o.id, 'feedback_reply') == 1
        assert len(sent) == 2
        assert PushSubscription.query.filter_by(order_uuid=o.uuid).count() == 1
        # 失效 (410) 才刪除
        monkeypatch.setattr(push_service.requests, 'post', lambda url, **kw: _Resp(410))
        assert push_service.send_to_order(o.uuid, {'title': 't'}, tid) == 0
        assert PushSubscription.query.filter_by(order_uuid=o.uuid).count() == 0


# ---------------------------------------------------------------------------
# B-347：外送到府訂單不叫顧客到櫃檯取餐；已送達不再通知出餐
# ---------------------------------------------------------------------------
def test_home_delivery_ready_push_text(app, push_on, monkeypatch):
    from app.services import push_service
    payloads = []
    monkeypatch.setattr(push_service, 'send_to_order', lambda uuid, payload, tid: payloads.append(payload) or 1)
    with app.app_context():
        tid = default_tenant_id(app)
        pickup = _order(tid, status='Completed')
        assert push_service._deferred_send(pickup.id, 'ready') == 1
        assert '請至櫃檯取餐' in payloads[-1]['body']
        home = _order(tid, status='Completed', order_type='外送到府')
        assert push_service._deferred_send(home.id, 'ready') == 1
        assert '櫃檯' not in payloads[-1]['body'] and '外送員即將出發' in payloads[-1]['body']
        delivered = _order(tid, status='PickedUp', order_type='外送到府', delivery_status='delivered')
        n = len(payloads)
        assert push_service._deferred_send(delivered.id, 'ready') == 0 and len(payloads) == n


# ---------------------------------------------------------------------------
# B-348：線上端套用分店同步上來的出餐 / 出發時補發推播
# ---------------------------------------------------------------------------
def test_notify_synced_order_kinds(app, monkeypatch):
    from app.services import push_service
    kinds = []
    monkeypatch.setattr(push_service, 'notify_order', lambda order, kind='ready': kinds.append(kind))

    class O:
        def __init__(self, status, ds=''):
            self.status, self.delivery_status = status, ds
    push_service.notify_synced_order(O('Completed'), 'Pending', '')
    push_service.notify_synced_order(O('Completed'), 'Completed', '')            # 沒有變化
    push_service.notify_synced_order(O('PickedUp'), 'Pending', '')               # 直接取餐：不通知出餐
    push_service.notify_synced_order(O('Completed', 'out'), 'Completed', 'assigned')
    push_service.notify_synced_order(O('Completed', 'out'), 'Pending', '')       # 同批出餐又出發：只通知出發
    push_service.notify_synced_order(None, 'Pending', '')
    assert kinds == ['ready', 'delivery_out', 'delivery_out']


# ---------------------------------------------------------------------------
# B-343 / B-352 / B-357：行銷頁的會員名單權限、分群參數、人數
# ---------------------------------------------------------------------------
def test_marketing_member_list_requires_members_permission(app, monkeypatch):
    from app.extensions import db
    from app.models.tenant import StaffAccount
    from app.routes import marketing as marketing_route
    make_member(app, 'MNI2 名單會員', '0977660001')
    staff = login_staff(app, 'mni2_coupon_staff', 'staff')
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='mni2_coupon_staff').one()
        acc.permissions = 'coupons'
        db.session.commit()
    staff = login_staff(app, 'mni2_coupon_staff', 'staff')
    r = staff.get('/admin/marketing?segment=new')
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and 'MNI2 名單會員' not in html and 'segmentMembersDenied' in html
    assert 'format=xlsx' not in html
    assert staff.get('/admin/marketing?segment=new&format=xlsx').status_code == 403
    owner = login_staff(app, 'mni2_mkt_owner', 'owner')
    html = owner.get('/admin/marketing?segment=new').get_data(as_text=True)
    assert 'MNI2 名單會員' in html and 'format=xlsx' in html
    assert owner.get('/admin/marketing?segment=new&format=xlsx').data[:2] == b'PK'
    # 不存在的分群：不再 500
    assert owner.get('/admin/marketing?segment=foo').status_code == 200
    r = owner.get('/admin/marketing?segment=a/b&format=xlsx')
    assert r.status_code == 200 and 'segmentMembers' not in r.get_data(as_text=True)
    # 名單超過上限：標題顯示實際人數並提示已截斷
    make_member(app, 'MNI2 名單會員二', '0977660002')
    monkeypatch.setattr(marketing_route, 'MEMBER_LIST_LIMIT', 1)
    with app.app_context():
        from app.services import marketing_service
        total = sum(1 for row in marketing_service.member_stats(default_tenant_id(app)) if row['segment'] == 'new')
    html = owner.get('/admin/marketing?segment=new').get_data(as_text=True)
    assert f'({total} 位' in html and 'segmentMembersTruncated' in html


# ---------------------------------------------------------------------------
# R2-124：執行中的活動不可刪除
# ---------------------------------------------------------------------------
def test_running_campaign_cannot_be_deleted(app):
    from app.extensions import db
    from app.models.marketing import MarketingCampaign
    with app.app_context():
        tid = default_tenant_id(app)
        c = MarketingCampaign(tenant_id=tid, name='MNI2 執行中', kind='winback', status='running', message='hi')
        idle = MarketingCampaign(tenant_id=tid, name='MNI2 可刪', kind='winback', status='paused', message='hi')
        db.session.add_all([c, idle])
        db.session.commit()
        cid, idle_id = c.id, idle.id
    owner = login_staff(app, 'mni2_mkt_owner2', 'owner')
    html = owner.get('/admin/marketing').get_data(as_text=True)
    row = html.split(f'data-campaign-id="{cid}"')[1].split('</tr>')[0]
    assert 'value="delete"' not in row
    owner.post('/admin/marketing', data={'action': 'delete', 'campaign_id': cid})
    owner.post('/admin/marketing', data={'action': 'delete', 'campaign_id': idle_id})
    with app.app_context():
        assert db.session.get(MarketingCampaign, cid) is not None
        assert db.session.get(MarketingCampaign, idle_id) is None
        db.session.delete(db.session.get(MarketingCampaign, cid))
        db.session.commit()


# ---------------------------------------------------------------------------
# R2-127：停用中的 Webhook 不可測試；重新啟用時恢復待送
# ---------------------------------------------------------------------------
def test_inactive_webhook_ping_rejected_and_resume_on_enable(app):
    from datetime import datetime, timedelta
    from app.extensions import db
    from app.models.integration import WebhookDelivery, WebhookEndpoint
    from tests.support import get_or_create_tenant
    tid = get_or_create_tenant(app, 'mni2-hook-shop')
    owner = login_staff(app, 'mni2_hook_owner', 'owner', tenant_id=tid)
    owner.post('/admin/integrations', data={'action': 'create_hook', 'name': 'ERP', 'url': 'https://erp.example.test/h',
                                            'events': ['order.created']})
    with app.app_context():
        ep = WebhookEndpoint.query.filter_by(tenant_id=tid).one()
        ep.is_active = False
        later = datetime.now() + timedelta(hours=3)
        db.session.add_all([
            WebhookDelivery(tenant_id=tid, endpoint_id=ep.id, event='order.created', payload='{}', status='pending',
                            next_attempt_at=later, attempts=2),
            WebhookDelivery(tenant_id=tid, endpoint_id=ep.id, event='order.paid', payload='{}', status='failed',
                            next_attempt_at=later, attempts=6, last_error='端點已停用'),
            WebhookDelivery(tenant_id=tid, endpoint_id=ep.id, event='order.cancelled', payload='{}', status='failed',
                            next_attempt_at=later, attempts=6, last_error='HTTP 500')])
        db.session.commit()
        eid = ep.id
    flashes(owner)
    owner.post('/admin/integrations', data={'action': 'ping_hook', 'endpoint_id': eid})
    assert not any('15 秒' in m for m in flashes(owner))
    with app.app_context():
        assert WebhookDelivery.query.filter_by(endpoint_id=eid, event='ping').count() == 0
    owner.post('/admin/integrations', data={'action': 'toggle_hook', 'endpoint_id': eid})
    with app.app_context():
        rows = {d.event: d for d in WebhookDelivery.query.filter_by(endpoint_id=eid).all()}
        assert db.session.get(WebhookEndpoint, eid).is_active is True
        assert rows['order.created'].status == 'pending' and rows['order.created'].next_attempt_at <= datetime.now()
        assert (rows['order.paid'].status, rows['order.paid'].attempts) == ('pending', 0)
        assert rows['order.cancelled'].status == 'failed'
    owner.post('/admin/integrations', data={'action': 'ping_hook', 'endpoint_id': eid})
    with app.app_context():
        assert WebhookDelivery.query.filter_by(endpoint_id=eid, event='ping').count() == 1


# ---------------------------------------------------------------------------
# B-351：配對網址帶店家代號
# ---------------------------------------------------------------------------
def test_device_pair_url_includes_shop(app):
    from app.extensions import db
    from app.models.integration import Device
    from tests.support import get_or_create_tenant
    tid = get_or_create_tenant(app, 'mni2-device-shop')
    owner = login_staff(app, 'mni2_device_owner', 'owner', tenant_id=tid)
    owner.post('/admin/devices', data={'action': 'create', 'name': 'MNI2 平板', 'kind': 'kiosk'})
    msgs = flashes(owner)
    assert any('/device/pair?shop=mni2-device-shop' in m for m in msgs), msgs
    with app.app_context():
        code = Device.query.filter_by(tenant_id=tid, name='MNI2 平板').one().pair_code
    fresh = app.test_client()   # 全新瀏覽器：沒有員工登入、沒開過 /shop/<代號>
    assert fresh.get('/device/pair?shop=mni2-device-shop').status_code == 200
    r = fresh.post('/device/pair?shop=mni2-device-shop', data={'code': code})
    assert r.status_code == 302 and 'kiosk_device=' in r.headers.get('Set-Cookie', '')
    with app.app_context():
        d = Device.query.filter_by(tenant_id=tid, name='MNI2 平板').one()
        db.session.delete(d)
        db.session.commit()


# ---------------------------------------------------------------------------
# B-353：線上端寄信連線固定到檢查過的 IP
# ---------------------------------------------------------------------------
def test_hub_smtp_connects_to_checked_ip(app, monkeypatch):
    import smtplib
    from app.errors import AppError
    from app.services import mail_service, webhook_service
    from app.sync import runtime as sync_runtime
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: True)
    answers = iter([{'93.184.216.34'}, {'10.0.0.5'}])
    monkeypatch.setattr(webhook_service, 'resolve_ips', lambda host: next(answers))
    targets = []

    def fake_create_connection(address, *a, **kw):
        targets.append(address)
        raise OSError('stop')
    monkeypatch.setattr(smtplib.socket, 'create_connection', fake_create_connection)
    with app.test_request_context('/'):
        with pytest.raises(OSError):
            mail_service._connect({'host': 'mail.example.com', 'port': 25})
        assert targets == [('93.184.216.34', 25)]
        with pytest.raises(AppError):   # 第二次解析到內網：拒絕
            mail_service._connect({'host': 'mail.example.com', 'port': 25})
        assert len(targets) == 1
    # 門市主機：不檢查、照主機名稱連線
    monkeypatch.setattr(sync_runtime, 'is_hub', lambda: False)
    with app.test_request_context('/'):
        with pytest.raises(OSError):
            mail_service._connect({'host': 'smtp.lan', 'port': 587})
    assert targets[-1] == ('smtp.lan', 587)


# ---------------------------------------------------------------------------
# B-355：寄信主機名稱格式錯誤 (smtplib 丟 UnicodeError) 轉成 AppError，不是 500
# ---------------------------------------------------------------------------
def test_send_mail_bad_hostname_becomes_app_error(app, store_settings, monkeypatch):
    from app.errors import AppError
    from app.services import mail_service
    store_settings('smtp', host='a..b', port=587, sender='shop@example.com')

    def bad_connect(cfg):
        raise UnicodeError('label empty or too long')
    monkeypatch.setattr(mail_service, '_connect', bad_connect)
    with app.app_context():
        with pytest.raises(AppError) as exc:
            mail_service.send_mail(default_tenant_id(app), ['a@example.com'], 's', 'b')
    assert '寄信失敗' in str(exc.value)


# ---------------------------------------------------------------------------
# R2-129：活動表格同時顯示發送次數與不重複人數
# ---------------------------------------------------------------------------
def test_campaign_table_shows_unique_members(app):
    from datetime import datetime, timedelta
    from app.extensions import db
    from app.models.marketing import CampaignSend, MarketingCampaign
    uid = make_member(app, 'MNI2 重複收件', '0977660009')
    with app.app_context():
        tid = default_tenant_id(app)
        c = MarketingCampaign(tenant_id=tid, name='MNI2 人數', kind='winback', status='paused', message='hi')
        db.session.add(c)
        db.session.flush()
        now = datetime.now()
        db.session.add_all([CampaignSend(tenant_id=tid, campaign_id=c.id, user_id=uid, sent_at=now - timedelta(days=60)),
                            CampaignSend(tenant_id=tid, campaign_id=c.id, user_id=uid, sent_at=now)])
        db.session.commit()
        cid = c.id
    owner = login_staff(app, 'mni2_mkt_owner3', 'owner')
    html = owner.get('/admin/marketing').get_data(as_text=True)
    row = html.split(f'data-campaign-id="{cid}"')[1].split('</tr>')[0]
    assert '>2<div class="small text-white-50">1 人</div>' in row
    with app.app_context():
        db.session.delete(db.session.get(MarketingCampaign, cid))
        db.session.commit()


# ---------------------------------------------------------------------------
# R2-139：會員姓名含控制字元時分群匯出不 500
# ---------------------------------------------------------------------------
def test_segment_export_strips_illegal_characters(app):
    import io
    from openpyxl import load_workbook
    make_member(app, 'MNI2\x0b控制字元', '0977660010')
    owner = login_staff(app, 'mni2_mkt_owner4', 'owner')
    r = owner.get('/admin/marketing?segment=new&format=xlsx')
    assert r.status_code == 200 and r.data[:2] == b'PK'
    names = [row[0] for row in load_workbook(io.BytesIO(r.data)).active.iter_rows(values_only=True)]
    assert 'MNI2控制字元' in names
