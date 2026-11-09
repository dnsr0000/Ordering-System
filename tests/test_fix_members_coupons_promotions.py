"""修正小組 members-coupons-promotions 的回歸測試：會員等級 / 生日禮 / 儲值金調整 / 促銷活動後台與預覽"""
from datetime import date, timedelta

from app.extensions import db
from app.models.member import MemberTier
from app.models.menu import MenuItem
from app.models.promotion import Promotion
from app.models.user import StaffAuditLog, User
from tests.support import default_tenant_id, flashes, guest_client, line, make_item, make_member


def _tier(app, name):
    with app.app_context():
        t = MemberTier.query.filter_by(tenant_id=default_tenant_id(app), name=name).first()
        return None if t is None else {'id': t.id, 'name': t.name, 'min_spend': t.min_spend}


def _drop_tiers(app, *names):
    with app.app_context():
        ids = [t.id for t in MemberTier.query.filter(MemberTier.name.in_(names)).all()]
        if ids:
            User.query.filter(User.tier_id.in_(ids)).update({'tier_id': None}, synchronize_session=False)
            MemberTier.query.filter(MemberTier.id.in_(ids)).delete(synchronize_session=False)
        db.session.commit()


# ---------------------------------------------------------------------------
# B-337：編輯等級改成其他等級的名稱 / 空白名稱
# ---------------------------------------------------------------------------
def test_edit_tier_duplicate_or_blank_name_is_rejected(app, admin_client):
    names = ('修正銀卡', '修正金卡')
    try:
        for n in names:
            admin_client.post('/admin/members', data={'action': 'add_tier', 'name': n, 'min_spend': '100'})
        silver = _tier(app, '修正銀卡')
        flashes(admin_client)
        r = admin_client.post('/admin/members', data={'action': 'edit_tier', 'tier_id': silver['id'], 'name': '修正金卡',
                                                      'min_spend': '100'})
        assert r.status_code != 500
        assert any('已存在' in m for m in flashes(admin_client))
        assert _tier(app, '修正銀卡') is not None
        r = admin_client.post('/admin/members', data={'action': 'edit_tier', 'tier_id': silver['id'], 'name': '   ',
                                                      'min_spend': '100'})
        assert r.status_code != 500
        assert any('請輸入等級名稱' in m for m in flashes(admin_client))
        assert _tier(app, '修正銀卡') is not None
        # 名稱不變 (與自己同名) 仍可儲存其他欄位
        admin_client.post('/admin/members', data={'action': 'edit_tier', 'tier_id': silver['id'], 'name': '修正銀卡',
                                                  'min_spend': '300'})
        assert _tier(app, '修正銀卡')['min_spend'] == 300
    finally:
        _drop_tiers(app, *names)


# ---------------------------------------------------------------------------
# B-338：分店主機不可手動送出生日禮
# ---------------------------------------------------------------------------
def test_birthday_now_blocked_on_node(app, admin_client, store_settings, monkeypatch):
    store_settings('member', birthday_enabled=True, birthday_points=30)
    today = date.today()
    make_member(app, '分店壽星', '0966350001', points=0, birthday=today.replace(year=1990, day=1))
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'node')
    page = admin_client.get('/admin/members').get_data(as_text=True)
    assert 'value="birthday_now"' not in page and '這台是分店主機' in page
    r = admin_client.post('/admin/members', data={'action': 'birthday_now'})
    assert r.status_code == 403 and '分店主機不執行' in r.get_data(as_text=True)
    with app.app_context():
        u = User.query.filter_by(phone='0966350001').first()
        assert u.points == 0 and (u.birthday_gift_year or 0) < today.year


# ---------------------------------------------------------------------------
# R2-120：未啟用等級時「重新計算」仍要重算累積消費
# ---------------------------------------------------------------------------
def test_recompute_spent_without_tiers(app, admin_client, store_settings):
    store_settings('member', tiers_enabled=False)
    uid = make_member(app, '偏高累積會員', '0966350002', total_spent=1000)
    admin_client.post('/admin/members', data={'action': 'recompute_tiers'})
    assert any('已重新計算累積消費' in m for m in flashes(admin_client))
    with app.app_context():
        assert db.session.get(User, uid).total_spent == 0   # 沒有任何已收款訂單


# ---------------------------------------------------------------------------
# R2-121：數字欄位極大值回 400 而不是 500
# ---------------------------------------------------------------------------
def test_huge_numbers_rejected(app, admin_client):
    try:
        r = admin_client.post('/admin/members', data={'action': 'add_tier', 'name': '修正巨額卡', 'min_spend': '1e30'})
        assert r.status_code != 500
        assert any('不可超過' in m for m in flashes(admin_client))
        assert _tier(app, '修正巨額卡') is None
    finally:
        _drop_tiers(app, '修正巨額卡')
    uid = make_member(app, '巨額儲值會員', '0966350003')
    r = admin_client.post('/admin/members', data={'action': 'wallet_adjust', 'user_id': uid, 'delta': '99999999999',
                                                  'note': '測試'})
    assert r.status_code != 500
    assert any('不可超過' in m for m in flashes(admin_client))
    with app.app_context():
        assert db.session.get(User, uid).wallet_balance == 0
    for field in ('priority', 'threshold', 'amount'):
        data = {'action': 'save', 'name': f'修正巨額活動{field}', 'kind': 'threshold_discount', 'threshold': '100',
                'amount': '10', 'is_active': '1', field: '1e30'}
        r = admin_client.post('/admin/promotions', data=data)
        assert r.status_code != 500
        assert any('不可超過' in m for m in flashes(admin_client)), field
        with app.app_context():
            assert Promotion.query.filter_by(name=f'修正巨額活動{field}').first() is None


