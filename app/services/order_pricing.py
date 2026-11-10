"""
結帳計價 (由 order_service 拆出)：優惠碼查詢 / 套用、加購配額、購物車明細解析 (價格、規格、套餐配餐、客製化)、
通路價與服務費、整單折扣。只計算與驗證，不寫入訂單。
"""
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, update

from app.domain.menu.service import MenuDomainService
from app.errors import AppError
from app.services.coupon_service import normalize_code  # noqa: F401  (其他模組以 order_pricing.normalize_code 取用)
from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.enums import DELIVERY_SOURCES, OrderSource, OrderType
from app.models.menu import ComboOption, MenuItem, ModifierOption
from app.models.user import User
from app.utils import safe_id, safe_int

logger = logging.getLogger(__name__)

logger = logging.getLogger(__name__)

NON_MAIN_ADDON_CATEGORIES = MenuDomainService.NON_MAIN_ADDON_CATEGORIES


def _norm_category(value):
    return MenuDomainService.normalize_category(value)


def _is_main_category(category):
    return MenuDomainService.is_main_category(category)


# 每一列 / 整張訂單的份數上限 (團購合併送單同一品項最多 300 人 × 20 份 = 6000 份)；
# 防止「1e10 份」這類請求讓促銷計算、庫存檢查耗盡伺服器資源
MAX_LINE_QUANTITY = 9999
MAX_ORDER_QUANTITY = 99999
_PRICE_TAG = re.compile(r'\(\+\d+\)')
_COMBO_TAG = re.compile(r'\[套餐:[^\]]*\]')
_CONTROL_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')


# ============================================================================
# 優惠碼
# ============================================================================
@dataclass
class PromoResult:
    valid: bool
    discount: float
    message: str
    coupon: Coupon | None = None
    user_coupon: UserCoupon | None = None

    def to_dict(self):
        return {'valid': self.valid, 'discount': self.discount, 'message': self.message}


