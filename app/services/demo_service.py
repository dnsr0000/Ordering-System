"""
一鍵展示資料 (第四輪)：flask demo seed

建立一家獨立的「示範店家」(預設網址代號 demo)，產生過去 N 天的營運資料，讓每一張報表都有內容可以展示：
菜單 / 套餐 / 規格、原料配方與進貨 (成本變動歷史)、供應商、會員與等級、優惠券、
每天依星期與時段分布的點餐機 / POS 訂單 (多種付款方式、內用外帶、少量作廢與部分退款、會員點數)、
餐後評分、打卡與班表、每月營業費用、一個收單中的團體預購。

安全性：
- 只會寫入示範店家；其他店家的資料完全不動 (重建時以 tenant_id 逐表刪除示範店家的資料)。
- 示範店家以 store_setting 的 demo_seed 標記辨識；同代號的店家若不是由本指令建立，一律拒絕 (不會覆蓋真實店家)。
- 只在單機 (SYNC_ROLE=standalone) 建立：分店的資料會同步到線上端；線上端的訪客訂單需要分店取餐與線上付款。
- 建到一半失敗時整個示範店家刪除 (不留下不完整的資料)。
- 資料庫直接刪除不產生同步刪除紀錄；示範店家沒有分店，不會同步到任何地方。
"""
import json
import random
import secrets
from datetime import date, datetime, time, timedelta

from sqlalchemy import delete, select, update

from app.errors import AppError
from app.extensions import db
from app.models.enums import OrderStatus
from app.models.expense import Expense
from app.models.group_order import GroupOrder
from app.models.inventory import Ingredient, RecipeLine, Supplier
from app.models.member import MemberTier
from app.models.menu import ComboOption, ComboOptionItem, MenuItem
from app.models.order import Order, OrderFeedback
from app.models.payment import PaymentRecord
from app.models.settings import StoreSetting
from app.models.staff import TimeClock, WorkSchedule
from app.models.tenant import StaffAccount, StaffSession, Tenant
from app.models.coupon import Coupon
from app.models.user import User
from app.sync import runtime as sync_runtime

MARKER_KEY = 'demo_seed'
DEFAULT_SLUG = 'demo'
MEMBER_PIN = '2468'   # 示範會員 (手機 0900000001 ~ 0900000040) 的 PIN

MENU = (
    # 名稱, 分類, 售價, 外帶價, 單位成本 (無配方時), 規格, 配方 [(原料, 用量)]
    ('招牌牛肉堡', '主餐', 150, 0, 0, '', [('牛肉餅', 1), ('漢堡麵包', 1), ('生菜', 20)]),
    ('香烤雞腿堡', '主餐', 135, 0, 0, '', [('雞腿排', 1), ('漢堡麵包', 1), ('生菜', 20)]),
    ('雙層起司堡', '主餐', 180, 0, 0, '', [('牛肉餅', 2), ('漢堡麵包', 1), ('起司片', 2)]),
    ('蔬食蘑菇堡', '主餐', 140, 0, 0, '', [('蘑菇', 80), ('漢堡麵包', 1), ('生菜', 30)]),
    ('脆薯', '副餐', 55, 0, 0, '', [('馬鈴薯', 150)]),
    ('洋蔥圈', '副餐', 65, 0, 18, '', []),
    ('雞塊 6 塊', '點心', 70, 0, 22, '', []),
    ('凱薩沙拉', '副餐', 90, 0, 0, '', [('生菜', 120), ('起司片', 1)]),
    ('紅茶', '飲料', 35, 35, 0, '中杯:0,大杯:+10', [('紅茶葉', 8)]),
    ('拿鐵', '飲料', 70, 70, 0, '中杯:0,大杯:+15', [('牛奶', 200), ('咖啡豆', 18)]),
    ('檸檬氣泡飲', '飲料', 60, 60, 12, '', []),
    ('布朗尼', '點心', 75, 0, 25, '', []),
)
INGREDIENTS = (
    # 名稱, 單位, 單位成本, 庫存, 低庫存
    ('牛肉餅', '片', 32, 400, 60), ('雞腿排', '片', 28, 300, 50), ('漢堡麵包', '個', 8, 600, 100),
    ('生菜', 'g', 0.12, 20000, 3000), ('起司片', '片', 6, 500, 80), ('蘑菇', 'g', 0.25, 8000, 1500),
    ('馬鈴薯', 'g', 0.06, 50000, 8000), ('紅茶葉', 'g', 0.8, 5000, 800), ('牛奶', 'ml', 0.07, 30000, 5000),
    ('咖啡豆', 'g', 1.1, 6000, 1000),
)
FIRST = '陳林黃張李王吳劉蔡楊許鄭謝郭洪曾邱廖賴周'
GIVEN = ('小明', '美玲', '志豪', '雅婷', '家豪', '怡君', '建宏', '淑芬', '俊傑', '佳穎', '冠宇', '宜蓁', '承恩', '詩涵')
COMMENTS = ('很好吃，出餐很快', '薯條很酥', '飲料太甜了', '等有點久', '服務親切', '牛肉堡很多汁', '下次還會再來')
HOUR_WEIGHTS = {10: 2, 11: 8, 12: 12, 13: 7, 14: 3, 15: 3, 16: 3, 17: 6, 18: 9, 19: 7, 20: 3}


