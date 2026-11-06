"""全面檢查修正 (後台服務)：報表不重複扣部分退款、套餐配餐的成本與備料預測、採購單重複收貨、
菜單匯入不覆蓋沒附上的欄位、卡在「送出中」的列印工作、設定匯入的時間 / 網址檢查"""
import io
import json
from datetime import date, datetime, timedelta

import pytest
from werkzeug.datastructures import FileStorage

from app.errors import AppError
from app.extensions import db
from app.models.inventory import Ingredient, PurchaseOrder, PurchaseOrderLine
from app.models.menu import MenuItem
from app.models.order import Order, OrderItem
from app.models.payment import Refund
from tests.support import default_tenant_id, tenant_request


@pytest.fixture
def shop(app):
    with tenant_request(app, default_tenant_id(app)):
        yield default_tenant_id(app)


def _order(tid, total, items, status='Completed', when=None):
    o = Order(tenant_id=tid, table_number='後台修正測試', total_price=total, status=status, pickup_number=900,
              created_at=when or datetime.now(), payment_status='')
    db.session.add(o)
    db.session.flush()
    for menu_item, qty, price, sides in items:
        db.session.add(OrderItem(order_id=o.id, menu_item_id=menu_item.id, item_name=menu_item.name, price=price,
                                 quantity=qty, combo_sides=json.dumps(sides) if sides else ''))
    db.session.commit()
    return o


def test_report_does_not_subtract_refunds_of_cancelled_orders_twice(shop):
    from app.services import report_service
    item = MenuItem(name='退款報表餐', category='主餐', price=500, stock=10, total_stock=10)
    db.session.add(item)
    db.session.commit()
    today = date.today()
    before = report_service.build_report(shop, today, today)
    o = _order(shop, 500, [(item, 1, 500, None)])
    db.session.add(Refund(tenant_id=shop, order_id=o.id, kind='partial', amount=100, method='Cash', status='done'))
    db.session.commit()
    mid = report_service.build_report(shop, today, today)
    assert mid['net_after_refunds'] - before['net_after_refunds'] == 400, '部分退款照常扣'
    o.status = 'Cancelled'   # 之後整張取消：整筆不算營業額，部分退款不可再扣一次
    db.session.commit()
    after = report_service.build_report(shop, today, today)
    assert after['net_after_refunds'] == before['net_after_refunds']


def test_combo_sides_count_in_cost_and_forecast(shop):
    from app.services import forecast_service, report_service
    main = MenuItem(name='配餐成本主餐', category='主餐', price=150, stock=10, total_stock=10, unit_cost=40)
    side = MenuItem(name='配餐成本薯條', category='點心', price=50, stock=10, total_stock=10, unit_cost=15)
    db.session.add_all([main, side])
    db.session.commit()
    today = date.today()
    before = report_service.build_report(shop, today, today)
    _order(shop, 300, [(main, 2, 150, [side.uuid])])   # 兩份套餐，每份含一份薯條
    after = report_service.build_report(shop, today, today)
    assert round(after['cogs'] - before['cogs'], 2) == (40 + 15) * 2, '銷貨成本含配餐'
    # 備料預測：上週同一天賣出的套餐配餐也算進薯條的需求
    last_week = datetime.now() - timedelta(days=7)
    _order(shop, 300, [(main, 2, 150, [side.uuid])], when=last_week)
    result = forecast_service.forecast(target=today, weeks=1, today=today)
    rows = {r['name']: r for r in result['items']} if isinstance(result, dict) else {r['name']: r for r in result}
    assert rows['配餐成本薯條']['qty'] >= 2


