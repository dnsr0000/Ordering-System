"""多家 AI 服務：金鑰開頭辨識廠商、依模型清單自動挑模型 (不寫死版本)、金鑰依序備援、系統設定「AI 服務」與測試連線"""
import time

import pytest

from app.services import ai_service, llm_providers as lp
from tests.support import login_staff

# 實際查到的 Gemini 模型清單 (節錄)：有圖片 / 語音 / 預覽版，正式的 Flash 到 3.8、Flash-Lite 到 3.5
GEMINI_LIST = [{'id': i, 'created': 0} for i in (
    'gemini-flash-latest', 'gemini-flash-lite-latest', 'gemini-2.5-flash-lite', 'gemini-2.5-flash-image', 'gemini-3-flash-preview',
    'gemini-3.1-flash-lite', 'gemini-3.1-flash-image', 'gemini-3.5-flash', 'gemini-3.5-flash-lite', 'gemini-3.6-flash',
    'gemini-3.7-flash', 'gemini-3.8-flash', 'gemini-3.8-flash-tts', 'gemini-3.8-flash-lite-tts', 'gemini-omni-1.2-flash',
    'gemini-3.1-pro-preview')]


@pytest.fixture(autouse=True)
def _clean():
    lp._cache.clear()
    ai_service._cooldowns.clear()
    yield
    lp._cache.clear()
    ai_service._cooldowns.clear()


def test_key_prefix_candidates():
    assert lp.candidates('sk-ant-api03-xxxx') == ('anthropic',)
    assert lp.candidates('sk-or-v1-xxxx') == ('openrouter',)
    assert lp.candidates('gsk_xxxx') == ('groq',)
    assert lp.candidates('xai-xxxx') == ('xai',)
    assert lp.candidates('AIzaSyXXXX') == ('gemini',) and lp.candidates('AQ.Ab8RN6xxxx') == ('gemini',)
    assert lp.candidates('sk-proj-xxxx') == ('openai',)
    assert lp.candidates('sk-abcdef') == ('openai', 'deepseek'), 'sk- 開頭不唯一：依序試'
    assert lp.candidates('32charsmistralkey') == ('gemini', 'mistral')


def test_pick_models_by_family_not_hardcoded_version():
    assert lp.pick_models('gemini', GEMINI_LIST) == ['gemini-3.8-flash', 'gemini-3.5-flash-lite'], '最新正式版 Flash，備援 Flash-Lite'
    assert lp.pick_models('gemini', [{'id': 'gemini-flash-latest', 'created': 0}]) == ['gemini-flash-latest']
    claude = [{'id': 'claude-opus-5', 'created': 100}, {'id': 'claude-opus-5-5', 'created': 300},
              {'id': 'claude-sonnet-5-5', 'created': 250}, {'id': 'claude-haiku-5-5', 'created': 260}]
    assert lp.pick_models('anthropic', claude) == ['claude-opus-5-5', 'claude-sonnet-5-5'], '最新的 Opus，備援最新的 Sonnet'
    openai = [{'id': i, 'created': 0} for i in ('gpt-5-mini', 'gpt-5.2-mini', 'gpt-5.2', 'gpt-5.2-mini-tts', 'text-embedding-3', 'gpt-image-2')]
    assert lp.pick_models('openai', openai) == ['gpt-5.2-mini', 'gpt-5.2']
    assert lp.pick_models('deepseek', [{'id': 'deepseek-chat', 'created': 0}, {'id': 'deepseek-reasoner', 'created': 0}]) == ['deepseek-chat']
    groq = [{'id': 'whisper-large-v3', 'created': 50}, {'id': 'llama-4-70b', 'created': 30}, {'id': 'llama-4-8b', 'created': 40}]
    assert lp.pick_models('groq', groq) == ['llama-4-8b'], '沒有專屬規則：最新建立的對話模型 (排除語音)'


def test_resolve_ambiguous_key_and_cache(monkeypatch):
    calls = []

    def fake_list(provider, key):
        calls.append(provider)
        if provider == 'openai':
            raise lp.ProviderError('auth', 'HTTP 401 invalid key')
        return [{'id': 'deepseek-chat', 'created': 0}]
    monkeypatch.setattr(lp, 'list_models', fake_list)
    cred = lp.Credential(key='sk-1234567890abcdef', source='settings:1')
    r = lp.resolve(cred)
    assert r.ok and r.provider == 'deepseek' and r.models == ['deepseek-chat'] and r.label == 'DeepSeek' and r.auto
    assert calls == ['openai', 'deepseek']
    lp.resolve(cred)
    assert calls == ['openai', 'deepseek'], '快取：不重查'
    pinned = lp.resolve(lp.Credential(key='sk-1234567890abcdef', source='settings:1', model='deepseek-reasoner'), force=True)
    assert pinned.models == ['deepseek-reasoner'] and not pinned.auto
    monkeypatch.setattr(lp, 'list_models', lambda p, k: (_ for _ in ()).throw(lp.ProviderError('auth', 'HTTP 401')))
    bad = lp.resolve(lp.Credential(key='sk-ant-bad', source='settings:2'))
    assert not bad.ok and 'Anthropic Claude' in bad.error
    # .env 的 GEMINI_API_KEY 已知是 Gemini 且指定了模型：不查清單
    env = lp.resolve(lp.Credential(key='AQ.x', source='env:GEMINI_API_KEY', model='gemini-a,gemini-b', provider='gemini'))
    assert env.ok and env.models == ['gemini-a', 'gemini-b']


def _resolved(provider, *models):
    return lp.Resolved(provider=provider, models=list(models), at=time.time())


