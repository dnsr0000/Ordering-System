"""修正小組 menu-backend-signage-1 的回歸測試：套餐配餐同步刪除紀錄、菜單欄位長度 / 金額上限、售完勾選、
任選群組解析 (全形加號、品名含分隔字元、同名品項)、匯入佔位表 / 金額格式、熱門取消、改名後加購條件、$0 紅利品"""
import pytest

from app.errors import AppError
from app.extensions import db
from app.models.menu import ComboOption, MenuItem
from app.models.sync import SyncTombstone
from app.services import menu_service
from tests.support import assert_flash, default_tenant_id, make_item, tenant_request

P = 'FXMB'


def _get(app, item_id):
    with app.app_context():
        return db.session.get(MenuItem, item_id)


# ---------------------------------------------------------------- B-281
def test_edit_combo_keeps_side_uuids_and_tombstones_replaced_sides(app, admin_client):
    main = make_item(app, f'{P}堡', price=150)
    fries = make_item(app, f'{P}脆薯', price=50)
    tea = make_item(app, f'{P}紅茶', price=30)
    nugget = make_item(app, f'{P}雞塊', price=60)
    form = {'main_item_id': main, 'name': f'{P}套餐', 'additional_price': '70', 'item1_id': fries, 'item2_id': tea}
    assert admin_client.post('/admin/add_combo', data=form).status_code == 302
    with app.app_context():
        combo = ComboOption.query.filter_by(name=f'{P}套餐').one()
        combo_id = combo.id
        before = {s.menu_item_id: s.uuid for s in combo.sides}
    # 只改加價：配餐列沿用 (uuid 不變)，不產生刪除紀錄
    assert admin_client.post(f'/admin/edit_combo/{combo_id}', data=dict(form, additional_price='80')).status_code == 302
    with app.app_context():
        combo = db.session.get(ComboOption, combo_id)
        assert combo.additional_price == 80
        assert {s.menu_item_id: s.uuid for s in combo.sides} == before
    # 把紅茶換成雞塊：被換掉的紅茶列要寫刪除紀錄，其他主機才會刪掉
    assert admin_client.post(f'/admin/edit_combo/{combo_id}',
                             data=dict(form, item2_id=nugget, additional_price='80')).status_code == 302
    with app.app_context():
        combo = db.session.get(ComboOption, combo_id)
        assert sorted(s.menu_item_id for s in combo.sides) == sorted([fries, nugget])
        assert {s.menu_item_id: s.uuid for s in combo.sides}[fries] == before[fries]
        assert SyncTombstone.query.filter_by(table_name='combo_option_item', row_uuid=before[tea]).count() == 1


# ---------------------------------------------------------------- B-283 / R2-101
def test_menu_form_rejects_too_long_text_and_huge_numbers(app, admin_client):
    base = dict(category='主餐', price='100', stock='10', total_stock='10')
    long_name = f'{P}長' * 40
    admin_client.post('/admin/add', data=dict(base, name=long_name))
    assert_flash(admin_client, '餐點名稱最多 100 個字')
    admin_client.post('/admin/add', data=dict(base, name=f'{P}長分類', category='分' * 60))
    assert_flash(admin_client, '分類最多 50 個字')
    admin_client.post('/admin/add', data=dict(base, name=f'{P}條碼價', price='4710088410122'))
    assert_flash(admin_client, '單價必須介於')
    admin_client.post('/admin/add', data=dict(base, name=f'{P}條碼庫存', stock='4710088410122'))
    assert_flash(admin_client, '剩餘數量必須介於')
    with app.app_context():
        assert MenuItem.query.filter(MenuItem.name.in_(
            [long_name, f'{P}長分類', f'{P}條碼價', f'{P}條碼庫存'])).count() == 0


