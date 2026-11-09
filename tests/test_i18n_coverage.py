"""
顧客端多語系 (前端翻譯、後端維持中文)：
- UI 字串涵蓋所有顧客頁樣板 (不再有寫死的中文)、鍵名不可與 dict 方法撞名 (Jinja 的 ui.clear 會取到 dict.clear)
- 英文 / 日文渲染每一個顧客頁，去掉店名 / 菜單內容後不再有中文
- 後端訊息對照 (translate_message)：完全比對、樣式比對、沒有對照時原樣、圖示保留
- JSON 錯誤回應附 message_i18n、flash 訊息以 tr_msg 翻譯、HTTP 錯誤頁翻譯
- 員工頁 (後台 / POS / 廚房) 一律中文；第一次進站依瀏覽器 Accept-Language 選語言
"""
import os
import re
from datetime import date, datetime, timedelta

import pytest

from app.services import i18n_service
from tests.support import checkout, line, login_staff, make_item, make_member, member_client

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
CJK = re.compile(r'[一-鿿]')

# 顧客看得到的樣板 (點餐機 / 手機)；員工頁不在此列
CUSTOMER_TEMPLATES = (
    'customer.html', 'register.html', 'forgot_pin.html', 'track.html', 'pay.html', 'payment_return.html',
    'table_pay.html', 'table_pay_status.html', 'reservation_pay.html', 'reservation_status.html', 'reserve.html',
    'waitlist_join.html', 'waitlist_status.html', 'offline.html', 'error.html', 'rewards.html',
    'group_new.html', 'group_join.html', 'group_manage.html', 'group_created.html', '_group_form_fields.html',
    'privacy.html', '_flashes.html',
)


def _template_leftovers(name):
    with open(os.path.join(ROOT, 'templates', name), encoding='utf-8') as fh:
        src = fh.read()
    src = re.sub(r'{#.*?#}', '', src, flags=re.S)
    src = re.sub(r'<!--.*?-->', '', src, flags=re.S)
    src = re.sub(r'{{.*?}}', '', src, flags=re.S)          # 變數 (ui.xxx、item.name…)
    src = re.sub(r'{%.*?%}', '', src, flags=re.S)          # 樣板邏輯 (含送給後端的中文值，例如 order_type)
    src = re.sub(r'\b(?:value|data-[\w-]+|name|id)="[^"]*"', '', src)   # 送給後端的值不是顯示文字
    return sorted({m.group(0).strip() for m in re.finditer(r'[^<>{}\n]*' + CJK.pattern + r'[^<>{}\n]*', src)})


def _visible_text(html):
    """去掉 script / 送給後端的表單值 / 標籤後的可見文字 (每行一段)"""
    body = re.sub(r'<script\b.*?</script>', '', html, flags=re.S)
    body = re.sub(r'<(?:option|input|form)\b[^>]*>', '', body)   # 送給後端的中文值 (內用 / 外帶…)
    body = re.sub(r'<[^>]+>', '\n', body)
    return [ln.strip() for ln in body.splitlines() if ln.strip()]


def _chinese_ui_lines(html, lang, allow=()):
    """頁面上殘留的中文介面文字 (排除店名 / 菜單內容等店家填寫的文字)。
    英文頁：任何含漢字的片段都算；日文頁 (漢字與中文共用)：片段剛好等於某個 UI 字串的中文版 (且與日文版不同) 才算。"""
    zh_only = {v['zh'].strip(' ：:') for v in i18n_service.UI.values() if v['zh'] != v[lang] and len(v['zh']) >= 2}
    out = []
    for ln in _visible_text(html):
        stripped = ln
        for word in allow:
            stripped = stripped.replace(word, '')
        if not CJK.search(stripped):
            continue
        if ln.strip(' ：:') in zh_only or (lang == 'en'):
            out.append(ln)
    return out


# ---------------------------------------------------------------------------
# 字典本身
# ---------------------------------------------------------------------------
def test_ui_keys_do_not_shadow_dict_methods():
    clash = [k for k in i18n_service.UI if hasattr({}, k)]
    assert clash == [], f'這些鍵在 Jinja 的 ui.<鍵> 會取到 dict 的方法而不是文字：{clash}'
    assert 'clear_filters' in i18n_service.UI and 'copy_btn' in i18n_service.UI


