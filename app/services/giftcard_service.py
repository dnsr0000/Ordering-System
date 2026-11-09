"""
禮物卡 (商品禮券)：POS 售出 (收款記為預收 gift_sale，不開發票；兌換消費時才開立)、結帳以卡號扣款、
訂單退款退回卡片、作廢。餘額 = gift_card_ledger 合計 (各分店同步合併，與點數 / 儲值金同樣的設計)。
"""
import secrets
from datetime import datetime

from sqlalchemy import func, update

from app.errors import AppError
from app.extensions import db
from app.models.enums import PaymentMethod, normalize_payment_method
from app.models.member import GiftCard, GiftCardLedger
from app.models.member import mask_code as mask_code   # 卡號遮罩定義在 models.member (模型不能反向匯入服務)，由這裡提供給服務層 / 路由
from app.services import settings_service
from app.utils import safe_int

CODE_LENGTH = 16


config = settings_service.reader('gift_card')
enabled = settings_service.flag('gift_card')


def normalize_code(raw):
    return ''.join(ch for ch in str(raw or '') if ch.isdigit())[:CODE_LENGTH]


def preset_amounts(tenant_id=None):
    out = []
    for part in str(config(tenant_id).get('preset_amounts') or '').replace('，', ',').split(','):
        part = part.strip()
        if part.isdigit() and int(part) > 0:
            out.append(int(part))
    return out[:8]


def balance(card):
    return db.session.query(func.coalesce(func.sum(GiftCardLedger.delta), 0)) \
        .filter(GiftCardLedger.gift_card_id == card.id).scalar() or 0


def _new_code(tenant_id):
    for _ in range(20):
        code = str(secrets.randbelow(9 * 10 ** (CODE_LENGTH - 1)) + 10 ** (CODE_LENGTH - 1))
        if not GiftCard.query.filter_by(tenant_id=tenant_id, code=code).execution_options(skip_tenant_filter=True).first():
            return code
    raise AppError('無法產生卡號，請再試一次', 500)


def find(tenant_id, code):
    code = normalize_code(code)
    if len(code) != CODE_LENGTH:
        raise AppError('禮物卡卡號應為 16 位數字')
    card = GiftCard.query.filter_by(tenant_id=tenant_id, code=code).execution_options(skip_tenant_filter=True).first()
    if card is None:
        raise AppError('查無此禮物卡', 404)
    return card


def summary(card):
    return {'code': card.code, 'masked': card.masked_code, 'balance': balance(card), 'status': card.status,
            'initial_amount': card.initial_amount, 'created_at': card.created_at.strftime('%Y-%m-%d')}


SALE_METHODS = (PaymentMethod.CASH, PaymentMethod.CREDIT_CARD, PaymentMethod.LINE_PAY, PaymentMethod.JKO_PAY,
                PaymentMethod.APPLE_PAY)   # 售出禮物卡可收的付款方式 (實際收款，與 wallet_service.TOPUP_METHODS 相同)


def sell(tenant_id, amount, method, staff, payment_info=None, note=''):
    """POS 售出：建立卡片與售出異動、收款紀錄 (預收)。由呼叫端 commit。"""
    from app.models.payment import PaymentKind
    from app.services import payment_service
    if not enabled(tenant_id):
        raise AppError('尚未啟用禮物卡 (系統設定 → 禮物卡)')
    amount = safe_int(amount, 0)   # 非數字 ('abc'、'1,000') 走下面的面額檢查回 400，不丟 ValueError
    limit = int(config(tenant_id).get('max_amount') or 10000)
    if not 1 <= amount <= limit:
        raise AppError(f'禮物卡面額需介於 $1 ~ ${limit:,}')
    method = normalize_payment_method(method)
    if method not in SALE_METHODS:   # 儲值金 / 禮物卡 / 平台代收 / ATM / 超商代碼 / 混合付款 都不是實際收款
        raise AppError('購買禮物卡的付款方式不正確')
    if payment_service.open_shift_id(tenant_id, lock=True) is None \
            and settings_service.get('pos', tenant_id).get('require_shift'):
        raise AppError('尚未開班：請先在 POS 按「交班」輸入開班零用金後再收款', 409, need_shift=True)
    reference, last4 = '', ''
    if method == PaymentMethod.CREDIT_CARD:
        reference, last4 = payment_service.validate_card_details(payment_info, True)
    card = GiftCard(tenant_id=tenant_id, code=_new_code(tenant_id), initial_amount=amount, sold_by=staff,
                    note=str(note or '').strip()[:100], created_at=datetime.now())
    db.session.add(card)
    db.session.flush()
    db.session.add(GiftCardLedger(tenant_id=tenant_id, gift_card_id=card.id, delta=amount, reason='issue',
                                  staff_username=staff))
    payment_service.record_payment(None, PaymentKind.GIFT_SALE, method, amount, reference=reference, card_last4=last4,
                                   note=f'禮物卡售出 {card.masked_code}', staff=staff, tenant_id=tenant_id)
    return card


