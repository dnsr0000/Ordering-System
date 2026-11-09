"""
AI 文案與營運建議，三層降級：Google Gemini → 本機 Qwen2.5 SLM (選用) → 規則引擎。
- Gemini：主要模型 (GEMINI_MODEL) 回 503 / 5xx 或逾時先重試一次，再改用備援模型 (GEMINI_FALLBACK_MODEL)；
  都失敗時依原因冷卻 (金鑰錯誤 60 秒、限流 30 秒、過載 20 秒)，期間直接使用下一層。最近一次錯誤可在系統健康頁看到。
- 本機 SLM：需要 llama-cpp-python 與模型檔；沒裝套件 / 找不到檔案時記錄原因 (ai_status) 並降到規則引擎，不會一直重試載入。
- 營運建議以「今日訂單狀態」為快取鍵，只有訂單變動才重新產生，且在背景執行緒呼叫 API，不拖慢頁面。
- 依用途分兩檔模型 (tier)：fast (客服、語音點餐、短文案、營運建議) 用各家快速便宜的模型，strong (翻譯菜單) 用主力模型。
- 每次呼叫記下用量與成敗 (app/services/ai_usage.py)：最近常失敗的模型排到後面、本月估計費用達預算上限就停用雲端 AI。
- 冷卻以「金鑰」為單位 (多組 Gemini 金鑰互不影響)；設定 REDIS_URL 時各行程共用冷卻與辨識結果。
"""
import atexit
import contextvars
import hashlib
import logging
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from flask import current_app

from app.services import ai_usage, llm_providers  # 載入 llm_providers 時也註冊系統設定「AI 服務」的測試連線

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT = (5, 30)        # (連線, 讀取) 秒
# 即時對話 (客服機器人、語音點餐)：顧客在等，不重試也不換模型；503 / 5xx / 限流 / 逾時直接改用本機模型，
# 並進入冷卻，接下來的問題在冷卻期間直接由本機模型回答 (顧客看不到中斷)
INTERACTIVE_TIMEOUT = (3, 12)
RETRY_DELAY = 1.0               # 503 / 5xx / 逾時後重試前等待 (測試可改為 0)
COOLDOWN = {'auth': 60, 'rate': 30, 'overload': 20}
GEMINI_MIN_OUTPUT_TOKENS = (8192, 4000)   # maxOutputTokens 下限 (一般, 即時對話)：思考 token 也算在內，見 gemini_generate
LLAMA_INSTALL_HINT = ('pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu '
                      '(預編譯 CPU 版，不需要 C++ 編譯環境)')
_PLACEHOLDER_KEYS = {"", "YOUR_API_KEY", "1234", "none"}
# 店家 id -> {'ok_at', 'error_at', 'error', 'model'}：最近一次 Gemini 呼叫 (系統健康頁顯示；各店分開，不會看到別家的錯誤)
_gemini_last: dict = {}
_local_llm = None
_local_llm_loaded = False
_local_llm_error = ''
_llm_lock = threading.Lock()
_advice_cache: dict = {}  # tenant_id -> {'signature', 'advice', 'fetching'}


def _api_key():
    key = (current_app.config.get('GEMINI_API_KEY') or '').strip()
    return None if key in _PLACEHOLDER_KEYS or '你的' in key else key


_INVISIBLE = re.compile('[​‌‍⁠﻿]')   # 從 LINE / Notion 複製金鑰常夾帶的零寬字元


def _usable(key):
    key = _INVISIBLE.sub('', key or '').strip()
    return key if key and key not in _PLACEHOLDER_KEYS and '你的' not in key else ''


def credentials(tenant_id=None):
    """依序備援的 AI 金鑰：系統設定「AI 服務」金鑰 1~3 (目前店家) → .env AI_API_KEYS → GEMINI_API_KEY (重複的略過)"""
    from flask import g, has_request_context

    from app.services import settings_service
    out, seen = [], set()

    def add(key, source, model='', provider=''):
        key = _usable(key)
        if key and key not in seen:
            seen.add(key)
            out.append(llm_providers.Credential(key=key, source=source, model=model.strip(), provider=provider))
    tid = tenant_id if tenant_id is not None else (getattr(g, 'tenant_id', None) if has_request_context() else None)
    if tid is not None:
        try:
            cfg = settings_service.get('ai', tid)
        except Exception:  # noqa: BLE001  設定表尚未建立 (測試 / 初次啟動) 時只用 .env
            cfg = {}
        for n in (1, 2, 3):
            add(cfg.get(f'key{n}'), f'settings:{n}', cfg.get(f'model{n}') or '')
    for key in str(current_app.config.get('AI_API_KEYS') or '').split(','):
        add(key, 'env:AI_API_KEYS')
    gemini_override = ','.join(m for m in (current_app.config.get('GEMINI_MODEL'), current_app.config.get('GEMINI_FALLBACK_MODEL'))
                               if (m or '').strip())
    add(current_app.config.get('GEMINI_API_KEY'), 'env:GEMINI_API_KEY', gemini_override, 'gemini')
    return out


def has_api_key() -> bool:
    """有沒有任何可用的雲端 AI 金鑰 (不檢查金鑰是否有效)"""
    return bool(credentials())