def test_combo_rejects_too_long_description_and_huge_price(app, admin_client):
    main = make_item(app, f'{P}長說明主餐')
    admin_client.post('/admin/add_combo', data={'main_item_id': main, 'name': f'{P}長說明套餐', 'additional_price': '10',
                                                 'description': '說' * 250})
    assert_flash(admin_client, '套餐說明最多 200 個字')
    admin_client.post('/admin/add_combo', data={'main_item_id': main, 'name': f'{P}巨額套餐',
                                                 'additional_price': '99999999999'})
    assert_flash(admin_client, '套餐加價必須介於')
    with app.app_context():
        assert ComboOption.query.filter(ComboOption.name.in_([f'{P}長說明套餐', f'{P}巨額套餐'])).count() == 0


def test_import_rejects_too_long_description(app):
    with tenant_request(app, default_tenant_id(app)):
        with pytest.raises(AppError, match='商品描述最多 200 個字'):
            menu_service.upsert_menu_items_from_rows(
                [{'餐點名稱': f'{P}長描述', '單價': 100, '商品描述(Description)': '描' * 250}])
        db.session.rollback()


# ---------------------------------------------------------------- B-291
def test_unchanged_sold_out_checkbox_keeps_current_db_value(app, admin_client):
    item_id = make_item(app, f'{P}已恢復供應', price=80, stock=5)
    form = dict(name=f'{P}已恢復供應', category='主餐', price='90', stock='5', total_stock='5', loaded_stock='5',
                loaded_total_stock='5', is_sold_out='1', loaded_sold_out='1')
    assert admin_client.post(f'/admin/edit/{item_id}', data=form).status_code == 302
    item = _get(app, item_id)
    assert item.price == 90 and not item.is_sold_out
    # 使用者真的勾選售完 (開窗時未售完) 才標示售完
    form.update(loaded_sold_out='0')
    admin_client.post(f'/admin/edit/{item_id}', data=form)
    assert _get(app, item_id).is_sold_out


# ---------------------------------------------------------------- B-292 / R2-106 / R2-107
def test_choice_parsing_fullwidth_plus_and_names_with_separators(app):
    boba, tea = make_item(app, f'{P}珍奶', price=60), make_item(app, f'{P}奶茶', price=50)
    make_item(app, f'{P}薯條', price=40)
    platter = make_item(app, f'{P}薯條、雞塊拼盤', price=90)
    rings = make_item(app, f'{P}洋蔥圈', price=45)
    with tenant_request(app, default_tenant_id(app)):
        out = menu_service.parse_combo_choices(f'飲料：{P}奶茶，{P}珍奶＋１５')
        assert [(g, i.id, e) for g, i, e in out] == [('飲料', tea, 0), ('飲料', boba, 15)]
        out = menu_service.parse_combo_choices(f'點心: {P}薯條、雞塊拼盤+10, {P}洋蔥圈')
        assert [(i.id, e) for _, i, e in out] == [(platter, 10), (rings, 0)]


def test_choice_parsing_duplicate_names_rejected_or_keep_current_binding(app):
    first = make_item(app, f'{P}同名紅茶', price=30)
    second = make_item(app, f'{P}同名紅茶', price=0, category='套餐飲料')
    green = make_item(app, f'{P}綠茶', price=30)
    assert first != second

    class Side:   # 既有套餐目前綁定的配餐列
        def __init__(self, item_id):
            self.choice_group, self.menu_item_id = '飲料', item_id

    with tenant_request(app, default_tenant_id(app)):
        with pytest.raises(AppError, match='同名品項'):
            menu_service.parse_combo_choices(f'飲料: {P}同名紅茶, {P}綠茶')
        out = menu_service.parse_combo_choices(f'飲料: {P}同名紅茶, {P}綠茶', [Side(second), Side(green)])
        assert [i.id for _, i, _ in out] == [second, green]