class DemoError(AppError):
    pass


def _marker(tenant_id):
    return StoreSetting.query.filter_by(tenant_id=tenant_id, key=MARKER_KEY) \
        .execution_options(skip_tenant_filter=True).first()


def find_demo(slug=DEFAULT_SLUG):
    tenant = Tenant.query.filter_by(slug=slug).first()
    if tenant is None:
        return None
    if _marker(tenant.id) is None:
        raise DemoError(f'店家代號「{slug}」已被真實店家使用，示範資料不會覆蓋它 (請改用 --slug 指定其他代號)')
    return tenant


def remove(slug=DEFAULT_SLUG):
    """刪除示範店家的所有資料 (只刪 tenant_id 為示範店家的列)；回傳刪除的列數"""
    tenant = find_demo(slug)
    if tenant is None:
        return 0
    tid = tenant.id
    staff_ids = [s for (s,) in db.session.execute(select(StaffAccount.id).where(StaffAccount.tenant_id == tid))]
    removed = 0
    if staff_ids:
        removed += db.session.execute(delete(StaffSession).where(StaffSession.staff_account_id.in_(staff_ids))).rowcount
    db.session.execute(update(GroupOrder).where(GroupOrder.tenant_id == tid).values(order_id=None))
    for table in reversed(db.metadata.sorted_tables):
        if 'tenant_id' in table.c and table.name != 'tenant':
            removed += db.session.execute(table.delete().where(table.c.tenant_id == tid)).rowcount or 0
    db.session.execute(delete(Tenant).where(Tenant.id == tid))
    db.session.commit()
    return removed + 1


def _accounts(slug):
    return ((f'{slug}_owner', 'owner', 0), (f'{slug}_manager', 'manager', 210), (f'{slug}_pos', 'staff', 190),
            (f'{slug}_pos2', 'staff', 185), (f'{slug}_kitchen', 'kitchen', 190))