# 店家 id -> {'at', 'ok_at', 'text'}：最近一次雲端 AI 失敗 (系統監控頁 / 翻譯訊息；各店分開，含遮罩金鑰與預算金額)
_last_error: dict = {}
_cooldowns: dict[str, float] = {}   # 金鑰 id -> 冷卻到何時 (本行程；有 Redis 時另存一份給其他行程)
_tenant_var: contextvars.ContextVar = contextvars.ContextVar('ai_tenant', default=None)   # 用量記在哪家店


def key_id(key):
    """金鑰的代號 (與 llm_providers.Credential.id 相同)"""
    return hashlib.sha256((key or '').encode()).hexdigest()[:12]


def cooldown_left(cid) -> float:
    """這組金鑰還要冷卻幾秒 (0 = 可以呼叫)"""
    until = max(_cooldowns.get(cid, 0.0), float(llm_providers.shared_get(f'cool:{cid}') or 0))
    return max(0.0, until - time.time())


def _set_cooldown(cid, seconds):
    until = time.time() + seconds
    _cooldowns[cid] = until
    llm_providers.shared_set(f'cool:{cid}', until, seconds)


def clear_cooldown(cid):
    """解除這組金鑰的冷卻 (「測試連線」要實際送出請求)"""
    _cooldowns.pop(cid, None)
    llm_providers.shared_delete(f'cool:{cid}')


def _current_tenant(tenant_id=None):
    if tenant_id is not None:
        return tenant_id
    if _tenant_var.get() is not None:
        return _tenant_var.get()
    from flask import g, has_request_context
    return getattr(g, 'tenant_id', None) if has_request_context() else None


def _error_state(tenant_id=None):
    return _last_error.setdefault(_current_tenant(tenant_id), {'at': 0.0, 'ok_at': 0.0, 'text': ''})


def _gemini_state(tenant_id=None):
    return _gemini_last.setdefault(_current_tenant(tenant_id), {'ok_at': 0.0, 'error_at': 0.0, 'error': '', 'model': ''})


def last_ai_error(tenant_id=None) -> str:
    """這家店 (省略 = 目前店家) 最近一次雲端 AI 呼叫失敗的原因 (之後已成功過就回空字串)；
    給「AI 翻譯」等功能告訴使用者為什麼改用備援"""
    err, gem = _error_state(tenant_id), _gemini_state(tenant_id)
    if err['at'] > err['ok_at']:
        return err['text']
    return gem['error'] if gem['error_at'] > gem['ok_at'] else ''


def clear_ai_error(tenant_id=None):
    """清掉這家店記下的最近錯誤 (「測試連線」只顯示這次呼叫的失敗原因)"""
    _last_error.pop(_current_tenant(tenant_id), None)
    _gemini_last.pop(_current_tenant(tenant_id), None)


def gemini_models():
    """.env 指定的 Gemini 模型 (主要、備援)；沒指定時用 Google 的「最新版」別名 (自動挑選見 llm_providers)"""
    out = []
    for name in (current_app.config.get('GEMINI_MODEL'), current_app.config.get('GEMINI_FALLBACK_MODEL')):
        name = (name or '').strip()
        if name and name not in out:
            out.append(name)
    return out or ['gemini-flash-latest', 'gemini-flash-lite-latest']


def cloud_generate(prompt, max_tokens=800, temperature=0.3, json_mode=False, interactive=False, patient=False, creds=None,
                   tier=None, prefix='', tenant_id=None, deadline=None):
    """
    依序試每一組金鑰 (自動辨識廠商與模型)，第一個成功的回 (文字, 廠商名稱 gemini / anthropic / openai…)；全部失敗回 (None, '')。
    同一家：Gemini 走 gemini_generate (重試 + 備援模型)；其他廠商依 interactive / patient 重試、過載換下一組金鑰。
    interactive (即時對話)：每組只試一次、逾時短，失敗的金鑰進冷卻；patient (店家願意等)：不理會冷卻、多試幾次。
    tier：'fast' (快速便宜) / 'strong' (主力)；省略時即時對話用 fast、其他用 strong。
    prefix：每次都一樣的前段 (店家資訊、菜單…)，放在最前面讓各家的提示快取生效 (Claude 明確標記快取)。
    tenant_id：用量記在哪家店 (省略 = 目前請求的店家)；本月估計費用達預算上限時直接回 (None, '')。
    deadline：time.time() 的絕對時間上限 (翻譯菜單的 TRANSLATE_TIME_BUDGET)；每次嘗試前檢查，請求逾時也不超過剩餘時間。
    """
    tid = _current_tenant(tenant_id)
    over = ai_usage.budget_exceeded(tid)
    if over:
        _note_ai_error(over, tid)
        return None, ''
    tier = tier or ('fast' if interactive else 'strong')
    creds = credentials() if creds is None else creds
    token = _tenant_var.set(tid)
    try:
        for cred in creds:
            if deadline is not None and time.time() >= deadline:
                break
            resolved = llm_providers.resolve(cred)
            if not resolved.ok:
                _note_ai_error(f'{cred.masked}：{resolved.error}')
                continue
            models = ai_usage.order_models(cred.id, resolved.for_tier(tier))   # 最近常失敗的排後面
            if resolved.provider == 'gemini':
                text = gemini_generate(prompt, max_tokens=max_tokens, temperature=temperature, api_key=cred.key,
                                       json_mode=json_mode, interactive=interactive, patient=patient, models=models,
                                       prefix=prefix, cred=cred if resolved.auto else None, deadline=deadline)
                if text:
                    _error_state()['ok_at'] = time.time()
                    return text, 'gemini'
                _note_ai_error(f"{resolved.label}：{_gemini_state()['error'] or '暫停呼叫中'}")
                continue
            text = _call_other(cred, resolved, models, prompt, max_tokens, temperature, interactive, patient, prefix,
                               deadline=deadline)
            if text:
                _error_state()['ok_at'] = time.time()
                return text, resolved.provider
        return None, ''
    finally:
        _tenant_var.reset(token)


