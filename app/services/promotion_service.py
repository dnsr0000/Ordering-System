"""
促銷活動計算 (結帳與購物車預覽共用同一套規則)。

- 單品類 (買 X 送 Y、第 N 件折扣)：把符合條件的品項視為「一份一份」依單價由高到低排列 (依品項分組計算，不逐份展開)，
  每 (X+Y) 份中最便宜的 Y 份免費；每 N 份中的第 N 份 (較便宜者) 以 percent% 計價。每一份只能參加一個單品活動 (依優先順序)。
- 滿額類 (滿額折、滿額贈)：以單品折扣後的金額判斷門檻。
- 紅利兌換品、加購品、贈品不參加促銷。
- stack_with_coupon=False 的活動成立時，優惠券不可同時使用。
"""
from dataclasses import dataclass, field
from datetime import datetime

from app.models.menu import MenuItem, service_day
from app.models.promotion import Promotion

ITEM_LEVEL = ('bogo', 'nth_discount')


@dataclass
class PromotionResult:
    discount: int = 0
    names: list = field(default_factory=list)
    details: list = field(default_factory=list)      # [{'name', 'amount'}]
    gifts: list = field(default_factory=list)        # [(MenuItem, 數量, 活動名稱)]
    blocks_coupon: bool = False

    def to_dict(self):
        return {'discount': self.discount, 'names': self.names, 'details': self.details,
                'gifts': [{'menu_item_id': m.id, 'name': m.name, 'quantity': q, 'promotion': n} for m, q, n in self.gifts],
                'blocks_coupon': self.blocks_coupon}


def is_active(promo, channel, has_member, now=None):
    now = now or datetime.now()
    if not promo.is_active:
        return False
    if promo.start_time or promo.end_time:
        hm = now.strftime('%H:%M')
        start, end = promo.start_time or '00:00', promo.end_time or '23:59'
        # 跨午夜 (宵夜 22:00~02:00) 與菜單供應時段同一套規則
        inside = start <= hm <= end if start <= end else (hm >= start or hm <= end)
        if not inside:
            return False
    # 跨午夜時段午夜後的部分屬於前一晚 (週五宵夜的週六 01:00 算週五)：星期與活動期間用前一天比對
    day = service_day(now, promo.start_time, promo.end_time)
    today = day.date()
    if promo.start_date and today < promo.start_date:
        return False
    if promo.end_date and today > promo.end_date:
        return False
    if str(day.isoweekday()) not in (promo.days or '1234567'):
        return False
    channels = {c.strip() for c in (promo.channels or '').split(',') if c.strip()}
    if channels and channel not in channels:
        return False
    if promo.member_only and not has_member:
        return False
    return True


def _matches(promo, menu_item):
    if promo.target_type == 'category':
        cats = {c.strip() for c in (promo.target_value or '').replace('，', ',').split(',') if c.strip()}
        return (menu_item.category or '').strip() in cats
    if promo.target_type == 'items':
        ids = {c.strip() for c in (promo.target_value or '').split(',') if c.strip()}
        return menu_item.uuid in ids
    return True


def _free_before(pos, buy, get):
    """買 buy 送 get：排列中前 pos 份裡免費的份數 (每組 buy+get 份的後 get 份)"""
    size = buy + get
    return pos // size * get + max(0, pos % size - buy)


def _round(value):
    return int(value + 0.5) if value >= 0 else -int(-value + 0.5)


