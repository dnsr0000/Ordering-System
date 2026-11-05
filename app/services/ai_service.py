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

logger = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_TIMEOUT = (5, 30)        # (連線, 讀取) 秒
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


def has_api_key() -> bool:
    return _api_key() is not None


def gemini_models():
    """依序嘗試的模型：主要模型、備援模型 (去除重複 / 空白)"""
    out = []
    for name in (current_app.config.get('GEMINI_MODEL'), current_app.config.get('GEMINI_FALLBACK_MODEL')):
        name = (name or '').strip()
        if name and name not in out:
            out.append(name)
    return out or ['gemini-3.6-flash']


def _note_error(status, detail, model):
    _gemini_last.update(error_at=time.time(), error=f'{status} {detail}'.strip(), model=model)


def _extract_text(payload):
    parts = (payload.get('candidates') or [{}])[0].get('content', {}).get('parts') or []
    return (parts[0].get('text') or '').strip() if parts else ''


def gemini_generate(prompt, max_tokens=800, temperature=0.3, api_key=None, json_mode=False):
    """
    呼叫 Gemini；失敗回傳 None。
    每個模型最多試 2 次 (503 / 5xx / 逾時後等 RETRY_DELAY 秒重試)，主要模型失敗改用備援模型；
    金鑰錯誤 (401/403) 立刻冷卻 60 秒，限流 (429) 換模型再試、都不行冷卻 30 秒，過載 / 連線問題冷卻 20 秒。
    """
    global _gemini_cooldown_until
    api_key = api_key or _api_key()
    if not api_key or time.time() < _gemini_cooldown_until:
        return None
    body = {"contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}}
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    reason = 'overload'
    for model in gemini_models():
        for attempt in (1, 2):
            try:
                resp = requests.post(GEMINI_URL.format(model=model), timeout=GEMINI_TIMEOUT,
                                     headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, json=body)
            except requests.RequestException as e:
                _note_error('', f'連線異常 {e.__class__.__name__}', model)
                logger.warning('Gemini (%s) 連線異常 (%s)，第 %d 次', model, e, attempt)
                if attempt == 1:
                    time.sleep(RETRY_DELAY)
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
                logger.warning('Gemini (%s) 回應 %s (過載)，第 %d 次', model, status, attempt)
                if attempt == 1:
                    time.sleep(RETRY_DELAY)
                continue
            logger.warning('Gemini (%s) 回應 %s %s，改用下一個模型', model, status, detail)
            break
    _gemini_cooldown_until = time.time() + COOLDOWN[reason]
    logger.warning('Gemini 全部模型失敗，冷卻 %d 秒後再試；改用下一層', COOLDOWN[reason])
    return None


def _get_local_llm():
    """第一次需要時才載入本機模型 (llama-cpp-python 為選用套件)；失敗原因記在 _local_llm_error，不重複嘗試"""
    global _local_llm, _local_llm_loaded, _local_llm_error
    if not _local_llm_loaded:
        _local_llm_loaded = True
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
    return _local_llm


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


def local_available():
    return _get_local_llm() is not None


def ai_status():
    """系統健康頁：Gemini 與本機模型目前的狀態"""
    now = time.time()
    path = current_app.config.get('LOCAL_MODEL_PATH') or ''
    try:
        import llama_cpp  # noqa: F401
        installed = True
    except ImportError:
        installed = False
    return {
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


def generate_json(prompt, max_tokens=600, system=None, local_max_tokens=None):
    """結構化輸出：Gemini (JSON 模式) → 本機 SLM；回 (解析後的物件, 來源) 或 (None, '')"""
    data = extract_json(gemini_generate(prompt, max_tokens=max_tokens, temperature=0.1, json_mode=True))
    if data is not None:
        return data, 'gemini'
    data = extract_json(run_local_slm(prompt, max_tokens=local_max_tokens or max_tokens, system=system,
                                      stop=["<|im_end|>"], temperature=0.1))
    if data is not None:
        return data, 'local'
    return None, ''


def generate_text(prompt, max_tokens=400, system=None, local_max_tokens=None, temperature=0.3):
    """自由文字：Gemini → 本機 SLM；回 (文字, 來源) 或 (None, '')"""
    text = gemini_generate(prompt, max_tokens=max_tokens, temperature=temperature)
    if text:
        return text, 'gemini'
    text = run_local_slm(prompt, max_tokens=local_max_tokens or max_tokens, system=system, temperature=temperature)
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
    text = _clean_text(gemini_generate(prompt, max_tokens=1200, temperature=0.2), min_len=12)
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


def _fetch_advice(app, tenant_id, analytics, signature, api_key):
    with app.app_context():
        text = _clean_text(gemini_generate(_advice_prompt(analytics), api_key=api_key), 12) or _offline_advice(analytics)
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
    api_key = _api_key()
    if api_key and time.time() >= _gemini_cooldown_until:
        if not entry['fetching']:
            entry['fetching'] = True
            threading.Thread(target=_fetch_advice, daemon=True,
                             args=(current_app._get_current_object(), tenant_id, analytics, signature, api_key)).start()
        return entry['advice'] or fallback_advice(analytics)
    entry.update(advice=_offline_advice(analytics), signature=signature)
    return entry['advice']


def invalidate_ai_advice(tenant_id):
    entry = _advice_cache.get(tenant_id)
    if entry:
        entry['signature'] = None


# ---------------------------------------------------------------------------
# 菜單翻譯 (第六輪)：Gemini 一次翻一批 → 規則字典 (常見分類 / 品項字眼) → 留白
# ---------------------------------------------------------------------------
TRANSLATE_BATCH = 25
TRANSLATE_LANGS = ('en', 'ja')
# 常見分類與餐飲字眼 (沒有 API 金鑰時至少把分類翻好；品名只在整個字串命中時翻)
_RULE_WORDS: dict[str, tuple[str, str]] = {
    '主餐': ('Mains', 'メイン'), '副餐': ('Sides', 'サイド'), '飲料': ('Drinks', 'ドリンク'), '點心': ('Snacks', 'スナック'),
    '甜點': ('Desserts', 'デザート'), '湯品': ('Soups', 'スープ'), '套餐': ('Combos', 'セット'), '前菜': ('Starters', '前菜'),
    '小菜': ('Side dishes', '小皿'), '飯類': ('Rice', 'ご飯もの'), '麵類': ('Noodles', '麺類'), '素食': ('Vegetarian', 'ベジタリアン'),
    '兒童餐': ('Kids', 'キッズ'), '早餐': ('Breakfast', '朝食'), '午餐': ('Lunch', 'ランチ'), '晚餐': ('Dinner', 'ディナー'),
    '限定': ('Limited', '限定'), '熱飲': ('Hot drinks', 'ホットドリンク'), '冷飲': ('Cold drinks', 'コールドドリンク'),
    '咖啡': ('Coffee', 'コーヒー'), '茶': ('Tea', 'お茶'), '紅茶': ('Black tea', '紅茶'), '綠茶': ('Green tea', '緑茶'),
    '奶茶': ('Milk tea', 'ミルクティー'), '薯條': ('Fries', 'フライドポテト'), '雞塊': ('Chicken nuggets', 'チキンナゲット'),
    '漢堡': ('Burger', 'ハンバーガー'), '沙拉': ('Salad', 'サラダ'), '披薩': ('Pizza', 'ピザ'), '義大利麵': ('Pasta', 'パスタ'),
    '牛肉麵': ('Beef noodle soup', '牛肉麺'), '滷肉飯': ('Braised pork rice', '魯肉飯'), '雞腿飯': ('Chicken leg rice', '鶏もも肉ご飯'),
    '排骨飯': ('Pork chop rice', '排骨ご飯'), '便當': ('Lunch box', '弁当'), '珍珠奶茶': ('Bubble milk tea', 'タピオカミルクティー'),
    '可樂': ('Cola', 'コーラ'), '汽水': ('Soda', 'ソーダ'), '豆漿': ('Soy milk', '豆乳'), '拿鐵': ('Latte', 'ラテ'),
    '未分類': ('Other', 'その他'),
}


def rule_translate(text: str) -> dict[str, str]:
    """規則字典：整個字串命中才翻 (避免把「招牌和牛堡」亂翻)；回 {'en': ..., 'ja': ...} (沒命中為空字串)"""
    hit = _RULE_WORDS.get((text or '').strip())
    return {'en': hit[0], 'ja': hit[1]} if hit else {'en': '', 'ja': ''}


def _translate_prompt(entries: list[dict]) -> str:
    rows = '\n'.join(f'{i + 1}. {e.get("name", "")}' + (f'｜{e["description"]}' if e.get('description') else '')
                     for i, e in enumerate(entries))
    return ('你是餐廳菜單翻譯。把下列繁體中文餐點名稱 (與「｜」之後的說明，若有) 翻成自然的英文與日文，'
            '保留品牌 / 專有名詞、數字與規格，不要加解釋。只輸出 JSON 陣列，每個元素形如 '
            '{"i": 編號, "name_en": "", "name_ja": "", "description_en": "", "description_ja": ""}，沒有說明時留空字串。\n'
            f'{rows}')


def _parse_translations(text: str | None, count: int) -> dict[int, dict[str, str]]:
    """Gemini 回應 → {編號(0 起): {name_en, ...}}；格式不對就當作失敗 (空 dict)"""
    import json
    if not text:
        return {}
    m = re.search(r'\[.*\]', text, re.S)
    if not m:
        return {}
    try:
        rows = json.loads(m.group(0))
    except ValueError:
        return {}
    out: dict[int, dict[str, str]] = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            idx = int(row.get('i')) - 1
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 <= idx < count:
            out[idx] = {k: str(row.get(k) or '').strip()[:300] for k in ('name_en', 'name_ja', 'description_en', 'description_ja')}
    return out


def translate_entries(entries: list[dict]) -> tuple[list[dict[str, str]], str]:
    """翻譯一批 {name, description}：回 ([{name_en, name_ja, description_en, description_ja}, ...], 來源 'gemini' / 'rules')。
    Gemini 失敗 (沒金鑰 / 冷卻 / 格式錯) 時用規則字典，只有整字命中的才有值。"""
    results: list[dict[str, str]] = [{'name_en': '', 'name_ja': '', 'description_en': '', 'description_ja': ''} for _ in entries]
    source = 'rules'
    for start in range(0, len(entries), TRANSLATE_BATCH):
        batch = entries[start:start + TRANSLATE_BATCH]
        parsed = _parse_translations(gemini_generate(_translate_prompt(batch), max_tokens=4000, temperature=0.1), len(batch))
        if parsed:
            source = 'gemini'
        for i, entry in enumerate(batch):
            got = parsed.get(i)
            if got and got.get('name_en'):
                results[start + i] = got
            else:
                rule = rule_translate(entry.get('name', ''))
                results[start + i].update(name_en=rule['en'], name_ja=rule['ja'])
    return results, source
