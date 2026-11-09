"""修正小組 menu-backend-signage-2 的回歸測試：AI 一鍵翻譯 / 背景補翻、多語系、碳足跡同步、數位看板、規則字典、菜單工程分析"""
from datetime import date

from flask import g
from sqlalchemy.orm import Session
from werkzeug.datastructures import MultiDict

from app.extensions import db
from app.models.menu import CategoryTranslation, MenuItem
from app.services import ai_service, ai_usage, menu_translate_service as mts
from tests.support import checkout, default_tenant_id, get_or_create_tenant, guest_client, line, make_item, tenant_request


def _empty(entries):
    return [{'name_en': '', 'name_ja': '', 'description_en': '', 'description_ja': ''} for _ in entries]


# ---------------------------------------------------------------------------
# B-157：全部重翻時 AI 失敗，規則字典不可覆蓋店家手動校正過的譯名
# ---------------------------------------------------------------------------
def test_overwrite_fallback_only_fills_blank_fields(app, monkeypatch):
    monkeypatch.setattr(ai_service, 'RETRY_DELAY', 0)
    monkeypatch.setattr(ai_service, 'has_api_key', lambda: True)
    monkeypatch.setattr(ai_service, 'cloud_generate', lambda *a, **k: (None, ''))   # AI 一直失敗
    manual = make_item(app, '珍珠奶茶', price=60, category='mbs2飲料', name_en='House Boba', name_ja='自家製タピオカ')
    blank = make_item(app, '蘿蔔糕', price=40, category='mbs2小吃')
    with tenant_request(app, default_tenant_id(app)):
        mts.translate_menu(overwrite=True)
        a, b = db.session.get(MenuItem, manual), db.session.get(MenuItem, blank)
        assert (a.name_en, a.name_ja) == ('House Boba', '自家製タピオカ'), '手動譯名不可被規則字典覆蓋'
        assert b.name_en == 'Turnip cake', '空白欄位仍以規則字典補上'
        db.session.rollback()
        # 常見品名會成為其他測試的店家譯名 (glossary)：用完就刪
        for item_id in (manual, blank):
            db.session.delete(db.session.get(MenuItem, item_id))
        db.session.commit()


def test_overwrite_uses_ai_results(app, monkeypatch):
    item_id = make_item(app, 'mbs2重翻品', price=60, category='mbs2重翻', name_en='Old', name_ja='古い')

    def fake(entries, stats, glossary):
        stats.update(ai=len(entries), store=0, fallback=0, provider='Gemini')
        return [{'name_en': f"AI {e['name']}", 'name_ja': 'AI', 'description_en': '', 'description_ja': ''}
                for e in entries], 'gemini'
    monkeypatch.setattr(ai_service, 'translate_entries', fake)
    with tenant_request(app, default_tenant_id(app)):
        mts.translate_menu(overwrite=True)
        assert db.session.get(MenuItem, item_id).name_en == 'AI mbs2重翻品', 'AI 翻好的照常覆蓋'
        db.session.rollback()


# ---------------------------------------------------------------------------
# B-158：翻譯期間別人已建立同一分類的翻譯列，不可 IntegrityError
# ---------------------------------------------------------------------------
def test_category_row_created_concurrently_is_reused(app, monkeypatch):
    tid = default_tenant_id(app)
    cat = 'mbs2並行分類'
    make_item(app, 'mbs2並行品', price=50, category=cat, name_en='x', name_ja='x')

    def fake(entries, stats, glossary):
        # 模擬背景補翻 (另一個連線) 在 AI 翻譯期間先建立了這個分類的資料列
        with Session(db.engine) as other:
            other.add(CategoryTranslation(tenant_id=tid, category=cat, name_en='', name_ja=''))
            other.commit()
        stats.update(ai=len(entries), store=0, fallback=0, provider='Gemini')
        return [{'name_en': 'Concurrent' if e['name'] == cat else '', 'name_ja': 'ヘイコウ' if e['name'] == cat else '',
                 'description_en': '', 'description_ja': ''} for e in entries], 'gemini'
    monkeypatch.setattr(ai_service, 'translate_entries', fake)
    with tenant_request(app, tid):
        mts.translate_menu(overwrite=False)
        db.session.commit()
        rows = CategoryTranslation.query.filter_by(category=cat).all()
        assert len(rows) == 1 and rows[0].name_en == 'Concurrent'


# ---------------------------------------------------------------------------
# B-165：達每月預算上限不排背景補翻，訊息不說「忙碌」
# ---------------------------------------------------------------------------
def test_budget_exceeded_no_retry_and_message(app, monkeypatch):
    from app.routes.menu_pro import _translate_message
    tid = default_tenant_id(app)
    monkeypatch.setattr(ai_service, 'has_api_key', lambda: True)
    monkeypatch.setattr(ai_usage, 'budget_exceeded', lambda _tid: '本月 AI 估計費用已達預算上限')
    with app.test_request_context('/'):
        g.tenant_id = tid
        assert mts.note_result(tid, {'fallback': 2, 'remaining': 2}) is None
        db.session.commit()
        assert mts.get_pending(tid) is None
        msg, level = _translate_message(0, {'pending': 2, 'fallback': 2, 'ai': 0, 'store': 0, 'provider': ''})
        assert '預算上限' in msg and '忙碌' not in msg and '背景' not in msg and level == 'warning'


