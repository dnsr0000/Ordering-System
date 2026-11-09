"""
雲端 AI 的用量、估計費用、每月預算上限，以及各模型最近的成功率 (呼叫順序依此調整)。

- 用量：每次呼叫記下廠商 / 模型 / token 數 (含提示快取讀寫)，先累積在記憶體，背景執行緒每分鐘併入資料庫
  (sync_state 的 ai_usage:{店家}:{年-月}，各行程各自併入、以列鎖避免互相覆蓋)，不拖慢呼叫，也不會動到呼叫端的交易；
  行程結束時 (atexit) 再寫入一次，重啟 / 部署前最後不到一分鐘的用量不會遺失。
- 估計費用：依模型系列的公開牌價概算 (PRICES，美元 / 百萬 token)，實際以各家帳單為準。
- 預算：系統設定「AI 服務」的「每月預算上限」(美元，0 = 不限)；本月估計費用達上限後不再呼叫雲端 AI，
  改用本機模型 / 規則字典，下個月自動恢復。
- 模型健康：每組金鑰 × 模型最近 HEALTH_SIZE 次的成敗與耗時 (有 Redis 時各行程共用)；最近常失敗的模型排到後面先試別的，
  恢復後 (HEALTH_WINDOW 內沒有再失敗) 自動回到原本的順序。
"""
import atexit
import json
import logging
import re
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

from app.services import llm_providers

logger = logging.getLogger(__name__)

# (模型名稱規則, 輸入, 輸出, 快取讀取) 美元 / 百萬 token；第一個符合的為準。快取寫入以輸入價 × 1.25 計
PRICES = (
    (r'claude-(fable|mythos)', 10.0, 50.0, 0.25),
    (r'claude-opus-5-5', 4.0, 20.0, 0.20),
    (r'claude-opus', 5.0, 25.0, 0.50),
    (r'claude-sonnet-5-5', 2.0, 10.0, 0.20),
    (r'claude-sonnet', 3.0, 15.0, 0.30),
    (r'claude-haiku-5-5', 0.10, 0.50, 0.01),
    (r'claude-haiku', 1.0, 5.0, 0.10),
    (r'gemini.*flash-lite', 0.10, 0.40, 0.025),
    (r'gemini.*flash', 0.30, 2.50, 0.075),
    (r'gemini.*pro', 1.25, 10.0, 0.31),
    (r'gpt-.*nano', 0.05, 0.40, 0.005),
    (r'gpt-.*mini', 0.25, 2.0, 0.025),
    (r'gpt-', 1.25, 10.0, 0.125),
    (r'deepseek', 0.28, 0.42, 0.028),
    (r'mistral-small', 0.10, 0.30, 0.01),
    (r'mistral', 2.0, 6.0, 0.2),
    (r'llama|qwen|gemma|mixtral', 0.10, 0.40, 0.01),
    (r'', 1.0, 4.0, 0.10),   # 其他：保守估計
)
FLUSH_SECONDS = 60
HEALTH_SIZE = 10
HEALTH_WINDOW = 600   # 秒：這段時間內的失敗才算數

_pending: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(int)))   # (tid, 月) -> 模型鍵 -> 欄位 -> 值
_pending_lock = threading.Lock()
_flusher = None
_exit_hook = False   # 已註冊 atexit 寫入
_month_cache: dict = {}   # (tid, 月) -> (查詢時間, 資料庫裡的用量)
_health: dict = defaultdict(lambda: deque(maxlen=HEALTH_SIZE))   # (金鑰 id, 模型) -> deque[(時間, 成功, 秒)]


def price_of(model):
    for pattern, inp, out, cached in PRICES:
        if re.search(pattern, model or '', re.I):
            return inp, out, cached
    return PRICES[-1][1:]


def estimate_cost(model, usage):
    """usage = {'input', 'output', 'cache_read', 'cache_write'} → 美元"""
    inp, out, cached = price_of(model)
    return (usage.get('input', 0) * inp + usage.get('output', 0) * out + usage.get('cache_read', 0) * cached
            + usage.get('cache_write', 0) * inp * 1.25) / 1_000_000


