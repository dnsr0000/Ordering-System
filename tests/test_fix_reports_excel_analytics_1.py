"""修正小組 reports-excel-analytics-1：Excel 匯出 / 智慧匯入的回歸測試 (各自在獨立店家建立資料)"""
import io
from datetime import date, datetime, timedelta

import openpyxl
import pytest
from werkzeug.datastructures import FileStorage

from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon
from app.models.enums import DiscountType
from app.models.order import Order, OrderItem
from app.models.user import User
from app.services import excel_service
from tests.support import count_queries, get_or_create_tenant, tenant_request


def _sheet_rows(buf, title):
    ws = openpyxl.load_workbook(buf)[title]
    rows = list(ws.iter_rows(values_only=True))
    # 日期相關工作表第 1 列是區間說明，標題在第 2 列
    start = 1 if rows and rows[0] and isinstance(rows[0][0], str) and rows[0][0].startswith('報表資料區間') else 0
    header = rows[start]
    return [dict(zip(header, r)) for r in rows[start + 1:]]


def _xlsx(sheets):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for title, rows in sheets.items():
        ws = wb.create_sheet(title)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return FileStorage(buf, filename='import.xlsx')


def test_export_date_range_is_half_open_and_items_from_loaded_orders(app):
    """B-366 / B-376：結束日最後 1 秒內的訂單也要匯出；明細不再把所有訂單 id 塞進同一個 IN"""
    tid = get_or_create_tenant(app, 'fix-rea1-export')
    day = date(2024, 3, 5)
    late = datetime.combine(day, datetime.min.time()) + timedelta(hours=23, minutes=59, seconds=59, microseconds=500000)
    with tenant_request(app, tid):
        o1 = Order(tenant_id=tid, table_number='REA1-末秒', total_price=10, status='Completed', pickup_number=1, created_at=late)
        o2 = Order(tenant_id=tid, table_number='REA1-隔天', total_price=20, status='Completed', pickup_number=2,
                   created_at=late + timedelta(seconds=1))
        db.session.add_all([o1, o2])
        db.session.flush()
        db.session.add_all([OrderItem(order_id=o1.id, item_name='REA1末秒餐', price=10, quantity=1),
                            OrderItem(order_id=o2.id, item_name='REA1隔天餐', price=20, quantity=1)])
        db.session.commit()
        buf = excel_service.generate_excel_export(['Order', 'OrderItem'], '2024-03-05', '2024-03-05')
        orders = _sheet_rows(buf, '訂單總覽(Order)')
        assert [r['ID'] for r in orders] == [o1.id]
        buf.seek(0)
        items = _sheet_rows(buf, '訂單明細(OrderItem)')
        assert [r['品名(Item Name)'] for r in items] == ['REA1末秒餐']


def test_export_invalid_date_is_400(app):
    """B-375：日期格式錯誤回 AppError (400)，不是 ValueError → 500"""
    tid = get_or_create_tenant(app, 'fix-rea1-export')
    with tenant_request(app, tid):
        for bad in ('20261-01-01', '2026/11/01'):
            with pytest.raises(AppError):
                excel_service.generate_excel_export(['Order'], bad, None)
            with pytest.raises(AppError):
                excel_service.generate_excel_export(['Order'], None, bad)


def test_export_menu_only_does_not_load_orders(app):
    """B-377：只勾跟訂單無關的表時不查詢訂單"""
    tid = get_or_create_tenant(app, 'fix-rea1-export')
    with tenant_request(app, tid):
        with count_queries(app) as box:
            excel_service.generate_excel_export(['MenuItem', 'Coupon'])
        assert not any('FROM "order"' in s or 'FROM order ' in s for s in box['sql']), box['sql']


def test_export_strips_illegal_control_characters(app):
    """R2-139：顧客輸入的控制字元不會讓匯出 500"""
    tid = get_or_create_tenant(app, 'fix-rea1-ctrl')
    with tenant_request(app, tid):
        db.session.add(User(tenant_id=tid, name='Amy\x0bLee', phone='0955050111', photo_path='', points=0))
        o = Order(tenant_id=tid, table_number='REA1-控制字元', total_price=10, status='Completed', pickup_number=1,
                  created_at=datetime.now())
        db.session.add(o)
        db.session.flush()
        db.session.add(OrderItem(order_id=o.id, item_name='REA1控制字元餐', price=10, quantity=1, customization='少冰\x1b'))
        db.session.commit()
        buf = excel_service.generate_excel_export(['User', 'OrderItem'])
        users = _sheet_rows(buf, '會員資料(User)')
        assert 'AmyLee' in [r['姓名(Name)'] for r in users]
        buf.seek(0)
        items = _sheet_rows(buf, '訂單明細(OrderItem)')
        assert '少冰' in [r['客製化(Customization)'] for r in items if r['品名(Item Name)'] == 'REA1控制字元餐']