def provider_names(providers):
    """{'gemini', 'anthropic'} → 'Google Gemini、Anthropic Claude' (訊息顯示用)"""
    from app.services.llm_providers import PROVIDERS
    return '、'.join(PROVIDERS.get(p, (p,))[0] for p in sorted(providers)) if providers else ''


def _note_ai_error(text, tenant_id=None):
    _error_state(tenant_id).update(at=time.time(), text=str(text)[:200])


def _time_left(deadline):
    """離 deadline 還有幾秒 (None = 沒有時間上限)"""
    return None if deadline is None else deadline - time.time()


def _sleep_before_retry(seconds, deadline):
    left = _time_left(deadline)
    if left is None or seconds < left:   # 等完就超過時間上限時不等 (下一次嘗試前會發現時間到了)
        time.sleep(seconds)


def _call_other(cred, resolved, models, prompt, max_tokens, temperature, interactive, patient, prefix='', deadline=None):
    """Gemini 以外的廠商：每個模型試 1 / 2 / 3 次 (即時 / 一般 / 耐心)，過載間隔拉長。
    金鑰錯誤、限流、過載 (逾時 / 連線 / 5xx)、模型不存在才讓整組金鑰冷卻；拒答 / 空回應 / 其他 4xx 只跟這次請求的內容有關，
    只算這次失敗 (不然顧客一句會被拒答的問題就讓全店的雲端 AI 停 20 秒)"""
    if not patient and cooldown_left(cred.id):
        return None
    attempts = 1 if interactive else 3 if patient else 2
    cool = ''   # 服務層級的失敗 (要冷卻)：'rate' / 'overload'
    tid = _current_tenant()
    for model in models[:1] if interactive else models:
        for attempt in range(1, attempts + 1):
            left = _time_left(deadline)
            if left is not None and left < 1:
                break
            extra = {} if left is None else {'timeout': left}   # 只有翻譯 (有時間上限) 才帶，保持既有呼叫方式
            started = time.time()
            try:
                if llm_providers.PROVIDERS[resolved.provider][1] == 'anthropic':
                    text = llm_providers.call_anthropic(cred.key, model, prompt, max_tokens, interactive, prefix=prefix, **extra)
                else:
                    text = llm_providers.call_openai_compatible(resolved.provider, cred.key, model, prompt, max_tokens,
                                                                temperature, interactive, prefix=prefix, **extra)
                ai_usage.record(tid, resolved.provider, model, True, getattr(text, 'usage', None), time.time() - started, cred.id)
                return text
            except llm_providers.ProviderError as exc:
                ai_usage.record(tid, resolved.provider, model, False, None, time.time() - started, cred.id)
                _note_ai_error(f'{resolved.label} {model}：{exc.detail or exc.reason}')
                logger.warning('%s (%s) 失敗：%s，第 %d 次', resolved.label, model, exc, attempt)
                if exc.reason == 'auth':
                    _set_cooldown(cred.id, COOLDOWN['auth'])
                    llm_providers.forget(cred)   # 金鑰可能被換掉：下次重新辨識
                    return None
                if exc.reason == 'notfound':
                    cool = 'overload'
                    llm_providers.forget(cred)   # 模型下架：下次重新查模型清單
                    break
                if exc.reason == 'rate':
                    cool = 'rate'
                    break   # 換下一個模型
                if exc.reason in ('refusal', 'other'):
                    break   # 這次請求的內容問題：換下一個模型，不冷卻
                cool = 'overload'
                if attempt < attempts:
                    _sleep_before_retry(RETRY_DELAY * ((2, 5)[attempt - 1] if patient else 1), deadline)
    if cool:
        _set_cooldown(cred.id, COOLDOWN[cool])
    return None


def _note_error(status, detail, model):
    _gemini_state().update(error_at=time.time(), error=f'{status} {detail}'.strip(), model=model)


def _extract_text(payload):
    parts = (payload.get('candidates') or [{}])[0].get('content', {}).get('parts') or []
    # 思考摘要 (thought) 不是回答
    texts = [p.get('text') or '' for p in parts if isinstance(p, dict) and not p.get('thought')]
    return ''.join(texts).strip()


def _gemini_usage(payload):
    u = payload.get('usageMetadata') or {}
    cached = int(u.get('cachedContentTokenCount') or 0)
    return {'input': max(0, int(u.get('promptTokenCount') or 0) - cached), 'cache_read': cached,
            'output': int(u.get('candidatesTokenCount') or 0) + int(u.get('thoughtsTokenCount') or 0)}


