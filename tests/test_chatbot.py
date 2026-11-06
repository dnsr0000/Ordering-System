"""AI 客服機器人：過敏原純檢索、訂單進度、FAQ / 營業資訊、菜單查詢、LLM 備援與關閉、API 與頁面嵌入"""
from app.services import ai_service, chatbot_service
from tests.support import checkout, guest_client, line, make_item


def _setup(app, store_settings, **extra):
    store_settings('chatbot', enabled=True, bot_name='阿福', welcome='歡迎！', store_info='每日 11:00–21:00，週一公休。電話 06-1234567',
                   faq='停車 車位 => 門口有 3 個機車位\nwifi 無線 => 密碼在收據上', llm_enabled=True, **extra)


def test_allergen_and_menu_answers_are_retrieval_only(app, store_settings, monkeypatch):
    _setup(app, store_settings)
    make_item(app, '客服蝦仁炒飯', price=120, category='客服主餐', allergens='shellfish,egg', spicy_level=1, stock=10)
    make_item(app, '客服蔬菜咖哩', price=110, category='客服主餐', diet='vegan', is_recommended=True, stock=10)
    make_item(app, '客服拿鐵', price=60, category='客服飲料', allergens='milk', stock=10, description='濃郁奶香')
    called = []
    monkeypatch.setattr(ai_service, 'generate_text', lambda *a, **k: (called.append(a) or ('AI 亂講', 'gemini')))
    with app.test_request_context('/'):
        from app.models.tenant import Tenant
        from app.tenant_context import _use_tenant
        _use_tenant(Tenant.query.filter_by(slug='default').first(), remember=False)
        r = chatbot_service.answer('客服蝦仁炒飯有什麼過敏原？')
        assert r['source'] == 'retrieval' and '甲殼類' in r['answer'] and '蛋' in r['answer'] and '櫃檯' in r['answer']
        r = chatbot_service.answer('炒飯有什麼過敏原')
        assert r['source'] == 'retrieval' and '客服蝦仁炒飯' in r['answer'] and '甲殼類' in r['answer'], '部分品名也能對到'
        r = chatbot_service.answer('有什麼過敏原要注意？')
        assert r['source'] == 'retrieval' and '甲殼類' in r['answer'] and '餐點名稱' in r['answer']
        r = chatbot_service.answer('我對牛奶過敏，哪些不能吃？')
        assert r['source'] == 'retrieval' and '客服拿鐵' in r['answer'].split('不含')[0]
        r = chatbot_service.answer('有素食嗎')
        assert '客服蔬菜咖哩' in r['answer'] and '全素' in r['answer']
        r = chatbot_service.answer('客服拿鐵多少錢')
        assert r['source'] == 'retrieval' and '$60' in r['answer']
        r = chatbot_service.answer('有什麼推薦的？')
        assert '客服蔬菜咖哩' in r['answer']
        r = chatbot_service.answer('客服飲料有哪些')
        assert '客服拿鐵' in r['answer']
        r = chatbot_service.answer('你們幾點打烊')
        assert r['source'] == 'faq' and '21:00' in r['answer']
        r = chatbot_service.answer('請問有車位嗎')
        assert r['source'] == 'faq' and '機車位' in r['answer']
        r = chatbot_service.answer('你好')
        assert '阿福' in r['answer']
        assert called == [], '以上都不該呼叫 LLM'
        r = chatbot_service.answer('你們的牛肉是哪裡來的？')
        assert r['source'] == 'llm' and r['answer'] == 'AI 亂講' and len(called) == 1
        assert '客服蝦仁炒飯' in called[0][0] and '11:00' in called[0][0], 'LLM 提示包含菜單與店家資訊'
        monkeypatch.setattr(ai_service, 'generate_text', lambda *a, **k: (None, ''))
        r = chatbot_service.answer('你們的牛肉是哪裡來的？')
        assert r['source'] == 'fallback' and '櫃檯' in r['answer']
    store_settings('chatbot', enabled=True, llm_enabled=False, fallback='請打電話問我們')
    with app.test_request_context('/'):
        _use_tenant(Tenant.query.filter_by(slug='default').first(), remember=False)
        r = chatbot_service.answer('牛肉哪裡來')
        assert (r['answer'], r['source']) == ('請打電話問我們', 'fallback') and r['handoff'] is True and r['did_you_mean']


def test_chat_api_order_status_and_page_embed(app, store_settings, staff_client, monkeypatch):
    _setup(app, store_settings)
    monkeypatch.setattr(ai_service, 'generate_text', lambda *a, **k: (None, ''))
    item = make_item(app, '客服便當', price=100, stock=10)
    c = guest_client(app)
    page = c.get('/').get_data(as_text=True)
    assert 'id="chatbot"' in page and 'data-bot-name="阿福"' in page and 'js/chatbot.js' in page
    r = c.post('/api/chat', json={'message': '客服便當多少錢', 'history': []})
    assert r.status_code == 200 and '$100' in r.get_json()['answer']

    order = checkout(c, [line(item)], cash_received=1000).get_json()
    uuid = order['order_uuid']
    track = c.get(f'/track/{uuid}').get_data(as_text=True)
    assert f'data-order-uuid="{uuid}"' in track and '我的餐點好了嗎' in track
    r = c.post('/api/chat', json={'message': '我的餐點好了嗎？', 'order_uuid': uuid}).get_json()
    assert r['source'] == 'retrieval' and '製作中' in r['answer']
    staff_client.post(f"/api/orders/{order['order_id']}/status", json={'status': 'Completed'})
    r = c.post('/api/chat', json={'message': '好了嗎', 'order_uuid': uuid}).get_json()
    assert '已完成' in r['answer'] and r['suggestions'][0] == '我的餐點好了嗎？'
    assert c.post('/api/chat', json={'message': ''}).get_json()['source'] == 'fallback'

    store_settings('chatbot', enabled=False)
    assert c.post('/api/chat', json={'message': 'hi'}).status_code == 404
    assert 'id="chatbot"' not in c.get('/').get_data(as_text=True)
    assert 'id="chatbot"' not in c.get(f'/track/{uuid}').get_data(as_text=True)
