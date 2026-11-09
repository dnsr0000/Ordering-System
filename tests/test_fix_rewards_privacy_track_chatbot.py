"""修正回歸：AI 客服 (跨店菜單、分店價 / 下架、品名含過敏原字、待分店確認的取餐號、LLM 菜單截斷)、
個資 (清除長期未使用會員不刪儲值金 / 寄杯、無活動紀錄的新會員、刪除帳號擋寄杯、去識別化、確認字、PIN 鎖定、下載資料完整性)"""
import json
from datetime import datetime, timedelta

from app.services import ai_service, chatbot_service
from tests.support import default_tenant_id, get_or_create_tenant, make_item, make_member, member_client, tenant_request


def _chatbot_on(store_settings):
    store_settings('chatbot', enabled=True, bot_name='阿福', store_info='', faq='', llm_enabled=True)


# ---------------------------------------------------------------- AI 客服
def test_chatbot_menu_is_limited_to_own_shop_even_under_tenant_bypass(app, store_settings, monkeypatch):
    """B-268：/api/chat 在 tenant_bypass 內呼叫時，菜單也只能是本店的"""
    from app.extensions import db
    from app.models.menu import MenuItem
    from app.tenant_context import tenant_bypass
    _chatbot_on(store_settings)
    monkeypatch.setattr(ai_service, 'generate_text', lambda *a, **k: (None, ''))
    make_item(app, '修測本店花生糕', price=30, category='修測本店分類', allergens='peanut')
    other = get_or_create_tenant(app, 'fix-chat-other')
    with app.app_context():
        db.session.add(MenuItem(tenant_id=other, name='修測別店花生湯', category='修測別店分類', price=99, stock=10,
                                total_stock=10, allergens='peanut'))
        db.session.commit()
    tid = default_tenant_id(app)
    with tenant_request(app, tid):
        with tenant_bypass():
            names = {i.name for i in chatbot_service._items(tid)}
            assert '修測本店花生糕' in names and '修測別店花生湯' not in names
            r = chatbot_service.answer('菜單有哪些分類？', tenant_id=tid)
            assert '修測本店分類' in r['answer'] and '修測別店分類' not in r['answer']
            r = chatbot_service.answer('哪些含花生？', tenant_id=tid)
            assert '修測別店花生湯' not in r['answer']


def test_chatbot_uses_branch_price_hidden_and_channel_price(app, store_settings, monkeypatch):
    """R2-098：分店價 / 分店下架 / 線上通路價"""
    from app.extensions import db
    from app.models.branch import BranchMenuOverride
    from app.sync import runtime
    _chatbot_on(store_settings)
    monkeypatch.setattr(ai_service, 'generate_text', lambda *a, **k: (None, ''))
    tid = default_tenant_id(app)
    a = make_item(app, '修測分店價便當', price=100, category='修測分店分類')
    b = make_item(app, '修測分店下架麵', price=80, category='修測分店分類')
    with app.app_context():
        db.session.add_all([BranchMenuOverride(tenant_id=tid, node_uuid='fix-chat-node', menu_item_id=a, price=150),
                            BranchMenuOverride(tenant_id=tid, node_uuid='fix-chat-node', menu_item_id=b, is_hidden=True)])
        db.session.commit()
    monkeypatch.setattr(runtime, 'is_hub', lambda: False)
    monkeypatch.setattr(runtime, 'is_node', lambda: True)
    monkeypatch.setattr(runtime, 'local_node_uuid', lambda: 'fix-chat-node')
    with tenant_request(app, tid):
        assert '$150' in chatbot_service.answer('修測分店價便當多少錢')['answer']
        r = chatbot_service.answer('修測分店分類有哪些')
        assert '修測分店價便當' in r['answer'] and '修測分店下架麵' not in r['answer']

    c = make_item(app, '修測線上價飯糰', price=60, online_price=70)
    monkeypatch.setattr(runtime, 'is_hub', lambda: True)
    monkeypatch.setattr(runtime, 'is_node', lambda: False)
    monkeypatch.setattr(runtime, 'local_node_uuid', lambda: None)
    with tenant_request(app, tid):
        assert '$70' in chatbot_service.answer('修測線上價飯糰多少錢')['answer']
    assert c


