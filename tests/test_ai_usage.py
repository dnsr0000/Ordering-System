"""AI 優化：依用途分快慢模型、最近常失敗的模型排後面、用量 / 估計費用 / 每月預算、每組金鑰各自冷卻、
多行程共用狀態 (Redis)、翻譯批次並行與沿用店家譯名、Claude 提示快取"""
import json
import time
from types import SimpleNamespace

from app.services import ai_service, ai_usage, llm_providers as lp
from tests.support import default_tenant_id, login_staff


def _resolved(provider, models, fast=()):
    return lp.Resolved(provider=provider, models=list(models), fast=list(fast), at=time.time())


# ---------------------------------------------------------------- 依用途分兩檔
def test_pick_fast_tier_per_provider():
    gem = [{'id': i, 'created': 0} for i in ('gemini-3.8-flash', 'gemini-3.5-flash-lite', 'gemini-3.1-flash-lite', 'gemini-3.8-flash-tts')]
    assert lp.pick_fast('gemini', gem, ['gemini-3.8-flash', 'gemini-3.5-flash-lite']) == ['gemini-3.5-flash-lite', 'gemini-3.8-flash']
    claude = [{'id': 'claude-opus-5-5', 'created': 300}, {'id': 'claude-sonnet-5-5', 'created': 250},
              {'id': 'claude-haiku-4-5', 'created': 100}, {'id': 'claude-haiku-5-5', 'created': 260}]
    assert lp.pick_fast('anthropic', claude, ['claude-opus-5-5', 'claude-sonnet-5-5']) == ['claude-haiku-5-5', 'claude-sonnet-5-5']
    gpt = [{'id': i, 'created': 0} for i in ('gpt-5.2', 'gpt-5.2-mini', 'gpt-5.2-nano')]
    assert lp.pick_fast('openai', gpt, ['gpt-5.2-mini', 'gpt-5.2']) == ['gpt-5.2-nano', 'gpt-5.2-mini']
    assert lp.pick_fast('deepseek', [{'id': 'deepseek-chat', 'created': 0}], ['deepseek-chat']) == ['deepseek-chat']
    r = _resolved('anthropic', ['claude-opus-5-5'])
    assert r.for_tier('fast') == ['claude-opus-5-5'], '沒有快速檔 (指定模型) 時沿用主力'


def test_resolve_fills_fast_tier_unless_model_pinned(monkeypatch):
    monkeypatch.setattr(lp, 'list_models', lambda p, k: [{'id': 'claude-opus-5-5', 'created': 3}, {'id': 'claude-sonnet-5-5', 'created': 2},
                                                         {'id': 'claude-haiku-5-5', 'created': 1}])
    auto = lp.resolve(lp.Credential(key='sk-ant-auto', source='settings:1'))
    assert auto.models == ['claude-opus-5-5', 'claude-sonnet-5-5'] and auto.fast == ['claude-haiku-5-5', 'claude-sonnet-5-5']
    pinned = lp.resolve(lp.Credential(key='sk-ant-pinned', source='settings:1', model='claude-sonnet-5-5'))
    assert pinned.for_tier('fast') == pinned.for_tier('strong') == ['claude-sonnet-5-5']


def test_interactive_and_short_jobs_use_fast_tier(app, monkeypatch):
    cred = lp.Credential(key='sk-ant-1', source='settings:1')
    monkeypatch.setattr(lp, 'resolve', lambda c, force=False: _resolved('anthropic', ['claude-opus-5-5'], ['claude-haiku-5-5']))
    used = []
    monkeypatch.setattr(lp, 'call_anthropic', lambda key, model, *a, **k: used.append(model) or '好')
    with app.app_context():
        ai_service.cloud_generate('hi', creds=[cred], interactive=True)
        ai_service.cloud_generate('hi', creds=[cred])
        ai_service.cloud_generate('hi', creds=[cred], tier='fast')
    assert used == ['claude-haiku-5-5', 'claude-opus-5-5', 'claude-haiku-5-5'], '即時對話 / 短文案用快速檔，其他用主力'


