"""修正小組 admin-home-1 的回歸測試 (app/routes/admin.py)"""
import io
import os
from datetime import datetime

import openpyxl

from tests.support import default_tenant_id, login_staff, make_item, make_member, other_shop_owner_client


def _staff_with_perms(app, username, role, perms):
    from app.extensions import db
    from app.models.tenant import StaffAccount
    client = login_staff(app, username, role)
    with app.app_context():
        acc = StaffAccount.query.filter_by(username=username).first()
        acc.permissions = perms
        db.session.commit()
    return client


def _flash_text(client):
    with client.session_transaction() as sess:
        return ' '.join(msg for _cat, msg in sess.pop('_flashes', []))


# ---------------------------------------------------------------- 員工帳號
def test_node_rejects_owner_password_reset(app, monkeypatch):
    """B-301：分店不可修改負責人的密碼 / 啟用狀態 (線上端會整列拒收)"""
    from app.extensions import db
    from app.models.tenant import StaffAccount
    owner = other_shop_owner_client(app, 'fix-ah1-node')
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='owner_fix-ah1-node').first()
        acc_id, old_hash = acc.id, acc.password_hash
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'node')
    r = owner.post('/admin/staff', data={'action': 'reset_password', 'account_id': acc_id, 'new_password': 'BrandNewPass!2026',
                                         'current_password': 'StaffPass!2026'})
    assert r.status_code == 403
    with app.app_context():
        assert db.session.get(StaffAccount, acc_id).password_hash == old_hash


def test_reset_own_password_requires_current_and_clears_lockout(app):
    """R2-113：重設自己的密碼要輸入目前密碼；R2-114：重設密碼解除該帳號的登入鎖定"""
    from app.extensions import db
    from app.models.system import AuthThrottle
    from app.models.tenant import StaffAccount
    from app.services import auth_throttle
    owner = other_shop_owner_client(app, 'fix-ah1-self')
    with app.app_context():
        me = StaffAccount.query.filter_by(username='owner_fix-ah1-self').first()
        me_id = me.id
        clerk = StaffAccount(tenant_id=me.tenant_id, username='fix_ah1_Locked', role='staff', is_active=True)
        clerk.set_password('ClerkPass!2026')
        db.session.add(clerk)
        db.session.commit()
        clerk_id = clerk.id
        keys = auth_throttle.staff_keys('fix_ah1_Locked', '10.0.0.9')
        for _ in range(auth_throttle.STAFF_DEVICE_MAX_FAILS):
            auth_throttle.record_staff_failure(keys)
        assert auth_throttle.lock_remaining(keys.values())

    r = owner.post('/admin/staff', data={'action': 'reset_password', 'account_id': me_id, 'new_password': 'Hijacked!20260'})
    assert r.status_code == 302 and '目前密碼' in _flash_text(owner)
    with app.app_context():
        assert db.session.get(StaffAccount, me_id).check_password('StaffPass!2026')
    r = owner.post('/admin/staff', data={'action': 'reset_password', 'account_id': me_id, 'new_password': 'MyNewPass!20260',
                                         'current_password': 'StaffPass!2026'})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(StaffAccount, me_id).check_password('MyNewPass!20260')

    owner = login_staff(app, 'owner_fix-ah1-self')   # 改密碼後 session_version 變了：重新登入
    r = owner.post('/admin/staff', data={'action': 'reset_password', 'account_id': clerk_id, 'new_password': 'ClerkNew!20260'})
    assert r.status_code == 302
    with app.app_context():
        assert AuthThrottle.query.filter(AuthThrottle.key.like('staff:%:fix_ah1_locked%')).count() == 0
        assert not auth_throttle.lock_remaining(keys.values())


def test_staff_username_length_limited(app, admin_client):
    """B-305：員工帳號超過 50 字不可建立"""
    from app.models.tenant import StaffAccount
    name = 'u' * 51
    r = admin_client.post('/admin/staff', data={'action': 'create', 'username': name, 'role': 'staff', 'password': 'LongNamePass!2026'})
    assert r.status_code == 302
    with app.app_context():
        assert StaffAccount.query.filter_by(username=name).first() is None


