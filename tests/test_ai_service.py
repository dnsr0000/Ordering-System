"""AI 服務：Gemini 503 重試與備援模型、冷卻、本機模型狀態診斷、JSON 擷取、系統健康頁顯示"""

import pytest

from app.services import ai_service


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def json(self):
        if self._payload == 'bad':
            raise ValueError('not json')
        return self._payload


def _ok(text):
    return _Resp(200, {'candidates': [{'content': {'parts': [{'text': text}]}}]})


@pytest.fixture
def gemini(app, monkeypatch):
    """假的 requests.post：依序回傳 responses 裡的回應，並記錄打到哪個模型"""
    calls = []
    monkeypatch.setattr(ai_service, 'RETRY_DELAY', 0)
    monkeypatch.setitem(app.config, 'GEMINI_API_KEY', 'test-key-123')
    monkeypatch.setitem(app.config, 'GEMINI_MODEL', 'gemini-main')
    monkeypatch.setitem(app.config, 'GEMINI_FALLBACK_MODEL', 'gemini-backup')

    def install(responses):
        queue = list(responses)

        def fake_post(url, **kw):
            calls.append(url.split('/models/')[1].split(':')[0])
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        monkeypatch.setattr(ai_service.requests, 'post', fake_post)
        return calls
    return install


def test_gemini_retries_503_then_uses_fallback_model(app, gemini):
    calls = gemini([_Resp(503, {'error': {'message': 'overloaded'}}), _Resp(503), _ok('備援回答')])
    with app.app_context():
        assert ai_service.gemini_generate('hi') == '備援回答'
    assert calls == ['gemini-main', 'gemini-main', 'gemini-backup'], '主要模型重試一次後換備援'
    assert ai_service.cooldown_left(ai_service.key_id('test-key-123')) == 0, '成功就不冷卻'
    assert ai_service._gemini_last['model'] == 'gemini-backup' and ai_service._gemini_last['error'] == ''


def test_gemini_retries_on_timeout_and_cools_down_when_all_fail(app, gemini, monkeypatch):
    import requests
    calls = gemini([requests.Timeout('slow'), _Resp(503), _Resp(503), _Resp(503)])
    with app.app_context():
        assert ai_service.gemini_generate('hi') is None
        assert calls == ['gemini-main', 'gemini-main', 'gemini-backup', 'gemini-backup']
        remaining = ai_service.cooldown_left(ai_service.key_id('test-key-123'))
        assert 15 < remaining <= ai_service.COOLDOWN['overload'], '過載冷卻 20 秒'
        assert '503' in ai_service._gemini_last['error']
        # 冷卻期間不再打 API
        assert ai_service.gemini_generate('hi') is None and len(calls) == 4
        status = ai_service.ai_status()
        assert status['gemini']['cooldown_seconds'] > 0 and status['gemini']['models'] == ['gemini-main', 'gemini-backup']


def test_gemini_auth_error_cools_down_immediately_and_429_switches_model(app, gemini):
    calls = gemini([_Resp(403, {'error': {'message': 'bad key'}})])
    with app.app_context():
        assert ai_service.gemini_generate('hi') is None
        assert calls == ['gemini-main'] and ai_service.cooldown_left(ai_service.key_id('test-key-123')) > 50
    ai_service._cooldowns.clear()
    calls = gemini([_Resp(429), _ok('ok')])
    with app.app_context():
        assert ai_service.gemini_generate('hi') == 'ok'
    assert calls[-2:] == ['gemini-main', 'gemini-backup'], '429 不重試同一個模型，直接換備援'


def test_gemini_json_mode_and_bad_payload(app, gemini):
    calls = gemini([_Resp(200, 'bad'), _ok('{"a": 1}')])
    with app.app_context():
        data, source = ai_service.generate_json('give json')
    assert data == {'a': 1} and source == 'gemini' and calls == ['gemini-main', 'gemini-backup']


def test_extract_json_tolerates_fences_and_prose():
    assert ai_service.extract_json('```json\n{"x": [1, 2]}\n```') == {'x': [1, 2]}
    assert ai_service.extract_json('結果如下：[{"name": "紅茶"}] 以上') == [{'name': '紅茶'}]
    assert ai_service.extract_json('沒有 json') is None and ai_service.extract_json('') is None


def test_local_model_status_reports_missing_file_or_package(app, monkeypatch):
    monkeypatch.setattr(ai_service, '_local_llm', None)
    monkeypatch.setattr(ai_service, '_local_llm_loaded', False)
    monkeypatch.setattr(ai_service, '_local_llm_error', '')
    monkeypatch.setitem(app.config, 'LOCAL_MODEL_PATH', 'C:/no/such/model.gguf')
    with app.app_context():
        assert ai_service.run_local_slm('hi') is None
        st = ai_service.ai_status()['local']
        assert st['model_exists'] is False and '找不到模型檔' in st['error'] and st['tried'] is True
    monkeypatch.setattr(ai_service, '_local_llm_loaded', False)
    monkeypatch.setattr(ai_service, '_local_llm_error', '')
    monkeypatch.setitem(app.config, 'LOCAL_MODEL_PATH', __file__)   # 檔案存在但套件載入失敗的情境
    import builtins
    real_import = builtins.__import__

    def no_llama(name, *a, **k):
        if name == 'llama_cpp':
            raise ImportError('No module named llama_cpp')
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, '__import__', no_llama)
    with app.app_context():
        assert ai_service.run_local_slm('hi') is None
        st = ai_service.ai_status()['local']
    assert '未安裝 llama-cpp-python' in st['error'] and 'pip install' in st['error']


def test_system_health_page_shows_ai_status(app, admin_client, monkeypatch):
    monkeypatch.setitem(app.config, 'GEMINI_API_KEY', '')
    page = admin_client.get('/admin/system').get_data(as_text=True)
    assert 'id="aiGeminiTile"' in page and '未設定金鑰' in page and 'id="aiLocalTile"' in page and 'AI 服務' in page


def test_patient_mode_ignores_cooldown_and_retries_three_times(app, gemini, monkeypatch):
    """AI 翻譯菜單等店家願意等的工作：冷卻中也照樣呼叫，每個模型試 3 次 (尖峰 503 通常幾秒後恢復)"""
    calls = gemini([_Resp(503), _Resp(503), _ok('第三次成功')])
    ai_service._set_cooldown(ai_service.key_id('test-key-123'), 60)
    with app.app_context():
        assert ai_service.gemini_generate('hi') is None, '一般呼叫在冷卻中直接略過'
        assert calls == []
        assert ai_service.gemini_generate('hi', patient=True) == '第三次成功'
    assert calls == ['gemini-main'] * 3