def seed(slug=DEFAULT_SLUG, days=60, orders_per_day=80, seed_value=42, password=None, reset=False, today=None,
         progress=None):
    """建立示範店家與資料；回傳 dict (店家、帳號、密碼、訂單數)"""
    if sync_runtime.is_node() or sync_runtime.is_hub():
        # 分店：示範資料會同步到線上端；線上端：點餐機訪客訂單需要分店取餐與線上付款，無法產生門市營運資料
        raise DemoError('示範資料請在單機 (SYNC_ROLE=standalone) 建立，分店主機與線上端不可執行')
    if not 1 <= days <= 400 or not 1 <= orders_per_day <= 500:
        raise DemoError('days 需為 1 ~ 400、orders-per-day 需為 1 ~ 500')
    if find_demo(slug) is not None:
        if not reset:
            raise DemoError(f'示範店家「{slug}」已存在 (加上 --reset 重新建立)')
        remove(slug)
    for username, _role, _wage in _accounts(slug):
        if StaffAccount.query.filter_by(username=username).execution_options(skip_tenant_filter=True).first():
            raise DemoError(f'員工帳號 {username} 已被其他店家使用 (請改用 --slug)')
    rng = random.Random(seed_value)
    today = today or date.today()
    password = password or secrets.token_urlsafe(9)
    tenant = Tenant(name='示範店家', slug=slug, is_active=True)
    db.session.add(tenant)
    db.session.flush()
    db.session.add(StoreSetting(tenant_id=tenant.id, key=MARKER_KEY,
                                value=json.dumps({'created_at': datetime.now().isoformat(timespec='seconds'),
                                                  'days': days})))
    db.session.commit()

    from app.tenant_context import _use_tenant
    from flask import current_app
    try:
        with current_app.test_request_context('/'):
            _use_tenant(tenant, remember=False)
            result = _populate(tenant, rng, today, days, orders_per_day, password, progress or (lambda msg: None))
    except Exception:
        db.session.rollback()
        remove(slug)   # 建到一半失敗：不留下不完整的示範店家
        raise
    return dict(result, slug=slug, password=password)


