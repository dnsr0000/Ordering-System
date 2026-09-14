import os
import re
import time
import random
import threading
import requests
from datetime import datetime
from app.config import Config
from app.extensions import db, LLM_LOCK
from app.models.order import Order

# 本地 SLM 初始化
local_llm = None
try:
    from llama_cpp import Llama
    if os.path.exists(Config.LOCAL_MODEL_PATH):
        local_llm = Llama(
            model_path=Config.LOCAL_MODEL_PATH,
            n_ctx=2048,
            n_batch=512,
            n_threads=4,
            verbose=False
        )
        print("✅ 本地繁中 SLM 載入完成！")
except Exception as e:
    print(f"本地 SLM 載入失敗: {e}")

_AI_ADVICE_STATE = {
    'last_signature': None,
    'cached_advice': None,
    'is_fetching': False,
    'cooldown_until': 0,
    'last_request_time': 0
}

def run_local_slm(prompt_text, max_tokens=100):
    if not local_llm:
        return None
    formatted_prompt = f"<|im_start|>system\n你是一位專業餐飲營運顧問，請嚴格使用台灣繁體中文給出 1~2 句具體營運建議（60字以內），禁止思考草稿。<|im_end|>\n<|im_start|>user\n{prompt_text}<|im_end|>\n<|im_start|>assistant\n"
    
    if not LLM_LOCK.acquire(timeout=3.0):
        print("[!] 本地 SLM 忙碌中，立即啟動第三層動態辭庫進行熔斷備援。")
        return None
    try:
        local_llm.reset()
        res = local_llm(formatted_prompt, max_tokens=max_tokens, temperature=0.3, stop=["<|im_end|>", "\n\n"])
        return res["choices"][0]["text"].strip().replace('"', '').replace("'", '').replace('「', '').replace('」', '').strip()
    except Exception as e:
        print(f"[!] 本地 SLM 推論異常: {e}")
        return None
    finally:
        LLM_LOCK.release()

def fallback_advice(analytics_data):
    pending = analytics_data.get('pending_count', 0)
    eta = analytics_data.get('eta_minutes', 0)
    aov = analytics_data.get('avg_order_value', 0)
    top_items = analytics_data.get('top_items', [])

    if pending >= 5 or eta > 20:
        return f"⚠️ 廚房負載偏高（{pending} 筆待製作，預估等待 {eta} 分鐘），建議啟動備料支援並暫緩外帶出餐推播。"
    elif 0 < aov < 150:
        hot_item = top_items[0]['name'] if top_items else '熱門餐點'
        return f"💡 平均客單價（${int(aov)}）偏低，建議前台推廣「{hot_item}」加料升級或推播點數滿額加價購優惠券。"
    elif pending == 0 and analytics_data.get('today_orders', 0) > 0:
        return "✅ 目前出餐流程順暢無積單，可安排前台進行備料盤點與清潔。"
    return "🌟 今日營業剛起步，請確認廚房出單機與各項食材庫存是否充足。"

def fallback_menu_description(name, category):
    intros = ["嚴選優質新鮮食材現點現做", "特選產地直送原料慢火細熬", "黃金比例獨門秘方精準調配", "主廚匠心工藝悉心烹調", "保留食材純粹原汁原味"]
    traits = ["香氣四溢且口感層次鮮明", "風味醇厚濃郁且回味無窮", "外酥內嫩且肉汁豐盈飽滿", "口感清爽甘醇且韻味悠長", "每一口都帶來無與倫比的美味體驗"]
    return f"{random.choice(intros)}，【{name}】{random.choice(traits)}。"

def _resolve_offline_advice(analytics_data):
    if local_llm:
        top_items_str = ", ".join([f"{i['name']}({i['quantity']}份)" for i in analytics_data.get('top_items', [])]) or "尚無"
        prompt = f"數據：待製作 {analytics_data.get('pending_count', 0)} 筆、預估出餐 {analytics_data.get('eta_minutes', 0)} 分鐘、今日營收 ${analytics_data.get('today_revenue', 0)}、熱銷餐點：{top_items_str}。"
        local_res = run_local_slm(prompt)
        if local_res and len(local_res) >= 10:
            if not local_res.endswith(('。', '！', '!')):
                local_res += '。'
            return local_res
    return fallback_advice(analytics_data)

