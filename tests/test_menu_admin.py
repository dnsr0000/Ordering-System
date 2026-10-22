"""後台菜單管理：新增 / 編輯 / 刪除餐點、套餐與配餐、手動售完"""


from app.extensions import db
from app.models.menu import ComboOption, MenuItem
from app.models.user import StaffAuditLog
from tests.support import (
    assert_flash,
    cancel_order,
    make_item,
    pos_checkout,
)


def test_create_menu_items(app, admin_client, ctx):
    """建立主餐、飲料、副餐、點心與三種加購觸發條件 (any / category / item)"""
    items = [
        dict(name='招牌和牛堡', category='主餐', price='150', stock='30', is_discount='1', discount_price='120',
             modifiers='addons', description='頂級多汁和牛漢堡'),
        dict(name='錫蘭紅茶', category='飲料', price='40', stock='50', modifiers='ice_sugar', can_be_add_on='1',
             add_on_price='25', addon_trigger_type='any'),
        dict(name='美式脆薯', category='副餐', price='60', stock='40', modifiers='spicy', can_be_add_on='1',
             add_on_price='45', addon_trigger_type='category', addon_trigger_target='主餐'),
        dict(name='黃金雞塊', category='點心', price='70', stock='25', modifiers='none'),
    ]
    for form in items:
        assert admin_client.post('/admin/add', data=form).status_code == 302
    with app.app_context():
        burger = MenuItem.query.filter_by(name='招牌和牛堡').first()
        assert burger.is_discount and burger.discount_price == 120 and burger.unit_price == 120
    r = admin_client.post('/admin/add', data=dict(name='松露沾醬', category='點心', price='35', stock='30', modifiers='none',
                                                 can_be_add_on='1', add_on_price='20', addon_trigger_type='item',
                                                 addon_trigger_target=str(burger.id)))
    assert r.status_code == 302
    with app.app_context():
        ctx['menu_ids'] = {m.name: m.id for m in MenuItem.query.all()}
    assert len(ctx['menu_ids']) >= 5


def test_edit_delete_and_validation(app, admin_client):
    """加購價高於原價擋下；編輯、刪除與刪除稽核"""
    admin_client.post('/admin/add', data=dict(name='違法高價加購品', category='點心', price='50', stock='10',
                                              can_be_add_on='1', add_on_price='80'))
    assert_flash(admin_client, '加購專屬價')
    admin_client.post('/admin/add', data=dict(name='季節限定濃湯', category='副餐', price='55', stock='15'))
    with app.app_context():
        assert MenuItem.query.filter_by(name='違法高價加購品').first() is None
        soup_id = MenuItem.query.filter_by(name='季節限定濃湯').first().id
    r = admin_client.post(f'/admin/edit/{soup_id}', data=dict(name='主廚松露濃湯', category='副餐', price='65',
                                                              stock='20', modifiers='none'))
    assert r.status_code == 302
    with app.app_context():
        soup = db.session.get(MenuItem, soup_id)
        assert soup.name == '主廚松露濃湯' and soup.price == 65
    assert admin_client.post(f'/admin/delete/{soup_id}').status_code == 302
    with app.app_context():
        assert db.session.get(MenuItem, soup_id) is None
        audit = StaffAuditLog.query.filter_by(action_type='DELETE_MENU_ITEM').first()
        assert audit is not None and '主廚松露濃湯' in audit.details