def _lock(card):
    """
    鎖住這張卡 (PostgreSQL 列鎖 / SQLite 寫入鎖)，避免兩台 POS 同時扣款超扣。
    以 Core Table 的 UPDATE 寫回原值 (含 updated_at)：不標記為同步變更、不更新時間戳。否則每次扣款都會把
    整張卡 (本機看到的狀態) 以最新時間戳上傳，離線分店在作廢後才扣款時，LWW 會把作廢蓋回「可使用」。
    """
    table = GiftCard.__table__
    db.session.execute(update(table).where(table.c.id == card.id)
                       .values(status=table.c.status, updated_at=table.c.updated_at))


def charge(tenant_id, code, amount, order_id, staff=''):
    """結帳扣款 (與訂單同一交易)；餘額不足拋出 AppError。回傳卡片"""
    card = find(tenant_id, code)
    if card.status != 'active':
        raise AppError('此禮物卡已作廢', 409)
    _lock(card)
    left = balance(card)
    if left < amount:
        raise AppError(f'禮物卡餘額不足 (餘額 ${left}，需 ${amount})，請改用其他付款方式', 409, insufficient_gift=True)
    db.session.add(GiftCardLedger(tenant_id=tenant_id, gift_card_id=card.id, delta=-int(amount), reason='redeem',
                                  order_id=order_id, staff_username=staff))
    return card


def _order_card(order):
    code = normalize_code(order.payment_reference)
    return GiftCard.query.filter_by(tenant_id=order.tenant_id, code=code).execution_options(skip_tenant_filter=True).first()


def voided_card(order):
    """這筆訂單使用的禮物卡已作廢時回傳該卡 (退款不能退回卡片，需改用其他方式人工退款)，否則 None"""
    card = _order_card(order)
    return card if card is not None and card.status != 'active' else None


def refund(order, amount, staff=''):
    """
    訂單作廢 / 部分退款：退回原禮物卡。
    卡片已作廢時拋出 409：作廢的卡永遠不能再使用，退回卡片等於顧客拿不到退款 (呼叫端可先以 voided_card 判斷，改記人工退款)。
    """
    card = _order_card(order)
    if card is None:
        raise AppError('找不到這筆訂單使用的禮物卡，請人工處理', 409)
    if card.status != 'active':
        raise AppError(f'這筆訂單使用的禮物卡 {card.masked_code} 已作廢，無法退回卡片，請改用現金等其他方式人工退款', 409)
    db.session.add(GiftCardLedger(tenant_id=order.tenant_id, gift_card_id=card.id, delta=int(amount), reason='refund',
                                  order_id=order.id, staff_username=staff))
    return card


def void(card, staff):
    """作廢 (遺失 / 退卡)：餘額歸零。由呼叫端 commit"""
    if card.status == 'void':
        raise AppError('此禮物卡已作廢', 409)
    _lock(card)
    left = balance(card)
    if left:
        db.session.add(GiftCardLedger(tenant_id=card.tenant_id, gift_card_id=card.id, delta=-left, reason='void',
                                      staff_username=staff))
    card.status, card.voided_at = 'void', datetime.now()
    return left
