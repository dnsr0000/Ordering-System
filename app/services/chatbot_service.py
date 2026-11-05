"""
AI 客服機器人 (顧客端：點餐頁與出餐進度頁的對話窗)。

回答順序 (食安優先、便宜優先)：
1. 過敏原 / 素食 / 辣度：純檢索菜單資料直接列出，不經 LLM 改寫 (避免幻覺)。
2. 訂單狀態 (出餐進度頁帶訂單)：直接讀訂單。
3. 營業資訊 / 自訂 FAQ：店家在後台填的文字，關鍵字命中就回。
4. 菜單查詢 (價格 / 分類 / 推薦)：檢索菜單資料。
5. 其他：LLM (Gemini → 本機 Qwen)，只給菜單摘要 + 店家資訊當上下文，並要求不確定就請顧客洽櫃檯；
   LLM 不可用時回固定的「請洽櫃檯」。
每次回答都附來源 (retrieval / faq / llm / fallback)，前端標示 AI 回答需再確認。
"""
import re

from app.models.enums import OrderStatus
from app.models.menu import ALLERGENS, DIETS, MenuItem
from app.services import ai_service, settings_service

MAX_QUESTION = 300
MAX_HISTORY = 6
ALLERGY_WORDS = ('過敏', '過敏原', '甲殼', '蝦', '蟹', '花生', '堅果', '牛奶', '奶製品', '乳製品', '蛋', '芝麻', '麩質', '小麥', '大豆',
                 '魚', '亞硫酸', '芒果', '素食', '素的', '全素', '蛋奶素', '葷', '辣不辣', '會辣', '辣度', '不辣')
STATUS_WORDS = ('訂單', '進度', '好了嗎', '做好', '多久', '等多久', '還要多久', '取餐', '外送到', '送到')
HOURS_WORDS = ('營業', '幾點', '開到', '開門', '關門', '打烊', '休息', '公休', '時間', '地址', '在哪', '電話', '停車', '位置')
MENU_WORDS = ('多少錢', '價格', '價錢', '幾元', '有什麼', '有哪些', '推薦', '招牌', '菜單', '人氣', '熱門', '新品', '最便宜', '最貴')
GREETINGS = ('你好', '哈囉', 'hi', 'hello', '嗨', '您好')
ALLERGEN_KEYWORDS = {
    'shellfish': ('甲殼', '蝦', '蟹', '海鮮'), 'mango': ('芒果',), 'peanut': ('花生',), 'milk': ('牛奶', '奶製品', '乳製品', '乳糖'),
    'egg': ('蛋',), 'nuts': ('堅果', '核桃', '杏仁', '腰果'), 'sesame': ('芝麻',), 'gluten': ('麩質', '小麥', '麵粉'),
    'soy': ('大豆', '黃豆'), 'fish': ('魚',), 'sulfite': ('亞硫酸',),
}


def config(tenant_id=None):
    return settings_service.get('chatbot', tenant_id)


def enabled(tenant_id=None):
    return bool(config(tenant_id).get('enabled'))


def _items():
    return [i for i in MenuItem.query.all() if not i.is_branch_hidden]


def _mentioned_items(question, items):
    """問句裡提到的品名：先比完整品名 (長的先，避免「奶茶」搶走「珍珠奶茶」)，
    再比部分品名 (顧客說「薯條」而菜單是「黃金脆薯條」：問句的片段只出現在唯一一個品名時算命中)"""
    found = []
    text = question.lower()
    for item in sorted(items, key=lambda i: len(i.name), reverse=True):
        names = [item.name] + ([item.name_en.lower()] if item.name_en else [])
        if any(n and n.lower() in text for n in names) and not any(item.name in f.name for f in found):
            found.append(item)
    if not found:
        for length in range(min(6, len(text)), 1, -1):   # 長的片段先
            for start in range(0, len(text) - length + 1):
                part = text[start:start + length].strip()
                if len(part) < 2 or not re.fullmatch(r'[\w\u4e00-\u9fff]+', part):
                    continue
                hits = [i for i in items if part in i.name.lower()]
                if len(hits) == 1 and hits[0] not in found:
                    found.append(hits[0])
            if found:
                break
    return found[:5]


def _price_line(item):
    hint = '' if item.is_available and item.service_status()[0] else '（目前無法供應）'
    return f'{item.name} ${item.unit_price}{hint}'