def test_tenant_register_starts_session_and_validates_length(app):
    """B-314：公開註冊後建立工作階段與登入日誌；R2-112：代號過長回 400 而不是 500"""
    from app.models.tenant import StaffAccount, StaffSession
    from app.models.user import AdminLog
    client = app.test_client()
    r = client.post('/tenant/register', data={'shop_name': '過長代號', 'username': 'fix_ah1_long', 'password': 'RegisterPass!2026',
                                              'slug': 'a' * 81})
    assert r.status_code == 302
    with app.app_context():
        assert StaffAccount.query.filter_by(username='fix_ah1_long').first() is None

    client = app.test_client()
    r = client.post('/tenant/register', data={'shop_name': '註冊測試店', 'username': 'fix_ah1_reg_owner',
                                              'password': 'RegisterPass!2026', 'slug': 'fix-ah1-reg'})
    assert r.status_code == 302
    with app.app_context():
        owner = StaffAccount.query.filter_by(username='fix_ah1_reg_owner').first()
        assert owner is not None
        assert StaffSession.query.filter_by(staff_account_id=owner.id).count() == 1
        assert AdminLog.query.filter_by(username='fix_ah1_reg_owner').count() == 1


def test_feedback_only_staff_gets_entry(app):
    """B-304：只被授權顧客評價的櫃檯，按「後台管理」進入評價頁 (不是導回 POS)"""
    client = _staff_with_perms(app, 'fix_ah1_feedback', 'staff', 'feedback')
    r = client.get('/admin')
    assert r.status_code == 302 and '/admin/feedback' in r.headers['Location']


# ---------------------------------------------------------------- 菜單
def test_delete_item_used_as_combo_side_is_blocked(app, admin_client):
    """R2-108：品項是其他套餐的配餐時不可刪除"""
    from app.extensions import db
    from app.models.menu import ComboOption, ComboOptionItem, MenuItem
    main = make_item(app, 'AH1 雞腿飯', price=150)
    side = make_item(app, 'AH1 薯條', price=50)
    with app.app_context():
        tid = default_tenant_id(app)
        combo = ComboOption(tenant_id=tid, main_item_id=main, name='AH1 B餐', additional_price=60)
        db.session.add(combo)
        db.session.flush()
        db.session.add(ComboOptionItem(tenant_id=tid, combo_option_id=combo.id, menu_item_id=side))
        db.session.commit()
    r = admin_client.post(f'/admin/delete/{side}')
    assert r.status_code == 302 and 'AH1 B餐' in _flash_text(admin_client)
    with app.app_context():
        assert db.session.get(MenuItem, side) is not None
        assert ComboOptionItem.query.filter_by(menu_item_id=side).count() == 1


def test_menu_only_staff_cannot_change_reward_via_edit(app):
    """R2-115：只有菜單權限時，編輯品項不會上架紅利商品"""
    from app.extensions import db
    from app.models.menu import MenuItem
    item = make_item(app, 'AH1 牛排', price=500)
    client = _staff_with_perms(app, 'fix_ah1_menu_only', 'staff', 'menu')
    r = client.post(f'/admin/edit/{item}', data={'name': 'AH1 牛排', 'category': '主餐', 'price': '500', 'stock': '10',
                                                 'modifiers': 'none', 'is_reward': '1', 'reward_points': '1'})
    assert r.status_code == 302
    with app.app_context():
        assert not db.session.get(MenuItem, item).is_reward