def gemini_generate(prompt, max_tokens=800, temperature=0.3, api_key=None, json_mode=False, interactive=False, patient=False,
                    models=None, prefix='', cred=None, deadline=None):
    """
    呼叫 Gemini；失敗回傳 None。
    每個模型最多試 2 次 (503 / 5xx / 逾時後等 RETRY_DELAY 秒重試)，主要模型失敗改用備援模型；
    金鑰錯誤 (401/403) 立刻冷卻 60 秒，限流 (429) 換模型再試、都不行冷卻 30 秒，過載 / 連線問題 / 模型不存在冷卻 20 秒；
    空回應 / 其他 4xx 只跟這次請求有關，不冷卻。
    interactive=True (即時對話)：只試主要模型一次、讀取逾時較短，失敗立刻回 None 並冷卻，呼叫端馬上改用本機模型。
    patient=True (店家按下後願意等的批次工作，例如 AI 翻譯菜單)：不理會冷卻、每個模型試 3 次並逐次拉長等待
    (RETRY_DELAY × 2 / 5 秒)；Gemini 尖峰時段的 503 通常幾秒內就會恢復。
    cred：自動挑選模型的金鑰；回 404 (模型下架 / 改名) 時清掉辨識結果，下次重新查模型清單。
    deadline：見 cloud_generate。
    """
    api_key = api_key or _api_key()
    cid = key_id(api_key)
    if not api_key or (not patient and cooldown_left(cid)):
        return None
    # Gemini 2.5 起的模型預設會思考，思考 token 也算在 maxOutputTokens 裡：上限太小 (客服 300、測試連線 50) 會在
    # 思考完之前就用完而沒有回答。比照 Claude 拉高下限 (只是上限，費用依實際用量計)；回答長度由提示控制
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max(max_tokens, GEMINI_MIN_OUTPUT_TOKENS[bool(interactive)]),
                                 "temperature": temperature}}
    if prefix:   # 固定的前段：Gemini 會自動快取重複的開頭 (隱式快取)
        body["systemInstruction"] = {"parts": [{"text": prefix}]}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    tid = _current_tenant()

    def done(model, ok, started, payload=None):
        ai_usage.record(tid, 'gemini', model, ok, _gemini_usage(payload) if payload else None, time.time() - started, cid)
    reason = ''   # 服務層級的失敗 (要冷卻)：'rate' / 'overload'；空 = 只有這次請求的內容問題
    models = list(models or gemini_models())
    models = models[:1] if interactive else models
    attempts = (1,) if interactive else (1, 2, 3) if patient else (1, 2)
    base_timeout = INTERACTIVE_TIMEOUT if interactive else GEMINI_TIMEOUT

    def pause(attempt):
        if attempt < len(attempts):
            _sleep_before_retry(RETRY_DELAY * ((2, 5)[attempt - 1] if patient else 1), deadline)
    out_of_time = False
    for model in models:
        for attempt in attempts:
            left = _time_left(deadline)
            if left is not None and left < 1:
                out_of_time = True
                break
            timeout = base_timeout if left is None else (min(base_timeout[0], left), min(base_timeout[1], left))
            started = time.time()
            try:
                resp = requests.post(GEMINI_URL.format(model=model), timeout=timeout,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, json=body)
            except requests.RequestException as e:
                done(model, False, started)
                _note_error('', f'連線異常 {e.__class__.__name__}', model)
                logger.warning('Gemini (%s) 連線異常 (%s)，第 %d 次', model, e, attempt)
                reason = reason or 'overload'
                pause(attempt)
                continue
            except UnicodeError:   # 金鑰含零寬空白 / 全形字等，HTTP 標頭無法編碼：等同金鑰無效
                _note_error('', llm_providers.INVALID_KEY_CHARS, model)
                _set_cooldown(cid, COOLDOWN['auth'])
                logger.warning('Gemini 金鑰含無效字元，冷卻 %d 秒', COOLDOWN['auth'])
                return None
            status = resp.status_code
            if status == 200:
                try:
                    payload = resp.json()
                    text = _extract_text(payload)
                except (ValueError, AttributeError):
                    payload, text = None, ''
                done(model, bool(text), started, payload if isinstance(payload, dict) else None)
                if text:
                    _gemini_state().update(ok_at=time.time(), model=model, error='')
                    return text
                _note_error(status, '回應沒有內容', model)
                break
            done(model, False, started)
            detail = ''
            try:
                detail = (resp.json().get('error') or {}).get('message', '')[:160]
            except (ValueError, AttributeError):
                pass
            _note_error(status, detail, model)
            if status in (401, 403):
                _set_cooldown(cid, COOLDOWN['auth'])
                logger.warning('Gemini 金鑰無效或無權限 (%s)，冷卻 %d 秒', status, COOLDOWN['auth'])
                return None
            if status == 429:
                reason = 'rate'
                logger.warning('Gemini (%s) 被限流 (429)，改用下一個模型', model)
                break
            if status in (500, 502, 503, 504):
                reason = reason or 'overload'
                logger.warning('Gemini (%s) 回應 %s (過載)，第 %d 次%s', model, status, attempt,
                               '，即時對話直接改用本機模型' if interactive else '')
                pause(attempt)
                continue
            if status == 404:
                reason = reason or 'overload'
                if cred is not None:
                    llm_providers.forget(cred)   # 自動挑到的模型下架 / 改名：下次重新查模型清單
            logger.warning('Gemini (%s) 回應 %s %s，改用下一個模型', model, status, detail)
            break
        if out_of_time:
            break
    if reason:
        _set_cooldown(cid, COOLDOWN[reason])
        logger.warning('Gemini 全部模型失敗，冷卻 %d 秒後再試；改用下一層', COOLDOWN[reason])
    else:
        logger.warning('Gemini 全部模型失敗 (%s)；改用下一層', '時間到' if out_of_time else '這次請求的回應有問題，不冷卻')
    return None


_load_lock = threading.Lock()