def _populate(tenant, rng, today, days, per_day, password, progress):
    from app.services import inventory_service, settings_service
    from app.services.bootstrap import seed_tenant_defaults
    from app.services.order_service import process_checkout
    tid = tenant.id
    seed_tenant_defaults(tid)
    # ---- 帳號 ----
    staff = {}
    for username, role, wage in _accounts(tenant.slug):
        acc = StaffAccount(tenant_id=tid, username=username, role=role, is_active=True, hourly_wage=wage)
        acc.set_password(password)
        db.session.add(acc)
        staff[role if role not in staff else role + '2'] = acc
    db.session.flush()
    # ---- 設定 ----
    for key, values in (('group_order', {'enabled': True}),):
        settings_service.put(key, dict(settings_service.get(key, tid), **values), tenant_id=tid)
    # ---- 原料、供應商、菜單、配方 ----
    supplier = Supplier(tenant_id=tid, name='示範食材行', contact='王先生', phone='02-0000-0000', lead_days=1)
    db.session.add(supplier)
    db.session.flush()
    ings = {}
    for name, unit, cost, stock, low in INGREDIENTS:
        ings[name] = Ingredient(tenant_id=tid, name=name, unit=unit, unit_cost=cost, stock=stock, low_threshold=low,
                                supplier_id=supplier.id, par_level=low * 4)
        db.session.add(ings[name])
    items = {}
    for name, cat, price, takeout, unit_cost, sizes, recipe in MENU:
        items[name] = MenuItem(tenant_id=tid, name=name, category=cat, price=price, takeout_price=takeout,
                               unit_cost=unit_cost, size_options=sizes, stock=100000, total_stock=100000,
                               description=f'示範餐點：{name}', modifiers='none')
        db.session.add(items[name])
    db.session.flush()
    for name, _c, _p, _t, _u, _s, recipe in MENU:
        for ing, qty in recipe:
            db.session.add(RecipeLine(tenant_id=tid, menu_item_id=items[name].id, ingredient_id=ings[ing].id,
                                      quantity=qty))
    combo = ComboOption(tenant_id=tid, main_item_id=items['招牌牛肉堡'].id, name='牛肉堡套餐 (脆薯 + 紅茶)',
                        additional_price=70)
    combo.sides = [ComboOptionItem(tenant_id=tid, menu_item_id=items['脆薯'].id, position=0),
                   ComboOptionItem(tenant_id=tid, menu_item_id=items['紅茶'].id, position=1)]
    db.session.add(combo)
    db.session.add_all([
        Coupon(tenant_id=tid, code='DEMO50', title='示範折 50 元', discount_type='fixed', discount_value=50, min_spend=300,
               reward_points=0, is_reward=False),
        MemberTier(tenant_id=tid, name='銀卡', min_spend=1500, points_multiplier=1.2, discount_percent=0, color='secondary'),
        MemberTier(tenant_id=tid, name='金卡', min_spend=5000, points_multiplier=1.5, discount_percent=5, color='warning'),
    ])
    # ---- 會員 ----
    members = []
    pin = User()
    pin.set_pin(MEMBER_PIN)   # 雜湊很慢：所有示範會員共用同一個 PIN 的雜湊
    for i in range(40):
        u = User(tenant_id=tid, name=rng.choice(FIRST) + rng.choice(GIVEN), phone=f'0900{i + 1:06d}', photo_path='',
                 points=rng.randint(0, 300), pin_hash=pin.pin_hash)
        db.session.add(u)
        members.append(u)
    db.session.commit()
    progress('菜單、原料、會員已建立')

    # ---- 每天的訂單 ----
    names = list(items)
    weights = [9, 7, 5, 3, 8, 4, 5, 2, 9, 6, 4, 3]
    hours = list(HOUR_WEIGHTS)
    hour_w = list(HOUR_WEIGHTS.values())
    # 會員有「常客」與「久未消費」之分 (RFM 分群才有內容)
    regulars, lapsed = members[:12], members[12:22]
    owner, cashiers = staff['owner'], [staff['staff'], staff['staff2']]
    created = 0
    for back in range(days, -1, -1):
        day = today - timedelta(days=back)
        factor = 1.35 if day.weekday() >= 5 else 1.0
        n = int(per_day * factor * rng.uniform(0.85, 1.15))
        if back == 0:   # 今天：只產生到目前時刻
            n = max(1, int(n * min(1.0, max(0.0, (datetime.now().hour - 9) / 11))))
        for _ in range(n):
            hour = rng.choices(hours, hour_w)[0]
            at = datetime.combine(day, time(hour, rng.randint(0, 59), rng.randint(0, 59)))
            if back == 0 and at > datetime.now():
                at = datetime.now() - timedelta(minutes=rng.randint(1, 30))
            lines = []
            for name in set(rng.choices(names, weights, k=rng.randint(1, 3))):
                line = {'menu_item_id': items[name].id, 'quantity': rng.choice((1, 1, 1, 2))}
                if items[name].size_options:
                    line['size'] = rng.choice(('中杯', '大杯'))
                lines.append(line)
            if rng.random() < 0.08:
                lines.append({'menu_item_id': items['招牌牛肉堡'].id, 'quantity': 1, 'combo_option_id': combo.id})
            pos = rng.random() < 0.4
            user = None
            r = rng.random()
            if r < 0.25:
                user = rng.choice(regulars)
            elif r < 0.30 and back > 45:
                user = rng.choice(lapsed)
            elif r < 0.35:
                user = rng.choice(members[22:])
            cashier = rng.choices(cashiers, (7, 3))[0] if pos else None
            order, _ = process_checkout(
                tenant_id=tid, items=lines, order_type=rng.choice(('內用', '內用', '外帶')),
                user_id=user.id if user else None, order_source='POS' if pos else 'Kiosk',
                cashier_username=cashier.username if cashier else '', now=at,
                promo_code='DEMO50' if rng.random() < 0.03 else '')
            _finish(order, at, rng, back)
            created += 1
        db.session.commit()
        if back % 10 == 0:
            progress(f'{day}：已建立 {created} 張訂單')

    _backdate_payments(tid, rng)
    _cancel_some(tid, rng, owner, staff['staff2'], today)
    _feedback(tid, rng, today, days)
    _timeclock(tid, rng, today, days, [staff['manager'], staff['staff'], staff['staff2'], staff['kitchen']])
    _expenses(tid, today, days)
    _purchases(tid, ings, inventory_service, today)
    _group_order(tid, items, today)
    db.session.commit()
    progress('完成')
    return {'tenant_id': tid, 'orders': created, 'owner': owner.username, 'members': len(members),
            'accounts': [s.username for s in staff.values()]}