def test_edit_item_image_kept_when_commit_fails(app, monkeypatch):
    """B-315：commit 失敗時舊圖不可被刪，新圖不留孤兒"""
    from app.extensions import db
    from app.models.menu import MenuItem
    from app.routes import admin as admin_routes
    item_id = make_item(app, 'AH1 換圖餐點')
    upload_dir = app.config['UPLOAD_FOLDER_MENU']
    os.makedirs(upload_dir, exist_ok=True)
    old_rel, new_rel = 'ah1_old.jpg', 'ah1_new.jpg'
    for rel in (old_rel, new_rel):
        with open(os.path.join(upload_dir, rel), 'wb') as fh:
            fh.write(b'img')
    tid = default_tenant_id(app)
    with app.test_request_context():
        from flask import g
        g.tenant_id = tid
        item = db.session.get(MenuItem, item_id)
        item.image_path = new_rel

        def boom():
            raise RuntimeError('commit failed')
        monkeypatch.setattr(db.session, 'commit', boom)
        try:
            admin_routes._commit_item_image(item, old_rel)
        except RuntimeError:
            pass
        monkeypatch.undo()
    assert os.path.exists(os.path.join(upload_dir, old_rel)), '交易失敗時舊圖保留'
    assert not os.path.exists(os.path.join(upload_dir, new_rel)), '交易失敗時刪掉剛存的新圖'
    os.remove(os.path.join(upload_dir, old_rel))


# ---------------------------------------------------------------- 優惠券 / 紅利設定
def _coupon_form(code, **kw):
    data = {'title': f'測試 {code}', 'code': code, 'discount_type': 'fixed', 'discount_value': '50', 'min_spend': '0',
            'reward_points': '0', 'per_user_limit': '無上限'}
    data.update(kw)
    return data


def test_percent_coupon_zero_rejected(app, admin_client):
    """B-306：折數 0 不可存成 0.1 折"""
    from app.models.coupon import Coupon
    admin_client.post('/admin/add_coupon', data=_coupon_form('AH1PCT0', discount_type='percent', discount_value='0'))
    admin_client.post('/admin/add_coupon', data=_coupon_form('AH1PCTNEG', discount_type='percent', discount_value='-5'))
    with app.app_context():
        assert Coupon.query.filter(Coupon.code.in_(('AH1PCT0', 'AH1PCTNEG'))).count() == 0


def test_coupon_integer_fields_bounded(app, admin_client):
    """R2-109：極大的門檻 / 點數夾在上限內，不會 500"""
    from app.models.coupon import Coupon
    r = admin_client.post('/admin/add_coupon', data=_coupon_form('AH1BIG', min_spend='1e19', reward_points='3000000000'))
    assert r.status_code == 302
    with app.app_context():
        c = Coupon.query.filter_by(code='AH1BIG').first()
        assert c.min_spend <= 1_000_000 and c.reward_points <= 100_000_000


def test_delete_coupon_with_redeemed_unused_is_blocked(app, admin_client):
    """B-307：會員用點數換到、還沒用的券存在時不可刪除優惠券"""
    from app.extensions import db
    from app.models.coupon import Coupon, UserCoupon
    admin_client.post('/admin/add_coupon', data=_coupon_form('AH1REDEEM', reward_points='30'))
    uid = make_member(app, '兌換會員', '0933770101')
    with app.app_context():
        c = Coupon.query.filter_by(code='AH1REDEEM').first()
        cid = c.id
        db.session.add(UserCoupon(tenant_id=c.tenant_id, user_id=uid, coupon_id=cid, code=c.code, source='reward'))
        db.session.commit()
    r = admin_client.post(f'/admin/delete_coupon/{cid}')
    assert r.status_code == 302 and '點數兌換' in _flash_text(admin_client)
    with app.app_context():
        assert db.session.get(Coupon, cid) is not None
        assert UserCoupon.query.filter_by(coupon_id=cid).count() == 1


def test_issue_coupon_huge_valid_days(app, admin_client):
    """B-317：發放後 N 天極大值不會 OverflowError"""
    from app.models.coupon import Coupon
    admin_client.post('/admin/add_coupon', data=_coupon_form('AH1DAYS', member_only='1'))
    make_member(app, '發放會員', '0933770102')
    with app.app_context():
        cid = Coupon.query.filter_by(code='AH1DAYS').first().id
    r = admin_client.post(f'/admin/coupons/{cid}/issue', data={'target': 'phones', 'phones': '0933770102',
                                                                'expire_mode': 'days', 'valid_days': '99999999'})
    assert r.status_code == 302


