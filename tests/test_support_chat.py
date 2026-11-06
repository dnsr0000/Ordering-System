"""客服助手：Gemini 503 立即改用本機模型、只回答本店相關問題 (你是否要問)、清除 Markdown、轉接真人客服與後台回覆"""
import time

import requests

from app.models.tenant import Tenant
from app.services import ai_service, chatbot_service
from app.tenant_context import _use_tenant
from tests.support import login_staff, make_item, other_shop_owner_client
from tests.test_ai_service import _Resp, gemini  # noqa: F401  (fixture)


def _setup(app, store_settings, **extra):
    store_settings('chatbot', enabled=True, bot_name='阿福', welcome='歡迎！', store_info='每日 11:00–21:00',
                   faq='停車 車位 => 門口有機車位', llm_enabled=True, **extra)


def _in_shop(app):
    ctx = app.test_request_context('/')
    ctx.push()
    _use_tenant(Tenant.query.filter_by(slug='default').first(), remember=False)
    return ctx


# ---------------------------------------------------------------- 503 → 本機模型
def test_interactive_503_switches_to_local_model_without_waiting(app, gemini, monkeypatch):  # noqa: F811
    calls = gemini([_Resp(503, {'error': {'message': 'overloaded'}})])
    slept, local = [], []
    monkeypatch.setattr(ai_service.time, 'sleep', lambda s: slept.append(s))
    monkeypatch.setattr(ai_service, 'run_local_slm', lambda prompt, **kw: local.append(kw) or '本機回答')
    with app.app_context():
        assert ai_service.generate_text('牛肉產地？', interactive=True) == ('本機回答', 'local')
        assert calls == ['gemini-main'], '即時對話不重試、不換備援模型'
        assert slept == [], '不等待重試'
        assert ai_service._gemini_cooldown_until > time.time(), '進入冷卻：接下來的問題直接由本機模型回答'
        assert local[0]['lock_timeout'] >= 10
        assert ai_service.generate_text('第二題', interactive=True) == ('本機回答', 'local')
        assert calls == ['gemini-main'], '冷卻期間不再呼叫 Gemini'


def test_interactive_timeout_also_switches_immediately(app, gemini, monkeypatch):  # noqa: F811
    calls = gemini([requests.Timeout('slow')])
    monkeypatch.setattr(ai_service, 'run_local_slm', lambda prompt, **kw: '本機回答')
    with app.app_context():
        assert ai_service.generate_text('hi', interactive=True) == ('本機回答', 'local') and calls == ['gemini-main']


def test_local_model_warmup_runs_once_in_background(app, monkeypatch):
    loads = []
    monkeypatch.setattr(ai_service, '_local_llm_loaded', False)
    monkeypatch.setattr(ai_service, '_load_local_llm', lambda: loads.append(1))
    monkeypatch.setitem(app.config, 'LOCAL_MODEL_PATH', '/models/qwen.gguf')
    with app.app_context():
        ai_service.warm_local_llm_async().join(5)
    assert loads == [1]
    monkeypatch.setitem(app.config, 'LOCAL_MODEL_PATH', '')
    with app.app_context():
        assert ai_service.warm_local_llm_async() is None, '沒設定本機模型時不做事'


# ---------------------------------------------------------------- 只回答本店相關 / 你是否要問 / Markdown
def test_clean_llm_text_removes_markdown_and_detects_off_topic():
    assert chatbot_service.clean_llm_text('**招牌**是 `牛肉麵`\n\n- 辣度可選\n# 備註') == '招牌是 牛肉麵\n辣度可選\n備註'
    assert chatbot_service.clean_llm_text('回答：好的') == '好的'
    assert chatbot_service.clean_llm_text(' off topic ') == chatbot_service.OFF_TOPIC_MARK
    assert chatbot_service.clean_llm_text('*') is None and chatbot_service.clean_llm_text(None) is None