def test_combo_sides_and_customizable_flag(app, admin_client, ctx):
    """建立套餐；配餐沒有客製化群組時 customizable 一律存為 False"""
    ids = ctx['menu_ids']
    admin_client.post('/admin/add_combo', data=dict(main_item_id=ids['招牌和牛堡'], name='A套餐(薯條+紅茶)',
                                                    additional_price='75', item1_id=ids['美式脆薯'], item2_id=ids['錫蘭紅茶'],
                                                    item3_id='', item1_customizable='1', item2_customizable='1', can_addon='1'))
    admin_client.post('/admin/add_combo', data=dict(main_item_id=ids['招牌和牛堡'], name='B套餐(單配雞塊)',
                                                    additional_price='55', item1_id=ids['黃金雞塊'], item1_customizable='1',
                                                    can_addon='0'))
    with app.app_context():
        a = ComboOption.query.filter_by(name='A套餐(薯條+紅茶)').first()
        b = ComboOption.query.filter_by(name='B套餐(單配雞塊)').first()
        assert [s.menu_item_id for s in a.sides] == [ids['美式脆薯'], ids['錫蘭紅茶']]
        assert a.sides[0].customizable is True and a.can_addon is True
        assert b.sides[0].customizable is False and b.can_addon is False
        ctx['combo_a'] = a.id


def test_combo_validation_edit_delete(app, admin_client, ctx):
    """配餐重複、同名、同內容皆擋下；編輯與刪除套餐"""
    ids = ctx['menu_ids']
    burger = ids['招牌和牛堡']
    admin_client.post('/admin/add_combo', data=dict(main_item_id=burger, name='重複配餐套餐', additional_price='60',
                                                    item1_id=ids['錫蘭紅茶'], item2_id=ids['錫蘭紅茶']))
    assert_flash(admin_client, '配餐不可重複')
    admin_client.post('/admin/add_combo', data=dict(main_item_id=burger, name='A套餐(薯條+紅茶)', additional_price='1'))
    assert_flash(admin_client, '已存在名為')
    admin_client.post('/admin/add_combo', data=dict(main_item_id=burger, name='同內容', additional_price='1',
                                                    item1_id=ids['錫蘭紅茶'], item2_id=ids['美式脆薯']))
    assert_flash(admin_client, '相同配餐內容')
    with app.app_context():
        assert ComboOption.query.filter_by(name='重複配餐套餐').first() is None
        b_id = ComboOption.query.filter_by(name='B套餐(單配雞塊)').first().id

    r = admin_client.post(f'/admin/edit_combo/{b_id}', data=dict(main_item_id=burger, name='B套餐(雞塊+松露醬)',
                                                                 additional_price='68', item1_id=ids['黃金雞塊'],
                                                                 item2_id=ids['松露沾醬']))
    assert r.status_code == 302
    with app.app_context():
        b = db.session.get(ComboOption, b_id)
        assert b.name == 'B套餐(雞塊+松露醬)' and b.additional_price == 68 and len(b.sides) == 2

    admin_client.post('/admin/add_combo', data=dict(main_item_id=burger, name='臨時待刪套餐', additional_price='20',
                                                    item1_id=ids['松露沾醬']))
    with app.app_context():
        tmp_id = ComboOption.query.filter_by(name='臨時待刪套餐').first().id
    assert admin_client.post(f'/admin/delete_combo/{tmp_id}').status_code == 302
    with app.app_context():
        assert db.session.get(ComboOption, tmp_id) is None


def test_manual_sold_out(app, admin_client):
    """手動標示售完的餐點不可下單；取消訂單回補庫存但不解除手動售完"""
    iid = make_item(app, '模組15售完品', price=50, stock=5, is_sold_out=True)
    r = pos_checkout(admin_client, iid)
    assert r.status_code == 400 and r.get_json()['sold_out'] is True
    with app.app_context():
        it = db.session.get(MenuItem, iid)
        assert it.stock == 5
        it.is_sold_out = False
        db.session.commit()
    r = pos_checkout(admin_client, iid)
    assert r.status_code == 200
    with app.app_context():
        it = db.session.get(MenuItem, iid)
        assert it.stock == 4
        it.is_sold_out = True
        db.session.commit()
    assert cancel_order(admin_client, r.get_json()['order_id']).status_code == 200
    with app.app_context():
        it = db.session.get(MenuItem, iid)
        assert (it.stock, it.is_sold_out) == (5, True)