def test_reward_setting_redirects_to_rewards_tab(app, admin_client, reward_setting_snapshot):
    """B-318：紅利設定存檔後回到回饋商城分頁"""
    r = admin_client.post('/admin/update_reward_setting', data={'is_enabled': '1', 'points_per_dollar': '1',
                                                                'max_discount_per_order': '1e19', 'spend_per_point': '10'})
    assert r.status_code == 302 and 'tab=rewards' in r.headers['Location']


# ---------------------------------------------------------------- 會員
def test_delete_member_with_wallet_or_pass_is_blocked(app, admin_client):
    """B-302：有儲值金餘額 / 未兌換完寄杯的會員不可從後台刪除"""
    from app.extensions import db
    from app.models.member import MemberPass, PassLedger
    from app.models.user import User
    rich = make_member(app, '儲值會員', '0933770103', wallet_balance=1000)
    r = admin_client.post(f'/admin/delete_user/{rich}')
    assert r.status_code == 302 and '儲值金' in _flash_text(admin_client)
    holder = make_member(app, '寄杯會員', '0933770104')
    item = make_item(app, 'AH1 寄杯美式', price=50)
    with app.app_context():
        mp = MemberPass(tenant_id=default_tenant_id(app), user_id=holder, menu_item_id=item, name='美式 5 杯',
                        total_uses=5, remaining=3)
        db.session.add(mp)
        db.session.flush()
        # 剩餘次數要有對應的寄杯異動帳 (test_sync_invariants 檢查 剩餘次數 = 異動帳合計)
        db.session.add(PassLedger(tenant_id=default_tenant_id(app), member_pass_id=mp.id, delta=3, reason='test'))
        db.session.commit()
    r = admin_client.post(f'/admin/delete_user/{holder}')
    assert r.status_code == 302 and '寄杯' in _flash_text(admin_client)
    with app.app_context():
        assert db.session.get(User, rich) is not None and db.session.get(User, holder) is not None


def test_delete_member_deidentifies_orders_and_support(app, admin_client):
    """B-308 / B-316：後台刪除會員後訂單不留姓名，客服對話一併刪除"""
    from app.extensions import db
    from app.models.order import Order
    from app.models.support import SupportChat
    uid = make_member(app, '王大明', '0933770105')
    with app.app_context():
        tid = default_tenant_id(app)
        order = Order(tenant_id=tid, user_id=uid, table_number='王大明', total_price=100, status='Pending',
                      created_at=datetime.now())
        chat = SupportChat(tenant_id=tid, user_id=uid, customer_label='王大明')
        db.session.add_all([order, chat])
        db.session.commit()
        order_id, chat_id = order.id, chat.id
    assert admin_client.post(f'/admin/delete_user/{uid}').status_code == 302
    with app.app_context():
        assert '王大明' not in (db.session.get(Order, order_id).table_number or '')
        assert db.session.get(SupportChat, chat_id) is None


def test_edit_negative_points_member(app, admin_client):
    """R2-110：點數為負的會員可只改姓名 (不動點數)，也能調到指定點數"""
    from app.extensions import db
    from app.models.user import User
    uid = make_member(app, '負點會員', '0933770106', points=-30)
    admin_client.post(f'/admin/edit_user/{uid}', data={'name': '負點會員改名', 'phone': '0933770106', 'points': '-30',
                                                       'loaded_points': '-30'})
    with app.app_context():
        u = db.session.get(User, uid)
        assert u.name == '負點會員改名' and u.points == -30
    admin_client.post(f'/admin/edit_user/{uid}', data={'name': '負點會員改名', 'phone': '0933770106', 'points': '20',
                                                       'loaded_points': '-30'})
    with app.app_context():
        assert db.session.get(User, uid).points == 20


