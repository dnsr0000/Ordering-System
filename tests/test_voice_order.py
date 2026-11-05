"""語音點餐：規則解析 (數量 / 規格 / 甜度冰塊 / 辣度 / 加料 / 部分品名)、LLM 備援與模糊比對、API 與開關"""
from types import SimpleNamespace

from app.services import ai_service, voice_order_service as voice
from tests.support import guest_client, make_item


def _item(id, name, mods='none', sizes=(), en=''):
    return SimpleNamespace(id=id, name=name, modifiers=mods, sizes=list(sizes), name_en=en, is_available=True,
                           service_status=lambda: (True, ''))


ITEMS = [_item(1, '珍珠奶茶', 'ice_sugar', [('中杯', 0), ('大杯', 15)]), _item(2, '奶茶', 'ice_sugar'), _item(3, '薯條', 'spicy'),
         _item(4, '炙燒起司牛肉堡', 'addons', en='Beef Burger'), _item(5, '紅茶', 'ice_sugar'), _item(6, '雞塊')]
ADDONS = [{'id': 7, 'name': '加蛋'}, {'id': 8, 'name': '加起司'}]


def _brief(lines):
    return [(l['name'], l['quantity'], l['size'], l['sugar'], l['ice'], l['spicy'], l['modifier_ids']) for l in lines]


def test_rule_parse_quantity_size_and_modifiers():
    lines, left = voice.rule_parse('我要一份大杯珍珠奶茶，半糖少冰，再一份薯條', ITEMS, ADDONS)
    assert _brief(lines) == [('珍珠奶茶', 1, '大杯', '半糖', '少冰', '', []), ('薯條', 1, '', '', '', '', [])] and left == []
    lines, left = voice.rule_parse('兩杯紅茶去冰無糖 牛肉堡加蛋三個 雞塊 10 份', ITEMS, ADDONS)
    assert ('紅茶', 2, '', '無糖', '去冰', '', []) in _brief(lines)
    assert ('雞塊', 10, '', '', '', '', []) in _brief(lines)
    burger = next(l for l in lines if l['name'] == '炙燒起司牛肉堡')
    assert burger['modifier_ids'] == [7] and burger['modifier_names'] == ['加蛋'], '只說「牛肉堡」也對到唯一含該字的品名'
    assert left == []
    lines, _ = voice.rule_parse('珍珠奶茶兩杯 要熱的', ITEMS, ADDONS)
    assert _brief(lines) == [('珍珠奶茶', 2, '中杯', '', '熱飲', '', [])], '沒說規格用第一個規格'
    lines, _ = voice.rule_parse('薯條 小辣 ２份', ITEMS, ADDONS)
    assert _brief(lines) == [('薯條', 2, '', '', '', '微辣', [])], '全形數字與口語辣度'
    lines, _ = voice.rule_parse('beef burger', ITEMS, ADDONS)
    assert lines[0]['name'] == '炙燒起司牛肉堡', '英文名也可以'
    lines, left = voice.rule_parse('來份雞排', ITEMS, ADDONS)
    assert lines == [] and left == ['雞排']
    assert voice._to_int('二十三') == 23 and voice._to_int('十') == 10 and voice._to_int('12') == 12 and voice._to_int('x') is None


def test_llm_fallback_only_when_rules_find_nothing(app, monkeypatch):
    calls = []

    def fake_json(prompt, **kw):
        calls.append(prompt)
        return [{'name': '珍珠奶荼', 'qty': 2, 'size': '大杯', 'sugar': '微糖', 'ice': '去冰'}, {'name': '火星漢堡', 'qty': 1},
                'junk', {'name': '炙燒起司牛肉堡', 'qty': '三', 'addons': ['加起司']}], 'gemini'
    monkeypatch.setattr(ai_service, 'generate_json', fake_json)
    with app.app_context():
        res = voice.parse('一份雞排', ITEMS, ADDONS)
    assert res['source'] == 'gemini' and len(calls) == 1 and '珍珠奶茶' in calls[0] and '一份雞排' in calls[0]
    assert _brief(res['lines']) == [('珍珠奶茶', 2, '大杯', '微糖', '去冰', '', []), ('炙燒起司牛肉堡', 3, '', '', '', '', [8])], \
        '錯字以模糊比對對回菜單；菜單沒有的略過'
    assert res['unmatched'] == ['火星漢堡']
    with app.app_context():
        res = voice.parse('一份薯條', ITEMS, ADDONS)
    assert res['source'] == 'rules' and len(calls) == 1, '規則命中就不叫 LLM'
    monkeypatch.setattr(ai_service, 'generate_json', lambda *a, **k: (None, ''))
    with app.app_context():
        res = voice.parse('來份雞排', ITEMS, ADDONS)
    assert res['lines'] == [] and res['source'] == 'none'
    with app.app_context():
        assert voice.parse('   ', ITEMS, ADDONS)['source'] == 'none'
        assert voice.parse('來份雞排', ITEMS, ADDONS, use_llm=False)['unmatched'] == ['雞排']


def test_voice_api_and_setting(app, store_settings, monkeypatch):
    make_item(app, '語音測試拿鐵', price=80, modifiers='ice_sugar', size_options='中杯:0, 大杯:15', stock=10)
    sold = make_item(app, '語音測試可頌', price=60, stock=0, is_sold_out=True)
    monkeypatch.setattr(ai_service, 'generate_json', lambda *a, **k: (None, ''))
    c = guest_client(app)
    page = c.get('/').get_data(as_text=True)
    assert 'id="voiceBtn"' in page and 'id="voiceModal"' in page and '"voiceOrdering": true' in page
    r = c.post('/api/voice/parse', json={'text': '兩杯大杯語音測試拿鐵微糖去冰，一個語音測試可頌'})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['source'] == 'rules'
    latte = next(l for l in body['lines'] if l['name'] == '語音測試拿鐵')
    assert (latte['quantity'], latte['size'], latte['sugar'], latte['ice'], latte['available']) == (2, '大杯', '微糖', '去冰', True)
    croissant = next(l for l in body['lines'] if l['menu_item_id'] == sold)
    assert croissant['available'] is False, '售完的仍列出但標示不可供應'
    assert c.post('/api/voice/parse', json={'text': ''}).status_code == 400
    store_settings('menu', voice_ordering=False)
    assert c.post('/api/voice/parse', json={'text': '拿鐵'}).status_code == 404
    page = c.get('/').get_data(as_text=True)
    assert 'id="voiceBtn"' not in page and '"voiceOrdering": false' in page
