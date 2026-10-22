"""
AI 文案與營運建議，三層降級：Google Gemini → 本機 Qwen2.5 SLM (選用) → 規則引擎。
- Gemini 失敗或被限流時冷卻 60 秒，期間直接使用下一層。
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

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
_PLACEHOLDER_KEYS = {"", "YOUR_API_KEY", "1234", "none"}
_gemini_cooldown_until = 0.0
_local_llm = None
_local_llm_loaded = False
_llm_lock = threading.Lock()
_advice_cache = {}  # tenant_id -> {'signature', 'advice', 'fetching'}


def _api_key():
    key = (current_app.config.get('GEMINI_API_KEY') or '').strip()
    return None if key in _PLACEHOLDER_KEYS or '你的' in key else key


def gemini_generate(prompt, max_tokens=800, temperature=0.3, api_key=None):
    """呼叫 Gemini；失敗回傳 None (並在 401/403/429 時冷卻 60 秒)"""
    global _gemini_cooldown_until
    api_key = api_key or _api_key()
    if not api_key or time.time() < _gemini_cooldown_until:
        return None
    try:
        resp = requests.post(
            GEMINI_URL, timeout=15,
            headers={"Content-Type": "application/json", "x-goog-api-key": api_key},
            json={"contents": [{"parts": [{"text": prompt}]}],
                  "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}},
        )
        if resp.status_code in (401, 403, 429):
            _gemini_cooldown_until = time.time() + 60
        if resp.status_code != 200:
            logger.warning('Gemini 回應 %s，改用下一層', resp.status_code)
            return None
        parts = (resp.json().get('candidates') or [{}])[0].get('content', {}).get('parts') or []
        return (parts[0].get('text') or '').strip() if parts else None
    except Exception as e:  # noqa: BLE001
        logger.warning('Gemini 連線異常 (%s)，改用下一層', e)
        return None


def _get_local_llm():
    """第一次需要時才載入本機模型 (llama-cpp-python 為選用套件)"""
    global _local_llm, _local_llm_loaded
    if not _local_llm_loaded:
        _local_llm_loaded = True
        path = current_app.config.get('LOCAL_MODEL_PATH')
        if path and os.path.exists(path):
            try:
                from llama_cpp import Llama
                _local_llm = Llama(model_path=path, n_ctx=2048, n_batch=512, n_threads=4, verbose=False)
                atexit.register(_close_local_llm)
            except Exception as e:  # noqa: BLE001
                logger.warning('本機 SLM 載入失敗: %s', e)
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


def run_local_slm(prompt_text, max_tokens=100):
    llm = _get_local_llm()
    if llm is None or not _llm_lock.acquire(timeout=3.0):
        return None
    try:
        llm.reset()
        prompt = ("<|im_start|>system\n你是一位專業餐飲營運顧問，請嚴格使用台灣繁體中文回答，禁止思考草稿。<|im_end|>\n"
                  f"<|im_start|>user\n{prompt_text}<|im_end|>\n<|im_start|>assistant\n")
        res = llm(prompt, max_tokens=max_tokens, temperature=0.3, stop=["<|im_end|>", "\n\n"])
        return res["choices"][0]["text"].strip()
    except Exception:  # noqa: BLE001
        return None
    finally:
        _llm_lock.release()


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