# ---------------------------------------------------------------- 匯出
def test_export_restricted_sheets_and_bad_date(app):
    """B-303：只有菜單 + 會員權限的櫃檯不能匯出稽核 / 營收；B-317：日期格式錯誤回 400 提示而不是 500"""
    client = _staff_with_perms(app, 'fix_ah1_exporter', 'staff', 'menu,members')
    r = client.post('/admin/export_excel', data={'tables': ['User', 'OrderItem', 'Analytics', 'AdminLog', 'Bogus']})
    assert r.status_code == 200
    names = set(openpyxl.load_workbook(io.BytesIO(r.data)).sheetnames)
    assert not any(k in n for n in names for k in ('AdminLog', 'StaffAuditLog', 'Analytics', 'OrderItem', 'ItemSales'))
    r = client.post('/admin/export_excel', data={'tables': ['User'], 'start_date': '2026/01/01'})
    assert r.status_code == 302 and '日期' in _flash_text(client)


# ---------------------------------------------------------------- 跨檔請求 (第二批)
def test_menu_item_stock_endpoint(app):
    """B-297：編輯品項開窗時取目前庫存；需要菜單權限，別店品項回 404"""
    from app.extensions import db
    from app.models.menu import MenuItem
    item = make_item(app, 'AH1 庫存查詢', stock=7)
    with app.app_context():
        db.session.get(MenuItem, item).total_stock = 20
        db.session.commit()
    menu_staff = _staff_with_perms(app, 'fix_ah1_stock_menu', 'staff', 'menu')
    r = menu_staff.get(f'/api/admin/menu_items/{item}/stock')
    assert r.status_code == 200
    assert r.get_json() == {'success': True, 'id': item, 'stock': 7, 'total_stock': 20, 'is_sold_out': False}
    plain = _staff_with_perms(app, 'fix_ah1_stock_none', 'staff', '')
    assert plain.get(f'/api/admin/menu_items/{item}/stock').status_code in (302, 401, 403)
    assert app.test_client().get(f'/api/admin/menu_items/{item}/stock').status_code in (302, 401, 403)
    other = other_shop_owner_client(app, 'fix-ah1-stock')
    assert other.get(f'/api/admin/menu_items/{item}/stock').status_code == 404


def test_extend_coupon_until_releases_old_snapshot(app, admin_client):
    """B-494：延長活動結束日後，舊版發放寫入的結束日快照不再讓持券提早過期"""
    from datetime import date, time, timedelta
    from app.extensions import db
    from app.models.coupon import Coupon, UserCoupon
    old_until = date.today() + timedelta(days=1)
    new_until = date.today() + timedelta(days=30)
    admin_client.post('/admin/add_coupon', data=_coupon_form('AH1EXTEND', member_only='1', valid_until=old_until.isoformat()))
    uid = make_member(app, '延長會員', '0933770107')
    snap = datetime.combine(old_until, time(23, 59, 59))
    with app.app_context():
        c = Coupon.query.filter_by(code='AH1EXTEND').first()
        cid = c.id
        uc = UserCoupon(tenant_id=c.tenant_id, user_id=uid, coupon_id=cid, code=c.code, source='batch',
                        created_at=datetime.now(), expires_at=snap)
        db.session.add(uc)
        db.session.commit()
        uc_id = uc.id
    r = admin_client.post(f'/admin/edit_coupon/{cid}', data=_coupon_form('AH1EXTEND', member_only='1',
                                                                          valid_until=new_until.isoformat()))
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(UserCoupon, uc_id).expires_at is None