def test_cloud_generate_falls_back_to_next_provider(app, monkeypatch):
    monkeypatch.setattr(ai_service, 'RETRY_DELAY', 0)
    claude = lp.Credential(key='sk-ant-1', source='settings:1')
    gpt = lp.Credential(key='sk-proj-2', source='settings:2')
    table = {claude.id: _resolved('anthropic', 'claude-opus-5-5', 'claude-sonnet-5-5'), gpt.id: _resolved('openai', 'gpt-5.2-mini')}
    monkeypatch.setattr(lp, 'resolve', lambda cred, force=False: table[cred.id])
    tried = []

    def busy(key, model, prompt, max_tokens, interactive):
        tried.append(model)
        raise lp.ProviderError('overload', '529 overloaded_error')
    monkeypatch.setattr(lp, 'call_anthropic', busy)
    monkeypatch.setattr(lp, 'call_openai_compatible', lambda provider, key, model, *a: f'{provider}:{model} 回答')
    with app.app_context():
        text, source = ai_service.cloud_generate('hi', creds=[claude, gpt])
        assert (text, source) == ('openai:gpt-5.2-mini 回答', 'openai')
        assert tried == ['claude-opus-5-5', 'claude-opus-5-5', 'claude-sonnet-5-5', 'claude-sonnet-5-5'], '一般呼叫：每個模型試 2 次'
        assert ai_service._cooldowns.get(claude.id, 0) > 0, '失敗的金鑰進冷卻'
        tried.clear()
        assert ai_service.cloud_generate('hi', creds=[claude, gpt])[1] == 'openai'
        assert tried == [], '冷卻中直接跳過'
        tried.clear()
        ai_service.cloud_generate('hi', creds=[claude], interactive=False, patient=True)
        assert len(tried) == 6, '耐心模式不理會冷卻、每個模型試 3 次'
        ai_service._cooldowns.clear()
        tried.clear()
        ai_service.cloud_generate('hi', creds=[claude], interactive=True)
        assert tried == ['claude-opus-5-5'], '即時對話：只試主要模型一次'


def test_auth_error_forgets_detection(app, monkeypatch):
    cred = lp.Credential(key='sk-ant-1', source='settings:1')
    lp._cache[cred.id] = _resolved('anthropic', 'claude-opus-5-5')
    monkeypatch.setattr(lp, 'call_anthropic', lambda *a: (_ for _ in ()).throw(lp.ProviderError('auth', 'invalid x-api-key')))
    with app.app_context():
        assert ai_service.cloud_generate('hi', creds=[cred]) == (None, '')
        assert cred.id not in lp._cache, '金鑰錯誤：下次重新辨識'
        assert 'invalid x-api-key' in ai_service.last_ai_error()


def test_credentials_order_settings_then_env(app, store_settings, monkeypatch):
    from app.models.tenant import Tenant
    from app.tenant_context import _use_tenant
    from tests.support import default_tenant_id
    store_settings('ai', key1='sk-ant-first', model1='claude-sonnet-5-5', key2='gsk_second', key3='')
    monkeypatch.setitem(app.config, 'AI_API_KEYS', 'sk-proj-env, gsk_second')
    monkeypatch.setitem(app.config, 'GEMINI_API_KEY', 'AQ.gemini')
    monkeypatch.setitem(app.config, 'GEMINI_MODEL', '')
    monkeypatch.setitem(app.config, 'GEMINI_FALLBACK_MODEL', '')
    with app.test_request_context('/'):
        from app.extensions import db
        _use_tenant(db.session.get(Tenant, default_tenant_id(app)), remember=False)
        creds = ai_service.credentials()
    assert [(c.key, c.source, c.model, c.provider) for c in creds] == [
        ('sk-ant-first', 'settings:1', 'claude-sonnet-5-5', ''), ('gsk_second', 'settings:2', '', ''),
        ('sk-proj-env', 'env:AI_API_KEYS', '', ''), ('AQ.gemini', 'env:GEMINI_API_KEY', '', 'gemini')], '依序、重複的略過'
    with app.app_context():
        assert [c.source for c in ai_service.credentials()] == ['env:AI_API_KEYS', 'env:AI_API_KEYS', 'env:GEMINI_API_KEY'], \
            '沒有店家情境 (背景工作) 時只用 .env'


def test_settings_page_and_tester(app, store_settings, monkeypatch):
    store_settings('ai', key1='sk-ant-first', key2='bad-key-123456')
    owner = login_staff(app, 'ai_settings_owner', 'owner')
    page = owner.get('/admin/settings/ai').get_data(as_text=True)
    assert 'AI 服務 (金鑰自動辨識)' in page and 'name="key1"' in page and 'sk-ant-first' not in page, '金鑰不回傳到瀏覽器'

    def fake_resolve(cred, force=False):
        return _resolved('anthropic', 'claude-opus-5-5', 'claude-sonnet-5-5') if cred.key.startswith('sk-ant') \
            else lp.Resolved(error='Google Gemini：HTTP 400 API key not valid', at=1)
    monkeypatch.setattr(lp, 'resolve', fake_resolve)
    monkeypatch.setattr(lp, 'call_anthropic', lambda *a: '正常')
    r = owner.post('/admin/settings/ai/test', follow_redirects=True).get_data(as_text=True)
    assert '測試成功' in r and '金鑰 1：Anthropic Claude，模型 claude-opus-5-5 → claude-sonnet-5-5 (自動挑選)，回應正常' in r
    assert '金鑰 2：Google Gemini：HTTP 400 API key not valid' in r