def _month(now=None):
    return (now or datetime.now()).strftime('%Y-%m')


def _state_key(tenant_id, month):
    return f'ai_usage:{tenant_id or 0}:{month}'


# ---------------------------------------------------------------------------
# 記錄
# ---------------------------------------------------------------------------
def record(tenant_id, provider, model, ok, usage=None, seconds=0.0, cred_id=''):
    """一次雲端 AI 呼叫的結果 (成功 / 失敗、token 用量、耗時)"""
    usage = usage or {}
    if cred_id:
        note_health(cred_id, model, ok, seconds)
    with _pending_lock:
        row = _pending[(tenant_id or 0, _month())][f'{provider}|{model}']
        row['calls'] += 1
        row['failures'] += 0 if ok else 1
        for k in ('input', 'output', 'cache_read', 'cache_write'):
            row[k] += int(usage.get(k) or 0)
        row['cost_micro'] += round(estimate_cost(model, usage) * 1_000_000)
    _ensure_flusher()


def _ensure_flusher():
    global _flusher, _exit_hook
    if _flusher is not None and _flusher.is_alive():
        return
    try:
        from flask import current_app
        app = current_app._get_current_object()
    except RuntimeError:
        return
    if app.config.get('TESTING'):
        return   # 測試直接呼叫 flush()
    _flusher = threading.Thread(target=_flush_forever, args=(app,), name='ai-usage-flush', daemon=True)
    _flusher.start()
    if not _exit_hook:   # 行程結束 (關閉單機程式、gunicorn worker 退出) 時把最後不到一分鐘的用量也寫進去，預算才不會少算
        _exit_hook = True
        atexit.register(flush_now, app)


def flush_now(app):
    """在自己的 app context 寫入一次 (背景執行緒、行程結束時用)；失敗只記 log"""
    with app.app_context():
        try:
            flush()
        except Exception:  # noqa: BLE001
            logger.exception('AI 用量寫入資料庫失敗 (下次再試)')
        finally:
            from app.extensions import db
            db.session.remove()


def _flush_forever(app):
    while True:
        time.sleep(FLUSH_SECONDS)
        flush_now(app)


def flush():
    """把記憶體裡累積的用量併入資料庫並 commit (需在 app context；請勿在請求的交易中途呼叫)"""
    from sqlalchemy import select

    from app.extensions import db
    from app.models.sync import SyncState
    with _pending_lock:
        batch = {k: {m: dict(v) for m, v in rows.items()} for k, rows in _pending.items()}
        _pending.clear()
    if not batch:
        return 0
    try:
        for (tid, month), rows in batch.items():
            key = _state_key(tid, month)
            row = db.session.execute(select(SyncState).where(SyncState.key == key).with_for_update()).scalar_one_or_none()
            data = _loads(row.value if row else '')
            for model_key, delta in rows.items():
                cur = data.setdefault(model_key, {})
                for k, v in delta.items():
                    cur[k] = int(cur.get(k, 0)) + int(v)
            if row is None:
                db.session.add(SyncState(key=key, value=json.dumps(data)))
            else:
                row.value = json.dumps(data)
            _month_cache.pop((tid, month), None)
        db.session.commit()
    except Exception:
        db.session.rollback()
        with _pending_lock:   # 寫入失敗：放回去，下次再併
            for k, rows in batch.items():
                for m, delta in rows.items():
                    for f, v in delta.items():
                        _pending[k][m][f] += v
        raise
    return sum(len(r) for r in batch.values())


