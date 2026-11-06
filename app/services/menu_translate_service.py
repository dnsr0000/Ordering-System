"""
AI 一鍵翻譯菜單 (品項名稱 / 說明、分類的英日文) 與 AI 服務過載時的背景自動補翻。

- translate_menu：後台按鈕與背景補翻共用。預設只翻還缺翻譯的 (不覆蓋手填)，雲端 AI 失敗的項目先以規則字典
  (app/services/menu_dictionary.py) 填常見品名 / 分類。
- 有金鑰但還有項目沒翻成 (AI 服務尖峰 503 很常見)：記下待補翻 (sync_state，本機狀態)，背景排程
  (maintenance_service 每分鐘呼叫 run_due) 在另一條執行緒依序重試 —— 5、10、15、30 分鐘…最多 MAX_ATTEMPTS 次，
  補完或已沒有缺的就清除。店家不用一直回來按，也不會擋住列印等其他排程。
"""
import json
import logging
import threading
from datetime import datetime, timedelta

from flask import g

from app.extensions import db
from app.models.menu import CategoryTranslation, MenuItem
from app.services import ai_service

logger = logging.getLogger(__name__)
RETRY_MINUTES = (5, 10, 15, 30)   # 第 n 次失敗後隔多久再試 (之後都是最後一個值)
MAX_ATTEMPTS = 24                 # 約 10 小時後停止
_running = threading.Lock()


def translate_menu(overwrite=False):
    """回 (填入欄位數, 來源, stats)。stats = {'ai': 雲端 AI 翻好的項數, 'provider': 廠商, 'fallback': 沒翻成的項數,
    'pending': 這次需要翻的項數, 'remaining': 翻完之後仍缺翻譯的項數}"""
    all_items = MenuItem.query.order_by(MenuItem.id).all()
    all_categories = sorted({(i.category or '未分類').strip() for i in all_items})
    translations = {t.category: t for t in CategoryTranslation.query.all()}

    # 只送還缺翻譯的 (不覆蓋時)：已翻好的不再送雲端 AI —— 尖峰時段 503 很常見，再按一次只補剩下的，也比較快
    def item_needs(i, force=overwrite):
        return force or not i.name_en or not i.name_ja or (bool(i.description) and (not i.description_en or not i.description_ja))

    def cat_needs(c, force=overwrite):
        row = translations.get(c)
        return force or row is None or not row.name_en or not row.name_ja
    items = [i for i in all_items if item_needs(i)]
    categories = [c for c in all_categories if cat_needs(c)]
    entries = [{'name': i.name, 'description': i.description or ''} for i in items] + [{'name': c, 'description': ''} for c in categories]
    stats = {'ai': 0, 'provider': '', 'fallback': 0, 'pending': len(entries), 'remaining': 0}
    if not entries:
        return 0, 'ai' if ai_service.has_api_key() else 'rules', stats
    results, source = ai_service.translate_entries(entries, stats)
    filled = 0

    def put(obj, field, value, limit):
        nonlocal filled
        if value and (overwrite or not getattr(obj, field)):
            setattr(obj, field, value[:limit])
            filled += 1
    for item, got in zip(items, results[:len(items)]):
        put(item, 'name_en', got['name_en'], 100)
        put(item, 'name_ja', got['name_ja'], 100)
        if item.description:
            put(item, 'description_en', got['description_en'], 300)
            put(item, 'description_ja', got['description_ja'], 300)
    for cat, got in zip(categories, results[len(items):]):
        row = translations.get(cat)
        if row is None and (got['name_en'] or got['name_ja']):
            row = CategoryTranslation(tenant_id=g.tenant_id, category=cat)
            db.session.add(row)
            translations[cat] = row
        if row is not None:
            put(row, 'name_en', got['name_en'], 60)
            put(row, 'name_ja', got['name_ja'], 60)
    stats['remaining'] = sum(1 for i in all_items if item_needs(i, False)) + sum(1 for c in all_categories if cat_needs(c, False))
    return filled, source, stats


