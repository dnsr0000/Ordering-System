"""
跨檔請求 (order-pricing-checkout-2) 的回歸測試：
品項 / 套餐 / 加料 ID 超出範圍不 500 (B-209)、客製化文字只移除完全符合的加料片段與控制字元 (R2-074、R2-139)、
寄杯標籤不重複 (B-227)、指定到期日的發券不寫入活動結束日 (B-494、B-493)、促銷贈品限本店 (B-004)、
取餐號依營業日與預約取餐日編號 (B-066、B-449、B-190)。
"""
from datetime import date, datetime, time, timedelta
from types import SimpleNamespace

import pytest

from app.errors import AppError
from app.extensions import db
from tests.support import default_tenant_id, get_or_create_tenant, guest_client, make_item, make_member, tenant_request

HUGE = '99999999999999999999'


# ---------------------------------------------------------------------------
# B-209：超出 64 位元的 ID 視為不存在 (400)，不是 OverflowError → 500
# ---------------------------------------------------------------------------
def test_huge_ids_do_not_overflow(app):
    from app.services.order_pricing import _parse_lines
    tid = default_tenant_id(app)
    item = make_item(app, 'B209 主餐', price=50, stock=20, modifiers='addons')
    with tenant_request(app, tid):
        with pytest.raises(AppError, match='不存在'):
            _parse_lines([{'menu_item_id': HUGE, 'quantity': 1}], tid)
        with pytest.raises(AppError):
            _parse_lines([{'menu_item_id': item, 'quantity': 1, 'modifier_ids': [HUGE]}], tid)
        ln = _parse_lines([{'menu_item_id': item, 'quantity': 1, 'combo_option_id': HUGE, 'pass_id': HUGE}], tid)[0]
        assert ln.combo is None and not ln.is_pass and ln.unit_price == 50
    r = guest_client(app).post('/submit_order', json={'items': [{'menu_item_id': HUGE, 'quantity': 1}],
                                                      'payment_method': 'Cash', 'order_type': '內用'})
    assert 400 <= r.status_code < 500


# ---------------------------------------------------------------------------
# R2-074 / R2-139：只移除完全等於加料名稱的片段；控制字元去除
# ---------------------------------------------------------------------------
def test_clean_customization_keeps_side_text_and_strips_control_chars():
    from app.services.order_pricing import _clean_customization
    names = {'珍珠', '起司', '蛋'}
    assert _clean_customization('少冰/半糖、珍珠奶茶(少冰/半糖)、珍珠(+10)', names) == ['少冰/半糖', '珍珠奶茶(少冰/半糖)']
    assert _clean_customization('蛋餅(微辣)、起司(+15)', names) == ['蛋餅(微辣)']
    # 舊版前端：加料放在配餐括號內，切段後的殘缺片段
    assert _clean_customization('薯條(起司(+15)、蛋(+10))', names) == ['薯條']
    assert _clean_customization('[套餐: A]、不要\x07香菜\x00、少冰\x1b', names) == ['不要香菜', '少冰']


# ---------------------------------------------------------------------------
# B-227：舊掛單送來的 [寄杯] 標籤不重複
# ---------------------------------------------------------------------------
def test_pass_tag_not_duplicated(app):
    from app.services.order_pricing import _parse_lines
    tid = default_tenant_id(app)
    drink = make_item(app, 'B227 寄杯紅茶', price=40, stock=20, category='飲料')
    with tenant_request(app, tid):
        ln = _parse_lines([{'menu_item_id': drink, 'quantity': 1, 'pass_id': 3, 'customization': '[寄杯]、少冰'}], tid)[0]
        assert ln.customization == '[寄杯]、少冰'