def _get_local_llm():
    """第一次需要時才載入本機模型 (llama-cpp-python 為選用套件)；失敗原因記在 _local_llm_error，不重複嘗試。
    多個執行緒同時要求時只載入一次 (其他人等它載完)。"""
    if _local_llm_loaded:
        return _local_llm
    with _load_lock:
        return _load_local_llm()


def _load_local_llm():
    global _local_llm, _local_llm_loaded, _local_llm_error
    if not _local_llm_loaded:
        path = current_app.config.get('LOCAL_MODEL_PATH')
        if not path:
            _local_llm_error = '未設定本機模型路徑 (LOCAL_MODEL_PATH)'
        elif not os.path.exists(path):
            _local_llm_error = f'找不到模型檔：{path}'
        else:
            try:
                from llama_cpp import Llama
            except ImportError:
                _local_llm_error = f'未安裝 llama-cpp-python：{LLAMA_INSTALL_HINT}'
            else:
                try:
                    _local_llm = Llama(model_path=path, n_ctx=2048, n_batch=512, n_threads=4, verbose=False)
                    atexit.register(_close_local_llm)
                    _local_llm_error = ''
                except Exception as e:  # noqa: BLE001
                    _local_llm_error = f'模型載入失敗：{e}'
        if _local_llm_error:
            logger.warning('本機 SLM 無法使用：%s (改用規則引擎)', _local_llm_error)
        _local_llm_loaded = True   # 載入完成 (或確定無法載入) 後才標記，其他執行緒不會拿到載入到一半的狀態
    return _local_llm


def warm_local_llm_async():
    """在背景先載入本機模型 (客服對話開始時呼叫)：Gemini 503 時改用本機模型不必等載入，顧客感覺不到切換。
    沒設定模型路徑或已經載入 / 試過時不做事。"""
    if _local_llm_loaded or not current_app.config.get('LOCAL_MODEL_PATH'):
        return None
    app = current_app._get_current_object()

    def run():
        with app.app_context():
            _get_local_llm()
    t = threading.Thread(target=run, name='local-llm-warmup', daemon=True)
    t.start()
    return t


def _close_local_llm():
    """
    程式結束前主動釋放模型。若留給 Llama.__del__ 在直譯器關閉階段處理，llama_cpp 內部函式可能已被清空，
    會印出「Exception ignored ... 'NoneType' object is not callable」。
    """
    global _local_llm
    llm, _local_llm = _local_llm, None
    if llm is not None:
        try:
            llm.close()
        except Exception:  # noqa: BLE001
            pass


DEFAULT_SYSTEM = '你是一位專業餐飲營運顧問，請嚴格使用台灣繁體中文回答，禁止思考草稿。'


def run_local_slm(prompt_text, max_tokens=100, system=None, stop=None, temperature=0.3, lock_timeout=3.0):
    """本機 Qwen 推論 (ChatML)；模型不可用或忙碌中回 None"""
    llm = _get_local_llm()
    if llm is None or not _llm_lock.acquire(timeout=lock_timeout):
        return None
    try:
        llm.reset()
        prompt = (f"<|im_start|>system\n{system or DEFAULT_SYSTEM}<|im_end|>\n"
                  f"<|im_start|>user\n{prompt_text}<|im_end|>\n<|im_start|>assistant\n")
        res = llm(prompt, max_tokens=max_tokens, temperature=temperature, stop=stop or ["<|im_end|>", "\n\n"])
        return res["choices"][0]["text"].strip()
    except Exception as e:  # noqa: BLE001
        logger.warning('本機 SLM 推論失敗：%s', e)
        return None
    finally:
        _llm_lock.release()



def ai_status():
    """系統健康頁：各組雲端 AI 金鑰、本月用量與本機模型目前的狀態"""
    path = current_app.config.get('LOCAL_MODEL_PATH') or ''
    try:
        import llama_cpp  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    providers = []
    for cred in credentials():
        # 只讀快取：快取過期時不在網頁請求裡查各家模型清單 (離線門市每次要等逾時)，改在背景辨識
        try:
            r = llm_providers.resolve(cred, cached_only=True)
        except Exception as exc:  # noqa: BLE001  單組金鑰的問題不讓整頁 500
            logger.exception('AI 金鑰 %s 狀態查詢失敗', cred.masked)
            r = llm_providers.Resolved(error=f'狀態查詢失敗 ({exc.__class__.__name__})')
        health = {m: ai_usage.health(cred.id, m) for m in dict.fromkeys(r.models + r.for_tier('fast'))}
        providers.append({'source': cred.source, 'key': cred.masked, 'provider': r.label, 'models': r.models,
                          'fast': r.for_tier('fast'), 'auto': r.auto, 'error': r.error, 'health': health,
                          'cooldown_seconds': int(cooldown_left(cred.id))})
    env_key = _api_key()
    gem = _gemini_state()   # 只看目前店家的紀錄
    return {
        'providers': providers,
        'last_error': last_ai_error(),
        'usage': ai_usage.month_summary(_current_tenant()),
        'gemini': {
            'configured': has_api_key(),
            'models': gemini_models(),
            'cooldown_seconds': int(cooldown_left(key_id(env_key))) if env_key else 0,
            'last_ok': gem['ok_at'],
            'last_error': gem['error'],
            'last_error_at': gem['error_at'],
            'last_model': gem['model'],
        },
        'local': {
            'installed': installed,
            'model_path': path,
            'model_exists': bool(path and os.path.exists(path)),
            'loaded': _local_llm is not None,
            'tried': _local_llm_loaded,
            'error': _local_llm_error,
            'install_hint': LLAMA_INSTALL_HINT,
        },
    }