def test_every_ui_key_has_three_languages():
    for key, langs in i18n_service.UI.items():
        assert set(langs) == {'zh', 'en', 'ja'}, key
        assert all(langs[lang] is not None for lang in ('zh', 'en', 'ja')), key
    for key in ('ice_values', 'sugar_values', 'spicy_values'):
        counts = {lang: len(v.split('|')) for lang, v in i18n_service.UI[key].items()}
        assert len(set(counts.values())) == 1, (key, counts)


def test_backend_message_table_is_well_formed():
    from app.errors import HTTP_MESSAGES
    from app.services.i18n_pages import MESSAGE_PATTERNS, MESSAGES
    for zh, (en, ja) in MESSAGES.items():
        assert CJK.search(zh) and en and ja, zh
        assert not CJK.search(en.replace('刪除', '')), zh   # 「刪除」是顧客要輸入的確認字，保留中文
    for pattern, (en, ja) in MESSAGE_PATTERNS:
        names = set(pattern.groupindex)
        assert names, pattern.pattern
        if callable(en):   # 自訂翻譯函式 (lang, **群組)
            continue
        assert set(re.findall(r'{(\w+)}', en)) <= names and set(re.findall(r'{(\w+)}', ja)) <= names, pattern.pattern
    assert set(HTTP_MESSAGES.values()) <= set(MESSAGES), '每個 HTTP 錯誤訊息都要有翻譯'


# ---------------------------------------------------------------------------
# 樣板：不再寫死中文
# ---------------------------------------------------------------------------
@pytest.mark.parametrize('name', CUSTOMER_TEMPLATES)
def test_customer_templates_have_no_hardcoded_chinese_text(name):
    leftovers = _template_leftovers(name)
    assert leftovers == [], f'{name} 仍有寫死的中文介面文字，請改用 ui.<鍵>：{leftovers[:10]}'


def test_customer_js_uses_ui_dictionary_for_visible_text():
    """顧客端 JS 的顯示文字一律經 Kiosk.t / ui.<鍵> 取得；送給後端的值 (內用 / 外帶、冰塊…) 才可以是中文字面"""
    with open(os.path.join(ROOT, 'static', 'js', 'common.js'), encoding='utf-8') as fh:
        common = fh.read()
    assert "pageData('ui-data')" in common and 'data.message_i18n' in common
    for rel in ('rewards.js', 'register.js', 'track.js', 'waitlist.js', 'member-extras.js', 'kiosk-offline.js'):
        with open(os.path.join(ROOT, 'static', 'js', rel), encoding='utf-8') as fh:
            assert 'Kiosk.t(' in fh.read(), rel
    for rel in ('cart.js', 'checkout.js', 'menu.js', 'totals.js'):
        with open(os.path.join(ROOT, 'static', 'js', 'customer', rel), encoding='utf-8') as fh:
            assert 'ui.' in fh.read(), rel


# ---------------------------------------------------------------------------
# 每個顧客頁以英 / 日文渲染
# ---------------------------------------------------------------------------
@pytest.fixture
def i18n_shop(app, store_settings):
    """開啟英 / 日文、線上訂位、團購；一位會員與一筆訂單 (設定一律經 store_settings，測試結束後還原)"""
    store_settings('menu', languages=['en', 'ja'])
    store_settings('reservation', online_enabled=True, open_time='11:00', close_time='13:00', slot_minutes=60,
                   capacity_per_slot=6, max_party_size=4)
    store_settings('group_order', enabled=True, min_lead_minutes=60)   # put 會與現有值合併
    item = make_item(app, 'i18n Burger', price=100)
    with app.app_context():
        from app.models.user import User
        exists = User.query.filter_by(phone='0977800099').first() is not None
    if not exists:
        make_member(app, 'i18n member', '0977800099', pin='2468')
    c = member_client(app, '0977800099', '2468')
    order = checkout(c, [line(item)]).get_json()
    assert order.get('order_uuid') or order.get('uuid'), order
    return dict(client=c, item=item, order=order, uuid=order.get('order_uuid') or order.get('uuid'))


