"""
AI 一鍵翻譯菜單 (Gemini 批次 / 規則字典 / 不覆蓋手填)、點餐機飲食篩選列 (素食 / 不辣 / 不含過敏原)、
點餐機禮物卡付款 (查餘額 / 扣款 / 餘額不足 / 未開放)、會員價 (price_for(member) / 點餐頁劃線價 / POS 綁定會員)、
後台頁面權限 (櫃檯授權 menu / coupons / members / settings / feedback 後可進對應分頁與頁面)。
"""
import json
import os
import re

from app.extensions import db
from app.models.menu import CategoryTranslation, MenuItem
from tests.support import checkout, guest_client, line, login_staff, make_item, make_member, member_client

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


# ---------------------------------------------------------------------------
# AI 翻譯
# ---------------------------------------------------------------------------
def test_translate_entries_parses_gemini_json_and_falls_back_to_rules(app, monkeypatch):
    from app.services import ai_service
    calls = []

    def fake_gemini(prompt, **kw):
        calls.append(prompt)
        return ('```json\n[{"i": 1, "name_en": "Beef Burger", "name_ja": "ビーフバーガー", "description_en": "Juicy", "description_ja": "ジューシー"},'
                ' {"i": 2, "name_en": "", "name_ja": ""}]\n```')
    monkeypatch.setattr(ai_service, 'gemini_generate', fake_gemini)
    with app.app_context():
        results, source = ai_service.translate_entries([{'name': '牛肉堡', 'description': '多汁'}, {'name': '飲料', 'description': ''}])
    assert source == 'gemini' and len(calls) == 1 and '牛肉堡｜多汁' in calls[0]
    assert results[0] == {'name_en': 'Beef Burger', 'name_ja': 'ビーフバーガー', 'description_en': 'Juicy', 'description_ja': 'ジューシー'}
    assert results[1]['name_en'] == 'Drinks' and results[1]['name_ja'] == 'ドリンク', '第 2 筆 Gemini 沒翻 → 規則字典'
    # 沒有金鑰 / 回應壞掉：整批走規則字典
    monkeypatch.setattr(ai_service, 'gemini_generate', lambda *a, **k: 'not json')
    with app.app_context():
        results, source = ai_service.translate_entries([{'name': '主餐'}, {'name': '招牌和牛堡'}])
    assert source == 'rules' and results[0]['name_en'] == 'Mains' and results[1]['name_en'] == '', '品名只在整個字串命中時才翻'
    assert ai_service.rule_translate('  點心 ') == {'en': 'Snacks', 'ja': 'スナック'}
    assert ai_service._parse_translations('[{"i": "x"}, {"i": 99, "name_en": "no"}]', 1) == {}


def test_menu_translate_action_fills_only_blank_fields(app, admin_client, monkeypatch):
    from app.services import ai_service
    item = make_item(app, '翻譯測試雞腿飯', price=120, category='翻譯測試分類', name_en='Hand typed')
    item2 = make_item(app, '翻譯測試紅茶', price=30, category='翻譯測試分類', description='香醇')
    monkeypatch.setattr(ai_service, 'gemini_generate', lambda prompt, **k: json.dumps(
        [{'i': i + 1, 'name_en': f'EN{i}', 'name_ja': f'JA{i}', 'description_en': f'DE{i}', 'description_ja': f'DJ{i}'}
         for i in range(60)]))
    r = admin_client.post('/admin/menu_pro', data={'action': 'translate'})
    assert r.status_code == 302
    with app.app_context():
        a, b = db.session.get(MenuItem, item), db.session.get(MenuItem, item2)
        assert a.name_en == 'Hand typed', '手填的不覆蓋'
        assert a.name_ja.startswith('JA') and b.name_en.startswith('EN') and b.description_en.startswith('DE')
        assert a.description_en == '', '沒有說明的品項不填說明翻譯'
        cat = CategoryTranslation.query.filter_by(category='翻譯測試分類').first()
        assert cat is not None and cat.name_en.startswith('EN') and cat.name_ja.startswith('JA')
        from app.models.user import StaffAuditLog
        assert StaffAuditLog.query.filter_by(action_type='MENU_TRANSLATE').count() >= 1
    # 連已填的也重新翻譯
    admin_client.post('/admin/menu_pro', data={'action': 'translate', 'overwrite': '1'})
    with app.app_context():
        assert db.session.get(MenuItem, item).name_en.startswith('EN')
    html = admin_client.get('/admin/menu_pro').get_data(as_text=True)
    assert 'AI 一鍵翻譯菜單' in html and 'menuTranslateForm' in html