# ---------------------------------------------------------------------------
# B-339：POS 改價品項的促銷試算
# ---------------------------------------------------------------------------
def test_pos_preview_uses_override_price(app, staff_client):
    iid = make_item(app, '修正改價促銷餐', price=100, stock=50, category='修正促銷分類')
    with app.app_context():
        uuid = db.session.get(MenuItem, iid).uuid
        p = Promotion(tenant_id=default_tenant_id(app), name='修正第二件半價', kind='nth_discount', target_type='items',
                      target_value=uuid, nth=2, percent=50)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    try:
        items = [dict(line(iid, 2), override_price=80)]
        d = staff_client.post('/api/promotions/preview', json={'items': items, 'channel': 'pos'}).get_json()
        assert d['discount'] == 40 and d['names'] == ['修正第二件半價']
        # 非員工 (點餐機) 帶改價仍不可用改價試算
        d = guest_client(app).post('/api/promotions/preview', json={'items': items, 'channel': 'pos'}).get_json()
        assert d['discount'] == 0
    finally:
        with app.app_context():
            Promotion.query.filter_by(id=pid).delete()
            db.session.commit()


# ---------------------------------------------------------------------------
# B-341：促銷列表狀態依日期；結束日不可早於開始日
# ---------------------------------------------------------------------------
def test_promotion_status_by_date_and_date_order(app, admin_client):
    today = date.today()
    with app.app_context():
        tid = default_tenant_id(app)
        ended = Promotion(tenant_id=tid, name='修正已過期活動', kind='bogo', end_date=today - timedelta(days=30))
        future = Promotion(tenant_id=tid, name='修正未開始活動', kind='bogo', start_date=today + timedelta(days=30))
        db.session.add_all([ended, future])
        db.session.commit()
        ids = (ended.id, future.id)
    try:
        page = admin_client.get('/admin/promotions').get_data(as_text=True)
        row = page.split('修正已過期活動', 1)[1].split('</tr>', 1)[0]
        assert '已結束' in row and '進行中' not in row
        row = page.split('修正未開始活動', 1)[1].split('</tr>', 1)[0]
        assert '未開始' in row and '進行中' not in row
        r = admin_client.post('/admin/promotions', data={
            'action': 'save', 'name': '修正日期顛倒活動', 'kind': 'bogo', 'is_active': '1',
            'start_date': today.isoformat(), 'end_date': (today - timedelta(days=1)).isoformat()})
        assert r.status_code != 500
        assert any('結束日不可早於開始日' in m for m in flashes(admin_client))
        with app.app_context():
            assert Promotion.query.filter_by(name='修正日期顛倒活動').first() is None
    finally:
        with app.app_context():
            Promotion.query.filter(Promotion.id.in_(ids)).delete(synchronize_session=False)
            db.session.commit()


# ---------------------------------------------------------------------------
# R2-122：刪除等級要確認、寫稽核
# ---------------------------------------------------------------------------
def test_delete_tier_confirm_and_audit(app, admin_client):
    try:
        admin_client.post('/admin/members', data={'action': 'add_tier', 'name': '修正刪除卡', 'min_spend': '0'})
        tier = _tier(app, '修正刪除卡')
        make_member(app, '刪除卡會員', '0966300004', tier_id=tier['id'])
        page = admin_client.get('/admin/members').get_data(as_text=True)
        assert 'data-confirm="確定要刪除等級「修正刪除卡」？目前有 1 位會員' in page
        admin_client.post('/admin/members', data={'action': 'delete_tier', 'tier_id': tier['id']})
        assert _tier(app, '修正刪除卡') is None
        with app.app_context():
            log = StaffAuditLog.query.filter_by(action_type='MEMBER_TIER_DELETE').order_by(StaffAuditLog.id.desc()).first()
            assert log is not None and '修正刪除卡' in log.details
    finally:
        _drop_tiers(app, '修正刪除卡')


# ---------------------------------------------------------------------------
# B-216：外送 / 外送到府的促銷預覽用外帶價 (與結帳 order_pricing._price_channel 一致)
# ---------------------------------------------------------------------------
def test_preview_delivery_uses_takeout_price(app):
    iid = make_item(app, '修正B216促銷餐', price=100, stock=50, takeout_price=80)
    with app.app_context():
        uuid = db.session.get(MenuItem, iid).uuid
        p = Promotion(tenant_id=default_tenant_id(app), name='修正外帶第二件半價', kind='nth_discount', target_type='items',
                      target_value=uuid, nth=2, percent=50)
        db.session.add(p)
        db.session.commit()
        pid = p.id
    try:
        client = guest_client(app)
        for order_type in ('外帶', '外送', '外送到府'):
            d = client.post('/api/promotions/preview', json={'items': [line(iid, 2)], 'channel': 'kiosk',
                                                             'order_type': order_type}).get_json()
            assert d['discount'] == 40, order_type
        d = client.post('/api/promotions/preview', json={'items': [line(iid, 2)], 'channel': 'kiosk',
                                                         'order_type': '內用'}).get_json()
        assert d['discount'] == 50
    finally:
        with app.app_context():
            Promotion.query.filter_by(id=pid).delete()
            db.session.commit()


# ---------------------------------------------------------------------------
# B-302：刪除會員的確認文字提示儲值金餘額與去識別化
# ---------------------------------------------------------------------------
def test_delete_member_confirm_mentions_wallet(app, admin_client):
    make_member(app, '修正有餘額會員', '0966300005', wallet_balance=120)
    page = admin_client.get('/admin/members', query_string={'q': '0966300005'}).get_data(as_text=True)
    assert '此會員儲值金尚有 $120 餘額' in page and '去識別化' in page
