"""第四輪：order_service 拆成 order_pricing / order_stock / order_checkout / order_lifecycle 後，對外介面不變、依賴方向單純"""
import importlib
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARTS = ('order_pricing', 'order_stock', 'order_checkout', 'order_lifecycle')
# 下層模組不可反向依賴上層 (避免循環匯入)
ALLOWED = {
    'order_pricing': set(),
    'order_stock': set(),
    'order_checkout': {'order_pricing', 'order_stock'},
    'order_lifecycle': {'order_stock'},
}


def _src(name):
    with open(os.path.join(ROOT, 'app', 'services', f'{name}.py'), encoding='utf-8') as fh:
        return fh.read()


def test_facade_reexports_the_same_objects():
    facade = importlib.import_module('app.services.order_service')
    for part in PARTS:
        mod = importlib.import_module(f'app.services.{part}')
        for name, obj in vars(mod).items():
            if callable(obj) and getattr(obj, '__module__', '') == mod.__name__:
                assert getattr(facade, name) is obj, f'order_service.{name} 應與 {part}.{name} 為同一個物件'
    # 既有呼叫端使用的名稱
    for name in ('process_checkout', 'change_order_status', 'todays_range', 'todays_orders_condition', 'find_promo',
                 'order_stock_demands', 'flag_refund_if_paid', 'ensure_daily_auto_close', 'void_item', 'system_cancel',
                 'online_pickup_node', 'next_pickup_number', 'consume_stock_unchecked', 'complete_all_pending_orders',
                 'auto_close_stale_orders', '_parse_lines', '_auto_close_done_for', 'LEGAL_TRANSITIONS', 'ROLE_TRANSITIONS'):
        assert hasattr(facade, name), name
    assert facade._auto_close_done_for is importlib.import_module('app.services.order_lifecycle')._auto_close_done_for


def test_order_modules_are_small_and_layered():
    for part in PARTS:
        src = _src(part)
        assert len(src.splitlines()) <= 600, f'{part}.py 超過 600 行'
        assert 'order_service' not in re.sub(r'""".*?"""', '', src, count=1, flags=re.S), f'{part} 不可匯入 order_service'
        deps = set(re.findall(r'^from app\.services\.(order_\w+) import', src, re.M)) - {part}
        assert deps <= ALLOWED[part], f'{part} 不應依賴 {deps - ALLOWED[part]}'
    facade = _src('order_service')
    assert 'def ' not in facade, 'order_service.py 只負責匯出，不放實作'


def test_todays_orders_condition_can_use_indexes():
    """「今天的訂單」= 今天建立 OR 預約今天取餐：兩個欄位都要有索引 (PostgreSQL 才能用 BitmapOr，不必掃描整個訂單表)"""
    from app.models.order import Order
    indexed = {col.name for idx in Order.__table__.indexes for col in idx.columns}
    assert {'created_at', 'pickup_at', 'status', 'tenant_id'} <= indexed
