"""跨檔請求 (ai-llm) 的回歸測試：B-165 達預算上限不補跑翻譯、B-157 翻譯結果標記來源"""
import json

from app.services import ai_service, ai_usage, llm_providers as lp


def _creds(monkeypatch):
    monkeypatch.setattr(ai_service, 'RETRY_DELAY', 0)
    monkeypatch.setattr(ai_service, 'credentials', lambda tenant_id=None: [lp.Credential(key='AIza-x', source='env:GEMINI_API_KEY')])
    monkeypatch.setattr(ai_service, 'has_api_key', lambda *a, **k: True)


def test_budget_exceeded_skips_second_translate_round(app, monkeypatch):
    _creds(monkeypatch)
    calls = []
    monkeypatch.setattr(ai_service, 'cloud_generate', lambda prompt, **kw: calls.append(prompt) or (None, ''))
    monkeypatch.setattr(ai_usage, 'budget_exceeded', lambda tid: '本月 AI 費用已達預算上限')
    with app.app_context():
        results, source = ai_service.translate_entries([{'name': '主餐'}, {'name': 'ai-llm 測試品'}])
    assert len(calls) == 1, '達預算上限：第一輪失敗後不再補跑'
    assert source == 'rules' and results[0]['name_en'] == 'Mains'


def test_translate_results_carry_source(app, monkeypatch):
    _creds(monkeypatch)
    monkeypatch.setattr(ai_usage, 'budget_exceeded', lambda tid: '')

    def fake_cloud(prompt, **kw):
        rows = [line.split('. ', 1) for line in prompt.splitlines() if line[:1].isdigit() and '. ' in line]
        out = [{'i': int(n), 'name_en': f'EN {name}', 'name_ja': f'JA {name}'} for n, name in rows if 'AI' in name]
        return json.dumps(out), 'gemini'
    monkeypatch.setattr(ai_service, 'cloud_generate', fake_cloud)
    glossary = {'ai-llm 沿用品': {'en': 'Reused', 'ja': 'リユース'}}
    entries = [{'name': 'ai-llm 沿用品'}, {'name': 'AI 翻品'}, {'name': '主餐'}]
    with app.app_context():
        results, source = ai_service.translate_entries(entries, {}, glossary)
    assert [r['source'] for r in results] == ['store', 'ai', 'rules']
    assert results[1]['name_en'] == 'EN AI 翻品' and results[2]['name_en'] == 'Mains' and source == 'gemini'