def test_off_topic_questions_get_refusal_did_you_mean_and_handoff(app, store_settings, monkeypatch):
    _setup(app, store_settings)
    make_item(app, '客服烏龍綠', price=45, category='客服茶飲', stock=10)
    replies = []
    monkeypatch.setattr(ai_service, 'generate_text', lambda prompt, **kw: (replies.append((prompt, kw)) or ('OFF_TOPIC', 'gemini')))
    ctx = _in_shop(app)
    try:
        r = chatbot_service.answer('今天台北天氣如何？')
        assert r['source'] == 'off_topic' and '只能回答' in r['answer'] and r['handoff'] is True
        assert len(r['did_you_mean']) == 3 and all(q.endswith('？') for q in r['did_you_mean'])
        assert 'OFF_TOPIC' in replies[0][0] and replies[0][1]['interactive'] is True, '提示要求無關問題回標記、即時模式'
        dym = chatbot_service.did_you_mean('客服烏龍綠的作者是誰', chatbot_service._items(), chatbot_service.config())
        assert dym[:2] == ['客服烏龍綠多少錢？', '客服烏龍綠有什麼過敏原？'], '提到的餐點優先'
        assert chatbot_service.did_you_mean('停車要錢嗎', [], chatbot_service.config())[0] == '停車？', '常見問答的關鍵字也是候選'
        assert all(chatbot_service.answer(q)['source'] in ('retrieval', 'faq') for q in dym), '「你是否要問」的問題都能直接回答'
        # 要求真人：不經 AI，直接提示轉接
        before = len(replies)
        r = chatbot_service.answer('我要找真人客服')
        assert r['handoff'] is True and '轉接真人客服' in r['answer'] and len(replies) == before
        assert '請按下方' not in chatbot_service.answer('幫我找人氣餐點')['answer'], '「找人氣」不是要求真人'
        assert '請按下方' not in chatbot_service.answer('有人工色素嗎')['answer'], '「人工色素」不是要求真人'
        # 一般 AI 回答：清掉 Markdown，不附你是否要問
        monkeypatch.setattr(ai_service, 'generate_text', lambda prompt, **kw: ('**有的**，可以外帶', 'local'))
        r = chatbot_service.answer('可以外帶嗎')
        assert (r['source'], r['answer'], r['did_you_mean'], r['handoff']) == ('llm', '有的，可以外帶', [], False)
    finally:
        ctx.pop()
    store_settings('chatbot', enabled=True, handoff_enabled=False, off_topic='只聊吃的喔')
    monkeypatch.setattr(ai_service, 'generate_text', lambda prompt, **kw: ('OFF_TOPIC', 'gemini'))
    ctx = _in_shop(app)
    try:
        r = chatbot_service.answer('講個笑話')
        assert r['answer'] == '只聊吃的喔' and r['handoff'] is False
        assert chatbot_service.answer('我要找真人客服')['handoff'] is False, '關閉轉接時不提示'
    finally:
        ctx.pop()


# ---------------------------------------------------------------- 轉接真人客服
def _customer(app):
    c = app.test_client()
    c.get('/?shop=default')
    return c


def test_handoff_conversation_between_customer_and_staff(app, store_settings, admin_client):
    _setup(app, store_settings, handoff_enabled=True)
    c = _customer(app)
    history = [{'role': 'user', 'text': '牛肉哪裡來？'}, {'role': 'bot', 'text': '這個問題我不太確定'}]
    r = c.post('/api/chat/handoff', json={'history': history, 'message': '想問過敏'})
    assert r.status_code == 200, r.get_json()
    data = r.get_json()
    token = data['token']
    assert [m['sender'] for m in data['messages']] == ['customer', 'bot', 'customer'], '系統紀錄不給顧客看'
    assert all('staff_name' not in m for m in data['messages'])
    last = data['messages'][-1]['id']

    # 後台：清單看得到待回覆、未讀數；打開看到完整紀錄 (含系統紀錄)
    lst = admin_client.get('/api/admin/support/chats').get_json()
    chat = next(x for x in lst['chats'] if x['customer'] == '訪客' and x['status'] == 'waiting')
    assert lst['waiting'] >= 1 and chat['unread'] == 2
    detail = admin_client.get(f"/api/admin/support/chats/{chat['id']}").get_json()
    assert '顧客要求轉接真人客服' in [m['text'] for m in detail['messages']]
    assert admin_client.get('/api/admin/support/chats').get_json()['chats'][0]['unread'] == 0, '打開後已讀'

    r = admin_client.post(f"/api/admin/support/chats/{chat['id']}/reply", json={'text': '您好，我是店長，請問哪一道？'})
    assert r.status_code == 200 and r.get_json()['chat']['status'] == 'open'
    poll = c.get(f'/api/chat/handoff/{token}?after={last}').get_json()
    assert poll['status'] == 'open' and [m['text'] for m in poll['messages']] == ['您好，我是店長，請問哪一道？']
    assert 'staff_name' not in poll['messages'][0], '顧客端不顯示店員帳號'

    r = c.post(f'/api/chat/handoff/{token}/message', json={'text': '牛肉麵'})
    assert r.status_code == 200 and r.get_json()['status'] == 'waiting', '顧客回話後回到待回覆'
    assert c.post(f'/api/chat/handoff/{token}/message', json={'text': '  '}).status_code == 400

    # 店員結束 → 顧客端看到 closed，之後不能再傳
    assert admin_client.post(f"/api/admin/support/chats/{chat['id']}/close").status_code == 200
    assert c.get(f'/api/chat/handoff/{token}').get_json()['status'] == 'closed'
    assert c.post(f'/api/chat/handoff/{token}/message', json={'text': '還在嗎'}).status_code == 409
    assert admin_client.post(f"/api/admin/support/chats/{chat['id']}/reply", json={'text': 'x'}).status_code == 409
    closed = admin_client.get('/api/admin/support/chats').get_json()['chats']
    assert chat['id'] not in [x['id'] for x in closed], '預設不列出已結束'
    assert chat['id'] in [x['id'] for x in admin_client.get('/api/admin/support/chats?closed=1').get_json()['chats']]


