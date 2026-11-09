"""修正小組 models-a 的回歸測試：優惠券折扣值解析 / 到期日、跨午夜供應星期、分店價差、規格全形符號、採購單溢位"""
from datetime import date, datetime, time, timedelta

from flask import g

from app.models.coupon import MAX_FIXED_DISCOUNT, Coupon, UserCoupon, parse_discount_value
from app.models.enums import DiscountType
from app.models.inventory import PurchaseOrder, PurchaseOrderLine
from app.models.menu import MenuItem, invalid_size_options, parse_size_options, service_day
from tests.support import default_tenant_id

PCT, FIXED = DiscountType.PERCENT, DiscountType.FIXED


# ---------------------------------------------------------------------------
# B-490 / B-492 / R2-192：折扣值解析
# ---------------------------------------------------------------------------
def test_parse_discount_value_rejects_unparseable_and_out_of_range():
    # 三種寫法照舊
    assert parse_discount_value(PCT, '85') == 85
    assert parse_discount_value(PCT, '8.5') == 85
    assert parse_discount_value(PCT, '0.85') == 85
    assert parse_discount_value(PCT, '0.1') == 10
    # 無法辨識：不可變成 0 (整單全免)
    for raw in ('9折', '85折', 'abc', '', '   ', None, 'nan', 'inf'):
        assert parse_discount_value(PCT, raw) is None, raw
        assert parse_discount_value(FIXED, raw) is None, raw
    # 換算後不在 1~99：不可夾成 1 (99% off) 或 99 (只折 1%)
    for raw in ('0', 0, '1', '10', '100', '-5', '0.995'):
        assert parse_discount_value(PCT, raw) is None, raw
    # 固定金額：負數視為 0，極大值有上限 (不會寫入 Integer 欄位時溢位)
    assert parse_discount_value(FIXED, '50') == 50
    assert parse_discount_value(FIXED, '-3') == 0
    assert parse_discount_value(FIXED, '1e20') == MAX_FIXED_DISCOUNT


def test_percent_coupon_with_invalid_stored_value_gives_no_discount():
    assert Coupon(discount_type=PCT, discount_value=0).discount_for(500) == 0
    assert Coupon(discount_type=PCT, discount_value=85).discount_for(200) == 30
    assert Coupon(discount_type=FIXED, discount_value=50).discount_for(200) == 50


# ---------------------------------------------------------------------------
# B-493 / B-494 / B-495：會員券到期日與來源
# ---------------------------------------------------------------------------
def test_expires_for_counts_from_valid_from_and_ignores_valid_until():
    issued = datetime(2026, 12, 1, 10, 0)
    cp = Coupon(valid_from=date(2026, 12, 20), valid_until=date(2026, 12, 25), valid_days=7)
    # 活動開始前發放：從活動開始日起算 N 天 (不會在開始前就到期)；活動結束日不寫進持券
    assert cp.expires_for(issued) == datetime.combine(date(2026, 12, 27), time(23, 59, 59))
    assert cp.expires_for(issued, valid_days=3) == datetime.combine(date(2026, 12, 23), time(23, 59, 59))
    # 活動期間內發放：從發放日起算
    assert cp.expires_for(datetime(2026, 12, 21, 9, 0)) == datetime.combine(date(2026, 12, 28), time(23, 59, 59))
    # 沒有天數：只受活動期間限制 (由 status 動態判斷)
    assert Coupon(valid_until=date(2026, 12, 25), valid_days=0).expires_for(issued) is None


def test_user_coupon_follows_extended_period_and_shows_stamp_source(app):
    with app.app_context():
        today = date.today()
        cp = Coupon(code='EXTEND1', title='延長測試', valid_until=today, valid_days=0)
        uc = UserCoupon(coupon=cp, code=cp.code, is_used=False, source='stamp',
                        created_at=datetime.now(), expires_at=cp.expires_for(datetime.now()))
        tomorrow = datetime.combine(today + timedelta(days=1), time(12, 0))
        assert uc.status(tomorrow) == 'expired'
        cp.valid_until = today + timedelta(days=30)   # 店家延長活動：已發出的券跟著延長
        assert uc.status(tomorrow) == 'available'
        assert uc.expired_at == datetime.combine(today + timedelta(days=30), time(23, 59, 59))
        cp.valid_until = today - timedelta(days=1)    # 縮短照樣即時生效
        assert uc.status(tomorrow) == 'expired'
        assert uc.to_dict(tomorrow)['source'] == '集點卡'

        # 活動尚未開始就先發放：先是 upcoming，活動期間內可用，不會直接變 expired
        start = today + timedelta(days=19)
        future = Coupon(code='LATER1', title='尚未開始', valid_from=start, valid_until=start + timedelta(days=5),
                        valid_days=7)
        issued = datetime.now()
        uc2 = UserCoupon(coupon=future, code=future.code, is_used=False, created_at=issued,
                         expires_at=future.expires_for(issued))
        assert uc2.status(issued) == 'upcoming'
        assert uc2.status(datetime.combine(start + timedelta(days=1), time(12, 0))) == 'available'