def test_purchase_order_cannot_be_received_twice(shop):
    from app.services import purchasing_service
    ing = Ingredient(name='重複收貨原料', unit='g', stock=0, unit_cost=1)
    db.session.add(ing)
    db.session.flush()
    po = PurchaseOrder(status='sent')
    db.session.add(po)
    db.session.flush()
    db.session.add(PurchaseOrderLine(purchase_order_id=po.id, ingredient_id=ing.id, quantity=100, unit_cost=1))
    db.session.commit()
    from types import SimpleNamespace
    stale = SimpleNamespace(id=po.id, status='sent', lines=list(po.lines), number=po.number)   # 另一個分頁讀到的仍是「已送出」
    purchasing_service.receive(po, {}, 'staff_a')
    db.session.commit()
    with pytest.raises(AppError) as exc:
        purchasing_service.receive(stale, {}, 'staff_b')
    assert exc.value.status == 409
    db.session.rollback()
    assert db.session.get(Ingredient, ing.id).stock == 100, '只入庫一次'


def _csv(text):
    return FileStorage(io.BytesIO(text.encode('utf-8-sig')), filename='menu.csv')


def test_menu_import_keeps_fields_that_are_not_in_the_file(shop):
    from app.services import excel_service
    item = MenuItem(name='匯入保留餐', category='飲料', price=60, stock=10, total_stock=10, modifiers='ice_sugar',
                    description='現煮', is_manual_popular=True, discount_price=50, is_discount=True)
    db.session.add(item)
    db.session.commit()
    excel_service.import_workbook(_csv('餐點名稱,單價\n匯入保留餐,65\n'))
    db.session.refresh(item)
    assert (item.price, item.modifiers, item.description, item.is_manual_popular, item.is_discount, item.discount_price,
            item.category) == (65, 'ice_sugar', '現煮', True, True, 50, '飲料'), '只改價時其他設定保留'
    excel_service.import_workbook(_csv('餐點名稱,單價,分類\n匯入保留餐,,\n'))
    db.session.refresh(item)
    assert item.price == 65 and item.category == '飲料', '空白的單價 / 分類保留原值，不會變成 $0'
    with pytest.raises(AppError):
        excel_service.import_workbook(_csv('餐點名稱,單價\n匯入新餐點無價,\n'))
    db.session.rollback()
    assert MenuItem.query.filter_by(name='匯入新餐點無價').count() == 0


def test_stuck_print_jobs_become_failed_for_reprint(shop):
    from sqlalchemy import update

    from app.models.printing import Printer, PrintJob
    from app.services import print_service
    printer = Printer(name='卡住測試機', host='192.0.2.10', port=9100)
    db.session.add(printer)
    db.session.flush()
    stuck = PrintJob(printer_id=printer.id, title='廚房單', payload=b'x', status='sending')
    fresh = PrintJob(printer_id=printer.id, title='剛送出', payload=b'x', status='sending')
    db.session.add_all([stuck, fresh])
    db.session.commit()
    db.session.execute(update(PrintJob).where(PrintJob.id == stuck.id).values(updated_at=datetime.now() - timedelta(minutes=10)))
    db.session.commit()
    assert print_service.recover_stuck_jobs(shop) == 1
    assert db.session.get(PrintJob, stuck.id).status == 'failed', '可在後台按「重印」'
    assert db.session.get(PrintJob, fresh.id).status == 'sending', '剛送出的不動'


def test_settings_import_validates_time_and_url(shop):
    from app.services import settings_service
    before = settings_service.get('online_order', shop).get('open_time') if 'online_order' in settings_service.SECTIONS \
        else None
    key = next(k for k, s in settings_service.SECTIONS.items()
               if any(f.name == 'open_time' and f.type == 'time' for f in s.fields) and not any(f.type == 'secret' for f in s.fields))
    before = settings_service.get(key, shop)['open_time']
    _applied, skipped = settings_service.import_all({'version': 1, 'sections': {key: {'open_time': '9:00'}}})
    assert f'{key}.open_time' in skipped and settings_service.get(key, shop)['open_time'] == before
    url_key = next(k for k, s in settings_service.SECTIONS.items()
                   if any(f.type == 'url' for f in s.fields) and not any(f.type == 'secret' for f in s.fields))
    url_field = next(f.name for f in settings_service.SECTIONS[url_key].fields if f.type == 'url')
    _applied, skipped = settings_service.import_all({'version': 1, 'sections': {url_key: {url_field: 'javascript:alert(1)'}}})
    assert f'{url_key}.{url_field}' in skipped
    db.session.rollback()
