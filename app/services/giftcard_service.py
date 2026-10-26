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
from app.services import settings_service

CODE_LENGTH = 16


def config(tenant_id=None):
    return settings_service.get('gift_card', tenant_id)


def enabled(tenant_id=None):
    return bool(config(tenant_id).get('enabled'))


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


def sell(tenant_id, amount, method, staff, payment_info=None, note=''):
    """POS 售出：建立卡片與售出異動、收款紀錄 (預收)。由呼叫端 commit。"""
    from app.models.payment import PaymentKind
    from app.services import payment_service
    cfg = config(tenant_id)
    if not cfg.get('enabled'):
        raise AppError('尚未啟用禮物卡 (系統設定 → 禮物卡)')
    amount = int(amount or 0)
    limit = int(cfg.get('max_amount') or 10000)
    if not 1 <= amount <= limit:
        raise AppError(f'禮物卡面額需介於 $1 ~ ${limit:,}')
    method = normalize_payment_method(method)
    if method is None or method in (PaymentMethod.STORED_VALUE, PaymentMethod.GIFT_CARD, PaymentMethod.PLATFORM,
                                    PaymentMethod.ATM, PaymentMethod.CVS):
        raise AppError('購買禮物卡的付款方式不正確')
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
    """鎖住這張卡 (PostgreSQL 列鎖 / SQLite 寫入鎖)，避免兩台 POS 同時扣款超扣"""
    db.session.execute(update(GiftCard).where(GiftCard.id == card.id).values(status=GiftCard.status)
                       .execution_options(synchronize_session=False))


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


def refund(order, amount, staff=''):
    """訂單作廢 / 部分退款：退回原禮物卡"""
    code = normalize_code(order.payment_reference)
    card = GiftCard.query.filter_by(tenant_id=order.tenant_id, code=code).execution_options(skip_tenant_filter=True).first()
    if card is None:
        raise AppError('找不到這筆訂單使用的禮物卡，請人工處理', 409)
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
