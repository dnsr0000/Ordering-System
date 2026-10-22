"""測試輔助函式：登入、建立測試資料、讀取 flash 訊息"""
import io
import re
import threading
import uuid

from PIL import Image

CSRF_META_RE = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')


def extract_csrf(html):
    m = CSRF_META_RE.search(html or '') or re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html or '')
    return m.group(1) if m else ''


def jpeg_bytes(width=120, height=120, color=(210, 160, 130)):
    buf = io.BytesIO()
    Image.new('RGB', (width, height), color=color).save(buf, format='JPEG', quality=85)
    buf.seek(0)
    return buf


def default_tenant_id(app):
    from app.models.tenant import Tenant
    with app.app_context():
        return Tenant.query.filter_by(slug='default').first().id


def login_staff(app, username, role='staff', client=None, tenant_id=None):
    """以員工身分登入 (帳號不存在時建立於 tenant_id 指定的店家，預設為預設店家)；直接寫入 Flask-Login 的 session，與密碼無關"""
    from app.extensions import db
    from app.models.tenant import StaffAccount
    with app.app_context():
        staff = StaffAccount.query.filter_by(username=username).first()
        if staff is None:
            staff = StaffAccount(tenant_id=tenant_id or default_tenant_id(app), username=username, role=role, is_active=True)
            staff.set_password('StaffPass!2026')
            db.session.add(staff)
            db.session.commit()
        session_id, tenant_id = staff.get_id(), staff.tenant_id
    client = client or app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = session_id
        sess['_fresh'] = True
        sess['tenant_id'] = tenant_id
    return client


def other_shop_owner_client(app, slug='other-shop'):
    """另一家店 (不存在時建立) 的負責人登入後的 client，用來驗證跨店隔離"""
    from app.extensions import db
    from app.models.tenant import Tenant
    with app.app_context():
        tenant = Tenant.query.filter_by(slug=slug).first()
        if tenant is None:
            tenant = Tenant(name=f'其他店家 {slug}', slug=slug)
            db.session.add(tenant)
            db.session.commit()
        tenant_id = tenant.id
    return login_staff(app, f'owner_{slug}', 'owner', tenant_id=tenant_id)


def guest_client(app):
    c = app.test_client()
    c.get('/?shop=default')
    c.post('/guest_login')
    return c


def member_client(app, phone, pin, ip='10.99.0.1'):
    c = app.test_client()
    c.get('/?shop=default')
    r = c.post('/phone_login', json={'phone': phone, 'pin': pin}, headers={'X-Forwarded-For': ip})
    assert r.status_code == 200, f'會員登入失敗: {r.status_code} {r.get_data(as_text=True)[:200]}'
    return c


def flashes(client):
    """取出 (並清除) 目前 session 中的 flash 訊息文字"""
    with client.session_transaction() as sess:
        return [msg for _cat, msg in sess.pop('_flashes', [])]


def assert_flash(client, contains):
    msgs = flashes(client)
    assert any(contains in m for m in msgs), f'找不到包含「{contains}」的提示訊息: {msgs}'
    return msgs


def make_item(app, name, price=100, stock=100, **kw):
    from app.extensions import db
    from app.models.menu import MenuItem
    with app.app_context():
        item = MenuItem(tenant_id=default_tenant_id(app), name=name, category=kw.pop('category', '主餐'), price=price,
                        stock=stock, total_stock=kw.pop('total_stock', stock), modifiers=kw.pop('modifiers', 'none'), **kw)
        db.session.add(item)
        db.session.commit()
        return item.id


def make_member(app, name, phone, points=0, pin=None, **kw):
    from app.extensions import db
    from app.models.user import User
    with app.app_context():
        user = User(tenant_id=kw.pop('tenant_id', None) or default_tenant_id(app), name=name, phone=phone,
                    photo_path=kw.pop('photo_path', f'{phone}.jpg'), points=points, **kw)
        if pin:
            user.set_pin(pin)
        db.session.add(user)
        db.session.commit()
        return user.id


def line(item_id, quantity=1, **extra):
    """結帳品項 (結構化格式)"""
    return dict(menu_item_id=item_id, quantity=quantity, **extra)


def checkout(client, items, url='/submit_order', ip=None, **fields):
    payload = {'items': items, 'payment_method': 'Cash', 'order_type': '內用', **fields}
    return client.post(url, json=payload, headers={'X-Forwarded-For': ip} if ip else None)


def pos_checkout(client, item_id, user_id=None, promo=''):
    """POS 結帳一份餐點"""
    return checkout(client, [line(item_id)], url='/api/pos/checkout', user_id=user_id, promo_code=promo)


def cancel_order(client, order_id):
    return client.post(f'/api/orders/{order_id}/status', json={'status': 'Cancelled'})


# 有照片、77 點的會員 (conftest 的 photo_member fixture 建立)
PHOTO_MEMBER_PHONE, PHOTO_MEMBER_PIN = '0933130013', '2468'