def _async_fetch_gemini_advice(analytics_data, signature):
    global _AI_ADVICE_STATE
    try:
        api_key = (Config.GEMINI_API_KEY or "").strip().strip("'").strip('"')
        if not api_key or api_key in ["YOUR_API_KEY", "1234", "none"] or time.time() < _AI_ADVICE_STATE.get('cooldown_until', 0):
            _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
            _AI_ADVICE_STATE['last_signature'] = signature
            return

        top_items_str = ", ".join([f"{i['name']}({i['quantity']}份)" for i in analytics_data.get('top_items', [])]) or "尚無"
        prompt = f"今日訂單：{analytics_data.get('today_orders', 0)}筆、營收：NT${analytics_data.get('today_revenue', 0)}、客單價：NT${int(analytics_data.get('avg_order_value', 0))}、待製作：{analytics_data.get('pending_count', 0)}筆、熱銷：{top_items_str}。請給出60字以內繁體中文營運行動建議，不要客套。"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key}"
        payload = {
            "contents": [{"parts": [{"text": "【重要指令】：禁止輸出任何英文思考或草稿，繁體中文介紹，必須有完整句號收尾。\n" + prompt}]}],
            "generationConfig": {"maxOutputTokens": 800, "temperature": 0.3}
        }
        resp = requests.post(url, headers={"Content-Type": "application/json", "x-goog-api-key": api_key}, timeout=20)
        res_json = resp.json() if resp.status_code == 200 else {}

        if resp.status_code == 200:
            candidates = res_json.get('candidates', [])
            
            if candidates and 'content' in candidates[0]:
                parts = candidates[0]['content'].get('parts', [])
                if parts:
                    text = parts[0].get('text', '').strip()
                    clean_text = text.replace('"', '').replace('「', '').replace('」', '').strip()
                    
                    valid_endings = ('。', '！', '!', '；', ';')
                    if len(clean_text) >= 12 and clean_text.endswith(valid_endings):
                        _AI_ADVICE_STATE['cached_advice'] = clean_text
                    elif len(clean_text) >= 12:
                        _AI_ADVICE_STATE['cached_advice'] = clean_text + '。'
                    else:
                        _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
                    
                    _AI_ADVICE_STATE['last_signature'] = signature

        elif resp.status_code in [401, 403, 429]:
            _AI_ADVICE_STATE['cooldown_until'] = time.time() + 60
            _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
            _AI_ADVICE_STATE['last_signature'] = signature
    except Exception as e:
        _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
        _AI_ADVICE_STATE['last_signature'] = signature
    finally:
        _AI_ADVICE_STATE['is_fetching'] = False

def generate_ai_business_advice(analytics_data):
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    orders = db.session.query(Order.id, Order.status).filter(Order.created_at >= today_start, Order.created_at <= today_end).order_by(Order.id.asc()).all()
    current_signature = ",".join(f"{oid}:{status}" for oid, status in orders)

    now_ts = time.time()
    if _AI_ADVICE_STATE['last_signature'] != current_signature:
        if now_ts < _AI_ADVICE_STATE.get('cooldown_until', 0) or (now_ts - _AI_ADVICE_STATE.get('last_request_time', 0)) < 15:
            _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
            _AI_ADVICE_STATE['last_signature'] = current_signature
            return _AI_ADVICE_STATE['cached_advice']

        if not _AI_ADVICE_STATE['is_fetching']:
            _AI_ADVICE_STATE['is_fetching'] = True
            _AI_ADVICE_STATE['last_request_time'] = now_ts
            t = threading.Thread(target=_async_fetch_gemini_advice, args=(analytics_data, current_signature), daemon=True)
            t.start()

    return _AI_ADVICE_STATE['cached_advice'] or fallback_advice(analytics_data)