# ---------------------------------------------------------------------------
# B-166：停用的店家不再背景補翻
# ---------------------------------------------------------------------------
def test_retry_skips_inactive_tenant(app, monkeypatch):
    from app.models.tenant import Tenant
    tid = get_or_create_tenant(app, 'mbs2-inactive')
    monkeypatch.setattr(mts, 'translate_menu', lambda **kw: (_ for _ in ()).throw(AssertionError('不可翻譯')))
    with app.app_context():
        db.session.get(Tenant, tid).is_active = False
        mts._save(tid, {'remaining': 1, 'attempts': 0, 'since': '2026-01-01T00:00:00', 'next_at': '2026-01-01T00:00:00'})
        db.session.commit()
        assert mts.retry_tenant(app, tid) is None
        assert mts.get_pending(tid) is None


# ---------------------------------------------------------------------------
# B-167：補翻期間按「停止自動補翻」，結束時不可把待補翻狀態寫回去
# ---------------------------------------------------------------------------
def test_cancel_during_retry_is_kept(app, monkeypatch):
    from app.models.sync import SyncState
    tid = default_tenant_id(app)
    monkeypatch.setattr(ai_service, 'has_api_key', lambda: True)

    def fake_translate(overwrite=False):
        with Session(db.engine) as other:   # 店家在另一個請求按了「停止自動補翻」
            other.get(SyncState, mts._key(tid)).value = ''
            other.commit()
        return 0, 'rules', {'ai': 0, 'store': 0, 'provider': '', 'fallback': 1, 'pending': 1, 'remaining': 1}
    with app.app_context():
        mts.note_result(tid, {'fallback': 1, 'remaining': 1})
        db.session.commit()
        monkeypatch.setattr(mts, 'translate_menu', fake_translate)
        mts.retry_tenant(app, tid)
        assert mts.get_pending(tid) is None
        db.session.expire_all()
        assert mts.get_pending(tid) is None, '已取消的待補翻不可被寫回'


# ---------------------------------------------------------------------------
# B-288 / B-289：員工頁一律中文；帶圖示的訊息去掉圖示後仍查得到翻譯
# ---------------------------------------------------------------------------
def test_staff_endpoints_stay_chinese_and_icon_messages(app, store_settings):
    from app.services import i18n_service
    store_settings('menu', languages=['en', 'ja'])
    tid = default_tenant_id(app)
    for path in ('/admin/tables', '/reservations', '/admin/privacy', '/admin/group-orders'):
        with app.test_request_context(path, headers={'Accept-Language': 'en-US'}):
            g.tenant_id = tid
            assert i18n_service.effective_language() == 'zh', path
    with app.test_request_context('/waitlist', headers={'Accept-Language': 'en-US'}):
        g.tenant_id = tid
        assert i18n_service.effective_language() == 'en', '同一 blueprint 的顧客頁照常依瀏覽器語言'
        tr = i18n_service.translate_message
        assert tr('照片未偵測到清晰人臉！請正對鏡頭並重新拍攝。', 'en').startswith('No clear face detected')
        assert tr('上傳的相片檔案已損壞或格式不支援，請上傳清晰的 JPG/PNG 圖片！', 'ja').startswith('写真が破損')
        assert tr('❌ 手機號碼格式不正確！請輸入 09 開頭的 10 碼數字。', 'en').startswith('❌ Invalid phone')


# ---------------------------------------------------------------------------
# B-290：前綴 + 專有名詞保留大寫
# ---------------------------------------------------------------------------
def test_dictionary_prefix_keeps_proper_nouns():
    from app.services.menu_dictionary import lookup
    assert lookup('冰美式咖啡')[0] == 'Iced Americano'
    assert lookup('古早味香腸')[0] == 'Old-fashioned Taiwanese sausage'
    assert lookup('冰養樂多')[0] == 'Iced Yakult'
    assert lookup('經典台灣啤酒')[0] == 'Classic Taiwan beer'
    assert lookup('招牌滷肉飯')[0] == 'Signature braised pork rice', '一般名詞仍改小寫'


# ---------------------------------------------------------------------------
# B-282：本機沒有配方時，儲存進階設定不可把 (同步來的) 碳足跡改成 0
# ---------------------------------------------------------------------------
def test_carbon_kept_without_local_recipe(app):
    from app.routes.menu_pro import apply_pro_form
    item_id = make_item(app, 'mbs2碳足跡', price=150, category='mbs2碳', carbon_grams=350, carbon_auto=True)
    form = MultiDict([('avail_days', d) for d in '1234567'] + [('carbon_auto', '1')])
    with tenant_request(app, default_tenant_id(app)):
        item = db.session.get(MenuItem, item_id)
        apply_pro_form(item, form)
        assert item.carbon_grams == 350 and item.carbon_auto
        db.session.rollback()