def test_handoff_access_control(app, store_settings, admin_client):
    _setup(app, store_settings, handoff_enabled=True)
    c = _customer(app)
    token = c.post('/api/chat/handoff', json={'history': []}).get_json()['token']
    chat_id = admin_client.get('/api/admin/support/chats').get_json()['chats'][0]['id']
    assert c.get('/api/chat/handoff/not-a-real-token-000').status_code == 404
    # 對話憑證只在建立它的瀏覽器有效：別人拿到憑證也讀不到、寫不進去
    stranger = _customer(app)
    assert stranger.get(f'/api/chat/handoff/{token}').status_code == 404
    assert stranger.post(f'/api/chat/handoff/{token}/message', json={'text': 'hi'}).status_code == 404
    # 共用點餐機：顧客登出 (或閒置登出) 後，下一位顧客接續不了前一位的對話
    kiosk = _customer(app)
    kiosk_token = kiosk.post('/api/chat/handoff', json={'history': []}).get_json()['token']
    assert kiosk.get(f'/api/chat/handoff/{kiosk_token}').status_code == 200
    kiosk.post('/logout')
    assert kiosk.get(f'/api/chat/handoff/{kiosk_token}').status_code == 404
    # 顧客自己結束
    assert c.post(f'/api/chat/handoff/{token}/close').status_code == 200
    assert c.get(f'/api/chat/handoff/{token}').get_json()['status'] == 'closed'
    # 其他店家的負責人看不到；廚房帳號沒有權限
    other = other_shop_owner_client(app)
    assert other.get(f'/api/admin/support/chats/{chat_id}').status_code == 404
    assert chat_id not in [x['id'] for x in other.get('/api/admin/support/chats?closed=1').get_json()['chats']]
    kitchen = login_staff(app, 'support_kitchen', 'kitchen')
    assert kitchen.get('/api/admin/support/chats').status_code in (302, 403)
    # 後台頁面與側欄
    page = admin_client.get('/admin/support').get_data(as_text=True)
    assert 'id="supportList"' in page and 'js/admin-support.js' in page and 'href="/admin/support"' in page
    # 關閉轉接：顧客端不能建立，對話窗不顯示按鈕
    store_settings('chatbot', enabled=True, handoff_enabled=False)
    assert _customer(app).post('/api/chat/handoff', json={}).status_code == 404
    assert c.get(f'/api/chat/handoff/{token}').status_code == 404


def test_member_deletion_removes_their_support_chats(app, store_settings):
    """會員刪除帳號時，客服對話 (含姓名與對話內容) 一併刪除"""
    from app.extensions import db
    from app.models.support import SupportChat, SupportMessage
    from app.models.user import User
    from app.services import privacy_service, support_service
    from tests.support import make_member
    uid = make_member(app, '客服刪除會員', '0977300001')
    ctx = _in_shop(app)
    try:
        user = db.session.get(User, uid)
        chat = support_service.start(user.tenant_id, [{'role': 'user', 'text': '我的地址是…'}], user=user)
        db.session.commit()
        chat_id = chat.id
        assert chat.customer_label == '客服刪除會員'
        privacy_service.delete_member_account(user)
        db.session.commit()
        assert db.session.get(SupportChat, chat_id) is None
        assert SupportMessage.query.filter_by(chat_id=chat_id).count() == 0
    finally:
        ctx.pop()