def _store_content(app):
    """店家自己填的文字 (店名、品名 / 分類 / 說明 / 規格、優惠券名稱)：沒有翻譯時維持中文，不算介面文字"""
    from app.models.coupon import Coupon
    from app.models.menu import MenuItem
    from app.models.tenant import Tenant
    from app.tenant_context import tenant_bypass
    words = set()
    with app.app_context(), tenant_bypass():
        for item in MenuItem.query.all():
            words.update(filter(None, (item.name, item.category, item.description)))
            words.update(name for name, _delta in item.sizes)   # 店家自訂的規格名稱 (中杯 / 大杯…)
        for cp in Coupon.query.all():
            words.add(cp.title)
        words.update(t.name for t in Tenant.query.all())
    return tuple(sorted((w for w in words if CJK.search(w)), key=len, reverse=True))   # 長的先去掉


def _group_pages(app, c):
    from app.models.group_order import GroupOrder
    pickup = (datetime.now() + timedelta(hours=4)).replace(minute=0, second=0, microsecond=0)
    r = c.post('/shop/default/group/new', data=dict(title='i18n group', organizer_name='P', organizer_phone='0912345678',
                                                     pickup_at=pickup.strftime('%Y-%m-%dT%H:%M'), deadline='',
                                                     order_type='外帶', auto_submit='1', note=''))
    created = r.get_data(as_text=True)
    assert r.status_code == 200, created[:300]
    manage_url = re.search(r'id="groupManageUrl">([^<]+)<', created).group(1).replace('&amp;', '&')
    manage_path = manage_url.split('://', 1)[-1]
    manage_path = manage_path[manage_path.index('/'):]
    with app.app_context():
        token = GroupOrder.query.filter_by(title='i18n group').first().token
    joined = c.get(f'/shop/default/group/{token}')
    # 管理連結帶 ?k=：記下金鑰後 302 導回乾淨網址，要跟著轉址才會真的渲染 group_manage.html
    manage = c.get(manage_path, follow_redirects=True)
    assert joined.status_code == 200 and manage.status_code == 200, (joined.status_code, manage.status_code)
    assert manage.request.path.endswith('/manage') and 'k=' not in manage.request.query_string.decode(), manage.request.url
    return created, joined.get_data(as_text=True), manage.get_data(as_text=True)


@pytest.mark.parametrize('lang, expect', (('en', 'Online reservation'), ('ja', 'オンライン予約')))
def test_every_customer_page_renders_without_chinese_ui(app, i18n_shop, monkeypatch, lang, expect):
    c = i18n_shop['client']
    assert c.get(f'/?lang={lang}').status_code == 200
    tomorrow = date.today() + timedelta(days=1)
    allow = _store_content(app) + ('中文', '日本語', '街口支付')   # 店家填寫的文字 / 語言原名 / 支付品牌不翻譯
    pages = {}
    for path in ('/', '/register', '/rewards', '/privacy', '/offline', f'/reserve?day={tomorrow}', '/waitlist',
                 f'/track/{i18n_shop["uuid"]}', '/shop/default/group/new', '/no-such-page-i18n'):
        r = c.get(path)
        assert r.status_code in (200, 404), (path, r.status_code)
        pages[path] = r.get_data(as_text=True)
    assert expect in pages[f'/reserve?day={tomorrow}']
    assert '<built-in method' not in ''.join(pages.values())
    r = c.post('/reserve', data={'slot': f'{tomorrow}T12:00:00', 'name': 'Res', 'phone': '0977000001', 'party_size': '2'})
    pages['/reserve/<token>'] = c.get(r.headers['Location']).get_data(as_text=True)
    r = c.post('/waitlist', data={'name': 'Wait', 'phone': '', 'party_size': '2'})
    pages['/waitlist/<token>'] = c.get(r.headers['Location']).get_data(as_text=True)
    pages['group_created'], pages['group_join'], pages['group_manage'] = _group_pages(app, c)
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')   # 付款頁只有線上端 (測試設定允許綠界公開測試金鑰)
    r = c.get(f'/payment/return/{i18n_shop["uuid"]}')
    assert r.status_code == 200, r.status_code
    pages['/payment/return'] = r.get_data(as_text=True)
    # /pay 只對待付款的訂單渲染付款頁 (其他狀態 302 轉到結果頁)：暫時把這張會員訂單改成待付款
    from app.extensions import db
    from app.models.order import Order
    with app.app_context():
        order = Order.query.filter_by(uuid=i18n_shop['uuid']).one()
        old_payment_status, order.payment_status = order.payment_status, 'pending'
        db.session.commit()
    try:
        r = c.get(f'/pay/{i18n_shop["uuid"]}')
    finally:
        with app.app_context():
            Order.query.filter_by(uuid=i18n_shop['uuid']).one().payment_status = old_payment_status
            db.session.commit()
    assert r.status_code == 200, r.status_code
    pages['/pay'] = r.get_data(as_text=True)
    assert 'payment-stage.ecpay.com.tw' in pages['/pay'], '應渲染送往綠界的付款頁'
    problems = {path: _chinese_ui_lines(html, lang, allow) for path, html in pages.items()}
    problems = {k: v for k, v in problems.items() if v}
    assert problems == {}, f'{lang} 頁面仍出現中文介面文字：{problems}'
    # 翻譯後的錯誤頁 (404) 與訂單狀態
    assert ('Not found' if lang == 'en' else 'データが見つかりません') in pages['/no-such-page-i18n']
    assert ('Order status' if lang == 'en' else '調理状況') in pages[f'/track/{i18n_shop["uuid"]}']