def find_promo(code, subtotal, user_id=None, is_pos=False, guard=None):
    """
    驗證優惠碼 (會員持有券優先，其次為公開促銷碼)。
    guard=(tenant_id, ip)：顧客端 (點餐機 / 手機 / 線上) 的防猜測保護。輸入不存在的代碼或未持有的會員專屬券會累計失敗次數，
    次數過多時暫停查詢公開代碼 (會員自己持有的券仍可使用)。POS 由員工操作，不受限制。
    """
    from app.services import auth_throttle, coupon_service
    code = normalize_code(code)
    subtotal = max(0, safe_int(subtotal, 0))
    if not code:
        return PromoResult(False, 0, '請輸入優惠代碼！')

    user_coupon, held_error = None, None
    if user_id:
        user_coupon, held_error = coupon_service.find_usable(user_id, code)
    coupon = user_coupon.coupon if user_coupon else None
    if coupon is None:
        if guard is not None:
            wait = auth_throttle.lock_remaining(auth_throttle.promo_keys(guard[0], guard[1], user_id))
            if wait is not None:
                minutes = max(1, int(wait.total_seconds() // 60) + 1)
                return PromoResult(False, 0, f'輸入錯誤的優惠代碼次數過多，請 {minutes} 分鐘後再試！')

        def fail(message):
            if guard is not None:
                auth_throttle.record_promo_failure(guard[0], guard[1], user_id)
            return PromoResult(False, 0, message)

        public = Coupon.query.filter_by(code=code).first()
        if public is None:
            return fail('無效的優惠代碼！')
        if public.requires_holding:
            if held_error:
                return PromoResult(False, 0, f'{held_error}，無法使用！')
            if not user_id:
                return fail('此為會員專屬優惠券，請先於 POS 綁定會員！' if is_pos else '此為會員專屬優惠券，請先登入會員！')
            return fail('該會員尚未持有此優惠券，或該券已核銷！')
        period_error = public.period_error()
        if period_error:
            return PromoResult(False, 0, f'此優惠代碼{period_error}！')
        if (public.login_required or public.uses_per_member) and not user_id:
            return PromoResult(False, 0, '此優惠代碼限會員使用，請先於 POS 綁定會員！' if is_pos else '此優惠代碼限會員使用，請先登入會員！')
        if public.uses_per_member and coupon_service.member_uses(user_id, code) >= public.uses_per_member:
            return PromoResult(False, 0, f'此優惠代碼每位會員限用 {public.uses_per_member} 次，已達使用上限！')
        coupon = public

    if subtotal < coupon.min_spend:
        return PromoResult(False, 0, f'未達使用門檻！需消費滿 ${coupon.min_spend} 元才可折抵。', coupon, user_coupon)
    discount = coupon.discount_for(subtotal)
    return PromoResult(True, discount, f'已套用「{coupon.discount_label}」，折抵 ${int(discount)} 元！', coupon, user_coupon)


# ============================================================================
# 加購配額
# ============================================================================
def _addon_qualifies(main_item, addon_item):
    """正價餐點 main_item 是否符合加購品 addon_item 的觸發條件 (與前端同一套規則)。"""
    return MenuDomainService.addon_qualifies(main_item, addon_item)


def _validate_addon_quota(main_units, addon_units):
    """每一份合格正價餐點最多搭配一份加購，且加購品必須符合觸發條件。"""
    return MenuDomainService.validate_addon_quota(main_units, addon_units)


# ============================================================================
# 結帳
# ============================================================================
@dataclass
class CheckoutLine:
    menu_item: MenuItem
    quantity: int
    unit_price: int
    customization: str
    combo: ComboOption | None = None
    is_add_on: bool = False
    is_reward: bool = False
    original_price: int | None = None   # 收銀改價前的單價
    is_gift: bool = False               # 促銷贈品
    sides: list = field(default_factory=list)   # 這一份套餐實際附的配餐 ComboOptionItem (固定 + 顧客任選)
    pass_id: int = 0                    # 以寄杯兌換 (會員持有的 MemberPass id)；這一份免費 (只收加料)

    @property
    def is_pass(self):
        return bool(self.pass_id)

    @property
    def side_item_ids(self):
        return [s.menu_item_id for s in self.sides]


def _clean_customization(raw, modifier_names):
    """
    移除前端送來的價格標籤 (+N)、套餐標籤、控制字元與加料片段 (加料由後端依 modifier_ids 重新產生)。
    只移除「完全等於加料名稱」的片段 (不用子字串比對，配餐「珍珠奶茶(少冰/半糖)」在有加料「珍珠」時要保留)；
    相容舊版前端把加料放進配餐括號內「薯條(起司(+15)、蛋(+10))」切段後的「薯條(起司」與「蛋)」。
    """
    parts = []
    for part in _COMBO_TAG.sub('', str(raw or '')).split('、'):
        part = _PRICE_TAG.sub('', _CONTROL_CHARS.sub('', part)).strip()
        head, sep, tail = part.rpartition('(')
        if sep and part.count('(') > part.count(')') and tail.strip() in modifier_names:
            part = head.strip()
        if part and part not in modifier_names and part.rstrip(')') not in modifier_names:
            parts.append(part)
    return parts


def _combo_choice_sides(combo, raw_choices):
    """
    套餐任選：每個任選群組必須選一項 (前端送出選中的 ComboOptionItem id)。回傳 (實際配餐清單, 加價合計, 顯示文字)。
    沒有任選群組的套餐回傳固定配餐。
    """
    if raw_choices and not isinstance(raw_choices, (list, tuple)):
        raise AppError(f'套餐【{combo.name}】的任選資料格式不正確，請重新選擇！')
    chosen_ids = {safe_id(c) for c in (raw_choices or []) if str(c).strip().lstrip('-').isdigit()}
    sides, extra, labels = list(combo.fixed_sides), 0, []
    for group, options in combo.choice_groups:
        picked = [o for o in options if o.id in chosen_ids]
        if len(picked) != 1:
            raise AppError(f'套餐【{combo.name}】請在「{group}」中選擇一項！')
        opt = picked[0]
        sides.append(opt)
        extra += opt.extra_price or 0
        name = opt.menu_item.name if opt.menu_item else ''
        labels.append(f'{group}:{name}' + (f'(+{opt.extra_price})' if opt.extra_price else ''))
    return sides, extra, labels


def _check_combo_sides(combo, now, sides=None):
    """套餐的配餐也要可供應：已售完、停售、不在供應時段 (主餐在上面檢查；庫存數量於扣庫存時檢查)"""
    for side in (combo.sides if sides is None else sides):
        item = side.menu_item
        if item is None:
            raise AppError(f'套餐【{combo.name}】的配餐已不存在，請重新選擇！')
        if item.is_sold_out or item.is_branch_hidden:
            raise AppError(f'套餐【{combo.name}】的配餐【{item.name}】已售完，請改選其他套餐！',
                           insufficient_stock=True, sold_out=True, sold_out_items=[item.name])
        in_service, hint = item.service_status(now)
        if not in_service:
            raise AppError(f'套餐【{combo.name}】的配餐【{item.name}】目前不供應 ({hint})，請改選其他套餐！',
                           sold_out=True, sold_out_items=[item.name])


def _combo_sides_snapshot(line):
    """賣出當下的配餐 (餐點 uuid 清單，跨資料庫通用)：之後店家改了或刪了套餐，取消 / 分店扣庫存仍以這份為準"""
    if line.combo is None:
        return ''
    return json.dumps([s.menu_item.uuid for s in line.sides if s.menu_item is not None])


def _parse_lines(items_data, tenant_id, price_channel='dine_in', now=None, allow_override=False, member=False):
    if not isinstance(items_data, list) or not items_data:
        raise AppError('點餐清單為空！')
    now = now or datetime.now()
    modifiers = {m.id: m for m in ModifierOption.query.filter_by(is_active=True).all()}
    modifier_names = {m.name for m in modifiers.values()}
    lines = []
    total_quantity = 0
    for raw in items_data:
        if not isinstance(raw, dict):
            continue
        item_id = safe_id(raw.get('menu_item_id') or raw.get('id'))
        menu_item = db.session.get(MenuItem, item_id) if item_id > 0 else None
        if menu_item is None or menu_item.tenant_id != tenant_id:
            raise AppError(f"餐點【{raw.get('name') or item_id}】不存在於系統中！")
        if menu_item.is_branch_hidden:
            raise AppError(f'餐點【{menu_item.name}】此分店未販售，請移除後再結帳！',
                           sold_out=True, sold_out_items=[menu_item.name])
        if menu_item.is_sold_out:
            raise AppError(f'餐點【{menu_item.name}】已售完，請移除後再結帳！',
                           insufficient_stock=True, sold_out=True, sold_out_items=[menu_item.name])
        in_service, hint = menu_item.service_status(now)
        if not in_service:
            raise AppError(f'餐點【{menu_item.name}】目前不供應 ({hint})，請移除後再結帳！',
                           sold_out=True, sold_out_items=[menu_item.name])

        quantity = max(1, safe_int(raw.get('quantity'), 1))
        if quantity > MAX_LINE_QUANTITY:
            raise AppError(f'餐點【{menu_item.name}】一次最多 {MAX_LINE_QUANTITY} 份，請調整數量！')
        total_quantity += quantity
        if total_quantity > MAX_ORDER_QUANTITY:
            raise AppError(f'單筆訂單最多 {MAX_ORDER_QUANTITY} 份，請分批結帳！')
        is_reward = bool(raw.get('is_reward'))
        pass_id = safe_id(raw.get('pass_id')) if not is_reward else 0
        is_add_on = bool(raw.get('is_add_on')) and not is_reward and not pass_id
        combo = None
        combo_id = safe_id(raw.get('combo_option_id'))
        if combo_id and pass_id:   # 寄杯只兌換單品本身 (只收加料)，不能附帶套餐配餐
            raise AppError(f'餐點【{menu_item.name}】以寄杯兌換時不能搭配套餐，請改為單點！')
        if combo_id and not (is_reward or is_add_on):
            combo = db.session.get(ComboOption, combo_id)
            if combo is None or combo.tenant_id != tenant_id or combo.main_item_id != menu_item.id:
                raise AppError(f'餐點【{menu_item.name}】的套餐選項不存在，請重新選擇！')
            combo_sides, choice_extra, choice_labels = _combo_choice_sides(combo, raw.get('combo_choices'))
            _check_combo_sides(combo, now, combo_sides)
        else:
            combo_sides, choice_extra, choice_labels = [], 0, []

        raw_mods = raw.get('modifier_ids') or []
        if not isinstance(raw_mods, (list, tuple)):
            raise AppError(f'餐點【{menu_item.name}】的加料資料格式不正確，請重新選擇！')
        mod_ids = [safe_id(m) for m in raw_mods if str(m).isdigit()]
        if mod_ids and combo is None and menu_item.modifiers != 'addons' and not menu_item.can_be_add_on:
            raise AppError(f'餐點【{menu_item.name}】不支援客製化加料！')
        if any(m not in modifiers for m in mod_ids):
            raise AppError('包含無效或已停用的加料品項！')
        extra = sum(modifiers[m].price for m in mod_ids)

        parts = _clean_customization(raw.get('customization'), modifier_names)
        parts += [f"{modifiers[m].name}(+{modifiers[m].price})" for m in mod_ids]

        # 規格 (大 / 中 / 小杯…)：單選、影響單價；沒有指定時使用第一個 (預設) 規格。紅利兌換與加購品不適用。
        size_delta = 0
        sizes = menu_item.sizes if not (is_reward or is_add_on) else []
        if sizes:
            wanted = str(raw.get('size') or '').strip()
            chosen = next((s for s in sizes if s[0] == wanted), None) if wanted else sizes[0]
            if chosen is None:
                raise AppError(f'餐點【{menu_item.name}】沒有「{wanted}」這個規格，請重新選擇！')
            if pass_id and chosen is not sizes[0]:   # 寄杯兌換只含預設規格 (單價不收規格加價)
                raise AppError(f'餐點【{menu_item.name}】以寄杯兌換時只能選擇預設規格「{sizes[0][0]}」！')
            size_delta = chosen[1]
            parts = [p for p in parts if p != chosen[0]]
            parts.insert(0, chosen[0])
        if combo is not None:
            prefixes = tuple(f'{group}:' for group, _opts in combo.choice_groups)
            parts = [p for p in parts if not (prefixes and p.startswith(prefixes))]   # 前端顯示用的任選文字由後端重新產生
            parts[0:0] = [f'[套餐: {combo.name}]', *choice_labels]

        if is_reward:
            if not menu_item.is_reward or menu_item.redeem_points <= 0:
                raise AppError(f'餐點【{menu_item.name}】未開放紅利兌換！')
            unit_price = extra
            parts = parts or ['紅利免費兌換']
        elif pass_id:   # 寄杯兌換：免費 (只收加料)，結帳時檢查並扣次數
            unit_price = extra
            parts = [p for p in parts if p != '[寄杯]']   # 舊掛單 / 舊版 POS 仍可能送來這個標籤，避免重複
            parts.insert(0, '[寄杯]')
        elif is_add_on:
            if not menu_item.can_be_add_on:
                raise AppError(f'餐點【{menu_item.name}】非合法加購品項！')
            unit_price = (menu_item.add_on_price or 0) + extra
        else:
            unit_price = max(0, menu_item.price_for(price_channel, member=member) + size_delta) + (combo.additional_price if combo else 0) \
                + choice_extra + extra

        original_price = None
        override = raw.get('override_price')
        if override not in (None, '') and not is_reward:
            if not allow_override:
                raise AppError('只有櫃檯收銀可以改價！', 403)
            new_price = safe_int(override, -1)
            if new_price < 0:
                raise AppError(f'餐點【{menu_item.name}】的改價金額不正確！')
            if new_price != unit_price:
                original_price, unit_price = int(unit_price), new_price
                parts.append(f'改價 ${original_price}→${new_price}')

        if pass_id and original_price is not None:
            raise AppError('寄杯兌換的品項不能改價')
        lines.append(CheckoutLine(menu_item, quantity, int(unit_price), '、'.join(parts)[:200],
                                  combo, is_add_on, is_reward, original_price, sides=combo_sides, pass_id=pass_id))
    if not lines:
        raise AppError('無有效結帳品項！')
    return lines


def _lock_member(user_id):
    """
    鎖住會員資料列到交易結束：同一位會員同時在多個通路 (點餐機、桌邊、POS、線上) 結帳時，
    「會員券是否還在、公開代碼已用幾次」依序檢查，不會兩邊都以為還沒用過。
    (SQLite 在第一個寫入後整個資料庫就已依序執行；PostgreSQL 以 SELECT ... FOR UPDATE 鎖住該列)
    """
    db.session.execute(select(User.id).where(User.id == user_id).with_for_update())


def _consumed_promo_error(code, user_id):
    """
    送出時明明可以用、結帳時卻已被用掉 (同一位會員在其他裝置 / 門市 / 線上同時使用) 的原因；其他情況回傳 None。
    這兩種情況要讓結帳失敗並說明，不可默默改用原價成立訂單 (顧客 / 收銀看到的金額與實收不同)。
    """
    from app.services import coupon_service
    if not user_id:
        return None
    held = UserCoupon.query.filter_by(user_id=user_id, code=code).all()
    if held and all(uc.is_used for uc in held):
        return f'優惠券「{code}」已被使用 (可能剛在其他裝置、門市或線上使用)，請重新確認金額後再結帳！'
    public = Coupon.query.filter_by(code=code).first()
    if public is not None and not public.requires_holding and public.uses_per_member \
            and coupon_service.member_uses(user_id, code) >= public.uses_per_member:
        return (f'優惠代碼「{code}」每位會員限用 {public.uses_per_member} 次，已達使用上限 '
                f'(可能剛在其他裝置、門市或線上使用)，請重新確認金額後再結帳！')
    return None


def _apply_promo(tenant_id, code, subtotal, user_id, guard=None):
    """在交易內套用優惠碼：會員持有券以條件式 UPDATE 核銷。回傳 (折抵金額, 實際套用代碼, 會員券 ID)"""
    if not code:
        return 0, '', None
    if user_id:
        _lock_member(user_id)
    promo = find_promo(code, subtotal, user_id, guard=guard)
    if not promo.valid:
        consumed = _consumed_promo_error(code, user_id)
        if consumed:
            raise AppError(consumed, 409, coupon_unavailable=True)
        return 0, '', None  # 代碼無效或未達門檻：照常結帳，只是不折抵
    if promo.user_coupon is not None:
        claimed = db.session.execute(
            update(UserCoupon)
            .where(UserCoupon.id == promo.user_coupon.id, UserCoupon.tenant_id == tenant_id, UserCoupon.is_used.is_(False),
                   (UserCoupon.expires_at.is_(None)) | (UserCoupon.expires_at >= datetime.now()))
            .values(is_used=True, used_at=datetime.now())
            .execution_options(synchronize_session=False)
        ).rowcount
        if not claimed:
            raise AppError('該優惠券已被核銷！')
        return promo.discount, promo.coupon.code, promo.user_coupon.id
    return promo.discount, promo.coupon.code, None


def _service_charge_applies(order_source, order_type, cfg):
    scope = set(cfg.get('service_charge_scope') or [])
    if order_type != OrderType.DINE_IN:
        return False
    if order_source == OrderSource.POS:
        return 'pos_dine_in' in scope
    if order_source == OrderSource.TABLE:
        return 'table' in scope
    if order_source == OrderSource.KIOSK:
        return 'kiosk_dine_in' in scope
    return False


def _price_channel(order_source, order_type, is_hub):
    if is_hub:
        return 'online'
    if order_source in DELIVERY_SOURCES:
        return 'delivery'
    return 'takeout' if order_type in (OrderType.TAKE_OUT, OrderType.DELIVERY, OrderType.HOME_DELIVERY) else 'dine_in'


def _manual_discount(order_discount, remaining):
    """整單折扣 {type: amount|percent, value} → 折抵金額 (不超過剩餘金額)"""
    if not isinstance(order_discount, dict):
        return 0, 0
    kind = order_discount.get('type')
    value = safe_int(order_discount.get('value'), 0)
    if value <= 0:
        return 0, 0
    if kind == 'percent':
        value = min(100, value)            # 折抵 value%
        amount = remaining * value / 100
        return min(amount, remaining), value
    amount = min(value, remaining)
    return amount, (amount * 100 / remaining if remaining else 100)