def test_item_names_with_allergen_words_still_answer_price_and_status(app, store_settings, monkeypatch):
    """R2-099：品名含「蛋 / 魚」時問價格、有沒有賣、進度不可只回過敏原"""
    from app.models.order import Order
    _chatbot_on(store_settings)
    monkeypatch.setattr(ai_service, 'generate_text', lambda *a, **k: (None, ''))
    make_item(app, '修測起司蛋餅', price=55, allergens='egg,milk')
    make_item(app, '修測鮮魚湯', price=90, allergens='fish')
    with tenant_request(app, default_tenant_id(app)):
        r = chatbot_service.answer('修測起司蛋餅多少錢？')
        assert '$55' in r['answer'] and '過敏原' not in r['answer']
        assert '$90' in chatbot_service.answer('修測鮮魚湯多少錢')['answer']
        assert '過敏原' not in chatbot_service.answer('起司蛋餅還有嗎')['answer']
        r = chatbot_service.answer('修測起司蛋餅有蛋嗎')
        assert '過敏原' in r['answer'] and '蛋' in r['answer'], '真正問過敏原時仍走過敏原檢索'
        assert '店家標示' in chatbot_service.answer('哪些含花生')['answer'], '過敏原用語本身不會被當成品名片段拿掉'
        order = Order(order_source='Kiosk', pickup_number=7, status='Pending', total_price=55, payment_status='')
        r = chatbot_service.answer('我的蛋餅好了嗎', order=order)
        assert '製作中' in r['answer'] and '#007' in r['answer']


def test_status_answer_for_unnumbered_online_and_unpaid_orders():
    """B-275：線上訂單尚未編號時不可報訂單 id 當取餐號；待付款時提示付款"""
    from app.models.order import Order
    order = Order(id=1234, order_source='Online', pickup_number=0, status='Pending', total_price=100, payment_status='paid')
    text = chatbot_service._status_answer(order)
    assert '待分店確認' in text and '1234' not in text
    order.status = 'Completed'
    assert '待分店確認' in chatbot_service._status_answer(order)
    order.payment_status = 'pending'
    assert '尚未完成付款' in chatbot_service._status_answer(order)


def test_llm_menu_summary_keeps_every_item(app):
    """B-276：超過上限的品項改列精簡清單，不直接截掉"""
    a = make_item(app, '修測摘要甲', price=10, category='修測摘要')
    b = make_item(app, '修測摘要乙', price=20, category='修測摘要')
    c = make_item(app, '修測摘要丙', price=30, category='修測摘要')
    with tenant_request(app, default_tenant_id(app)):
        items = [i for i in chatbot_service._items() if i.id in (a, b, c)]
        text = chatbot_service._menu_summary(items, limit=1)
    assert all(n in text for n in ('修測摘要甲', '修測摘要乙', '修測摘要丙'))
    assert text.count('$') == 1


# ---------------------------------------------------------------- 個資：清除長期未使用會員
def _age_member(app, uid, days=900):
    from app.extensions import db
    from app.models.order import Order
    from app.models.sync import PointLedger
    long_ago = datetime.now() - timedelta(days=days)
    with app.app_context():
        PointLedger.query.filter(PointLedger.user_id == uid).update({'created_at': long_ago})
        db.session.add(Order(tenant_id=default_tenant_id(app), user_id=uid, total_price=10, table_number='x', created_at=long_ago))
        db.session.commit()