def extract_json(text):
    """從模型回應中取出第一個 JSON 物件 / 陣列 (容忍 ```json 圍欄與前後說明)；失敗回 None"""
    import json
    if not text:
        return None
    text = text.strip()
    m = re.search(r'```(?:json)?\s*(.*?)```', text, re.S)
    if m:
        text = m.group(1).strip()
    candidates = sorted((text.find(o), o, c) for o, c in (('{', '}'), ('[', ']')) if text.find(o) >= 0)
    for start, _opener, closer in candidates:   # 先出現的括號優先 (陣列裡的物件不會被誤抓成答案)
        end = text.rfind(closer)
        if end <= start:
            continue
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            continue
    return None


def _joined(prefix, prompt):
    return f'{prefix}\n{prompt}' if prefix else prompt


def generate_json(prompt, max_tokens=600, system=None, local_max_tokens=None, interactive=False, prefix='', tier=None):
    """結構化輸出：雲端 AI (依序備援) → 本機 SLM；回 (解析後的物件, 來源) 或 (None, '')。
    interactive / prefix / tier 見 cloud_generate (prefix = 每次都一樣的前段，prompt = 這次才有的內容)"""
    text, source = cloud_generate(prompt, max_tokens=max_tokens, temperature=0.1, json_mode=True, interactive=interactive,
                                  prefix=prefix, tier=tier)
    data = extract_json(text)
    if data is not None:
        return data, source
    data = extract_json(run_local_slm(_joined(prefix, prompt), max_tokens=local_max_tokens or max_tokens, system=system,
                                      stop=["<|im_end|>"], temperature=0.1))
    if data is not None:
        return data, 'local'
    return None, ''


def generate_text(prompt, max_tokens=400, system=None, local_max_tokens=None, temperature=0.3, interactive=False, prefix='',
                  tier=None):
    """自由文字：雲端 AI (依序備援) → 本機 SLM；回 (文字, 來源) 或 (None, '')。interactive / prefix / tier 見 cloud_generate"""
    text, source = cloud_generate(prompt, max_tokens=max_tokens, temperature=temperature, interactive=interactive,
                                  prefix=prefix, tier=tier)
    if text:
        return text, source
    text = run_local_slm(_joined(prefix, prompt), max_tokens=local_max_tokens or max_tokens, system=system,
                         temperature=temperature, lock_timeout=10.0 if interactive else 3.0)
    if text:
        return text, 'local'
    return None, ''


def _clean_text(text, min_len=10):
    """去掉引號、Markdown 符號與編號，補上句號；太短視為失敗"""
    if not text:
        return None
    text = re.sub(r'[\(\[\（]\s*\d+\s*[\)\]\）]', '', text)
    text = re.sub(r'["\'「」*`]', '', text).strip(' ，,。\n')
    if len(text) < min_len:
        return None
    return text if text.endswith(('。', '！', '!', '；', ';')) else text + '。'


# ---------------------------------------------------------------------------
# 菜單文案
# ---------------------------------------------------------------------------
def fallback_menu_description(name, category):
    intros = ["嚴選優質新鮮食材現點現做", "特選產地直送原料慢火細熬", "黃金比例獨門秘方精準調配", "主廚匠心工藝悉心烹調", "保留食材純粹原汁原味"]
    traits = ["香氣四溢且口感層次鮮明", "風味醇厚濃郁且回味無窮", "外酥內嫩且肉汁豐盈飽滿", "口感清爽甘醇且韻味悠長", "每一口都帶來無與倫比的美味體驗"]
    return f"{random.choice(intros)}，【{name}】{random.choice(traits)}。"


def generate_menu_description(name, category):
    prompt = (f"你是一位專業的美食文案師。請直接輸出最終的繁體中文介紹，禁止輸出思考過程或英文。\n"
              f"為餐點【{name}】(分類：{category or '特色美饌'}) 撰寫 2~3 句、70 字以內的商品介紹，"
              f"聚焦口感層次、料理手法或風味特色，不要編號、引號或 Markdown。")
    text = _clean_text(cloud_generate(prompt, max_tokens=1200, temperature=0.2, tier='fast')[0], min_len=12)
    if text is None:
        text = _clean_text(run_local_slm(f"請為餐點【{name}】（分類：{category or '美饌'}）撰寫一段 50 字以內的誘人繁體中文商品介紹，直接輸出文案。", 120), 12)
    return text or fallback_menu_description(name, category)


# ---------------------------------------------------------------------------
# 營運建議
# ---------------------------------------------------------------------------
def fallback_advice(a):
    pending, eta, aov = a.get('pending_count', 0), a.get('eta_minutes', 0), a.get('avg_order_value', 0)
    if pending >= 5 or eta > 20:
        return f"⚠️ 廚房負載偏高（{pending} 筆待製作，預估等待 {eta} 分鐘），建議啟動備料支援並暫緩外帶出餐推播。"
    if 0 < aov < 150:
        hot = a['top_items'][0]['name'] if a.get('top_items') else '熱門餐點'
        return f"💡 平均客單價（${int(aov)}）偏低，建議前台推廣「{hot}」加料升級或推播點數滿額加價購優惠券。"
    if pending == 0 and a.get('today_orders', 0) > 0:
        return "✅ 目前出餐流程順暢無積單，可安排前台進行備料盤點與清潔。"
    return "🌟 今日營業剛起步，請確認廚房出單機與各項食材庫存是否充足。"