def test_import_users_keeps_points_and_normalizes_phone(app):
    """B-367 / B-371 / B-378：既有會員點數不被舊檔覆寫；電話正規化、格式不合略過；姓名截斷"""
    tid = get_or_create_tenant(app, 'fix-rea1-users')
    with tenant_request(app, tid):
        db.session.add(User(tenant_id=tid, name='舊名字', phone='0955150001', photo_path='', points=500))
        db.session.commit()
        counts = excel_service.import_workbook(_xlsx({'會員資料(User)': [
            ['姓名(Name)', '電話(Phone)', '紅利點數(Points)'],
            ['新名字', '0955150001', 10],               # 既有會員：改名、點數不覆寫
            ['分隔符號', '0955-150-002', 30],           # → 0955150002
            ['國碼', '+886 955 150 003', 40],           # → 0955150003
            ['數字欄位', 955150004, 50],                # Excel 數字欄位掉了開頭 0
            ['不是手機', '02-2345-6789', 60],           # 市話：略過
            ['長' * 150, '0955150005', 0],
        ]}))
        assert counts['User'] == 5
        users = {u.phone: u for u in User.query.filter(User.phone.like('0955150%'))}
        assert (users['0955150001'].name, users['0955150001'].points) == ('新名字', 500)
        assert users['0955150002'].points == 30 and users['0955150003'].points == 40 and users['0955150004'].points == 50
        assert len(users['0955150005'].name) == 100
        assert User.query.filter(User.phone.like('%2345%')).count() == 0


def test_coupon_export_import_round_trip(app):
    """B-370：匯出的每人上限與低折數百分比券匯回後不變"""
    tid = get_or_create_tenant(app, 'fix-rea1-coupon')
    with tenant_request(app, tid):
        db.session.add_all([
            Coupon(tenant_id=tid, code='REA1LIM', title='限兩次', discount_type=DiscountType.FIXED, discount_value=30,
                   min_spend=0, reward_points=100, is_reward=True, per_user_limit=2),
            Coupon(tenant_id=tid, code='REA1PCT', title='0.5 折', discount_type=DiscountType.PERCENT, discount_value=5,
                   min_spend=0, reward_points=0, is_reward=False, per_user_limit=0),
        ])
        db.session.commit()
        buf = excel_service.generate_excel_export(['Coupon'])
        excel_service.import_workbook(FileStorage(buf, filename='Kiosk_Export.xlsx'))
        lim = Coupon.query.filter_by(code='REA1LIM').one()
        pct = Coupon.query.filter_by(code='REA1PCT').one()
        assert (lim.per_user_limit, lim.is_reward, lim.reward_points) == (2, True, 100)
        assert (pct.discount_value, pct.per_user_limit) == (5, 0)
        assert excel_service.parse_per_user_limit('2次') == 2


def test_coupon_partial_import_keeps_other_fields(app):
    """R2-137：既有券只更新檔案有附上的欄位；折抵類型不分大小寫、可寫中文，無法辨識的列略過"""
    tid = get_or_create_tenant(app, 'fix-rea1-coupon2')
    with tenant_request(app, tid):
        db.session.add(Coupon(tenant_id=tid, code='VIP85', title='會員 85 折', discount_type=DiscountType.PERCENT,
                              discount_value=85, min_spend=500, reward_points=300, reward_discount_points=0,
                              is_reward=True, per_user_limit=1))
        db.session.commit()
        counts = excel_service.import_workbook(_xlsx({'優惠券(Coupon)': [
            ['代碼(Code)', '標題(Title)', '折抵類型(Discount Type)', '折抵值(Discount Value)'],
            ['VIP85', 'VIP 新標題', None, None],
            ['REA1P90', '九折', 'Percent', 90],
            ['REA1ZH', '中文九折', '百分比', 9],
            ['REA1BAD', '看不懂', '折價', 50],
        ]}))
        assert counts['Coupon'] == 3
        vip = Coupon.query.filter_by(code='VIP85').one()
        assert (vip.title, vip.discount_type, vip.discount_value, vip.min_spend, vip.reward_points,
                vip.is_reward, vip.per_user_limit) == ('VIP 新標題', 'percent', 85, 500, 300, True, 1)
        p90 = Coupon.query.filter_by(code='REA1P90').one()
        assert (p90.discount_type, p90.discount_value) == ('percent', 90)
        assert Coupon.query.filter_by(code='REA1ZH').one().discount_value == 90
        assert Coupon.query.filter_by(code='REA1BAD').first() is None