def _loads(raw):
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# 查詢：本月用量、預算
# ---------------------------------------------------------------------------
def month_usage(tenant_id, month=None):
    """{'provider|model': {'calls', 'failures', 'input', 'output', 'cache_read', 'cache_write', 'cost_micro'}}
    (資料庫 + 還沒寫入的)"""
    from app.sync.runtime import get_state
    month = month or _month()
    ck = (tenant_id or 0, month)
    hit = _month_cache.get(ck)
    if hit is None or time.time() - hit[0] > 30:
        try:
            stored = _loads(get_state(_state_key(tenant_id, month), ''))
        except Exception:  # noqa: BLE001  資料表還沒建立 (初次啟動)
            stored = {}
        hit = (time.time(), stored)
        _month_cache[ck] = hit
    out = {m: dict(v) for m, v in hit[1].items()}
    with _pending_lock:
        for m, delta in _pending.get(ck, {}).items():
            cur = out.setdefault(m, {})
            for k, v in delta.items():
                cur[k] = int(cur.get(k, 0)) + int(v)
    return out


def month_summary(tenant_id):
    """系統監控頁：本月每個模型的呼叫次數、token、估計費用，與合計"""
    rows = []
    for model_key, v in sorted(month_usage(tenant_id).items()):
        provider, _, model = model_key.partition('|')
        rows.append({'provider': llm_providers.PROVIDERS.get(provider, (provider,))[0], 'model': model,
                     'calls': v.get('calls', 0), 'failures': v.get('failures', 0),
                     'tokens': v.get('input', 0) + v.get('output', 0) + v.get('cache_read', 0) + v.get('cache_write', 0),
                     'cache_read': v.get('cache_read', 0), 'cost': v.get('cost_micro', 0) / 1_000_000})
    budget = monthly_budget(tenant_id)
    total = sum(r['cost'] for r in rows)
    return {'month': _month(), 'rows': rows, 'cost': total, 'calls': sum(r['calls'] for r in rows), 'budget': budget,
            'over_budget': bool(budget and total >= budget)}


def monthly_budget(tenant_id):
    if not tenant_id:
        return 0
    from app.services import settings_service
    try:
        return max(0, int(settings_service.get('ai', tenant_id).get('monthly_budget_usd') or 0))
    except Exception:  # noqa: BLE001
        return 0


def budget_exceeded(tenant_id):
    """本月估計費用已達預算上限時回提示文字，否則回空字串"""
    budget = monthly_budget(tenant_id)
    if not budget:
        return ''
    spent = sum(v.get('cost_micro', 0) for v in month_usage(tenant_id).values()) / 1_000_000
    if spent < budget:
        return ''
    return f'本月 AI 估計費用 US${spent:.2f} 已達預算上限 US${budget}，暫停呼叫雲端 AI (下個月恢復，或到系統設定「AI 服務」調高上限)'


# ---------------------------------------------------------------------------
# 模型健康 (呼叫順序)
# ---------------------------------------------------------------------------
def note_health(cred_id, model, ok, seconds):
    entry = (time.time(), bool(ok), round(float(seconds), 2))
    _health[(cred_id, model)].append(entry)
    llm_providers.shared_push(f'health:{cred_id}:{model}', entry, HEALTH_SIZE, HEALTH_WINDOW * 2)


def health(cred_id, model):
    """最近 HEALTH_WINDOW 秒內的 {'calls', 'failures', 'avg_seconds'}"""
    rows = llm_providers.shared_list(f'health:{cred_id}:{model}')
    if rows is None:
        rows = list(_health.get((cred_id, model), ()))
    cutoff = time.time() - HEALTH_WINDOW
    recent = [r for r in rows if r and r[0] >= cutoff]
    ok_times = [r[2] for r in recent if r[1]]
    return {'calls': len(recent), 'failures': sum(1 for r in recent if not r[1]),
            'avg_seconds': round(sum(ok_times) / len(ok_times), 1) if ok_times else 0}


def unhealthy(cred_id, model):
    h = health(cred_id, model)
    return h['failures'] >= 2 and h['failures'] * 2 >= h['calls']


def order_models(cred_id, models):
    """最近常失敗 (10 分鐘內至少 2 次且失敗過半) 的模型排到後面；其他維持原本順序"""
    return sorted(models, key=lambda m: unhealthy(cred_id, m))


def reset():
    """測試用：清掉記憶體裡的用量與健康紀錄"""
    with _pending_lock:
        _pending.clear()
    _month_cache.clear()
    _health.clear()
