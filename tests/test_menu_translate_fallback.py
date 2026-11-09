"""AI 翻譯菜單的備援：內建菜名字典 (台灣常見菜名、前綴、規格) 與 Gemini 過載時的背景自動補翻"""
import json
from datetime import datetime, timedelta

from app.extensions import db
from app.models.menu import CategoryTranslation, MenuItem
from app.services import ai_service, menu_translate_service as mts
from app.services.menu_dictionary import WORDS, lookup
from tests.support import make_item



def _cloud(fn):
    """把回傳文字的假 Gemini 包成 cloud_generate 的 (文字, 來源)"""
    def call(prompt, **kw):
        text = fn(prompt, **kw)
        return (text, 'gemini') if text else (None, '')
    return call

def test_dictionary_common_dishes_prefixes_and_sizes():
    assert lookup('蘿蔔糕') == ('Turnip cake', '大根餅')
    assert lookup('蚵仔煎') == ('Oyster omelette', '牡蠣オムレツ')
    assert lookup('臭豆腐') == ('Stinky tofu', '臭豆腐')
    assert lookup('招牌滷肉飯') == ('Signature braised pork rice', '看板ルーローハン')
    assert lookup('冰美式咖啡') == ('Iced Americano', 'アイスアメリカーノ')
    assert lookup(' 珍珠奶茶（大） ') == ('Bubble milk tea (Large)', 'タピオカミルクティー（大）')
    assert lookup('經典牛肉漢堡 (小)') == ('Classic beef burger (Small)', '定番ビーフバーガー（小）')
    assert lookup('招牌和牛堡') is None and lookup('') is None and lookup('炙燒起司牛肉堡') is None, '認不得整個名稱就不翻'
    assert len(WORDS) >= 200
    assert all(en and ja and en == en.strip() for en, ja in WORDS.values())
    assert ai_service.rule_translate('滷肉飯') == {'en': 'Braised pork rice', 'ja': 'ルーローハン'}


def _reset_menu(app):
    """其他測試留下的品項都標成已翻譯，只看這個測試新增的"""
    with app.app_context():
        MenuItem.query.update({'name_en': 'x', 'name_ja': 'x', 'description_en': 'x', 'description_ja': 'x'})
        for row in CategoryTranslation.query.all():
            row.name_en = row.name_ja = 'x'
        have = {(r.tenant_id, r.category) for r in CategoryTranslation.query.all()}
        for tid, cat in {(i.tenant_id, (i.category or '未分類').strip()) for i in MenuItem.query.all()} - have:
            db.session.add(CategoryTranslation(tenant_id=tid, category=cat, name_en='x', name_ja='x'))
        db.session.commit()


def test_gemini_busy_schedules_background_retry_and_retry_fills(app, admin_client, monkeypatch):
    from tests.support import default_tenant_id
    monkeypatch.setattr(ai_service, 'RETRY_DELAY', 0)
    monkeypatch.setattr(ai_service, 'has_api_key', lambda: True)
    monkeypatch.setattr(ai_service, 'last_ai_error', lambda: '503 high demand')
    _reset_menu(app)
    known = make_item(app, '蘿蔔糕', price=40, category='小吃')
    unknown = make_item(app, '阿嬤的秘製炒泡麵', price=90, category='小吃', description='加蛋加起司')
    monkeypatch.setattr(ai_service, 'cloud_generate', lambda *a, **k: (None, ''))   # AI 服務一直 503
    r = admin_client.post('/admin/menu', data={'action': 'translate'}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert '背景每隔幾分鐘自動補翻' in html and 'translatePendingBox' in html and '等 AI 服務恢復後自動補翻' in html
    tid = default_tenant_id(app)
    with app.app_context():
        assert db.session.get(MenuItem, known).name_en == 'Turnip cake', '字典認得的先翻好'
        assert db.session.get(MenuItem, unknown).name_en == ''
        pending = mts.get_pending(tid)
        assert pending['remaining'] == 1 and pending['attempts'] == 0 and pending['error'] == '503 high demand'
        assert mts._due_tenants(datetime.now()) == [], '5 分鐘後才補翻'
        assert tid in mts._due_tenants(datetime.now() + timedelta(minutes=6))

    # 背景補翻：還是失敗 → 次數 +1、間隔拉長
    with app.app_context():
        stats = mts.retry_tenant(app, tid)
        assert stats['remaining'] == 1
        pending = mts.get_pending(tid)
        assert pending['attempts'] == 1
        assert datetime.fromisoformat(pending['next_at']) > datetime.now() + timedelta(minutes=9)

    # Gemini 恢復：補翻成功、清除待補翻
    def ok(prompt, **kw):
        assert '阿嬤的秘製炒泡麵' in prompt and '蘿蔔糕' not in prompt, '只送還缺翻譯的'
        return json.dumps([{'i': 1, 'name_en': 'Grandma instant noodles', 'name_ja': 'おばあちゃんの焼きラーメン',
                            'description_en': 'With egg and cheese', 'description_ja': '卵とチーズ入り'}])
    monkeypatch.setattr(ai_service, 'cloud_generate', _cloud(ok))
    with app.app_context():
        stats = mts.retry_tenant(app, tid)
        assert stats['remaining'] == 0 and mts.get_pending(tid) is None
        item = db.session.get(MenuItem, unknown)
        assert item.name_en == 'Grandma instant noodles' and item.description_ja == '卵とチーズ入り'
    assert 'translatePendingBox' not in admin_client.get('/admin/menu').get_data(as_text=True)


def test_background_retry_gives_up_and_can_be_cancelled(app, admin_client, monkeypatch):
    from tests.support import default_tenant_id
    monkeypatch.setattr(ai_service, 'has_api_key', lambda: True)
    tid = default_tenant_id(app)
    with app.app_context():
        data = mts.note_result(tid, {'fallback': 3, 'remaining': 3}, attempts=mts.MAX_ATTEMPTS)
        db.session.commit()
        assert data['gave_up'] and tid not in mts._due_tenants(datetime.now() + timedelta(days=1)), '停止後不再自動重試'
    html = admin_client.get('/admin/menu').get_data(as_text=True)
    assert '背景自動補翻已停止' in html
    admin_client.post('/admin/menu', data={'action': 'translate_cancel'})
    with app.app_context():
        assert mts.get_pending(tid) is None
        # 沒有金鑰或全部翻成：不排補翻
        assert mts.note_result(tid, {'fallback': 0, 'remaining': 2}) is None
        monkeypatch.setattr(ai_service, 'has_api_key', lambda: False)
        assert mts.note_result(tid, {'fallback': 2, 'remaining': 2}) is None
        assert mts.run_due(app) == []