# ---------------------------------------------------------------- 1. 過敏原 (純檢索)
def _allergen_answer(question, items):
    mentioned = _mentioned_items(question, items)
    if mentioned:
        lines = []
        for item in mentioned:
            codes = item.allergen_codes
            parts = []
            parts.append('過敏原：' + ('、'.join(ALLERGENS[c] for c in codes) if codes else '無標示'))
            if item.diet:
                parts.append(DIETS.get(item.diet, item.diet))
            if item.spicy_level:
                parts.append('辣度 ' + '🌶' * item.spicy_level)
            lines.append(f'【{item.name}】' + '；'.join(parts))
        return '\n'.join(lines) + '\n以上為店家標示的資料；若有嚴重過敏，點餐前請再向櫃檯確認。'
    # 問「哪些有 / 沒有某過敏原」或「有素食嗎」
    for code, words in ALLERGEN_KEYWORDS.items():
        if any(w in question for w in words):
            without = [i.name for i in items if code not in i.allergen_codes and i.is_available]
            with_ = [i.name for i in items if code in i.allergen_codes]
            label = ALLERGENS[code]
            text = f'含{label}的餐點：' + ('、'.join(with_[:15]) if with_ else '無標示') + '。\n'
            text += f'不含{label}的餐點：' + ('、'.join(without[:15]) + ('…' if len(without) > 15 else '') if without else '無') + '。'
            return text + '\n以上為店家標示的資料；若有嚴重過敏，點餐前請再向櫃檯確認。'
    if any(w in question for w in ('素', '葷')):
        veg = [f'{i.name}（{DIETS.get(i.diet, i.diet)}）' for i in items if i.diet and i.is_available]
        return ('素食餐點：' + '、'.join(veg[:15]) if veg else '目前菜單沒有標示為素食的餐點') + '。\n詳細內容請以店家標示為準。'
    if any(w in question for w in ('辣',)):
        hot = [f'{i.name}（{"🌶" * i.spicy_level}）' for i in items if i.spicy_level]
        return ('會辣的餐點：' + '、'.join(hot[:15]) if hot else '目前菜單沒有標示辣度的餐點') + '。\n多數餐點可在點餐時選擇辣度。'
    if '過敏' in question:
        used = sorted({ALLERGENS[c] for i in items for c in i.allergen_codes})
        return ('本店餐點標示的過敏原有：' + '、'.join(used) + '。' if used else '目前菜單沒有標示過敏原。') + \
            '\n請告訴我餐點名稱 (例如「牛肉麵有什麼過敏原」) 或問「哪些不含花生」，我幫您查。'
    return None


# ---------------------------------------------------------------- 2. 訂單狀態
def _status_answer(order):
    if order is None:
        return None
    if order.status == OrderStatus.CANCELLED:
        return '這張訂單已取消，如有疑問請洽櫃檯。'
    if order.delivery_status:
        label = order.delivery_status_label
        extra = f'，外送員 {order.driver_name}' if order.driver_name and order.delivery_status in ('assigned', 'out') else ''
        return f'您的外送訂單目前「{label}」{extra}。配送中可在這一頁看到外送員位置。'
    if order.status == OrderStatus.COMPLETED:
        return f'餐點已完成，取餐號碼 #{order.display_pickup_number:03d}，請到櫃檯取餐。'
    if order.status == OrderStatus.PICKED_UP:
        return '這張訂單已取餐，祝您用餐愉快！'
    eta = order.eta_label
    return f'餐點製作中，取餐號碼 #{order.display_pickup_number:03d}' + (f'，預估 {eta}' if eta else '') + '。完成時這一頁會提示您。'


# ---------------------------------------------------------------- 3. 營業資訊 / FAQ
def _faq_entries(cfg):
    out = []
    for raw in str(cfg.get('faq') or '').splitlines():
        q, sep, a = raw.partition('=>')
        if not sep:
            q, sep, a = raw.partition('：')
        if sep and q.strip() and a.strip():
            out.append((q.strip(), a.strip()))
    return out


def _faq_answer(question, cfg):
    best, score = None, 0
    for q, a in _faq_entries(cfg):
        keys = [k for k in re.split(r'[\s,，、/|]+', q) if k]
        hit = sum(1 for k in keys if k.lower() in question.lower())
        if hit > score:
            best, score = a, hit
    return best


def _hours_answer(question, cfg):
    info = (cfg.get('store_info') or '').strip()
    if any(w in question for w in HOURS_WORDS) and info:
        return info
    return None


# ---------------------------------------------------------------- 4. 菜單查詢
def _menu_answer(question, items):
    mentioned = _mentioned_items(question, items)
    if mentioned and any(w in question for w in ('多少', '價', '幾元', '錢')):
        return '、'.join(_price_line(i) for i in mentioned) + '。'
    if mentioned:
        item = mentioned[0]
        desc = f'：{item.description}' if item.description else ''
        return f'{_price_line(item)}{desc}'
    if any(w in question for w in ('推薦', '招牌', '人氣', '熱門')):
        hot = [i for i in items if (i.is_recommended or i.is_manual_popular) and i.is_available]
        if hot:
            return '人氣推薦：' + '、'.join(_price_line(i) for i in hot[:6]) + '。'
    if '新品' in question:
        new = [i for i in items if i.is_new and i.is_available]
        if new:
            return '新品：' + '、'.join(_price_line(i) for i in new[:6]) + '。'
    if any(w in question for w in ('最便宜', '便宜')):
        cheap = sorted((i for i in items if i.is_available), key=lambda i: i.unit_price)[:5]
        if cheap:
            return '價格較低的餐點：' + '、'.join(_price_line(i) for i in cheap) + '。'
    for cat in sorted({(i.category or '').strip() for i in items}, key=len, reverse=True):
        if cat and cat in question:
            rows = [i for i in items if (i.category or '').strip() == cat]
            return f'{cat}：' + '、'.join(_price_line(i) for i in rows[:12]) + ('…' if len(rows) > 12 else '') + '。'
    if any(w in question for w in ('菜單', '有什麼', '有哪些')):
        cats = sorted({(i.category or '未分類').strip() for i in items})
        return '菜單分類有：' + '、'.join(cats) + '。想看哪一類？'
    return None