# ---------------------------------------------------------------------------
# B-494 / B-493：指定到期日發券
# ---------------------------------------------------------------------------
def test_issue_with_expires_on_keeps_own_date_and_reports_effective_end(app):
    from app.models.coupon import Coupon, UserCoupon
    from app.models.user import User
    from app.services import coupon_service
    tid = default_tenant_id(app)
    uid = make_member(app, 'B494 會員', '0968000494')
    today = date.today()
    with tenant_request(app, tid):
        try:
            coupon = Coupon(tenant_id=tid, code='B494X', title='B494 券', discount_value=10,
                            valid_from=today - timedelta(days=1), valid_until=today + timedelta(days=5))
            db.session.add(coupon)
            db.session.flush()
            user = db.session.get(User, uid)
            result = coupon_service.issue(tid, coupon, [user], expires_on=today + timedelta(days=20))
            uc = UserCoupon.query.filter_by(batch_id=result['batch_id']).one()
            assert uc.expires_at == datetime.combine(today + timedelta(days=20), time(23, 59, 59))
            assert result['expires_at'] == datetime.combine(today + timedelta(days=5), time(23, 59, 59))
            coupon.valid_until = today + timedelta(days=30)   # 延長活動：持券跟著延到自己的到期日
            assert uc.expired_at == uc.expires_at

            future = Coupon(tenant_id=tid, code='B493X', title='B493 券', discount_value=10,
                            valid_from=today + timedelta(days=10))
            db.session.add(future)
            db.session.flush()
            with pytest.raises(AppError, match='活動開始日'):
                coupon_service.issue(tid, future, [user], expires_on=today + timedelta(days=3))
        finally:
            db.session.rollback()
            db.session.delete(db.session.get(User, uid))
            db.session.commit()


# ---------------------------------------------------------------------------
# B-004：滿額贈品只取本店的品項 (背景排程沒有店家過濾)
# ---------------------------------------------------------------------------
def test_threshold_gift_uses_promotion_tenant(app):
    from app.models.menu import MenuItem
    from app.services import promotion_service
    tid = default_tenant_id(app)
    other = get_or_create_tenant(app, 'b004-other')
    with app.app_context():
        gift = MenuItem(tenant_id=other, name='B004 他店贈品', category='點心', price=30, stock=10, total_stock=10)
        db.session.add(gift)
        db.session.commit()
        gift_uuid = gift.uuid
        main = db.session.get(MenuItem, make_item(app, 'B004 主餐', price=300, stock=10))
        lines = [SimpleNamespace(menu_item=main, quantity=1, unit_price=300, is_add_on=False, is_reward=False)]

        def promo(tenant_id):
            return SimpleNamespace(id=1, tenant_id=tenant_id, priority=0, is_active=True, start_date=None, end_date=None,
                                   days='1234567', start_time='', end_time='', channels='', member_only=False,
                                   kind='threshold_gift', threshold=100, amount=0, percent=None, target_type='all',
                                   target_value='', name='B004 滿額贈', stack_with_coupon=True, buy_qty=1, get_qty=1,
                                   nth=2, gift_item_uuid=gift_uuid)
        assert promotion_service.evaluate(lines, 'pos', promotions=[promo(tid)]).gifts == []
        assert [g.id for g, _q, _n in promotion_service.evaluate(lines, 'pos', promotions=[promo(other)]).gifts] == [gift.id]


# ---------------------------------------------------------------------------
# B-066 / B-449 / B-190：取餐號依營業日換日；預約單依取餐日編號
# ---------------------------------------------------------------------------
def test_business_day_respects_start_hour(app, monkeypatch):
    from app.services import order_stock
    with app.app_context():
        monkeypatch.setitem(app.config, 'BUSINESS_DAY_START_HOUR', 4)
        assert order_stock.business_day(datetime(2026, 11, 9, 1, 30)) == date(2026, 11, 8)
        assert order_stock.business_day(datetime(2026, 11, 9, 4, 0)) == date(2026, 11, 9)
        monkeypatch.setitem(app.config, 'BUSINESS_DAY_START_HOUR', 0)
        assert order_stock.business_day(datetime(2026, 11, 9, 0, 0)) == date(2026, 11, 9)


def test_pickup_number_for_future_business_date(app):
    from app.models.order import DailyCounter
    from app.services.order_stock import PICKUP_COUNTER, next_pickup_number
    tid = default_tenant_id(app)
    day = date.today() + timedelta(days=400)
    with app.app_context():
        try:
            assert next_pickup_number(tid, business_date=day) == 1
            assert next_pickup_number(tid, business_date=day) == 2
            row = DailyCounter.query.filter_by(tenant_id=tid, business_date=day, name=PICKUP_COUNTER).one()
            assert row.value == 2
        finally:
            db.session.rollback()