def test_purge_keeps_members_with_wallet_balance_or_passes(app):
    """B-269：有儲值金餘額 / 未用完寄杯的會員不可被清除"""
    from app.extensions import db
    from app.models.member import MemberPass, PassLedger
    from app.models.user import User
    from app.services import privacy_service
    tid = default_tenant_id(app)
    wallet = make_member(app, '修測儲值會員', '0977810001')
    held = make_member(app, '修測寄杯會員', '0977810002')
    plain = make_member(app, '修測一般舊會員', '0977810003')
    for uid in (wallet, held, plain):
        _age_member(app, uid)
    with app.app_context():
        db.session.get(User, wallet).wallet_balance = 3000
        mp = MemberPass(tenant_id=tid, user_id=held, name='美式 10 杯', total_uses=10, price_paid=450,
                        status='active', remaining=8, created_at=datetime.now() - timedelta(days=900))
        db.session.add(mp)
        db.session.flush()
        # 剩餘次數要有對應的寄杯異動帳 (test_sync_invariants 檢查 剩餘次數 = 異動帳合計)
        db.session.add(PassLedger(tenant_id=tid, member_pass_id=mp.id, delta=8, reason='test',
                                  created_at=datetime.now() - timedelta(days=900)))
        db.session.commit()
        names = {u.name for u in privacy_service.purge_inactive_members(tid, 24, dry_run=True)}
        assert '修測一般舊會員' in names and '修測儲值會員' not in names and '修測寄杯會員' not in names
        kept = {u.name for u in privacy_service.inactive_members_with_balance(tid, 24)}
        assert {'修測儲值會員', '修測寄杯會員'} <= kept
        privacy_service.purge_inactive_members(tid, 24, dry_run=False)
        assert db.session.get(User, plain) is None
        assert db.session.get(User, wallet) is not None and db.session.get(User, held) is not None
        assert MemberPass.query.filter_by(user_id=held).count() == 1


def test_purge_skips_new_members_without_activity(app):
    """B-271：剛匯入、沒有任何活動紀錄的會員不可被當成逾期刪除"""
    from app.services import privacy_service
    tid = default_tenant_id(app)
    make_member(app, '修測剛匯入會員', '0977810010')
    with app.app_context():
        assert '修測剛匯入會員' not in {u.name for u in privacy_service.inactive_members(tid, 24)}


# ---------------------------------------------------------------- 個資：會員刪除帳號
def test_delete_account_blocks_unused_passes_and_deidentifies(app):
    """B-272 / B-273：有寄杯不可刪；刪除時清除外送地址電話與進場紀錄的姓名手機"""
    from app.errors import AppError
    from app.extensions import db
    from app.models.member import MemberPass
    from app.models.order import Order
    from app.models.user import CustomerLog, User
    from app.services import privacy_service
    tid = default_tenant_id(app)
    uid = make_member(app, '修測刪除會員', '0977810020')
    with app.app_context():
        mp = MemberPass(tenant_id=tid, user_id=uid, name='拿鐵 10 杯', total_uses=10, price_paid=500, status='active', remaining=8)
        done = Order(tenant_id=tid, user_id=uid, total_price=100, table_number='修測刪除會員', delivery_address='台南市某路 1 號',
                     delivery_phone='0977810020', delivery_status='delivered')
        live = Order(tenant_id=tid, user_id=uid, total_price=100, table_number='修測刪除會員', delivery_address='台南市某路 2 號',
                     delivery_phone='0977810020', delivery_status='out')
        log = CustomerLog(tenant_id=tid, user_type='會員', identifier='修測刪除會員 (0977810020)')
        db.session.add_all([mp, done, live, log])
        db.session.commit()
        done_id, live_id, log_id, mp_id = done.id, live.id, log.id, mp.id
    with tenant_request(app, tid):
        user = db.session.get(User, uid)
        try:
            privacy_service.delete_member_account(user)
            raise AssertionError('有未用完的寄杯不可刪除')
        except AppError as err:
            assert err.status == 409 and '寄杯' in str(err)
        db.session.rollback()
        db.session.get(MemberPass, mp_id).remaining = 0
        db.session.commit()
        privacy_service.delete_member_account(db.session.get(User, uid))
        db.session.commit()
        assert db.session.get(User, uid) is None
        o = db.session.get(Order, done_id)
        assert o.table_number == '已刪除會員' and o.delivery_address == '' and o.delivery_phone == ''
        assert db.session.get(Order, live_id).delivery_address == '台南市某路 2 號', '配送中的訂單保留地址'
        assert db.session.get(CustomerLog, log_id).identifier == '已刪除會員'