def reward_setting_values(app, tid):
    from app.models.user import RewardSetting
    with app.app_context():
        rs = RewardSetting.query.filter_by(tenant_id=tid).first()
        return rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point


def new_client_order_id():
    return str(uuid.uuid4())


def run_together(*callables):
    """以 Barrier 讓多個執行緒盡量同時開始，回傳各自結果 (依輸入順序)"""
    results = [None] * len(callables)
    barrier = threading.Barrier(len(callables))

    def runner(i, fn):
        barrier.wait(timeout=10)
        results[i] = fn()

    threads = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(callables)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


# ---------------------------------------------------------------------------
# 端對端測試用的示範資料
# ---------------------------------------------------------------------------
E2E_STAFF_PASSWORD = 'StaffPass!2026'


def seed_demo(app):
    """建立一組可操作的菜單、套餐、優惠券、會員與員工 (重複呼叫不會重複建立)"""
    from app.extensions import db
    from app.models.coupon import Coupon
    from app.models.menu import ComboOption, ComboOptionItem, MenuItem
    from app.models.tenant import StaffAccount
    from app.models.user import User
    tid = default_tenant_id(app)
    with app.app_context():
        if MenuItem.query.filter_by(tenant_id=tid, name='示範牛肉堡').first():
            return {m.name: m.id for m in MenuItem.query.filter_by(tenant_id=tid)}
        items = {
            '示範牛肉堡': MenuItem(name='示範牛肉堡', category='主餐', price=150, discount_price=120, is_discount=True,
                              modifiers='addons', stock=50, total_stock=50, description='多汁牛肉 <b>不是粗體</b>'),
            '示範紅茶': MenuItem(name='示範紅茶', category='飲料', price=40, modifiers='ice_sugar', stock=50, total_stock=50,
                             can_be_add_on=True, add_on_price=25, addon_trigger_type='any'),
            '示範薯條': MenuItem(name='示範薯條', category='副餐', price=60, modifiers='spicy', stock=50, total_stock=50),
            '示範雞塊': MenuItem(name='示範雞塊', category='點心', price=70, stock=50, total_stock=50, is_reward=True, reward_points=30),
            "O'Brien 的「特製」<script>alert(1)</script>": MenuItem(
                name="O'Brien 的「特製」<script>alert(1)</script>", category='主餐', price=99, stock=20, total_stock=20),
        }
        for item in items.values():
            item.tenant_id = tid
            db.session.add(item)
        db.session.flush()
        combo = ComboOption(tenant_id=tid, main_item_id=items['示範牛肉堡'].id, name="A套餐(薯條+紅茶) <i>'含飲料'</i>",
                            additional_price=60, can_addon=True)
        combo.sides = [ComboOptionItem(tenant_id=tid, menu_item_id=items['示範薯條'].id, position=0, customizable=True),
                       ComboOptionItem(tenant_id=tid, menu_item_id=items['示範紅茶'].id, position=1, customizable=True)]
        db.session.add(combo)
        db.session.add_all([
            Coupon(tenant_id=tid, code='E2E20', title='示範公開券', discount_type='fixed', discount_value=20, min_spend=0,
                   reward_points=0, is_reward=False),
            Coupon(tenant_id=tid, code='E2EVIP', title="示範紅利券 '85折'", discount_type='percent', discount_value=85,
                   min_spend=0, reward_points=10, is_reward=True),
        ])
        member = User(tenant_id=tid, name="示範會員 </script><b>王'小明'", phone='0911222333', photo_path='e2e.jpg', points=500)
        member.set_pin('2468')
        db.session.add(member)
        for name, role in (('e2e_manager', 'manager'), ('e2e_pos', 'staff'), ('e2e_kitchen', 'kitchen')):
            if not StaffAccount.query.filter_by(username=name).first():
                acc = StaffAccount(tenant_id=tid, username=name, role=role)
                acc.set_password(E2E_STAFF_PASSWORD)
                db.session.add(acc)
        db.session.commit()
        return {m.name: m.id for m in MenuItem.query.filter_by(tenant_id=tid)}


def browser_staff_login(page, username, password=E2E_STAFF_PASSWORD):
    page.goto('/admin')
    page.fill('input[name="username"]', username)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_load_state('networkidle')


def browser_member_login(page, phone='0911222333', pin='2468'):
    """以手機 + PIN 登入會員 (直接呼叫 API，取得與瀏覽器共用的 cookie)"""
    page.goto('/?shop=default')
    token = page.locator('meta[name="csrf-token"]').get_attribute('content')
    resp = page.request.post('/phone_login', data={'phone': phone, 'pin': pin}, headers={'X-CSRFToken': token})
    assert resp.ok, resp.text()


def head_revision():
    """migrations/ 目前最新的遷移版本"""
    import os

    from alembic.config import Config as AlembicConfig
    from alembic.script import ScriptDirectory
    cfg = AlembicConfig()
    cfg.set_main_option('script_location', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'migrations'))
    return ScriptDirectory.from_config(cfg).get_current_head()