def test_delete_coupon_used_by_campaign_is_blocked(app, admin_client):
    """B-496：自動行銷活動仍引用的優惠券不可刪除"""
    from app.extensions import db
    from app.models.coupon import Coupon
    from app.models.marketing import MarketingCampaign
    admin_client.post('/admin/add_coupon', data=_coupon_form('AH1CAMP', member_only='1'))
    with app.app_context():
        c = Coupon.query.filter_by(code='AH1CAMP').first()
        cid = c.id
        db.session.add(MarketingCampaign(tenant_id=c.tenant_id, name='AH1 喚回活動', coupon_id=cid, status='active'))
        db.session.commit()
    r = admin_client.post(f'/admin/delete_coupon/{cid}')
    assert r.status_code == 302 and 'AH1 喚回活動' in _flash_text(admin_client)
    with app.app_context():
        assert db.session.get(Coupon, cid) is not None


def test_menu_image_shared_path_not_removed(app):
    """B-169：其他餐點 (含別家店) 仍使用同一圖片路徑時，刪除 / 換圖不刪檔"""
    from flask import g
    from app.extensions import db
    from app.models.menu import MenuItem
    from app.routes import admin as admin_routes
    from tests.support import get_or_create_tenant
    upload_dir = app.config['UPLOAD_FOLDER_MENU']
    os.makedirs(upload_dir, exist_ok=True)
    rel = 'ah1_shared.jpg'
    path = os.path.join(upload_dir, rel)
    with open(path, 'wb') as fh:
        fh.write(b'img')
    mine = make_item(app, 'AH1 共用圖 A', image_path=rel)
    other_tid = get_or_create_tenant(app, 'fix-ah1-img')
    with app.app_context():
        other = MenuItem(tenant_id=other_tid, name='AH1 共用圖 B', category='主餐', price=100, stock=1, total_stock=1,
                         modifiers='none', image_path=rel)
        db.session.add(other)
        db.session.commit()
        other_id = other.id
    with app.test_request_context():
        g.tenant_id = default_tenant_id(app)
        admin_routes._remove_menu_image_if_unused(rel, mine)
    assert os.path.exists(path), '別店仍使用時保留圖片'
    with app.app_context():
        db.session.delete(db.session.get(MenuItem, other_id))
        db.session.commit()
    with app.test_request_context():
        g.tenant_id = default_tenant_id(app)
        admin_routes._remove_menu_image_if_unused(rel, mine)
    assert not os.path.exists(path), '沒有其他餐點使用時刪除'


def test_disable_staff_bumps_epoch_and_clears_future_schedules(app):
    """B-172：停用 / 重設密碼 / 刪除遞增 session_epoch；B-412：停用刪今天起的班表、刪除再刪可排時段"""
    from datetime import date, timedelta
    from app.extensions import db
    from app.models.staff import StaffAvailability, WorkSchedule
    from app.models.tenant import StaffAccount
    owner = other_shop_owner_client(app, 'fix-ah1-staff')
    with app.app_context():
        me = StaffAccount.query.filter_by(username='owner_fix-ah1-staff').first()
        tid = me.tenant_id
        clerk = StaffAccount(tenant_id=tid, username='fix_ah1_sched', role='staff', is_active=True)
        clerk.set_password('ClerkPass!2026')
        db.session.add(clerk)
        db.session.flush()
        cid = clerk.id
        today = date.today()
        db.session.add_all([
            WorkSchedule(tenant_id=tid, staff_account_id=cid, work_date=today - timedelta(days=1), start_time='09:00',
                         end_time='17:00'),
            WorkSchedule(tenant_id=tid, staff_account_id=cid, work_date=today + timedelta(days=2), start_time='09:00',
                         end_time='17:00'),
            StaffAvailability(tenant_id=tid, staff_account_id=cid, weekday=1, start_time='09:00', end_time='17:00'),
        ])
        db.session.commit()
    assert owner.post('/admin/staff', data={'action': 'toggle_active', 'account_id': cid}).status_code == 302
    with app.app_context():
        acc = db.session.get(StaffAccount, cid)
        assert not acc.is_active and acc.session_epoch == 1
        assert WorkSchedule.query.filter_by(staff_account_id=cid).count() == 1   # 過去的班表保留
        assert StaffAvailability.query.filter_by(staff_account_id=cid).count() == 1
    owner.post('/admin/staff', data={'action': 'reset_password', 'account_id': cid, 'new_password': 'ClerkNew!20260'})
    with app.app_context():
        assert db.session.get(StaffAccount, cid).session_epoch == 2
    owner.post('/admin/staff', data={'action': 'delete_account', 'account_id': cid})
    with app.app_context():
        acc = db.session.get(StaffAccount, cid)
        assert acc.deleted_at is not None and acc.session_epoch == 3
        assert StaffAvailability.query.filter_by(staff_account_id=cid).count() == 0


