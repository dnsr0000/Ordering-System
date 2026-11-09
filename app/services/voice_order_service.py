"""
語音點餐：把顧客說的一句話 (「我要一份大杯珍珠奶茶，半糖少冰，再一份薯條」) 解析成購物車品項。

兩層：
1. 規則比對 (離線可用)：以菜單品名 (含英文名) 做最長優先比對，品名前後的數量 (一份 / 2 杯)、規格 (大杯)、
   甜度 / 冰塊 / 辣度與加料字眼歸給最近的品項。
2. 規則完全沒命中時才交給 LLM (雲端 AI 依序備援 → 本機 Qwen)，要求輸出 JSON，品名再以模糊比對對回菜單；
   LLM 只負責「聽懂」，價格與可點與否一律由菜單資料決定。
解析結果一定先在點餐機畫面上讓顧客確認，才會加入購物車 (前端)；後端不直接建立訂單。
"""
import difflib
import re

from app.services import ai_service

NUM_WORDS = {'零': 0, '一': 1, '壹': 1, '二': 2, '兩': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
UNITS = '份杯個碗盤客塊包瓶罐碟顆支隻'
SUGAR_OPTIONS = ('全糖', '半糖', '微糖', '無糖')
ICE_OPTIONS = ('正常冰', '少冰', '去冰', '熱飲')
SPICY_OPTIONS = ('不辣', '微辣', '中辣', '大辣')
# 口語 → 點餐機選項
SUGAR_ALIASES = {'正常糖': '全糖', '正常甜': '全糖', '全糖': '全糖', '半糖': '半糖', '五分糖': '半糖', '微糖': '微糖', '少糖': '微糖',
                 '三分糖': '微糖', '一分糖': '微糖', '無糖': '無糖', '不加糖': '無糖', '不要糖': '無糖', '去糖': '無糖'}
ICE_ALIASES = {'正常冰': '正常冰', '多冰': '正常冰', '少冰': '少冰', '微冰': '少冰', '去冰': '去冰', '不要冰': '去冰', '無冰': '去冰',
               '常溫': '去冰', '熱飲': '熱飲', '熱的': '熱飲', '要熱的': '熱飲', '溫的': '熱飲'}
SPICY_ALIASES = {'不辣': '不辣', '不要辣': '不辣', '微辣': '微辣', '小辣': '微辣', '一點辣': '微辣', '中辣': '中辣', '大辣': '大辣',
                 '特辣': '大辣', '很辣': '大辣'}
SIZE_WORDS = ('大杯', '中杯', '小杯', '大份', '中份', '小份', '大碗', '小碗', '大', '中', '小')
MAX_QTY = 20
MAX_TEXT = 200
FILLERS = ('我要', '我想要', '請給我', '幫我', '來一', '來', '要', '一下', '謝謝', '麻煩', '然後', '還有', '再', '跟', '和', '加',
           '以及', '另外', '還要', '也要', '點', '的', '一樣', '各')


def _norm(text):
    text = str(text or '')[:MAX_TEXT]
    table = str.maketrans('０１２３４５６７８９，。！？、；：', '0123456789,.!?,;:')
    text = text.translate(table).replace('　', ' ')
    return re.sub(r'\s+', ' ', text).strip()


def _to_int(token):
    token = str(token or '').strip()
    if token.isdigit():
        return int(token)
    if not token:
        return None
    if token == '十':
        return 10
    if '十' in token:   # 十二、二十、二十三
        tens, _, ones = token.partition('十')
        t = NUM_WORDS.get(tens, 1) if tens else 1
        o = NUM_WORDS.get(ones, 0) if ones else 0
        return t * 10 + o
    total = 0
    for ch in token:
        if ch not in NUM_WORDS:
            return None
        total = total * 10 + NUM_WORDS[ch]
    return total


_NUM_RE = r'(\d+|[一二兩两三四五六七八九十壹]+)'
_QTY_BEFORE = re.compile(rf'{_NUM_RE}\s*[{UNITS}]?')


def _alias_hit(text, aliases):
    """在文字片段中找口語別名 (最長的先)，回對應的選項或 ''"""
    for word in sorted(aliases, key=len, reverse=True):
        if word in text:
            return aliases[word]
    return ''


def _size_hit(text, sizes):
    """規格：先比對餐點自己的規格名稱 (大杯 / 中杯)，再接受「大 / 中 / 小」單字對到含該字的規格"""
    names = [s[0] for s in sizes]
    for name in sorted(names, key=len, reverse=True):
        if name and name in text:
            return name
    text = _mask_option_aliases(text)   # 「大辣 / 中辣 / 小辣」等客製化字眼裡的大中小不是規格
    for word in ('大', '中', '小'):
        if word in text:
            for name in names:
                if word in name:
                    return name
    return ''


def _addon_hits(text, addons):
    out = []
    for m in sorted(addons, key=lambda a: len(a['name']), reverse=True):
        if m['name'] and m['name'] in text:
            out.append(m)
    return out


def _attributes(item, pre, post, addons):
    """品項前後文字裡的客製化選項；只套用該餐點支援的客製化群組"""
    text = pre + ' ' + post
    attrs = {'size': '', 'sugar': '', 'ice': '', 'spicy': '', 'modifier_ids': [], 'modifier_names': []}
    sizes = item.sizes
    if sizes:
        attrs['size'] = _size_hit(text, sizes) or sizes[0][0]
    if item.modifiers == 'ice_sugar':
        attrs['sugar'] = _alias_hit(text, SUGAR_ALIASES)
        attrs['ice'] = _alias_hit(text, ICE_ALIASES)
    elif item.modifiers == 'spicy':
        attrs['spicy'] = _alias_hit(text, SPICY_ALIASES)
    elif item.modifiers == 'addons':
        hits = _addon_hits(text, addons)
        attrs['modifier_ids'] = [m['id'] for m in hits]
        attrs['modifier_names'] = [m['name'] for m in hits]
    return attrs


def _name_index(items):
    """[(比對用名稱, item)]，長的先比對 (「珍珠奶茶」優先於「奶茶」)"""
    out = []
    for item in items:
        names = {item.name, re.sub(r'[\(（].*?[\)）]', '', item.name).strip()}
        if item.name_en:
            names.add(item.name_en.lower())
        for n in names:
            if n:
                out.append((n, item))
    return sorted(out, key=lambda x: len(x[0]), reverse=True)


def _make_line(item, qty, attrs, availability=None):
    """availability：{餐點 id: 可否點} (呼叫端依菜單頁相同規則算好：取餐分店庫存、原料不足…)；沒提供時看餐點本身"""
    if availability is not None and item.id in availability:
        available = bool(availability[item.id])
    else:
        available = bool(item.is_available and item.service_status()[0])
    line = {'menu_item_id': item.id, 'name': item.name, 'quantity': qty, 'available': available}
    line.update(attrs)
    return line


_NUMERIC_ALIASES = sorted((w for w in (*SUGAR_ALIASES, *ICE_ALIASES, *SPICY_ALIASES) if re.search(_NUM_RE, w)),
                          key=len, reverse=True)
_OPTION_ALIASES = sorted({*SUGAR_ALIASES, *ICE_ALIASES, *SPICY_ALIASES}, key=len, reverse=True)


def _mask_option_aliases(text):
    """甜度 / 冰塊 / 辣度字眼換成等長空白 (規格的「大 / 中 / 小」單字比對前使用)"""
    for word in _OPTION_ALIASES:
        text = text.replace(word, ' ' * len(word))
    return text
_SEP_RE = re.compile(r'[\s,.;:!?，。、；：！？]+')
_TRAIL_QTY_RE = re.compile(rf'(?:{_NUM_RE}\s*[{UNITS}]?)?\s*(?:{"|".join(SIZE_WORDS)})?\s*$')


def _mask_numeric_aliases(text):
    """「三分糖」「一點辣」裡的數字不是數量：換成等長空白 (保留位置)"""
    for word in _NUMERIC_ALIASES:
        text = text.replace(word, ' ' * len(word))
    return text


def _split_gap(gap):
    """兩個品名之間的文字分給前後兩個品項 (不可同時算給兩邊)：
    有標點 / 空白時以最後一個為界 (「珍珠奶茶兩杯，薯條」的兩杯屬於奶茶；「…，再一份薯條」的一份屬於薯條)；
    沒有時，緊接下一個品名的「數量 / 規格」屬於下一個，其餘 (甜度、冰塊、加料) 屬於前一個"""
    seps = list(_SEP_RE.finditer(gap))
    if seps:
        return gap[:seps[-1].start()], gap[seps[-1].end():]
    m = _TRAIL_QTY_RE.search(gap)
    cut = m.start() if m and m.group(0).strip() else len(gap)
    return gap[:cut], gap[cut:]


def _quantity(pre, post, is_last=True):
    """數量：品名後面的「兩杯」優先 (但緊接著下一個品名的數字屬於下一個品項)，否則取品名前面最後出現的數字"""
    pre, post = _mask_numeric_aliases(pre), _mask_numeric_aliases(post)
    for m in _QTY_BEFORE.finditer(post):   # 品名後面 (中間可夾甜度 / 辣度等字眼)
        if is_last or m.end() < len(post.rstrip()):
            n = _to_int(m.group(1))
            if n:
                return min(MAX_QTY, n)
            break
    found = _QTY_BEFORE.findall(pre)
    if found:
        n = _to_int(found[-1])
        if n:
            return min(MAX_QTY, n)
    return 1


def _strip_known(text, addons):
    """去掉口語贅字、客製化字眼、數量，留下可能是品名的片段"""
    for word in sorted(set(FILLERS) | set(SUGAR_ALIASES) | set(ICE_ALIASES) | set(SPICY_ALIASES) | set(SIZE_WORDS)
                       | {m['name'] for m in addons}, key=len, reverse=True):
        text = text.replace(word, ' ')
    text = re.sub(rf'(?:{_NUM_RE})?\s*[{UNITS}]', ' ', text)
    return [p for p in re.split(r'[\s,.;:!?，。、]+', text) if len(p) >= 2]


def rule_parse(text, items, addons, availability=None):
    """規則比對；回 (lines, leftover 沒有對到品項的片段)"""
    text = _norm(text)
    lowered = text.lower()
    if len(lowered) != len(text):
        # 'İ' 等字元小寫後會變成多個字元：逐字小寫並保留這類字元，讓比對位置與原字串一致 (否則標記位置越界)
        lowered = ''.join(ch.lower() if len(ch.lower()) == 1 else ch for ch in text)
    matches = []   # (start, end, item)
    taken = [False] * len(text)

    def claim(pos, end, item):
        if any(taken[pos:end]):
            return False
        matches.append((pos, end, item))
        for i in range(pos, end):
            taken[i] = True
        return True

    # 完整品名 (含英文名)，長的先比
    for name, item in _name_index(items):
        start = 0
        key = name.lower()
        while True:
            pos = lowered.find(key, start)
            if pos < 0:
                break
            claim(pos, pos + len(key), item)
            start = pos + len(key)
    # 沒對到的片段若「唯一」包含在某個品名裡 (顧客只說「牛肉堡」而菜單是「炙燒起司牛肉堡」) 也算命中
    leftover = []
    run_start = None
    runs = []
    for i, flag in enumerate(taken + [True]):
        if not flag and run_start is None:
            run_start = i
        elif flag and run_start is not None:
            runs.append((run_start, i))
            run_start = None
    for rs, re_ in runs:
        for token in _strip_known(text[rs:re_], addons):
            hits = [i for i in items if token in i.name]
            pos = text.find(token, rs, re_)
            if len(hits) == 1 and pos >= 0 and claim(pos, pos + len(token), hits[0]):
                continue
            leftover.append(token)
    matches.sort()
    if not matches:
        return [], leftover
    # 每個品項的前文 / 後文：兩個品名之間的文字只分給其中一個 (否則「珍珠奶茶兩杯，薯條」連薯條也變兩份、甜度也套到下一杯)
    pres = [text[:matches[0][0]]] + [''] * (len(matches) - 1)
    posts = [''] * (len(matches) - 1) + [text[matches[-1][1]:]]
    for i in range(len(matches) - 1):
        posts[i], pres[i + 1] = _split_gap(text[matches[i][1]:matches[i + 1][0]])
    lines = []
    for (_start, _end, item), pre, post in zip(matches, pres, posts):
        lines.append(_make_line(item, _quantity(pre, post), _attributes(item, pre, post, addons), availability))
    return lines, leftover


def _llm_prefix(items, addons):
    """每次都一樣的前段 (說明 + 菜單 + 加料)：放在最前面讓 AI 服務的提示快取生效"""
    names = '、'.join(i.name for i in items[:200])
    addon_names = '、'.join(m['name'] for m in addons) or '無'
    return ('你是餐廳點餐機的語音助理。顧客說了一句話，請從「菜單」中找出顧客要點的品項與數量，'
            '輸出 JSON 陣列，每個元素形如 {"name": 菜單上的品名, "qty": 數量, "size": 規格或空字串, '
            '"sugar": 甜度或空字串, "ice": 冰塊或空字串, "spicy": 辣度或空字串, "addons": [加料名稱]}。'
            '甜度只能是 全糖/半糖/微糖/無糖，冰塊只能是 正常冰/少冰/去冰/熱飲，辣度只能是 不辣/微辣/中辣/大辣；'
            '聽不出來或菜單沒有的品項不要編造，直接略過。只輸出 JSON，不要其他文字。\n'
            f'菜單：{names}\n加料：{addon_names}')


def _llm_prompt(text, items, addons):
    return f'{_llm_prefix(items, addons)}\n顧客說：{text}'


def llm_parse(text, items, addons, availability=None):
    """LLM 解析 (雲端 AI → 本機)；回 (lines, 沒對到菜單的名稱, 來源)"""
    data, source = ai_service.generate_json(f'顧客說：{_norm(text)}', max_tokens=500, prefix=_llm_prefix(items, addons),
                                            system='你是餐廳點餐助理，只輸出 JSON。', local_max_tokens=300, interactive=True)
    if not isinstance(data, list):
        return [], [], source
    by_name = {i.name: i for i in items}
    lines, unknown = [], []
    for row in data[:10]:
        if not isinstance(row, dict):
            continue
        name = str(row.get('name') or '').strip()
        item = by_name.get(name)
        if item is None and name:
            close = difflib.get_close_matches(name, list(by_name), n=1, cutoff=0.6)
            item = by_name.get(close[0]) if close else None
        if item is None:
            if name:
                unknown.append(name)
            continue
        try:   # 模型輸出的格式不可信 (顧客文字原樣進提示)：壞掉的一列略過，不可讓整句解析 500
            qty = _to_int(row.get('qty')) if not isinstance(row.get('qty'), int) else row.get('qty')
            qty = min(MAX_QTY, max(1, qty or 1))
            raw_addons = row.get('addons')
            if not isinstance(raw_addons, list):
                raw_addons = []
            desc = ' '.join(str(row.get(k) or '') for k in ('size', 'sugar', 'ice', 'spicy')) + ' ' + ' '.join(
                a for a in raw_addons if isinstance(a, str))
            lines.append(_make_line(item, qty, _attributes(item, desc, '', addons), availability))
        except (TypeError, ValueError):
            continue
    return lines, unknown, source


def parse(text, items, addons, use_llm=True, availability=None):
    """
    解析顧客說的話。items：可點的 MenuItem；addons：[{'id', 'name'}] (加料選項)。
    availability：{餐點 id: 可否點}，與菜單頁相同的判斷 (線上端看取餐分店庫存、門市看原料不足)；省略時只看餐點本身。
    回 {'lines': [...], 'unmatched': [...], 'source': 'rules' / 雲端廠商 (gemini / anthropic / openai…) / 'local' / 'none', 'text': 正規化後的句子}
    """
    text = _norm(text)
    if not text:
        return {'lines': [], 'unmatched': [], 'source': 'none', 'text': ''}
    lines, leftover = rule_parse(text, items, addons, availability)
    source = 'rules'
    if not lines and use_llm:
        lines, leftover, source = llm_parse(text, items, addons, availability)
        source = source or 'none'
    return {'lines': lines, 'unmatched': leftover[:5], 'source': source, 'text': text}