# ---------------------------------------------------------------- 模型健康
def test_recently_failing_model_moves_to_the_back_then_recovers(app, monkeypatch):
    monkeypatch.setattr(ai_service, 'RETRY_DELAY', 0)
    cred = lp.Credential(key='sk-proj-1', source='settings:1')
    monkeypatch.setattr(lp, 'resolve', lambda c, force=False: _resolved('openai', ['gpt-a', 'gpt-b']))
    tried = []

    def call(provider, key, model, *a, **k):
        tried.append(model)
        if model == 'gpt-a':
            raise lp.ProviderError('overload', '503')
        return 'ok'
    monkeypatch.setattr(lp, 'call_openai_compatible', call)
    with app.app_context():
        assert ai_service.cloud_generate('hi', creds=[cred])[0] == 'ok'
        assert tried == ['gpt-a', 'gpt-a', 'gpt-b']
        assert ai_usage.unhealthy(cred.id, 'gpt-a') and not ai_usage.unhealthy(cred.id, 'gpt-b')
        tried.clear()
        assert ai_service.cloud_generate('hi', creds=[cred])[0] == 'ok'
        assert tried == ['gpt-b'], '最近常失敗的 gpt-a 排到後面：直接用 gpt-b'
        # 10 分鐘後舊的失敗不算數：回到原本的順序
        later = time.time() + ai_usage.HEALTH_WINDOW + 5
        monkeypatch.setattr(ai_usage.time, 'time', lambda: later)
        assert ai_usage.order_models(cred.id, ['gpt-a', 'gpt-b']) == ['gpt-a', 'gpt-b']


# ---------------------------------------------------------------- 每組金鑰各自冷卻
def test_gemini_cooldown_is_per_key(app, monkeypatch):
    first = lp.Credential(key='AIza-first', source='settings:1')
    second = lp.Credential(key='AIza-second', source='settings:2')
    monkeypatch.setattr(lp, 'resolve', lambda c, force=False: _resolved('gemini', ['gemini-x']))
    ai_service._set_cooldown(first.id, 60)
    calls = []

    class Resp:   # 只假造最底層的 HTTP 請求：冷卻判斷走真正的 gemini_generate
        status_code = 200

        @staticmethod
        def json():
            return {'candidates': [{'content': {'parts': [{'text': '第二組回答'}]}}]}

    def fake_post(url, headers=None, **kw):
        calls.append(headers['x-goog-api-key'])
        return Resp()
    monkeypatch.setattr(ai_service.requests, 'post', fake_post)
    with app.app_context():
        assert ai_service.cloud_generate('hi', creds=[first, second]) == ('第二組回答', 'gemini'), '第一組冷卻不影響第二組'
    assert calls == ['AIza-second'], '冷卻中的第一組金鑰不發出請求，只有第二組送出'


# ---------------------------------------------------------------- 用量、費用、預算
def test_usage_is_recorded_flushed_and_budget_stops_cloud_calls(app, store_settings, monkeypatch):
    tid = default_tenant_id(app)
    store_settings('ai', key1='sk-ant-budget', monthly_budget_usd=1)
    cred = lp.Credential(key='sk-ant-budget', source='settings:1')
    monkeypatch.setattr(lp, 'resolve', lambda c, force=False: _resolved('anthropic', ['claude-opus-5-5']))
    monkeypatch.setattr(lp, 'call_anthropic', lambda *a, **k: lp.Reply.of('好', {'input': 100_000, 'output': 20_000,
                                                                                   'cache_read': 50_000}))
    with app.app_context():
        assert ai_service.cloud_generate('hi', creds=[cred], tenant_id=tid)[0] == '好'
        # Opus 5.5：輸入 $4、輸出 $20、快取讀取 $0.20 (每百萬 token)
        expected = (100_000 * 4 + 20_000 * 20 + 50_000 * 0.2) / 1_000_000
        summary = ai_usage.month_summary(tid)
        assert summary['calls'] == 1 and abs(summary['cost'] - expected) < 1e-6 and summary['budget'] == 1
        assert ai_usage.flush() == 1, '寫入資料庫'
        stored = ai_usage.month_usage(tid)
        assert stored['anthropic|claude-opus-5-5']['input'] == 100_000 and stored['anthropic|claude-opus-5-5']['calls'] == 1
        ai_service.cloud_generate('hi', creds=[cred], tenant_id=tid)   # 累計 $1.62 ≥ 預算 $1
        assert ai_usage.month_summary(tid)['over_budget']
        assert ai_service.cloud_generate('hi', creds=[cred], tenant_id=tid) == (None, ''), '超過預算：不再呼叫雲端 AI'
        assert '預算上限' in ai_service.last_ai_error(tid)
        ai_usage.flush()
    owner = login_staff(app, 'ai_usage_owner', 'owner')
    page = owner.get('/admin/system').get_data(as_text=True)
    assert 'id="aiUsage"' in page and '已達上限' in page and 'claude-opus-5-5' in page