def test_delete_account_accepts_other_language_confirm_words(app):
    """B-270：英 / 日文介面打得出來的確認字也接受"""
    from app.extensions import db
    from app.models.user import User
    uid = make_member(app, '修測英文刪除', '0977810030', pin='5813')
    c = member_client(app, '0977810030', '5813', ip='10.81.0.30')
    r = c.post('/privacy/delete_account', data={'pin': '5813', 'confirm': 'Delete'}, headers={'X-Forwarded-For': '10.81.0.30'})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(User, uid) is None
    uid = make_member(app, '修測日文刪除', '0977810031', pin='5813')
    c = member_client(app, '0977810031', '5813', ip='10.81.0.31')
    r = c.post('/privacy/delete_account', data={'pin': '5813', 'confirm': '削除'}, headers={'X-Forwarded-For': '10.81.0.31'})
    assert r.status_code == 302
    with app.app_context():
        assert db.session.get(User, uid) is None


def test_delete_account_pin_respects_lockout(app):
    """B-274：PIN 鎖定中時，即使猜中 PIN 也不可刪除帳號；猜錯會累計失敗"""
    from app.extensions import db
    from app.models.user import User
    from app.services import auth_throttle
    uid = make_member(app, '修測鎖定會員', '0977810040', pin='2580')
    c = member_client(app, '0977810040', '2580', ip='10.81.0.40')
    hdr = {'X-Forwarded-For': '10.81.0.40'}
    tid = default_tenant_id(app)
    assert c.post('/privacy/delete_account', data={'pin': '0000', 'confirm': '刪除'}, headers=hdr).status_code in (302, 403)
    with app.test_request_context('/'):
        keys = auth_throttle.pin_keys(tid, '0977810040', '10.81.0.40')
        assert db.session.get(auth_throttle.AuthThrottle, keys['account']).fails >= 1
        for _ in range(12):
            auth_throttle.record_pin_failure(keys)
    c.post('/privacy/delete_account', data={'pin': '2580', 'confirm': '刪除'}, headers=hdr)
    with app.app_context():
        assert db.session.get(User, uid) is not None, '鎖定中不可刪除'


def test_delete_account_reserves_pin_attempt_before_verifying(app, monkeypatch):
    """B-358：驗證 PIN 前先預扣額度 (同時送出的請求已用完額度時，猜中 PIN 也不可刪除)；猜錯不重複計數"""
    from app.extensions import db
    from app.models.user import User
    from app.services import auth_throttle
    tid = default_tenant_id(app)
    uid = make_member(app, '修測預扣會員', '0977810060', pin='1470')
    c = member_client(app, '0977810060', '1470', ip='10.81.0.60')
    hdr = {'X-Forwarded-For': '10.81.0.60'}
    c.post('/privacy/delete_account', data={'pin': '0000', 'confirm': '刪除'}, headers=hdr)
    with app.test_request_context('/'):
        keys = auth_throttle.pin_keys(tid, '0977810060', '10.81.0.60')
        fails = {k: db.session.get(auth_throttle.AuthThrottle, v).fails for k, v in keys.items()}
    assert fails == {'device': 1, 'account': 1, 'ip': 1}, fails
    calls = []
    monkeypatch.setattr(auth_throttle, 'reserve_pin_attempt', lambda k: calls.append(k) or timedelta(minutes=5))
    c.post('/privacy/delete_account', data={'pin': '1470', 'confirm': '刪除'}, headers=hdr)
    assert calls, '刪除帳號前必須預扣 PIN 額度'
    with app.app_context():
        assert db.session.get(User, uid) is not None, '額度已用完時不可刪除'