def test_coupon_import_clamps_numbers_and_zero_point_reward(app):
    """R2-192 / R2-109 / B-098：超大數值夾在上限 (不溢位 500)；「是否上架」填是但 0 點的券不上架"""
    tid = get_or_create_tenant(app, 'fix-rea1-coupon3')
    with tenant_request(app, tid):
        counts = excel_service.import_workbook(_xlsx({'優惠券(Coupon)': [
            ['代碼(Code)', '標題(Title)', '折抵值(Discount Value)', '門檻(Min Spend)', '兌換所需點數(Reward Points)',
             '限時特惠點數(Reward Discount Points)', '每人兌換上限(Per User Limit)', '上架回饋商城(Is Reward)'],
            ['REA1BIG', '超大數值', 10, '1e20', '1e20', '1e20', '1e20', '是'],
            ['REA1ZERO', '零點上架', 10, 0, 0, 0, None, '是'],
        ]}))
        assert counts['Coupon'] == 2
        big = Coupon.query.filter_by(code='REA1BIG').one()
        assert (big.min_spend, big.reward_points, big.reward_discount_points, big.per_user_limit, big.is_reward) == (
            excel_service.MAX_AMOUNT, excel_service.MAX_POINTS, excel_service.MAX_POINTS,
            excel_service.MAX_PER_USER_LIMIT, True)
        assert Coupon.query.filter_by(code='REA1ZERO').one().is_reward is False
        assert excel_service.parse_per_user_limit('1e20') == excel_service.MAX_PER_USER_LIMIT


def test_import_user_points_clamped(app):
    """R2-109：匯入新會員的點數夾在上限"""
    tid = get_or_create_tenant(app, 'fix-rea1-users2')
    with tenant_request(app, tid):
        excel_service.import_workbook(_xlsx({'會員資料(User)': [
            ['姓名(Name)', '電話(Phone)', '紅利點數(Points)'], ['大點數', '0955250001', '1e20']]}))
        assert User.query.filter_by(phone='0955250001').one().points == excel_service.MAX_POINTS


def test_analytics_counts_only_revenue_orders_net_of_refunds(app):
    """B-368：待付款 / 未結帳的訂單不計入營收與熱銷；營收扣掉部分退款"""
    tid = get_or_create_tenant(app, 'fix-rea1-analytics')
    now = datetime(2024, 4, 2, 12, 0)
    with tenant_request(app, tid):
        paid = Order(tenant_id=tid, table_number='REA1-已付', total_price=100, refunded_amount=30, status='Completed',
                     payment_status='paid', pickup_number=1, created_at=now)
        pending = Order(tenant_id=tid, table_number='REA1-待付', total_price=500, status='Pending',
                        payment_status='pending', pickup_number=2, created_at=now)
        db.session.add_all([paid, pending])
        db.session.flush()
        db.session.add_all([OrderItem(order_id=paid.id, item_name='REA1已付餐', price=100, quantity=1),
                            OrderItem(order_id=pending.id, item_name='REA1待付餐', price=500, quantity=1)])
        db.session.commit()
        buf = excel_service.generate_excel_export(['Analytics'], '2024-04-02', '2024-04-02')
        metrics = {r['指標 (Metric)']: r['數值 (Value)'] for r in _sheet_rows(buf, '區間營運總覽(Analytics)')}
        assert metrics['有效訂單數'] == 1 and metrics['總營收'] == 70
        buf.seek(0)
        names = [r['餐點名稱 (Item Name)'] for r in _sheet_rows(buf, '區間熱銷排行(ItemSales)')]
        assert names == ['REA1已付餐']