def test_estimate_cost_by_model_family():
    assert ai_usage.price_of('claude-haiku-5-5') == (0.10, 0.50, 0.01)
    assert ai_usage.price_of('gemini-3.5-flash-lite')[0] < ai_usage.price_of('gemini-3.8-flash')[0]
    assert ai_usage.estimate_cost('unknown-model', {'input': 1_000_000}) == 1.0


def test_gemini_usage_metadata_is_recorded(app, monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return {'candidates': [{'content': {'parts': [{'text': '想一下', 'thought': True}, {'text': '回答'}]}}],
                    'usageMetadata': {'promptTokenCount': 1200, 'cachedContentTokenCount': 1000, 'candidatesTokenCount': 30,
                                      'thoughtsTokenCount': 70}}
    sent = []
    monkeypatch.setattr(ai_service.requests, 'post', lambda url, **kw: sent.append(kw['json']) or Resp())
    with app.app_context():
        assert ai_service.gemini_generate('hi', api_key='AIza-usage', models=['gemini-3.8-flash'], prefix='菜單…') == '回答'
    assert sent[0]['systemInstruction'] == {'parts': [{'text': '菜單…'}]}, '固定前段放 systemInstruction'
    row = ai_usage.month_usage(None)['gemini|gemini-3.8-flash']
    assert (row['input'], row['cache_read'], row['output']) == (200, 1000, 100)


# ---------------------------------------------------------------- Claude 提示快取
def test_claude_prefix_is_cached_in_system(monkeypatch):
    captured = {}

    class FakeMessages:
        def create(self, **kw):
            captured.update(kw)
            usage = SimpleNamespace(input_tokens=20, output_tokens=5, cache_read_input_tokens=900, cache_creation_input_tokens=0)
            return SimpleNamespace(stop_reason='end_turn', content=[SimpleNamespace(type='text', text='答')], usage=usage)

    class FakeClient:
        def __init__(self, **kw):
            self.messages = FakeMessages()
            self.beta = SimpleNamespace(messages=FakeMessages())
    fake = SimpleNamespace(Anthropic=FakeClient, APIError=Exception)
    monkeypatch.setattr(lp, '_anthropic', lambda: fake)
    reply = lp.call_anthropic('k', 'claude-haiku-5-5', '顧客問：hi', 300, True, prefix='店家資訊與菜單')
    assert reply == '答' and reply.usage == {'input': 20, 'output': 5, 'cache_read': 900, 'cache_write': 0}
    assert captured['system'] == [{'type': 'text', 'text': '店家資訊與菜單', 'cache_control': {'type': 'ephemeral'}}]
    assert captured['messages'] == [{'role': 'user', 'content': '顧客問：hi'}]


# ---------------------------------------------------------------- 多行程共用 (Redis)
class FakeRedis:
    def __init__(self):
        self.data, self.lists = {}, {}

    def get(self, k):
        return self.data.get(k)

    def set(self, k, v, ex=None):
        self.data[k] = v

    def delete(self, k):
        self.data.pop(k, None)

    def lrange(self, k, a, b):
        return list(self.lists.get(k, []))

    def pipeline(self):
        r = self

        class P:
            def lpush(self, k, v):
                r.lists.setdefault(k, []).insert(0, v)

            def ltrim(self, k, a, b):
                r.lists[k] = r.lists[k][a:b + 1]

            def expire(self, *a):
                pass

            def execute(self):
                pass
        return P()


def test_detection_cooldown_and_health_are_shared_through_redis(app, monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(lp, '_shared', lambda: redis)
    cred = lp.Credential(key='sk-ant-shared', source='settings:1')
    # 另一個行程已經辨識過
    redis.set(f'ai:detect:{cred.id}', json.dumps({'provider': 'anthropic', 'models': ['claude-opus-5-5'], 'error': '',
                                                   'at': time.time(), 'auto': True, 'fast': ['claude-haiku-5-5']}))
    monkeypatch.setattr(lp, 'list_models', lambda *a: (_ for _ in ()).throw(AssertionError('不該再查模型清單')))
    assert lp.resolve(cred).fast == ['claude-haiku-5-5']
    # 另一個行程讓這組金鑰進入冷卻
    redis.set(f'ai:cool:{cred.id}', json.dumps(time.time() + 30))
    assert ai_service.cooldown_left(cred.id) > 20
    ai_usage.note_health(cred.id, 'claude-opus-5-5', False, 1.0)
    ai_usage._health.clear()   # 本行程的紀錄清掉，仍可從 Redis 讀到
    assert ai_usage.health(cred.id, 'claude-opus-5-5')['failures'] == 1


# ---------------------------------------------------------------- 翻譯：並行、沿用店家譯名
def test_translate_batches_run_in_parallel_and_reuse_store_glossary(app, monkeypatch):
    monkeypatch.setattr(ai_service, 'RETRY_DELAY', 0)
    monkeypatch.setattr(ai_service, 'TRANSLATE_BATCH', 2)
    monkeypatch.setattr(ai_service, 'credentials', lambda tenant_id=None: [lp.Credential(key='AIza-x', source='env:GEMINI_API_KEY')])
    prompts, threads = [], set()
    import threading

    def fake_cloud(prompt, **kw):
        prompts.append(prompt)
        threads.add(threading.current_thread().name)
        time.sleep(0.05)
        rows = [line.split('. ', 1) for line in prompt.splitlines() if line[:1].isdigit() and '. ' in line]
        return json.dumps([{'i': int(n), 'name_en': f'EN {name}', 'name_ja': f'JA {name}'} for n, name in rows]), 'gemini'
    monkeypatch.setattr(ai_service, 'cloud_generate', fake_cloud)
    glossary = {'牛肉麵': {'en': 'Beef Noodle Soup', 'ja': '牛肉麺'}}
    entries = [{'name': '牛肉麵'}, {'name': '紅燒牛肉麵'}, {'name': '滷肉飯'}, {'name': '貢丸湯'}, {'name': '燙青菜'}]
    stats = {}
    with app.app_context():
        results, source = ai_service.translate_entries(entries, stats, glossary)
    assert results[0]['name_en'] == 'Beef Noodle Soup', '同名直接沿用店家譯名'
    assert all(r['name_en'].startswith('EN ') for r in results[1:])
    assert len(prompts) == 2 and not any('1. 牛肉麵\n' in p for p in prompts), '沿用的不再送 AI；其餘 4 項分 2 批'
    assert any('牛肉麵 = Beef Noodle Soup / 牛肉麺' in p and '紅燒牛肉麵' in p for p in prompts), '含相同字詞的附上既有譯名'
    assert len(threads) == 2, '兩批同時送出'
    assert stats['ai'] == 4 and stats['store'] == 1 and stats['fallback'] == 0 and source == 'gemini'


def test_translate_menu_uses_existing_translations_as_glossary(app, monkeypatch):
    from app.extensions import db
    from app.models.menu import MenuItem
    from app.services import menu_translate_service
    from tests.support import make_item, tenant_request
    make_item(app, '譯名牛肉麵', price=100, category='譯名麵類')
    make_item(app, '譯名牛肉麵 (大)', price=120, category='譯名麵類')
    with tenant_request(app, default_tenant_id(app)):
        item = MenuItem.query.filter_by(name='譯名牛肉麵').first()
        item.name_en, item.name_ja = 'House Beef Noodles', 'ハウス牛肉麺'
        db.session.commit()
        seen = []
        monkeypatch.setattr(ai_service, 'translate_entries', lambda entries, stats, glossary: seen.append(glossary) or (
            [{'name_en': '', 'name_ja': '', 'description_en': '', 'description_ja': ''} for _ in entries], 'rules'))
        menu_translate_service.translate_menu(overwrite=False)
        menu_translate_service.translate_menu(overwrite=True)
        db.session.rollback()
    assert seen[0].get('譯名牛肉麵') == {'en': 'House Beef Noodles', 'ja': 'ハウス牛肉麺'}
    assert seen[1] == {}, '全部重翻時不沿用舊譯名'