def _advice_prompt(a):
    top = ", ".join(f"{i['name']}({i['quantity']}份)" for i in a.get('top_items', [])) or "尚無"
    return (f"今日訂單：{a.get('today_orders', 0)}筆、營收：NT${a.get('today_revenue', 0)}、客單價：NT${int(a.get('avg_order_value', 0))}、"
            f"待製作：{a.get('pending_count', 0)}筆、熱銷：{top}。請給出60字以內繁體中文營運行動建議，不要客套，必須有完整句號收尾。")


def _offline_advice(a):
    return _clean_text(run_local_slm(_advice_prompt(a)), 10) or fallback_advice(a)


def _fetch_advice(app, tenant_id, analytics, signature, creds):
    entry = _advice_cache[tenant_id]
    try:
        with app.app_context():
            text = _clean_text(cloud_generate(_advice_prompt(analytics), creds=creds, tier='fast', tenant_id=tenant_id)[0], 12) \
                or _offline_advice(analytics)
        entry.update(advice=text, signature=signature)
    except Exception:  # noqa: BLE001  執行緒丟例外也要清掉 fetching，不然這家店的建議到重啟前都不會再更新
        logger.exception('AI 營運建議產生失敗 (店家 %s)，改用規則引擎', tenant_id)
        entry.update(advice=fallback_advice(analytics), signature=signature)   # 訂單有變動時再試
    finally:
        entry['fetching'] = False


def generate_ai_business_advice(analytics, tenant_id, signature):
    """
    回傳目前的營運建議。signature 為今日訂單狀態摘要：沒有變動時直接用快取；
    有變動且可呼叫 Gemini 時在背景更新，這次先回傳舊建議或規則引擎的建議。
    """
    entry = _advice_cache.setdefault(tenant_id, {'signature': None, 'advice': None, 'fetching': False})
    if entry['signature'] == signature and entry['advice']:
        return entry['advice']
    creds = credentials(tenant_id)
    if creds:
        if not entry['fetching']:
            entry['fetching'] = True
            threading.Thread(target=_fetch_advice, daemon=True,
                             args=(current_app._get_current_object(), tenant_id, analytics, signature, creds)).start()
        return entry['advice'] or fallback_advice(analytics)
    entry.update(advice=_offline_advice(analytics), signature=signature)
    return entry['advice']


def invalidate_ai_advice(tenant_id):
    entry = _advice_cache.get(tenant_id)
    if entry:
        entry['signature'] = None


# ---------------------------------------------------------------------------
# 菜單翻譯：Gemini 一次翻一批 → 規則字典 (常見分類 / 品項字眼) → 留白
# ---------------------------------------------------------------------------
# 每批品項數：英 + 日文 JSON 每項約 150~200 token，25 項會超過輸出上限被截斷 (整批解析失敗)；10 項加上 8192 上限留足空間
TRANSLATE_BATCH = 10
TRANSLATE_MAX_TOKENS = 8192
TRANSLATE_LANGS = ('en', 'ja')
TRANSLATE_WORKERS = 3   # 同時送出的批次數 (大菜單快很多；太多容易被限流)
GLOSSARY_HINTS = 20     # 每批最多附幾條店家既有譯名


def rule_translate(text: str) -> dict[str, str]:
    """規則字典 (app/services/menu_dictionary.py)：整個名稱 (可含「招牌」等前綴、「(大)」規格) 都認得才翻，
    避免把「招牌和牛堡」亂翻；回 {'en': ..., 'ja': ...} (沒命中為空字串)"""
    from app.services.menu_dictionary import lookup
    hit = lookup(text)
    return {'en': hit[0], 'ja': hit[1]} if hit else {'en': '', 'ja': ''}


def _translate_prompt(entries: list[dict], hint: str = '') -> str:
    rows = '\n'.join(f'{i + 1}. {e.get("name", "")}' + (f'｜{e["description"]}' if e.get('description') else '')
                     for i, e in enumerate(entries))
    return ('你是餐廳菜單翻譯。把下列繁體中文餐點名稱 (與「｜」之後的說明，若有) 翻成自然的英文與日文，'
            '保留品牌 / 專有名詞、數字與規格，不要加解釋。只輸出 JSON 陣列，每個元素形如 '
            '{"i": 編號, "name_en": "", "name_ja": "", "description_en": "", "description_ja": ""}，沒有說明時留空字串。\n'
            f'{hint}{rows}')


def _glossary_hint(batch: list[dict], glossary: dict) -> str:
    """這批品名裡出現的店家既有譯名 (例如「牛肉麵」已譯為 Beef Noodle Soup)：請 AI 沿用，整份菜單用詞一致"""
    names = ' '.join(e.get('name', '') for e in batch)
    hits = sorted((zh for zh, v in glossary.items() if len(zh) >= 2 and zh in names and v.get('en') and v.get('ja')),
                  key=len, reverse=True)[:GLOSSARY_HINTS]
    if not hits:
        return ''
    return '店家已採用的譯名 (出現相同字詞時請沿用)：\n' + '\n'.join(
        f'{zh} = {glossary[zh]["en"]} / {glossary[zh]["ja"]}' for zh in hits) + '\n要翻譯的項目：\n'


