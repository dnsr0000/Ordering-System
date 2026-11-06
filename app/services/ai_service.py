"""
AI 文案與營運建議，三層降級：Google Gemini → 本機 Qwen2.5 SLM (選用) → 規則引擎。
- Gemini：主要模型 (GEMINI_MODEL) 回 503 / 5xx 或逾時先重試一次，再改用備援模型 (GEMINI_FALLBACK_MODEL)；
  都失敗時依原因冷卻 (金鑰錯誤 60 秒、限流 30 秒、過載 20 秒)，期間直接使用下一層。最近一次錯誤可在系統健康頁看到。
- 本機 SLM：需要 llama-cpp-python 與模型檔；沒裝套件 / 找不到檔案時記錄原因 (ai_status) 並降到規則引擎，不會一直重試載入。
- 營運建議以「今日訂單狀態」為快取鍵，只有訂單變動才重新產生，且在背景執行緒呼叫 API，不拖慢頁面。
"""
import atexit
import logging
import os
import random
import re
import threading
import time

import requests
from flask import current_app

from app.services import llm_providers  # 載入時也註冊系統設定「AI 服務」的測試連線

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT = (5, 30)        # (連線, 讀取) 秒
# 即時對話 (客服機器人、語音點餐)：顧客在等，不重試也不換模型；503 / 5xx / 限流 / 逾時直接改用本機模型，
# 並進入冷卻，接下來的問題在冷卻期間直接由本機模型回答 (顧客看不到中斷)
INTERACTIVE_TIMEOUT = (3, 12)
RETRY_DELAY = 1.0               # 503 / 5xx / 逾時後重試前等待 (測試可改為 0)
COOLDOWN = {'auth': 60, 'rate': 30, 'overload': 20}
LLAMA_INSTALL_HINT = ('pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu '
                      '(預編譯 CPU 版，不需要 C++ 編譯環境)')
_PLACEHOLDER_KEYS = {"", "YOUR_API_KEY", "1234", "none"}
_gemini_cooldown_until = 0.0
_gemini_last: dict = {'ok_at': 0.0, 'error_at': 0.0, 'error': '', 'model': ''}   # 系統健康頁顯示
_local_llm = None
_local_llm_loaded = False
_local_llm_error = ''
_llm_lock = threading.Lock()
_advice_cache: dict = {}  # tenant_id -> {'signature', 'advice', 'fetching'}


def _api_key():
    key = (current_app.config.get('GEMINI_API_KEY') or '').strip()
    return None if key in _PLACEHOLDER_KEYS or '你的' in key else key


def _usable(key):
    key = (key or '').strip()
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


_last_error: dict = {'at': 0.0, 'ok_at': 0.0, 'text': ''}   # 最近一次雲端 AI 失敗 (系統監控頁 / 翻譯訊息)
_cooldowns: dict[str, float] = {}   # 金鑰 id -> 冷卻到何時 (Gemini 以外；Gemini 用 _gemini_cooldown_until)


def last_ai_error() -> str:
    """最近一次雲端 AI 呼叫失敗的原因 (之後已成功過就回空字串)；給「AI 翻譯」等功能告訴使用者為什麼改用備援"""
    if _last_error['at'] > _last_error['ok_at']:
        return _last_error['text']
    if time.time() < _gemini_cooldown_until and not _gemini_last['error']:
        return '暫停呼叫中 (剛才失敗，冷卻後會再試)'
    return _gemini_last['error'] if _gemini_last['error_at'] > _gemini_last['ok_at'] else ''


def gemini_models():
    """.env 指定的 Gemini 模型 (主要、備援)；沒指定時用 Google 的「最新版」別名 (自動挑選見 llm_providers)"""
    out = []
    for name in (current_app.config.get('GEMINI_MODEL'), current_app.config.get('GEMINI_FALLBACK_MODEL')):
        name = (name or '').strip()
        if name and name not in out:
            out.append(name)
    return out or ['gemini-flash-latest', 'gemini-flash-lite-latest']