def test_kitchen_on_hub_not_sent_to_kds(app, monkeypatch):
    """R2-016：線上端的廚房帳號不導向 /kitchen (線上端 404)"""
    client = _staff_with_perms(app, 'fix_ah1_hub_kitchen', 'kitchen', '')
    r = client.get('/admin')
    assert r.status_code == 302 and '/kitchen' in r.headers['Location']
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    r = client.get('/admin')
    assert r.status_code == 200
    assert '線上端沒有廚房看板' in r.get_data(as_text=True)


def test_dashboard_unpaid_tables_counts_tables_any_day(app):
    """B-311：未結帳桌位以桌為單位、不限日期 (與 POS 未結帳清單一致)"""
    from datetime import timedelta
    from flask import g
    from app.extensions import db
    from app.models.order import Order
    from app.models.table import DiningTable
    from app.routes import admin as admin_routes
    from tests.support import get_or_create_tenant
    tid = get_or_create_tenant(app, 'fix-ah1-unpaid')
    with app.app_context():
        t1, t2 = DiningTable(tenant_id=tid, name='A1'), DiningTable(tenant_id=tid, name='A2')
        db.session.add_all([t1, t2])
        db.session.flush()
        old = datetime.now() - timedelta(days=3)
        db.session.add_all([
            Order(tenant_id=tid, dining_table_id=t1.id, total_price=100, status='Pending', payment_status='unpaid', created_at=old),
            Order(tenant_id=tid, dining_table_id=t1.id, total_price=50, status='Pending', payment_status='unpaid',
                  created_at=datetime.now()),
            Order(tenant_id=tid, dining_table_id=t2.id, total_price=80, status='Completed', payment_status='unpaid', created_at=old),
            Order(tenant_id=tid, dining_table_id=t2.id, total_price=80, status='Cancelled', payment_status='unpaid', created_at=old),
        ])
        db.session.commit()
    with app.test_request_context():
        g.tenant_id = tid
        assert admin_routes._dashboard_summary()['unpaid_tables'] == 2


def test_dashboard_without_reports_skips_ai_advice(app, monkeypatch):
    """B-310：沒有報表權限時不產生 AI 營運建議"""
    from app.services import ai_service

    def no_ai(*_a, **_kw):
        raise AssertionError('不應呼叫 AI 營運建議')
    monkeypatch.setattr(ai_service, 'generate_ai_business_advice', no_ai)
    client = _staff_with_perms(app, 'fix_ah1_no_reports', 'staff', 'menu')
    assert client.get('/admin').status_code == 200


def test_live_orders_include_stale_pending_and_refunds(app, admin_client):
    """B-369：實時訂單看板列出前幾天遺留的製作中訂單；R2-138：帶 refunded_amount"""
    from datetime import timedelta
    from app.extensions import db
    from app.models.order import Order
    with app.app_context():
        o = Order(tenant_id=default_tenant_id(app), total_price=120, refunded_amount=20, status='Pending',
                  payment_status='paid', created_at=datetime.now() - timedelta(days=2))
        db.session.add(o)
        db.session.commit()
        oid = o.id
    r = admin_client.get('/api/admin_live_orders')
    assert r.status_code == 200
    row = next((x for x in r.get_json()['orders'] if x['id'] == oid), None)
    assert row is not None and row['refunded_amount'] == 20
