"""碳足跡標示 (參考值)：原料產地 / 排放係數、依配方自動計算、係數變更重算、手動填寫、點餐頁與 API 顯示、開關"""
from app.extensions import db
from app.models.inventory import Ingredient
from app.models.menu import MenuItem
from tests.support import guest_client, make_item


def _ing(app, name):
    with app.app_context():
        i = Ingredient.query.filter_by(name=name).first()
        return None if i is None else {'id': i.id, 'origin': i.origin, 'factor': i.carbon_factor}


def _item(app, iid):
    with app.app_context():
        i = db.session.get(MenuItem, iid)
        return {'carbon': i.carbon_grams, 'auto': i.carbon_auto, 'label': i.carbon_label, 'dict': i.to_dict()['carbon']}


def test_carbon_from_recipe_and_manual(app, admin_client, store_settings):
    store_settings('inventory', enabled=True)
    admin_client.post('/admin/inventory', data={'action': 'add', 'name': '碳牛肉', 'unit': 'g', 'stock': '5000',
                                                'unit_cost': '0.5', 'origin': '澳洲', 'carbon_factor': '60'})
    admin_client.post('/admin/inventory', data={'action': 'add', 'name': '碳麵條', 'unit': '份', 'stock': '100',
                                                'unit_cost': '8', 'carbon_factor': '150'})
    beef, noodle = _ing(app, '碳牛肉'), _ing(app, '碳麵條')
    assert beef['origin'] == '澳洲' and beef['factor'] == 60 and noodle['factor'] == 150

    iid = make_item(app, '碳足跡牛肉麵', price=180, stock=100)
    assert _item(app, iid)['carbon'] == 0
    admin_client.post('/admin/inventory', data={'action': 'recipe', 'menu_item_id': iid,
                                                'ingredient_id': [beef['id'], noodle['id']], 'quantity': ['150', '1']})
    info = _item(app, iid)
    assert info['carbon'] == 150 * 60 + 150 == 9150 and info['auto'] is True
    assert info['label'] == '9.15 kg CO₂e' and info['dict'] == 9150
    page = admin_client.get('/admin/inventory').get_data(as_text=True)
    assert '9.15 kg CO₂e' in page and '碳排係數' in page

    # 係數變更 → 用到的餐點重算；產地可編輯
    admin_client.post('/admin/inventory', data={'action': 'edit', 'ingredient_id': beef['id'], 'name': '碳牛肉', 'unit': 'g',
                                                'low_threshold': '0', 'unit_cost': '0.5', 'is_active': '1',
                                                'origin': '台灣 雲林', 'carbon_factor': '20'})
    assert _ing(app, '碳牛肉') == {'id': beef['id'], 'origin': '台灣 雲林', 'factor': 20}
    assert _item(app, iid)['carbon'] == 150 * 20 + 150 == 3150

    # 菜單進階設定：改為手動填寫，之後重算不覆蓋；再切回依配方就重算
    base = {'name': '碳足跡牛肉麵', 'avail_days': ['1', '2', '3', '4', '5', '6', '7'], 'action': 'item', 'item_id': iid}
    admin_client.post('/admin/menu', data=dict(base, carbon_grams='500'))
    info = _item(app, iid)
    assert (info['carbon'], info['auto']) == (500, False)
    admin_client.post('/admin/inventory', data={'action': 'carbon_refresh'})
    assert _item(app, iid)['carbon'] == 500
    pro_page = admin_client.get('/admin/menu').get_data(as_text=True)
    assert f'carbon-auto-{iid}' in pro_page and 'value="500"' in pro_page
    admin_client.post('/admin/menu', data=dict(base, carbon_grams='999', carbon_auto='1'))
    info = _item(app, iid)
    assert (info['carbon'], info['auto']) == (3150, True)

    # 點餐頁：標示參考值；關閉顯示後不出現
    c = guest_client(app)
    page = c.get('/').get_data(as_text=True)
    assert '3.15 kg CO₂e' in page and 'carbon-badge' in page and '"showCarbon": true' in page
    store_settings('menu', show_carbon=False)
    page = c.get('/').get_data(as_text=True)
    assert 'carbon-badge' not in page and '"showCarbon": false' in page


def test_carbon_label_formats():
    from app.services.carbon_service import label
    assert label(0) == '' and label(None) == ''
    assert label(350) == '350 g CO₂e'
    assert label(1000) == '1 kg CO₂e'
    assert label(1250) == '1.25 kg CO₂e'
    assert label(1200) == '1.2 kg CO₂e'