# ---------------------------------------------------------------------------
# 待補翻狀態 (本機 sync_state)
# ---------------------------------------------------------------------------
def _key(tenant_id):
    return f'menu_translate:{tenant_id}'


def get_pending(tenant_id):
    """{'remaining', 'attempts', 'next_at', 'since', 'error', 'gave_up'} 或 None"""
    from app.sync.runtime import get_state
    raw = get_state(_key(tenant_id), '')
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _save(tenant_id, data):
    from app.sync.runtime import set_state
    set_state(_key(tenant_id), json.dumps(data, ensure_ascii=False) if data else '')


def note_result(tenant_id, stats, now=None, attempts=0):
    """翻譯結束後更新待補翻狀態：有金鑰且還有雲端 AI 沒翻成的項目 → 排下一次背景補翻；否則清除 (由呼叫端 commit)"""
    now = now or datetime.now()
    if not (ai_service.has_api_key() and stats.get('fallback') and stats.get('remaining')):
        _save(tenant_id, None)
        return None
    prev = get_pending(tenant_id) or {}
    data = {'remaining': stats['remaining'], 'attempts': attempts, 'since': prev.get('since') or now.isoformat(timespec='seconds'),
            'error': ai_service.last_ai_error()[:200], 'gave_up': attempts >= MAX_ATTEMPTS,
            'next_at': (now + timedelta(minutes=RETRY_MINUTES[min(attempts, len(RETRY_MINUTES) - 1)])).isoformat(timespec='seconds')}
    _save(tenant_id, data)
    return data


def cancel(tenant_id):
    _save(tenant_id, None)


def _due_tenants(now):
    from app.models.sync import SyncState
    out = []
    for row in SyncState.query.filter(SyncState.key.like('menu_translate:%')).all():
        try:
            data = json.loads(row.value) if row.value else None
            tid = int(row.key.split(':', 1)[1])
        except (ValueError, IndexError):
            continue
        if isinstance(data, dict) and not data.get('gave_up') and data.get('next_at', '') <= now.isoformat(timespec='seconds'):
            out.append(tid)
    return out


def run_due(app, now=None):
    """背景排程 (每分鐘)：時間到的店家在另一條執行緒補翻 (一次只跑一條；翻譯可能要兩分鐘，不可擋住排程迴圈)。
    金鑰可能設在各店的系統設定，所以這裡不先檢查；補翻時沒有金鑰會清除待補翻"""
    tids = _due_tenants(now or datetime.now())
    if not tids or not _running.acquire(blocking=False):
        return []
    threading.Thread(target=_worker, args=(app, tids), name='menu-translate-retry', daemon=True).start()
    return tids


def _worker(app, tids):
    try:
        for tid in tids:
            with app.app_context():
                try:
                    retry_tenant(app, tid)
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    logger.exception('背景補翻菜單失敗 (店家 %s)', tid)
                finally:
                    db.session.remove()
    finally:
        _running.release()


def retry_tenant(app, tenant_id, now=None):
    """補翻一家店 (只翻還缺的)；回 stats"""
    from app.models.tenant import Tenant
    from app.tenant_context import _use_tenant
    prev = get_pending(tenant_id) or {}
    tenant = db.session.get(Tenant, tenant_id)
    if tenant is None:
        cancel(tenant_id)
        db.session.commit()
        return None
    with app.test_request_context('/'):
        _use_tenant(tenant, remember=False)
        filled, _source, stats = translate_menu(overwrite=False)
        data = note_result(tenant_id, stats, now=now, attempts=int(prev.get('attempts') or 0) + 1)
        db.session.commit()
        logger.info('背景補翻菜單 (店家 %s)：AI %d 項、填入 %d 個欄位、仍缺 %d 項%s', tenant_id, stats['ai'], filled,
                    stats['remaining'], '' if data is None else f"，下次 {data['next_at']}")
        if filled:
            from app.services.event_bus import order_event_bus
            order_event_bus.notify(tenant_id)   # 數位看板 / 點餐機的英日文菜單即時更新
    return stats