def _finish(order, at, rng, back):
    """過去的訂單：出餐 / 取餐完成 (出餐時間 3 ~ 15 分鐘)；今天最近的維持製作中"""
    if back == 0 and at > datetime.now() - timedelta(minutes=20):
        return
    order.status = OrderStatus.PICKED_UP
    order.completed_at = at + timedelta(minutes=rng.randint(3, 15))


def _backdate_payments(tid, rng):
    """收款紀錄的時間改為訂單時間，並分配付款方式 (示範各種付款方式的報表)"""
    methods = (('Cash', 50), ('Credit Card', 28), ('Line Pay', 14), ('Apple Pay', 5), ('街口支付', 3))
    orders = {o.id: o for o in Order.query.filter_by(tenant_id=tid).all()}
    for rec in PaymentRecord.query.filter_by(tenant_id=tid).all():
        order = orders.get(rec.order_id)
        if order is None:
            continue
        rec.created_at = order.created_at
        if rec.kind == 'sale' and order.payment_method == 'Cash':
            method = rng.choices([m for m, _w in methods], [w for _m, w in methods])[0]
            rec.method = method
            if method != 'Cash':
                order.payment_method = method
                rec.received, rec.change = rec.amount, 0
    db.session.commit()


def _cancel_some(tid, rng, owner, suspect, today):
    """
    約 1.5% 的訂單 (POS 單) 作廢 (產生退款紀錄，員工異常偵測與會計匯出才有內容)；
    九成是同一位收銀員 (suspect) 的單，讓「員工異常偵測」有一位被標示的員工。
    """
    from app.models.payment import Refund
    from app.services.order_service import change_order_status
    past = Order.query.filter(Order.tenant_id == tid, Order.created_at < datetime.combine(today, time()),
                              Order.cashier_username != '').order_by(Order.id).all()
    if not past:
        return
    k = max(1, (len(past) * 4) // 100)   # 約全部訂單的 1.5%
    mine = [o for o in past if o.cashier_username == suspect.username]
    picked = rng.sample(mine, k=min(len(mine), (k * 9) // 10))
    rest = [o for o in past if o not in picked]
    picked += rng.sample(rest, k=min(len(rest), max(0, k - len(picked))))
    for order in picked:
        at = order.created_at
        order.status = OrderStatus.PENDING
        db.session.commit()
        change_order_status(order.id, OrderStatus.CANCELLED, owner, tid, reason='示範作廢')
        db.session.commit()
        who = order.cashier_username or owner.username
        PaymentRecord.query.filter(PaymentRecord.order_id == order.id, PaymentRecord.kind != 'sale') \
            .update({'created_at': at + timedelta(minutes=5), 'staff_username': who}, synchronize_session=False)
        Refund.query.filter(Refund.order_id == order.id) \
            .update({'created_at': at + timedelta(minutes=5), 'staff_username': who}, synchronize_session=False)
    db.session.commit()


def _feedback(tid, rng, today, days):
    orders = Order.query.filter(Order.tenant_id == tid, Order.status == OrderStatus.PICKED_UP).all()
    for order in rng.sample(orders, k=min(len(orders), max(5, len(orders) // 25))):
        rating = rng.choices((5, 4, 3, 2, 1), (50, 30, 10, 6, 4))[0]
        db.session.add(OrderFeedback(tenant_id=tid, order_id=order.id, rating=rating,
                                     comment=rng.choice(COMMENTS) if rating <= 3 or rng.random() < 0.4 else '',
                                     created_at=order.created_at + timedelta(minutes=40)))


def _timeclock(tid, rng, today, days, people):
    """每天的打卡紀錄 (店長早班、收銀晚班、兼職午餐尖峰、廚房早班)，與未來 7 天的班表 (刻意少排晚餐尖峰)"""
    shifts = ((10, 15), (15, 20), (11, 14), (10, 15))
    for back in range(days, 0, -1):
        day = today - timedelta(days=back)
        for acc, (sh, eh) in zip(people, shifts):
            db.session.add(TimeClock(tenant_id=tid, staff_account_id=acc.id,
                                     clock_in=datetime.combine(day, time(sh)) + timedelta(minutes=rng.randint(-8, 8)),
                                     clock_out=datetime.combine(day, time(eh)) + timedelta(minutes=rng.randint(-5, 15))))
    for ahead in range(0, 8):
        day = today + timedelta(days=ahead)
        for acc, (sh, eh) in zip(people[:3], shifts):
            db.session.add(WorkSchedule(tenant_id=tid, staff_account_id=acc.id, work_date=day, start_time=f'{sh:02d}:00',
                                        end_time=f'{eh:02d}:00', note='示範班表'))


def _expenses(tid, today, days):
    month = today.replace(day=1)
    start = today - timedelta(days=days)
    while month >= start.replace(day=1):
        last = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        for day, cat, name, amount, fixed in ((5, 'rent', '店面租金', 68000, True), (10, 'utilities', '電費', 9800, False),
                                              (10, 'utilities', '水費 / 瓦斯', 3200, False),
                                              (15, 'payroll', '勞健保', 18500, True), (20, 'supplies', '包材與清潔用品', 6400, False),
                                              (25, 'marketing', '社群廣告', 5000, False), (28, 'fees', '刷卡機月租', 600, True)):
            spent = month.replace(day=min(day, last.day))
            if spent <= today:
                db.session.add(Expense(tenant_id=tid, spent_on=spent, category=cat, name=name, amount=amount,
                                       vendor='示範廠商', is_fixed=fixed, created_by='demo'))
        month = (month - timedelta(days=1)).replace(day=1)


def _purchases(tid, ings, inventory_service, today):
    """進貨 (加權平均改變單位成本 → 成本 / 售價歷史有紀錄)，模擬牛肉與牛奶漲價"""
    from app.models.expense import CostHistory
    for name, qty, cost, back in (('牛肉餅', 200, 38, 20), ('牛奶', 20000, 0.09, 12), ('生菜', 10000, 0.1, 8),
                                  ('牛肉餅', 200, 41, 5)):
        inventory_service.adjust(ings[name], qty, 'purchase', note='示範進貨', unit_cost=cost)
        db.session.commit()
        CostHistory.query.filter(CostHistory.tenant_id == tid, CostHistory.ref_id == ings[name].id) \
            .filter(CostHistory.changed_at > datetime.now() - timedelta(minutes=5)) \
            .update({'changed_at': datetime.combine(today - timedelta(days=back), time(9)), 'changed_by': 'demo'},
                    synchronize_session=False)
        db.session.commit()


def _group_order(tid, items, today):
    from app.services import group_order_service
    pickup = datetime.combine(today + timedelta(days=1), time(12, 0))
    grp, _key = group_order_service.create(tid, {'title': '示範公司午餐團', 'organizer_name': '王主揪',
                                                 'organizer_phone': '0900999888',
                                                 'pickup_at': pickup.strftime('%Y-%m-%dT%H:%M')})
    db.session.flush()
    for who, name, qty in (('小明', '招牌牛肉堡', 1), ('美玲', '蔬食蘑菇堡', 1), ('志豪', '雙層起司堡', 2)):
        group_order_service.save_entry(grp, who, [{'menu_item_id': items[name].id, 'quantity': qty, 'size': '',
                                                   'customization': ''}])