# ---------------------------------------------------------------------------
# 飲食篩選
# ---------------------------------------------------------------------------
def test_kiosk_menu_rows_carry_diet_data_and_filter_bar(app, store_settings):
    make_item(app, '篩選花生醬堡', price=90, allergens='peanut,milk', spicy_level=2)
    make_item(app, '篩選全素沙拉', price=80, diet='vegan')
    html = guest_client(app).get('/').get_data(as_text=True)
    assert 'id="dietFilterBar"' in html and 'data-kind="veg"' in html and 'data-kind="mild"' in html
    assert 'data-code="peanut"' in html and 'data-code="milk"' in html
    row = re.search(r'<div class="col-md-6 menu-row"[^>]*data-allergens="peanut,milk"[^>]*>', html)
    assert row and 'data-spicy="2"' in row.group(0) and 'data-diet=""' in row.group(0)
    assert re.search(r'data-allergens=""[^>]*data-diet="vegan"', html)
    with open(os.path.join(ROOT, 'static', 'js', 'customer', 'menu.js'), encoding='utf-8') as fh:
        js = fh.read()
    assert 'passesDietFilter' in js and 'kiosk_diet_filter' in js and 'toggleDietFilter' in js
    # 關閉過敏原顯示時沒有篩選列
    store_settings('menu', show_allergens=False)
    assert 'id="dietFilterBar"' not in guest_client(app).get('/').get_data(as_text=True)
    store_settings('menu', show_allergens=True)


# ---------------------------------------------------------------------------
# 點餐機禮物卡
# ---------------------------------------------------------------------------
def _sell_card(app, amount=300):
    from app.services import giftcard_service
    from tests.support import default_tenant_id
    with app.app_context():
        with app.test_request_context('/'):
            from flask import g
            g.tenant_id = default_tenant_id(app)
            card = giftcard_service.sell(g.tenant_id, amount, 'Cash', 'tester')
            db.session.commit()
            return card.code


def test_kiosk_gift_card_payment(app, store_settings):
    item = make_item(app, '禮物卡點餐機餐', price=100)
    store_settings('gift_card', enabled=True, kiosk_enabled=False)
    c = guest_client(app)
    assert c.post('/api/gift_cards/balance', json={'code': '1' * 16}).status_code == 404, '未開放點餐機禮物卡'
    assert '禮物卡' not in c.get('/').get_data(as_text=True).split('id="paymentMethod"')[1][:600]
    store_settings('gift_card', enabled=True, kiosk_enabled=True)
    code = _sell_card(app, 300)
    c = guest_client(app)
    html = c.get('/').get_data(as_text=True)
    assert 'id="giftCardBox"' in html and 'data-action="gift-card-check"' in html
    r = c.post('/api/gift_cards/balance', json={'code': code[:4] + ' ' + code[4:]})
    body = r.get_json()
    assert r.status_code == 200 and body['balance'] == 300 and '****' in body['masked_code'] and code not in json.dumps(body)
    assert c.post('/api/gift_cards/balance', json={'code': '123'}).status_code == 400
    # 餘額不足 → 409 insufficient_gift，訂單不成立
    r = checkout(c, [line(item, 4)], payment_method='禮物卡', payment_info={'gift_code': code})
    assert r.status_code == 409 and r.get_json().get('insufficient_gift') is True
    # 扣款成功
    r = checkout(c, [line(item, 2)], payment_method='禮物卡', payment_info={'gift_code': code})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert c.post('/api/gift_cards/balance', json={'code': code}).get_json()['balance'] == 100
    with app.app_context():
        from app.models.order import Order
        o = db.session.get(Order, r.get_json()['order_id'])
        assert o.payment_method == '禮物卡' and o.payment_reference == code
    # 關掉點餐機禮物卡後結帳被擋 (POS 仍可)
    store_settings('gift_card', enabled=True, kiosk_enabled=False)
    r = checkout(guest_client(app), [line(item)], payment_method='禮物卡', payment_info={'gift_code': code})
    assert r.status_code == 400 and '未開放' in r.get_json()['message']