def cloud_generate(prompt, max_tokens=800, temperature=0.3, json_mode=False, interactive=False, patient=False, creds=None):
    """
    依序試每一組金鑰 (自動辨識廠商與模型)，第一個成功的回 (文字, 廠商名稱 gemini / anthropic / openai…)；全部失敗回 (None, '')。
    同一家：Gemini 走 gemini_generate (重試 + 備援模型)；其他廠商依 interactive / patient 重試、過載換下一組金鑰。
    interactive (即時對話)：每組只試一次、逾時短，失敗的金鑰進冷卻；patient (店家願意等)：不理會冷卻、多試幾次。
    """
    creds = credentials() if creds is None else creds
    for cred in creds:
        resolved = llm_providers.resolve(cred)
        if not resolved.ok:
            _note_ai_error(f'{cred.masked}：{resolved.error}')
            continue
        if resolved.provider == 'gemini':
            text = gemini_generate(prompt, max_tokens=max_tokens, temperature=temperature, api_key=cred.key, json_mode=json_mode,
                                   interactive=interactive, patient=patient, models=resolved.models)
            if text:
                _last_error['ok_at'] = time.time()
                return text, 'gemini'
            _note_ai_error(f"{resolved.label}：{_gemini_last['error'] or '暫停呼叫中'}")
            continue
        text = _call_other(cred, resolved, prompt, max_tokens, temperature, interactive, patient)
        if text:
            _last_error['ok_at'] = time.time()
            return text, resolved.provider
    return None, ''


def provider_names(providers):
    """{'gemini', 'anthropic'} → 'Google Gemini、Anthropic Claude' (訊息顯示用)"""
    from app.services.llm_providers import PROVIDERS
    return '、'.join(PROVIDERS.get(p, (p,))[0] for p in sorted(providers)) if providers else ''


def _note_ai_error(text):
    _last_error.update(at=time.time(), text=str(text)[:200])


def _call_other(cred, resolved, prompt, max_tokens, temperature, interactive, patient):
    """Gemini 以外的廠商：每個模型試 1 / 2 / 3 次 (即時 / 一般 / 耐心)，過載間隔拉長；金鑰錯誤或全部失敗則冷卻"""
    if not patient and time.time() < _cooldowns.get(cred.id, 0):
        return None
    attempts = 1 if interactive else 3 if patient else 2
    reason = 'overload'
    for model in resolved.models[:1] if interactive else resolved.models:
        for attempt in range(1, attempts + 1):
            try:
                if llm_providers.PROVIDERS[resolved.provider][1] == 'anthropic':
                    return llm_providers.call_anthropic(cred.key, model, prompt, max_tokens, interactive)
                return llm_providers.call_openai_compatible(resolved.provider, cred.key, model, prompt, max_tokens,
                                                            temperature, interactive)
            except llm_providers.ProviderError as exc:
                _note_ai_error(f'{resolved.label} {model}：{exc.detail or exc.reason}')
                logger.warning('%s (%s) 失敗：%s，第 %d 次', resolved.label, model, exc, attempt)
                reason = exc.reason
                if exc.reason == 'auth':
                    _cooldowns[cred.id] = time.time() + COOLDOWN['auth']
                    llm_providers.forget(cred)   # 金鑰可能被換掉：下次重新辨識
                    return None
                if exc.reason == 'notfound':
                    llm_providers.forget(cred)   # 模型下架：下次重新查模型清單
                    break
                if exc.reason in ('rate', 'refusal', 'other'):
                    break   # 換下一個模型
                if attempt < attempts:
                    time.sleep(RETRY_DELAY * ((2, 5)[attempt - 1] if patient else 1))
    _cooldowns[cred.id] = time.time() + COOLDOWN['rate' if reason == 'rate' else 'overload']
    return None


def _note_error(status, detail, model):
    _gemini_last.update(error_at=time.time(), error=f'{status} {detail}'.strip(), model=model)


def _extract_text(payload):
    parts = (payload.get('candidates') or [{}])[0].get('content', {}).get('parts') or []
    return (parts[0].get('text') or '').strip() if parts else ''