def evaluate(lines, channel, has_member=False, now=None, promotions=None):
    """
    lines：具有 menu_item / quantity / unit_price / is_add_on / is_reward 屬性的結帳品項。
    channel：kiosk / pos / table / online / delivery。
    """
    result = PromotionResult()
    promos = promotions if promotions is not None else Promotion.query.filter_by(is_active=True).all()
    promos = sorted((p for p in promos if is_active(p, channel, has_member, now)), key=lambda p: (-p.priority, p.id))
    if not promos:
        return result

    regular = [ln for ln in lines if not ln.is_reward and not ln.is_add_on and not getattr(ln, 'is_gift', False)
               and not getattr(ln, 'is_pass', False)]   # 紅利兌換 / 加購 / 贈品 / 寄杯兌換不參與促銷
    # 依品項分組計算 (不逐份展開，份數再多也不會耗盡記憶體)：每列還沒參加單品活動的份數。
    # 「一份一份依單價由高到低排列」等同把各列依單價由高到低 (同價依原順序) 接起來，每列已參加的份數一定在該列的前段。
    remaining = [max(0, int(ln.quantity)) for ln in regular]
    item_discount_by_line = [0.0] * len(regular)

    for promo in (p for p in promos if p.kind in ITEM_LEVEL):
        pool = sorted((idx for idx, ln in enumerate(regular) if remaining[idx] and _matches(promo, ln.menu_item)),
                      key=lambda idx: -regular[idx].unit_price)
        total = sum(remaining[idx] for idx in pool)
        discount = 0.0
        if promo.kind == 'bogo':
            buy, get = max(1, promo.buy_qty or 1), max(1, promo.get_qty or 1)
            size = buy + get
        else:
            n = max(2, promo.nth or 2)
            pct = min(100, max(0, promo.percent if promo.percent is not None else 50))
            size = n
        consumed = total // size * size   # 湊滿整組的份數 (最後不足一組的不參加，留給下一個活動)
        pos = 0
        for idx in pool:
            take = min(remaining[idx], consumed - pos)
            if take <= 0:
                break
            price = regular[idx].unit_price
            if promo.kind == 'bogo':
                free = _free_before(pos + take, buy, get) - _free_before(pos, buy, get)
                discount += price * free
                item_discount_by_line[idx] += price * free
            else:
                off = price * (100 - pct) / 100
                for _ in range((pos + take) // n - pos // n):   # 每組最後一份 (最便宜) 打折；逐組累加，與逐份計算的捨入一致
                    discount += off
                    item_discount_by_line[idx] += off
            remaining[idx] -= take
            pos += take
        amount = _round(discount)
        if amount > 0:
            result.discount += amount
            result.names.append(promo.name)
            result.details.append({'name': promo.name, 'amount': amount})
            result.blocks_coupon = result.blocks_coupon or not promo.stack_with_coupon

    for promo in (p for p in promos if p.kind not in ITEM_LEVEL):
        base = sum(ln.unit_price * ln.quantity - item_discount_by_line[idx]
                   for idx, ln in enumerate(regular) if _matches(promo, ln.menu_item))
        base = max(0.0, base)
        if base < (promo.threshold or 0) or base <= 0:
            continue
        if promo.kind == 'threshold_discount':
            pct = promo.percent if promo.percent is not None else 100   # 0 = 應付 0% (免費)，不可當成沒設定
            off = promo.amount if (promo.amount or 0) > 0 else base * (100 - min(100, max(0, pct))) / 100
            amount = min(_round(off), _round(base))
            if amount > 0:
                result.discount += amount
                result.names.append(promo.name)
                result.details.append({'name': promo.name, 'amount': amount})
                result.blocks_coupon = result.blocks_coupon or not promo.stack_with_coupon
        elif promo.kind == 'threshold_gift' and promo.gift_item_uuid:
            gift = MenuItem.query.filter_by(tenant_id=promo.tenant_id, uuid=promo.gift_item_uuid).first()
            if gift is not None and gift.is_available and gift.service_status(now)[0]:
                result.gifts.append((gift, 1, promo.name))
                result.names.append(promo.name)
                result.details.append({'name': f'{promo.name} (贈 {gift.name})', 'amount': 0})
                result.blocks_coupon = result.blocks_coupon or not promo.stack_with_coupon
    return result


def channel_for(order_source, order_type, is_hub=False):
    from app.models.enums import DELIVERY_SOURCES, OrderSource
    if is_hub:
        return 'online'
    if order_source in DELIVERY_SOURCES:
        return 'delivery'
    return {OrderSource.POS: 'pos', OrderSource.TABLE: 'table'}.get(order_source, 'kiosk')