def test_deidentify_keeps_table_number_of_table_orders(app):
    """B-308：桌邊掃碼訂單的 table_number 是桌號，刪除會員時保留；其他訂單的姓名改為已刪除會員"""
    from app.extensions import db
    from app.models.order import Order
    from app.models.user import User
    from app.services import privacy_service
    tid = default_tenant_id(app)
    uid = make_member(app, '修測桌號會員', '0977810070')
    with app.app_context():
        table = Order(tenant_id=tid, user_id=uid, total_price=50, table_number='A3', order_source='Table')
        kiosk = Order(tenant_id=tid, user_id=uid, total_price=50, table_number='修測桌號會員', order_source='Kiosk')
        db.session.add_all([table, kiosk])
        db.session.commit()
        table_id, kiosk_id = table.id, kiosk.id
    with tenant_request(app, tid):
        privacy_service.delete_member_account(db.session.get(User, uid))
        db.session.commit()
        assert db.session.get(Order, table_id).table_number == 'A3'
        assert db.session.get(Order, kiosk_id).table_number == '已刪除會員'


def test_opening_point_entry_is_not_member_activity(app):
    """R2-197：升級時寫入的期初點數異動 (reason='opening') 不算會員活動"""
    from app.extensions import db
    from app.models.sync import PointLedger
    from app.services import privacy_service
    tid = default_tenant_id(app)
    long_ago = datetime.now() - timedelta(days=365 * 3)
    uid = make_member(app, '修測期初點數會員', '0977810080', last_login_at=long_ago)
    with app.app_context():
        db.session.add(PointLedger(tenant_id=tid, user_id=uid, delta=30, reason='opening', created_at=datetime.now()))
        db.session.commit()
        assert '修測期初點數會員' in {u.name for u in privacy_service.inactive_members(tid, 24)}
        db.session.add(PointLedger(tenant_id=tid, user_id=uid, delta=5, reason='earn', created_at=datetime.now()))
        db.session.commit()
        assert '修測期初點數會員' not in {u.name for u in privacy_service.inactive_members(tid, 24)}
        # 直接新增的異動帳：重算會員點數，維持 點數 = 異動帳合計 (test_sync_invariants)
        from app.services.points_service import recompute_points
        recompute_points([uid])
        db.session.commit()


def test_export_includes_email_passes_and_feedback(app):
    """B-277：下載我的資料包含 Email、行銷設定、寄杯"""
    from app.extensions import db
    from app.models.member import MemberPass, PassLedger
    from app.models.user import User
    tid = default_tenant_id(app)
    uid = make_member(app, '修測匯出會員', '0977810050', pin='3691')
    with app.app_context():
        db.session.get(User, uid).email = 'fix-export@example.com'
        mp = MemberPass(tenant_id=tid, user_id=uid, name='紅茶 5 杯', total_uses=5, price_paid=150, status='active',
                        remaining=5)
        db.session.add(mp)
        db.session.flush()
        # 剩餘次數要有對應的寄杯異動帳 (test_sync_invariants 檢查 剩餘次數 = 異動帳合計)
        db.session.add(PassLedger(tenant_id=tid, member_pass_id=mp.id, delta=5, reason='test'))
        db.session.commit()
    c = member_client(app, '0977810050', '3691', ip='10.81.0.50')
    data = json.loads(c.get('/privacy/my_data', headers={'X-Forwarded-For': '10.81.0.50'}).get_data(as_text=True))
    assert data['profile']['email'] == 'fix-export@example.com' and data['profile']['marketing_opt_out'] is False
    assert [p['name'] for p in data['member_passes']] == ['紅茶 5 杯'] and data['member_passes'][0]['remaining'] == 5
    assert 'order_feedback' in data and 'reward_redemptions' in data and 'support_chats' in data