def gemini_generate(prompt, max_tokens=800, temperature=0.3, api_key=None, json_mode=False, interactive=False, patient=False,
                    models=None):
    """
    呼叫 Gemini；失敗回傳 None。
    每個模型最多試 2 次 (503 / 5xx / 逾時後等 RETRY_DELAY 秒重試)，主要模型失敗改用備援模型；
    金鑰錯誤 (401/403) 立刻冷卻 60 秒，限流 (429) 換模型再試、都不行冷卻 30 秒，過載 / 連線問題冷卻 20 秒。
    interactive=True (即時對話)：只試主要模型一次、讀取逾時較短，失敗立刻回 None 並冷卻，呼叫端馬上改用本機模型。
    patient=True (店家按下後願意等的批次工作，例如 AI 翻譯菜單)：不理會冷卻、每個模型試 3 次並逐次拉長等待
    (RETRY_DELAY × 2 / 5 秒)；Gemini 尖峰時段的 503 通常幾秒內就會恢復。
    """
    global _gemini_cooldown_until
    api_key = api_key or _api_key()
    if not api_key or (not patient and time.time() < _gemini_cooldown_until):
        return None
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    reason = 'overload'
    models = list(models or gemini_models())
    models = models[:1] if interactive else models
    attempts = (1,) if interactive else (1, 2, 3) if patient else (1, 2)

    def pause(attempt):
        if attempt < len(attempts):
            time.sleep(RETRY_DELAY * ((2, 5)[attempt - 1] if patient else 1))
    for model in models:
        for attempt in attempts:
            try:
                resp = requests.post(GEMINI_URL.format(model=model), timeout=INTERACTIVE_TIMEOUT if interactive else GEMINI_TIMEOUT,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, json=body)
            except requests.RequestException as e:
                _note_error('', f'連線異常 {e.__class__.__name__}', model)
                logger.warning('Gemini (%s) 連線異常 (%s)，第 %d 次', model, e, attempt)
                pause(attempt)
                continue
            status = resp.status_code
            if status == 200:
                try:
                    text = _extract_text(resp.json())
                except ValueError:
                    text = ''
                if text:
                    _gemini_last.update(ok_at=time.time(), model=model, error='')
                    return text
                _note_error(status, '回應沒有內容', model)
                break
            detail = ''
            try:
                detail = (resp.json().get('error') or {}).get('message', '')[:160]
            except ValueError:
                pass
            _note_error(status, detail, model)
            if status in (401, 403):
                _gemini_cooldown_until = time.time() + COOLDOWN['auth']
                logger.warning('Gemini 金鑰無效或無權限 (%s)，冷卻 %d 秒', status, COOLDOWN['auth'])
                return None
            if status == 429:
                reason = 'rate'
                logger.warning('Gemini (%s) 被限流 (429)，改用下一個模型', model)
                break
            if status in (500, 502, 503, 504):
                logger.warning('Gemini (%s) 回應 %s (過載)，第 %d 次%s', model, status, attempt,
                               '，即時對話直接改用本機模型' if interactive else '')
                pause(attempt)
                continue
            logger.warning('Gemini (%s) 回應 %s %s，改用下一個模型', model, status, detail)
            break
    _gemini_cooldown_until = time.time() + COOLDOWN[reason]
    logger.warning('Gemini 全部模型失敗，冷卻 %d 秒後再試；改用下一層', COOLDOWN[reason])
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
    """系統健康頁：Gemini 與本機模型目前的狀態"""
    now = time.time()
    path = current_app.config.get('LOCAL_MODEL_PATH') or ''
    try:
        import llama_cpp  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    providers = []
    for cred in credentials():
        r = llm_providers.resolve(cred)
        cooldown = _gemini_cooldown_until if r.provider == 'gemini' else _cooldowns.get(cred.id, 0)
        providers.append({'source': cred.source, 'key': cred.masked, 'provider': r.label, 'models': r.models, 'auto': r.auto,
                          'error': r.error, 'cooldown_seconds': max(0, int(cooldown - now))})
    return {
        'providers': providers,
        'last_error': last_ai_error(),
        'gemini': {
            'configured': has_api_key(),
            'models': gemini_models(),
            'cooldown_seconds': max(0, int(_gemini_cooldown_until - now)),
            'last_ok': _gemini_last['ok_at'],
            'last_error': _gemini_last['error'],
            'last_error_at': _gemini_last['error_at'],
            'last_model': _gemini_last['model'],
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


def generate_json(prompt, max_tokens=600, system=None, local_max_tokens=None, interactive=False):
    """結構化輸出：雲端 AI (依序備援) → 本機 SLM；回 (解析後的物件, 來源) 或 (None, '')。interactive 見 cloud_generate"""
    text, source = cloud_generate(prompt, max_tokens=max_tokens, temperature=0.1, json_mode=True, interactive=interactive)
    data = extract_json(text)
    if data is not None:
        return data, source
    data = extract_json(run_local_slm(prompt, max_tokens=local_max_tokens or max_tokens, system=system,
                                      stop=["<|im_end|>"], temperature=0.1))
    if data is not None:
        return data, 'local'
    return None, ''


def generate_text(prompt, max_tokens=400, system=None, local_max_tokens=None, temperature=0.3, interactive=False):
    """自由文字：雲端 AI (依序備援) → 本機 SLM；回 (文字, 來源) 或 (None, '')。interactive 見 cloud_generate"""
    text, source = cloud_generate(prompt, max_tokens=max_tokens, temperature=temperature, interactive=interactive)
    if text:
        return text, source
    text = run_local_slm(prompt, max_tokens=local_max_tokens or max_tokens, system=system, temperature=temperature,
                         lock_timeout=10.0 if interactive else 3.0)
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
    text = _clean_text(cloud_generate(prompt, max_tokens=1200, temperature=0.2)[0], min_len=12)
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
    with app.app_context():
        text = _clean_text(cloud_generate(_advice_prompt(analytics), creds=creds)[0], 12) or _offline_advice(analytics)
    entry = _advice_cache[tenant_id]
    entry.update(advice=text, signature=signature, fetching=False)


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


def rule_translate(text: str) -> dict[str, str]:
    """規則字典 (app/services/menu_dictionary.py)：整個名稱 (可含「招牌」等前綴、「(大)」規格) 都認得才翻，
    避免把「招牌和牛堡」亂翻；回 {'en': ..., 'ja': ...} (沒命中為空字串)"""
    from app.services.menu_dictionary import lookup
    hit = lookup(text)
    return {'en': hit[0], 'ja': hit[1]} if hit else {'en': '', 'ja': ''}


def _translate_prompt(entries: list[dict]) -> str:
    rows = '\n'.join(f'{i + 1}. {e.get("name", "")}' + (f'｜{e["description"]}' if e.get('description') else '')
                     for i, e in enumerate(entries))
    return ('你是餐廳菜單翻譯。把下列繁體中文餐點名稱 (與「｜」之後的說明，若有) 翻成自然的英文與日文，'
            '保留品牌 / 專有名詞、數字與規格，不要加解釋。只輸出 JSON 陣列，每個元素形如 '
            '{"i": 編號, "name_en": "", "name_ja": "", "description_en": "", "description_ja": ""}，沒有說明時留空字串。\n'
            f'{rows}')


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


def translate_entries(entries: list[dict], stats: dict | None = None) -> tuple[list[dict[str, str]], str]:
    """翻譯一批 {name, description}：回 ([{name_en, name_ja, description_en, description_ja}, ...], 來源 'gemini' / 'rules')。
    Gemini 失敗 (沒金鑰 / 過載 / 格式錯) 的項目用規則字典，只有整字命中的才有值。
    Gemini 過載 (503) 很常見且通常幾秒後恢復：每批耐心重試 (patient)，失敗的批次在其他批次之後再補跑一輪；
    超過 TRANSLATE_TIME_BUDGET 就停止。stats (可省略) 會填入 {'ai': 雲端 AI 翻好的項數, 'fallback': 項數, 'provider': 廠商}。"""
    results: list[dict[str, str]] = [{'name_en': '', 'name_ja': '', 'description_en': '', 'description_ja': ''} for _ in entries]
    done = [False] * len(entries)
    deadline = time.time() + TRANSLATE_TIME_BUDGET
    creds = credentials()
    providers: set[str] = set()
    starts = list(range(0, len(entries), TRANSLATE_BATCH))
    for round_no in range(2):
        failed = []
        for start in starts:
            if time.time() > deadline:
                failed.append(start)
                continue
            batch = entries[start:start + TRANSLATE_BATCH]
            text, source = cloud_generate(_translate_prompt(batch), max_tokens=TRANSLATE_MAX_TOKENS, temperature=0.1,
                                          json_mode=True, patient=True, creds=creds)
            parsed = _parse_translations(text, len(batch))
            if parsed:
                providers.add(source)
            for i in range(len(batch)):
                got = parsed.get(i)
                if got and got.get('name_en'):
                    results[start + i], done[start + i] = got, True
            if not parsed:
                failed.append(start)
        if not failed or not has_api_key() or time.time() > deadline:
            break
        starts = failed
        time.sleep(RETRY_DELAY * 5)   # 補跑前再等一下，讓 Gemini 的尖峰過去
    for i, entry in enumerate(entries):
        if not done[i]:
            rule = rule_translate(entry.get('name', ''))
            results[i].update(name_en=rule['en'], name_ja=rule['ja'])
    ai_count = sum(done)
    if stats is not None:
        stats.update(ai=ai_count, fallback=len(entries) - ai_count, provider=provider_names(providers))
    return results, (sorted(providers)[0] if providers else 'ai') if ai_count else 'rules'