def test_english_and_japanese_kiosk_pages_have_no_chinese_ui(app, i18n_shop):
    c = i18n_shop['client']
    for lang, expect in (('en', 'Your usual favourites'), ('ja', 'いつものメニュー')):
        html = c.get(f'/?lang={lang}').get_data(as_text=True)
        assert expect in html and '<built-in method' not in html
        body = '\n'.join(_visible_text(html))
        for zh in ('專屬於您的常點餐點', '購物車目前空空如也', '清除', '加入購物車', '我的訂單', '登出', '應付金額', '歡迎回來'):
            assert zh not in body, (lang, zh)
    # 未登入頁也有語言切換與翻譯；切回中文
    anon = app.test_client()
    html = anon.get('/?shop=default&lang=en').get_data(as_text=True)
    assert 'langSwitchLanding' in html and 'Welcome to self-ordering' in html and 'Order as guest' in html
    assert '歡迎使用自助點餐系統' in anon.get('/?lang=zh').get_data(as_text=True)


# ---------------------------------------------------------------------------
# 後端訊息維持中文：顧客頁另附翻譯
# ---------------------------------------------------------------------------
def test_translate_message_exact_pattern_icon_and_passthrough(app):
    with app.test_request_context('/'):
        tr = i18n_service.translate_message
        assert tr('請填寫姓名、手機號碼與 PIN 碼！', 'en') == 'Please fill in your name, phone number and PIN.'
        assert tr('請填寫姓名、手機號碼與 PIN 碼！', 'ja') == 'お名前・電話番号・PIN を入力してください。'
        assert tr('請填寫姓名、手機號碼與 PIN 碼！', 'zh') == '請填寫姓名、手機號碼與 PIN 碼！'
        # 開頭圖示保留；只比對本文
        assert tr('❌ 手機號碼格式不正確！請輸入 09 開頭的 10 碼數字。', 'en') == '❌ Invalid phone number: enter 10 digits starting with 09.'
        assert tr('✅ 手機號碼格式不正確', 'ja').startswith('✅ ')
        # 樣式比對：帶品名 / 數量
        assert tr('餐點【牛肉堡】已售完，請移除後再結帳！', 'en') == '"牛肉堡" is sold out; please remove it before checking out.'
        assert tr('餐點或配餐【紅茶】庫存剩 2 份，庫存不足！', 'ja') == '「紅茶」は残り 2 点で在庫が足りません。'
        # 菜單供應提示與優惠券折扣 (自訂函式)
        assert tr('供應日：週一、三、五', 'en') == 'Available on Mon, Wed, Fri'
        assert tr('供應日：週六、日', 'ja') == '提供日：土・日'
        assert tr('供應時段 11:00~14:00', 'en') == 'Available 11:00–14:00' and tr('已停售', 'ja') == '販売終了'
        assert tr('現折 $50', 'en') == '$50 off' and tr('85 折', 'en') == '15% off' and tr('9 折', 'ja') == '10% オフ'
        assert tr('0.5 折', 'en') == '95% off'
        # 沒有對照：原樣 (不會丟出例外)
        assert tr('這句沒有翻譯', 'en') == '這句沒有翻譯'
        assert tr('', 'en') == '' and tr(None, 'ja') == ''


