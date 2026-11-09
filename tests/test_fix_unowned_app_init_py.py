"""create_app 安裝 BoundedIntConverter (B-209) 與 KioskSessionInterface (B-195) 的回歸測試"""
from app.utils import MAX_DB_ID, BoundedIntConverter, KioskSessionInterface

HUGE_ID = '9' * 20   # 超過 2^63-1


def test_create_app_installs_bounded_int_and_session_interface(app):
    assert app.url_map.converters['int'] is BoundedIntConverter
    assert isinstance(app.session_interface, KioskSessionInterface)
    # blueprint 的 <int:> 路由 (在 create_app 中註冊) 都使用有上限的轉換器
    rule = next(r for r in app.url_map.iter_rules() if r.endpoint == 'customer.reorder')
    conv = rule._converters['order_id']
    assert isinstance(conv, BoundedIntConverter)
    assert conv.max == MAX_DB_ID


def test_huge_int_id_is_404_not_500(app):
    client = app.test_client()
    assert client.get(f'/api/reorder/{HUGE_ID}').status_code == 404