# ---------------------------------------------------------------- 5. LLM
def _menu_summary(items, limit=60):
    rows = []
    for i in items[:limit]:
        tags = []
        if i.allergen_codes:
            tags.append('過敏原:' + '/'.join(ALLERGENS[c] for c in i.allergen_codes))
        if i.diet:
            tags.append(DIETS.get(i.diet, i.diet))
        if not (i.is_available and i.service_status()[0]):
            tags.append('售完')
        rows.append(f'{i.category}|{i.name}|${i.unit_price}' + (f'|{" ".join(tags)}' if tags else ''))
    return '\n'.join(rows)


def _llm_answer(question, history, cfg, items, shop_name):
    name = cfg.get('bot_name') or '小幫手'
    convo = '\n'.join(f'{"顧客" if h.get("role") == "user" else name}：{str(h.get("text") or "")[:200]}'
                      for h in (history or [])[-MAX_HISTORY:] if isinstance(h, dict))
    prompt = (f'你是「{shop_name}」的客服助理「{name}」，用台灣繁體中文、2~3 句內友善回答顧客。'
              '只能依據下面的店家資訊與菜單回答；資料裡沒有的事 (例如食材細節、過敏原) 不要猜，請顧客洽櫃檯或撥打店家電話。'
              '不要承諾折扣或訂位，不要輸出 Markdown。\n'
              f'店家資訊：{(cfg.get("store_info") or "").strip() or "未提供"}\n'
              f'常見問答：{" / ".join(f"{q}：{a}" for q, a in _faq_entries(cfg)) or "無"}\n'
              f'菜單 (分類|品名|價格|標示)：\n{_menu_summary(items)}\n'
              + (f'之前的對話：\n{convo}\n' if convo else '') + f'顧客問：{question}\n回答：')
    text, source = ai_service.generate_text(prompt, max_tokens=300, local_max_tokens=160,
                                            system=f'你是餐廳「{shop_name}」的客服助理，用繁體中文簡短回答，不確定就請顧客洽櫃檯。')
    return (text.strip()[:600] if text else None), source


def answer(question, order=None, history=None, tenant_id=None, shop_name=''):
    """回 {'answer', 'source', 'suggestions'}"""
    cfg = config(tenant_id)
    question = str(question or '').strip()[:MAX_QUESTION]
    if not question:
        return {'answer': '請輸入您想問的問題。', 'source': 'fallback', 'suggestions': _suggestions(order)}
    q = question.lower()
    items = _items()
    text, source = None, 'retrieval'
    if any(w in q for w in ALLERGY_WORDS):
        text = _allergen_answer(question, items)
    if text is None and order is not None and any(w in q for w in STATUS_WORDS):
        text = _status_answer(order)
    if text is None:
        faq = _faq_answer(question, cfg)
        if faq:
            text, source = faq, 'faq'
    if text is None:
        text = _hours_answer(question, cfg)
        source = 'faq' if text else source
    if text is None and any(w in q for w in MENU_WORDS) or (text is None and _mentioned_items(question, items)):
        text = _menu_answer(question, items)
        source = 'retrieval'
    if text is None and any(q.startswith(w) for w in GREETINGS) and len(q) <= 8:
        text = f'您好！我是{cfg.get("bot_name") or "小幫手"}，可以問我餐點、過敏原、營業時間或訂單進度。'
        source = 'faq'
    if text is None and cfg.get('llm_enabled', True):
        text, llm_source = _llm_answer(question, history, cfg, items, shop_name)
        source = 'llm' if text else source
    if text is None:
        text = (cfg.get('fallback') or '').strip() or '這個問題我不太確定，請直接洽詢櫃檯人員，謝謝！'
        source = 'fallback'
    return {'answer': text, 'source': source, 'suggestions': _suggestions(order)}


def _suggestions(order):
    base = ['有哪些素食餐點？', '營業時間是幾點？', '人氣推薦有哪些？']
    if order is not None:
        base.insert(0, '我的餐點好了嗎？')
    return base[:4]