def test_json_error_carries_message_i18n_only_on_non_chinese_customer_pages(app, i18n_shop):
    from app.extensions import db
    from app.models.menu import MenuItem
    c = i18n_shop['client']
    with app.app_context():
        db.session.get(MenuItem, i18n_shop['item']).is_sold_out = True
        db.session.commit()
    c.get('/?lang=en')
    r = checkout(c, [line(i18n_shop['item'])])
    body = r.get_json()
    assert r.status_code in (400, 409) and '已售完' in body['message'], body   # 後端訊息維持中文
    assert body['message_i18n'] == '"i18n Burger" is sold out; please remove it before checking out.'
    c.get('/?lang=ja')
    assert '「i18n Burger」は売り切れ' in checkout(c, [line(i18n_shop['item'])]).get_json()['message_i18n']
    c.get('/?lang=zh')
    assert 'message_i18n' not in checkout(c, [line(i18n_shop['item'])]).get_json()
    # HTTP 錯誤 (404) 的 JSON 也一樣
    c.get('/?lang=en')
    body = c.get('/api/track/' + 'f' * 36, headers={'Accept': 'application/json'}).get_json()
    assert body['message'] == '查無此資料。' and body['message_i18n'] == 'Not found.'


def test_flash_messages_translated_on_customer_pages(app, i18n_shop):
    c = i18n_shop['client']
    c.get('/?lang=en')
    anon = app.test_client()
    anon.get('/?shop=default&lang=ja')
    for client, expect, absent in ((c, 'Please fill in your name, phone number and PIN.', '請填寫姓名'),
                                   (anon, 'お名前・電話番号・PIN を入力してください。', '請填寫姓名')):
        r = client.post('/register', data={'agree_privacy': '1', 'name': '', 'phone': '', 'pin': ''})
        assert r.status_code == 302
        html = client.get(r.headers['Location']).get_data(as_text=True)
        assert expect in html and absent not in html
    # 中文照舊
    c.get('/?lang=zh')
    r = c.post('/register', data={'agree_privacy': '1', 'name': '', 'phone': '', 'pin': ''})
    assert '請填寫姓名、手機號碼與 PIN 碼！' in c.get(r.headers['Location']).get_data(as_text=True)


def test_staff_pages_stay_chinese_even_when_customer_language_is_english(app, i18n_shop):
    staff = login_staff(app, 'i18n_manager', 'manager')
    staff.get('/?lang=en')   # 同一個瀏覽器先把顧客頁切成英文
    html = staff.get('/admin?lang=en').get_data(as_text=True)
    assert '登出' in html and 'id="ui-data"' not in html, '員工頁一律中文、不送 UI 字典'
    with app.test_request_context('/admin'):
        from flask import g
        from tests.support import default_tenant_id
        g.tenant_id = default_tenant_id(app)
        assert i18n_service.effective_language() == 'zh'
    # 員工 API 的錯誤訊息不翻譯 (沒有 message_i18n)
    pos = login_staff(app, 'i18n_pos', 'staff')
    pos.get('/?lang=en')
    body = pos.post('/api/pos/checkout', json={'items': [], 'payment_method': 'Cash', 'order_type': '內用'}).get_json()
    assert body and 'message_i18n' not in body and CJK.search(body['message'])


def test_first_visit_language_follows_browser_accept_language(app, store_settings):
    store_settings('menu', languages=['ja'])   # 只開日文
    for header, expect in (('ja,en;q=0.8', 'ja'), ('en-US,en;q=0.9', 'zh'), ('zh-TW,zh;q=0.9,ja;q=0.8', 'zh'),
                           ('fr', 'zh'), ('', 'zh')):
        c = app.test_client()
        html = c.get('/?shop=default', headers={'Accept-Language': header}).get_data(as_text=True)
        got = 'ja' if 'セルフオーダーへようこそ' in html else 'zh'
        assert got == expect, (header, got)
        with c.session_transaction() as sess:
            assert sess.get(i18n_service.LANG_KEY) == ('ja' if expect == 'ja' else None), header
    # ?lang= 明確指定後，之後的請求不再看瀏覽器語言
    c = app.test_client()
    c.get('/?shop=default&lang=ja', headers={'Accept-Language': 'en'})
    assert 'セルフオーダーへようこそ' in c.get('/', headers={'Accept-Language': 'en'}).get_data(as_text=True)