def _parse_translations(text: str | None, count: int) -> dict[int, dict[str, str]]:
    """Gemini 回應 → {編號(0 起): {name_en, ...}}；格式不對就當作失敗 (空 dict)。
    回應被截斷 (超過輸出上限) 時整個陣列無法解析，改逐一解析完整的 {...}，已翻好的不浪費。"""
    import json
    if not text:
        return {}
    rows = None
    m = re.search(r'\[.*\]', text, re.S)
    if m:
        try:
            rows = json.loads(m.group(0))
        except ValueError:
            rows = None
    if not isinstance(rows, list):
        rows = []
        for obj in re.findall(r'\{[^{}]*\}', text):
            try:
                rows.append(json.loads(obj))
            except ValueError:
                continue
    out: dict[int, dict[str, str]] = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            idx = int(row.get('i')) - 1
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 <= idx < count:
            out[idx] = {k: str(row.get(k) or '').strip()[:300] for k in ('name_en', 'name_ja', 'description_en', 'description_ja')}
    return out


TRANSLATE_TIME_BUDGET = 150   # 秒：一次按鈕最多花這麼久 (Gemini 尖峰 503 時重試)；剩下的再按一次只翻還沒翻的


def translate_entries(entries: list[dict], stats: dict | None = None,
                      glossary: dict | None = None) -> tuple[list[dict[str, str]], str]:
    """翻譯一批 {name, description}：回 ([{name_en, name_ja, description_en, description_ja}, ...], 來源 'gemini' / 'rules'…)。
    glossary：店家既有譯名 {中文: {'en', 'ja'}} —— 品名完全相同且沒有說明的直接沿用 (不送 AI)，其他的附在提示裡請 AI 沿用用詞。
    雲端 AI 失敗 (沒金鑰 / 過載 / 格式錯) 的項目用店家譯名 / 規則字典，只有整字命中的才有值。
    批次同時送出 TRANSLATE_WORKERS 批；過載 (503) 很常見且通常幾秒後恢復：每批耐心重試 (patient)，失敗的批次在其他批次之後
    再補跑一輪；超過 TRANSLATE_TIME_BUDGET 就停止。
    每筆結果另有 'source'：'ai' (雲端 AI 翻的) / 'store' (店家譯名) / 'rules' (規則字典備援)。
    stats (可省略) 會填入 {'ai': 雲端 AI 翻好的項數, 'store': 沿用店家譯名的項數, 'fallback': 項數, 'provider': 廠商}。"""
    glossary = glossary or {}
    results: list[dict[str, str]] = [{'name_en': '', 'name_ja': '', 'description_en': '', 'description_ja': ''} for _ in entries]
    done = [False] * len(entries)
    reused = 0
    for i, entry in enumerate(entries):
        hit = glossary.get(entry.get('name', ''))
        if hit and hit.get('en') and hit.get('ja') and not entry.get('description'):
            results[i].update(name_en=hit['en'], name_ja=hit['ja'], source='store')
            done[i], reused = True, reused + 1
    deadline = time.time() + TRANSLATE_TIME_BUDGET
    creds = credentials()
    tid = _current_tenant()
    app = current_app._get_current_object()  # type: ignore[attr-defined]
    providers: set[str] = set()
    todo = [i for i in range(len(entries)) if not done[i]]
    batches = [todo[n:n + TRANSLATE_BATCH] for n in range(0, len(todo), TRANSLATE_BATCH)]

    def run(idx):
        if time.time() > deadline:
            return idx, {}, ''
        batch = [entries[i] for i in idx]
        with app.app_context():
            text, source = cloud_generate(_translate_prompt(batch, _glossary_hint(batch, glossary)),
                                          max_tokens=TRANSLATE_MAX_TOKENS, temperature=0.1, json_mode=True, patient=True,
                                          creds=creds, tier='strong', tenant_id=tid, deadline=deadline)
        return idx, _parse_translations(text, len(batch)), source
    for _round in range(2):
        if not batches:
            break
        if TRANSLATE_WORKERS > 1 and len(batches) > 1:
            with ThreadPoolExecutor(max_workers=min(TRANSLATE_WORKERS, len(batches)), thread_name_prefix='ai-translate') as pool:
                outcomes = list(pool.map(run, batches))
        else:
            outcomes = [run(b) for b in batches]
        failed = []
        for idx, parsed, source in outcomes:
            if parsed:
                providers.add(source)
            else:
                failed.append(idx)
            for n, i in enumerate(idx):
                got = parsed.get(n)
                if got and got.get('name_en'):
                    results[i], done[i] = dict(got, source='ai'), True
        # 達每月預算上限時不再等候補跑 (cloud_generate 一律直接失敗)
        if not failed or not has_api_key() or time.time() > deadline or ai_usage.budget_exceeded(tid):
            break
        batches = failed
        time.sleep(RETRY_DELAY * 5)   # 補跑前再等一下，讓 AI 服務的尖峰過去
    for i, entry in enumerate(entries):
        if not done[i]:
            hit = glossary.get(entry.get('name', '')) or {}
            store = bool(hit.get('en') and hit.get('ja'))
            rule = {'en': hit['en'], 'ja': hit['ja']} if store else rule_translate(entry.get('name', ''))
            results[i].update(name_en=rule['en'], name_ja=rule['ja'], source='store' if store else 'rules')
    ai_count = sum(done) - reused
    if stats is not None:
        stats.update(ai=ai_count, store=reused, fallback=len(entries) - ai_count - reused, provider=provider_names(providers))
    if ai_count:
        return results, sorted(providers)[0] if providers else 'ai'
    return results, 'store' if reused else 'rules'