# ---------------------------------------------------------------- B-293 / R2-103
def test_import_placeholder_sheet_and_price_formats(app):
    with tenant_request(app, default_tenant_id(app)):
        assert menu_service.upsert_menu_items_from_rows([{'資料': '目前無資料'}], columns=['資料']) == 0
        rows = [{'餐點名稱': f'{P}元價', '單價': '120元'}, {'餐點名稱': f'{P}千分位', '單價': '1,280'},
                {'餐點名稱': f'{P}幣別', '單價': 'NT$95', '特價金額': '＄80'}]
        assert menu_service.upsert_menu_items_from_rows(rows) == 3
        prices = {i.name: (i.price, i.discount_price) for i in
                  MenuItem.query.filter(MenuItem.name.in_([r['餐點名稱'] for r in rows]))}
        assert prices == {f'{P}元價': (120, 0), f'{P}千分位': (1280, 0), f'{P}幣別': (95, 80)}
        db.session.commit()
        with pytest.raises(AppError, match='單價「約一百」無法辨識'):
            menu_service.upsert_menu_items_from_rows([{'餐點名稱': f'{P}元價', '單價': '約一百'}])
        db.session.rollback()
        assert MenuItem.query.filter_by(name=f'{P}元價').one().price == 120


# ---------------------------------------------------------------- B-294
def test_unchecking_popular_clears_legacy_is_recommended(app, admin_client):
    item_id = make_item(app, f'{P}舊熱門', price=70, is_recommended=True)
    form = dict(name=f'{P}舊熱門', category='主餐', price='70', stock='100', total_stock='100', loaded_stock='100',
                loaded_total_stock='100')
    assert admin_client.post(f'/admin/edit/{item_id}', data=form).status_code == 302
    item = _get(app, item_id)
    assert not item.is_recommended and not item.is_manual_popular
    with app.app_context():
        assert menu_service.item_form_data(db.session.get(MenuItem, item_id))['is_recommended'] is False


# ---------------------------------------------------------------- R2-102
def test_rename_item_and_category_retargets_addon_rules(app, admin_client):
    burger = make_item(app, f'{P}招牌和牛堡', price=150, category=f'{P}主餐')
    by_item = make_item(app, f'{P}松露沾醬', price=40, category='點心', can_be_add_on=True, add_on_price=20,
                        addon_trigger_type='item', addon_trigger_target=f'{P}招牌和牛堡')
    by_cat = make_item(app, f'{P}起司片', price=30, category='點心', can_be_add_on=True, add_on_price=15,
                       addon_trigger_type='category', addon_trigger_target=f'{P}主餐')
    form = dict(name=f'{P}和牛堡', category=f'{P}漢堡', price='150', stock='100', total_stock='100',
                loaded_stock='100', loaded_total_stock='100')
    assert admin_client.post(f'/admin/edit/{burger}', data=form).status_code == 302
    assert _get(app, by_item).addon_trigger_target == f'{P}和牛堡'
    assert _get(app, by_cat).addon_trigger_target == f'{P}漢堡'


def test_category_rule_kept_while_other_items_remain(app, admin_client):
    a = make_item(app, f'{P}甲', category=f'{P}保留分類')
    make_item(app, f'{P}乙', category=f'{P}保留分類')
    rule = make_item(app, f'{P}保留加購', price=40, can_be_add_on=True, add_on_price=20,
                     addon_trigger_type='category', addon_trigger_target=f'{P}保留分類')
    form = dict(name=f'{P}甲', category=f'{P}新分類', price='100', stock='100', total_stock='100',
                loaded_stock='100', loaded_total_stock='100')
    admin_client.post(f'/admin/edit/{a}', data=form)
    assert _get(app, rule).addon_trigger_target == f'{P}保留分類'


# ---------------------------------------------------------------- R2-105
def test_zero_price_item_can_be_reward(app):
    cup_id, priced_id = make_item(app, f'{P}環保杯', price=0), make_item(app, f'{P}有價品', price=100)
    with tenant_request(app, default_tenant_id(app)):
        cup = db.session.get(MenuItem, cup_id)
        menu_service.set_item_reward_status(cup, True, 200, 0)
        assert cup.is_reward and cup.reward_points == 200
        with pytest.raises(AppError, match='不得高於原單價'):
            menu_service.set_item_reward_status(db.session.get(MenuItem, priced_id), True, 200, 0)
        db.session.rollback()