# ---------------------------------------------------------------------------
# B-491：跨午夜時段 + 供應星期
# ---------------------------------------------------------------------------
def test_cross_midnight_slot_uses_previous_business_day(app):
    friday = datetime(2026, 11, 13)          # 2026-11-13 是週五
    assert friday.isoweekday() == 5
    sat_0100 = (friday + timedelta(days=1)).replace(hour=1)
    fri_0100 = friday.replace(hour=1)
    fri_2300 = friday.replace(hour=23)
    assert service_day(sat_0100, '22:00', '02:00').date() == friday.date()
    assert service_day(fri_2300, '22:00', '02:00') == fri_2300
    assert service_day(sat_0100, '', '') == sat_0100
    with app.app_context():
        item = MenuItem(name='週五宵夜', price=100, avail_start='22:00', avail_end='02:00', avail_days='5',
                        tenant_id=default_tenant_id(app))
        assert item.service_status(fri_2300) == (True, '')
        assert item.service_status(sat_0100) == (True, '')          # 週五晚的宵夜
        assert item.service_status(fri_0100)[0] is False            # 週四晚：沒有營業
        assert item.service_status((friday + timedelta(days=1)).replace(hour=23))[0] is False
        # 販售期間也以營業日判斷：販售到週五，週六凌晨仍屬於週五晚
        item.avail_days, item.sale_until = '1234567', friday.date()
        assert item.service_status(sat_0100)[0] is True
        assert item.service_status((friday + timedelta(days=1)).replace(hour=23))[1] == '已停售'


# ---------------------------------------------------------------------------
# R2-190：分店價差不可把特價 / 通路價 / 會員價夾成 0 元
# ---------------------------------------------------------------------------
def test_branch_delta_never_clamps_channel_or_member_price_to_zero(app):
    with app.app_context():
        item = MenuItem(id=987654, name='分店價差', price=100, member_price=60, takeout_price=0)
        g.branch_menu = {item.id: (-70, False)}    # 分店價 $30
        try:
            assert item.list_price == 30
            assert item.price_for('dine_in', member=True) == 30
            assert item.price_for('dine_in') == 30
            # 總部之後調漲內用價：外帶價 60 + (80-150) <= 0 → 退回分店內用價，不是 0 元
            item.price, item.takeout_price, item.member_price = 150, 60, 0
            g.branch_menu = {item.id: (-70, False)}
            assert item.price_for('takeout') == 80
            # 特價加上價差 <= 0：視為未設定
            item.is_discount, item.discount_price = True, 50
            assert item.sale_price == 80 and item.unit_price == 80 and item.price_for('dine_in') == 80
            # 價差不足以變成 0 時照舊加減
            g.branch_menu = {item.id: (-20, False)}
            assert item.price_for('dine_in') == 30 and item.sale_price == 30
            item.is_discount = False
            assert item.price_for('takeout') == 40
            item.member_price = 50
            assert item.price_for('takeout', member=True) == 30
        finally:
            g.branch_menu = {}


# ---------------------------------------------------------------------------
# R2-191：規格的全形冒號 / 加號
# ---------------------------------------------------------------------------
def test_size_options_accept_fullwidth_symbols():
    assert parse_size_options('中杯：0，大杯：15') == [('中杯', 0), ('大杯', 15)]
    assert parse_size_options('中杯:0, 大杯:＋15') == [('中杯', 0), ('大杯', 15)]
    assert parse_size_options('小杯：－10、中杯:0') == [('小杯', -10), ('中杯', 0)]
    # 舊版誤存的「中杯：0:0, 大杯：15:0」可還原
    assert parse_size_options('中杯：0:0, 大杯：15:0') == [('中杯', 0), ('大杯', 15)]
    # 原有寫法與錯誤略過規則照舊；格式錯誤的片段可以列出來
    assert parse_size_options('中杯:0, 大杯:+15，特大杯:25') == [('中杯', 0), ('大杯', 15), ('特大杯', 25)]
    assert parse_size_options('小:-10\n大:abc') == [('小', -10)]
    assert invalid_size_options('小:-10\n大:abc, :5') == ['大:abc', ':5']
    assert invalid_size_options('中杯：0，大杯：＋15') == []


# ---------------------------------------------------------------------------
# R2-193：採購單極大值不會讓頁面 500
# ---------------------------------------------------------------------------
def test_purchase_order_total_survives_overflow():
    po = PurchaseOrder()
    po.lines = [PurchaseOrderLine(quantity=1e308, unit_cost=10), PurchaseOrderLine(quantity=2, unit_cost=3.5)]
    assert po.total == 0
    assert po.lines[0].amount == 0 and po.lines[1].amount == 7
    ok = PurchaseOrder()
    ok.lines = [PurchaseOrderLine(quantity=2, unit_cost=3.5), PurchaseOrderLine(quantity=1, unit_cost=10)]
    assert ok.total == 17