# ---------------------------------------------------------------------------
# 會員價
# ---------------------------------------------------------------------------
def test_member_price_applies_only_to_members(app, store_settings):
    item = make_item(app, '會員價牛排', price=200, member_price=150, takeout_price=180)
    sale = make_item(app, '會員價特價品', price=100, member_price=90, is_discount=True, discount_price=80)
    with app.app_context():
        it, sl = db.session.get(MenuItem, item), db.session.get(MenuItem, sale)
        assert it.price_for('dine_in') == 200 and it.price_for('dine_in', member=True) == 150
        assert it.price_for('takeout') == 180 and it.price_for('takeout', member=True) == 150
        assert it.member_deal() == 150 and sl.member_deal() is None, '特價比會員價低時不顯示會員價'
        assert sl.price_for('dine_in', member=True) == 80
        assert it.to_dict()['member_prices']['dine_in'] == 150
    # 訪客：原價
    guest = guest_client(app)
    html = guest.get('/').get_data(as_text=True)
    assert '會員價' not in html.split('id="menuList"')[1][:20000] or 'Member price' not in html
    r = checkout(guest, [line(item)])
    assert r.status_code == 200 and r.get_json()['total_price'] == 200
    # 會員：劃線價 + 會員價結帳
    make_member(app, '會員價會員', '0977800150', pin='2468')
    m = member_client(app, '0977800150', '2468')
    html = m.get('/').get_data(as_text=True)
    block = html.split(f'data-id="{item}"')[1][:3000]
    assert '<del class="text-white-50 small me-1">$200</del>' in block and '$150' in block and '會員價' in block
    r = checkout(m, [line(item)], expected_total=150)
    assert r.status_code == 200 and r.get_json()['total_price'] == 150
    # 促銷預覽也用會員價 (不會因金額不同被擋)
    r = m.post('/api/promotions/preview', json={'items': [line(item)], 'channel': 'kiosk', 'order_type': '內用'})
    assert r.status_code == 200
    # POS：綁定會員才有會員價；POS 菜單資料帶 member_prices
    pos = login_staff(app, 'member_price_pos', 'staff')
    assert 'member_prices' in pos.get('/pos').get_data(as_text=True)
    from tests.support import pos_checkout
    with app.app_context():
        from app.models.user import User
        uid = User.query.filter_by(phone='0977800150').first().id
    assert pos_checkout(pos, item).get_json()['total_price'] == 200
    assert pos_checkout(pos, item, user_id=uid).get_json()['total_price'] == 150
    with open(os.path.join(ROOT, 'static', 'js', 'pos', 'core.js'), encoding='utf-8') as fh:
        assert 'member_prices' in fh.read()
    with open(os.path.join(ROOT, 'static', 'js', 'customer', 'core.js'), encoding='utf-8') as fh:
        core = fh.read()
    assert 'member_prices' in core and 'isGuest' in core
    # 後台表單可設定
    admin = login_staff(app, 'member_price_admin', 'manager')
    admin.post('/admin/menu_pro', data={'action': 'item', 'item_id': item, 'member_price': '160', 'takeout_price': '0',
                                        'online_price': '0', 'delivery_price': '0', 'avail_start': '', 'avail_end': '',
                                        'sale_from': '', 'sale_until': '', 'spicy_level': '0',
                                        'avail_days': ['1', '2', '3', '4', '5', '6', '7']})
    with app.app_context():
        assert db.session.get(MenuItem, item).member_price == 160


# ---------------------------------------------------------------------------
# 後台頁面權限
# ---------------------------------------------------------------------------
def test_page_permissions_grant_staff_access_to_back_office_pages(app):
    from app.models.enums import PAGE_PERMISSIONS, PERMISSIONS, ROLE_DEFAULT_PERMISSIONS
    assert {'menu', 'coupons', 'members', 'feedback', 'settings'} <= set(PERMISSIONS)
    assert set(PAGE_PERMISSIONS) <= set(PERMISSIONS) and ROLE_DEFAULT_PERMISSIONS['manager'] == set(PERMISSIONS)
    staff = login_staff(app, 'perm_page_staff', 'staff')
    # 預設：後台導回 POS、各頁 403
    assert staff.get('/admin').status_code == 302
    for path in ('/admin/menu_pro', '/admin/promotions', '/admin/members_pro', '/admin/feedback', '/admin/settings'):
        assert staff.get(path).status_code == 403, path
    item = make_item(app, '權限測試餐', price=10)
    assert staff.post(f'/admin/edit/{item}', data={'name': 'x'}).status_code == 403
    # 負責人授權 menu + coupons
    from app.models.tenant import StaffAccount
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='perm_page_staff').first()
        acc.permissions = 'menu,coupons'
        db.session.commit()
    html = staff.get('/admin').get_data(as_text=True)
    assert 'id="rewards-pane"' in html and 'id="orders-pane"' not in html, '沒授權的分頁不渲染'
    assert 'id="nav-menu-pro"' in html and 'id="nav-promotions"' in html and 'id="nav-back-work"' in html and 'id="nav-settings"' not in html
    menu = staff.get('/admin/menu').get_data(as_text=True)
    assert 'id="menu-pane"' in menu and 'id="menuItemTable"' in menu, '菜單頁 (品項 + 套餐 + 進階設定) 以 menu 權限進入'
    assert staff.get('/admin/promotions').status_code == 200
    assert staff.get('/admin/members').status_code == 403 and staff.get('/admin/settings').status_code == 403, '會員頁含個資：沒有 members 權限 403'
    assert staff.post('/admin/delete_user/1', data={}).status_code == 403
    # 店長：全部分頁
    manager = login_staff(app, 'perm_page_manager', 'manager')
    html = manager.get('/admin').get_data(as_text=True)
    assert all(f'id="{p}-pane"' in html for p in ('home', 'orders', 'rewards'))
    assert manager.get('/admin/menu').status_code == 200 and manager.get('/admin/members').status_code == 200
    assert 'id="nav-settings"' in html
    # 員工帳號頁列出新權限
    owner = login_staff(app, 'perm_page_owner', 'owner')
    html = owner.get('/admin/staff').get_data(as_text=True)
    assert '後台：菜單品項' in html and '後台：系統設定' in html