# ---------------------------------------------------------------------------
# B-285 / B-286 / B-295：數位看板
# ---------------------------------------------------------------------------
def _board(app):
    c = app.test_client()
    c.get('/?shop=default')
    return c


def _items(data):
    return {i['name']: i for cat in data['categories'] for i in cat['items']}


def test_signage_hub_without_pickup_node_not_all_sold_out(app, monkeypatch):
    import app.routes.customer as customer
    make_item(app, 'mbs2看板有貨', price=80, stock=10, category='mbs2看板')
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    monkeypatch.setattr(customer, '_online_nodes', lambda: [])
    data = _board(app).get('/api/signage').get_json()
    assert _items(data)['mbs2看板有貨']['available'] is True


def test_signage_hide_sold_out_keeps_off_hours_items(app, store_settings):
    today = str(date.today().isoweekday())
    make_item(app, 'mbs2看板時段外', price=80, stock=10, category='mbs2看板2',
              avail_days=''.join(d for d in '1234567' if d != today))
    make_item(app, 'mbs2看板售完', price=80, stock=0, category='mbs2看板2', is_sold_out=True)
    store_settings('signage', hide_sold_out=True, show_pickup=False)
    data = _board(app).get('/api/signage').get_json()
    items = _items(data)
    assert 'mbs2看板售完' not in items
    assert items['mbs2看板時段外']['available'] is False and items['mbs2看板時段外']['hint'].startswith('供應日')
    assert data['layout']['showPickup'] is False and data['pickup'] is None and 'columns' in data['layout']


# ---------------------------------------------------------------------------
# R2-104：菜單工程分析納入零銷售的菜單品項
# ---------------------------------------------------------------------------
def test_menu_engineering_includes_zero_sales(app):
    from app.services import menu_engineering_service
    cat = 'mbs2工程零銷售'
    sold = make_item(app, 'mbs2有賣', price=100, stock=100, category=cat, unit_cost=40)
    make_item(app, 'mbs2沒賣', price=120, stock=100, category=cat, unit_cost=100)
    make_item(app, 'mbs2紅利品', price=0, stock=100, category=cat, is_reward=True)
    make_item(app, 'mbs2已停售', price=90, stock=100, category=cat, sale_until=date(2000, 1, 1))
    r = checkout(guest_client(app), [line(sold, 4)])
    assert r.status_code == 200, r.get_json()
    tid = default_tenant_id(app)
    with app.app_context():
        result = menu_engineering_service.analyze(tid, date.today(), date.today(), category=cat)
    by = {r['name']: r for r in result['items']}
    assert set(by) == {'mbs2有賣', 'mbs2沒賣'}
    assert result['pop_threshold'] == 1.4   # 4 份 / 2 項 × 70%
    zero = by['mbs2沒賣']
    assert zero['qty'] == 0 and zero['zero_sales'] and zero['cls'] == 'dog' and zero['unit_cm'] == 20
    assert zero['advice'].startswith('期間內零銷售') and by['mbs2有賣']['cls'] == 'star'
    assert result['counts']['dog'] == 1
    assert menu_engineering_service.to_excel(result).getvalue()[:2] == b'PK'


# ---------------------------------------------------------------------------
# R2-101 / R2-109：通路價 / 會員價 / 成本有上限；R2-191：規格有格式錯誤的片段要擋下
# ---------------------------------------------------------------------------
def _pro_form(**extra):
    return MultiDict([('avail_days', d) for d in '1234567'] + list(extra.items()))


def test_pro_form_amount_limits_and_bad_sizes(app):
    import pytest

    from app.errors import AppError
    from app.routes.menu_pro import apply_pro_form
    item_id = make_item(app, 'mbs2通路價上限', price=100, category='mbs2通路價')
    with tenant_request(app, default_tenant_id(app)):
        item = db.session.get(MenuItem, item_id)
        for field, label in (('online_price', '線上價'), ('member_price', '會員價'), ('unit_cost', '成本')):
            with pytest.raises(AppError, match=label):
                apply_pro_form(item, _pro_form(**{field: '99999999999'}))
        apply_pro_form(item, _pro_form(delivery_price='1000000', unit_cost='-5', size_options='中杯：0，大杯：15'))
        assert item.delivery_price == 1_000_000 and item.unit_cost == 0
        assert item.size_options == '中杯:0, 大杯:+15'
        with pytest.raises(AppError, match='大杯:abc'):
            apply_pro_form(item, _pro_form(size_options='中杯:0, 大杯:abc'))
        db.session.rollback()
