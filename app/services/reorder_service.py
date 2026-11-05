"""
再點一次 (我的訂單)：把歷史訂單的品項還原成購物車格式，前端以目前菜單重新加入 (價格一律以現在的菜單為準)。
訂單明細只存「客製化文字」(規格、套餐任選、加料(+價) 以頓號相連)，這裡反向對應回目前的規格 / 任選 / 加料 id；
對不回來的 (已刪除的加料、改名的規格) 當成一般客製化文字帶回。紅利兌換、寄杯、促銷贈品與已下架 / 售完的品項略過並回報。
"""
from typing import Any
import re

from app.models.menu import MenuItem, ModifierOption

MOD_RE = re.compile(r'^(.+?)\(\+(\d+)\)$')
SKIP_PREFIXES = ('[套餐:', '[寄杯]', '紅利免費兌換', '改價 $')


def _split(customization: Any) -> list[str]:
    return [p.strip() for p in str(customization or '').split('、') if p.strip()]


def _combo_choice_ids(combo: Any, parts: list[str]) -> tuple[list[int], set[str]]:
    """套餐任選的「群組:選項(+價)」文字 → ComboOptionItem id (對不到的群組留給前端用預設)"""
    ids, used = [], set()
    for group, options in combo.choice_groups:
        for p in parts:
            if not p.startswith(f'{group}:'):
                continue
            name = p[len(group) + 1:]
            name = MOD_RE.sub(r'\1', name)
            opt = next((o for o in options if o.menu_item is not None and o.menu_item.name == name), None)
            if opt is not None:
                ids.append(opt.id)
                used.add(p)
            break
    return ids, used


def lines_for(order: Any, tenant_id: int) -> tuple[list[dict], list[str]]:
    """回傳 (購物車列清單, 略過的品項名稱清單)"""
    modifiers = {m.name: m for m in ModifierOption.query.filter_by(is_active=True).all()}
    modifiers_by_id = {m.id: m for m in modifiers.values()}
    lines, skipped = [], []
    for it in order.items:
        qty = (it.quantity or 0)
        if qty <= 0:
            continue
        if it.is_reward or it.is_gift or (it.customization or '').startswith('[寄杯]'):
            skipped.append(it.item_name)
            continue
        item = MenuItem.query.filter_by(id=it.menu_item_id).first() if it.menu_item_id else None
        if item is None or item.tenant_id != tenant_id or not item.is_available or item.is_branch_hidden \
                or not item.service_status()[0]:
            skipped.append(it.item_name)
            continue
        parts = _split(it.customization)
        combo = None
        if it.combo_option_id:
            combo = next((c for c in item.combo_options if c.id == it.combo_option_id), None)
            if combo is None:
                skipped.append(it.item_name)
                continue
        combo_choices, used = _combo_choice_ids(combo, parts) if combo is not None else ([], set())
        size = ''
        for name, _delta in item.sizes:
            if name in parts:
                size, used = name, used | {name}
                break
        modifier_ids = []
        rest = []
        for p in parts:
            if p in used or p.startswith(SKIP_PREFIXES):
                continue
            m = MOD_RE.match(p)
            if m and m.group(1) in modifiers and str(modifiers[m.group(1)].price) == m.group(2):
                modifier_ids.append(modifiers[m.group(1)].id)
            else:
                rest.append(p)
        extra = sum(modifiers_by_id[m].price for m in modifier_ids)
        is_add_on = bool(item.can_be_add_on) and combo is None and (it.price - extra) == (item.add_on_price or 0) \
            and (item.add_on_price or 0) < item.unit_price
        lines.append({
            'menu_item_id': item.id, 'quantity': qty, 'combo_option_id': combo.id if combo else None,
            'combo_choices': combo_choices, 'size': size, 'modifier_ids': modifier_ids,
            'customization': '、'.join(rest)[:200], 'is_add_on': is_add_on,
        })
    return lines, skipped

