"""後台「禮物卡」：已售出的卡片、餘額、異動紀錄、作廢 (店長以上)"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from app.models.enums import MANAGER_ROLES
from app.errors import AppError
from app.extensions import db
from app.models.member import GiftCard, GiftCardLedger
from app.services import giftcard_service
from app.services.audit import record_staff_audit
from app.utils import find_own, get_own, role_required

gift_cards_bp = Blueprint('gift_cards', __name__)


@gift_cards_bp.route('/admin/gift_cards', methods=['GET', 'POST'], endpoint='index')
@role_required(*MANAGER_ROLES)
def index():
    if request.method == 'POST':
        card = get_own(GiftCard, request.form.get('card_id'), '禮物卡')
        if request.form.get('action') != 'void':
            raise AppError('不支援的操作')
        left = giftcard_service.void(card, current_user.username)
        record_staff_audit('GIFT_CARD_VOID', details=f'作廢禮物卡 {card.masked_code} (剩餘 ${left})：'
                                                     f'{(request.form.get("reason") or "").strip()[:100]}')
        db.session.commit()
        flash(f'已作廢禮物卡 {card.masked_code}，餘額 ${left} 歸零', 'info')
        return redirect(url_for('gift_cards.index'))
    q = (request.args.get('q') or '').strip()
    query = GiftCard.query
    digits = giftcard_service.normalize_code(q)
    if digits:
        query = query.filter(GiftCard.code.like(f'%{digits}%'))
    cards = query.order_by(GiftCard.id.desc()).limit(200).all()
    balances = dict(db.session.query(GiftCardLedger.gift_card_id, func.coalesce(func.sum(GiftCardLedger.delta), 0))
                    .filter(GiftCardLedger.gift_card_id.in_([c.id for c in cards]))
                    .group_by(GiftCardLedger.gift_card_id).all()) if cards else {}
    outstanding = db.session.query(func.coalesce(func.sum(GiftCardLedger.delta), 0)).scalar() or 0
    detail = None
    if request.args.get('card'):
        detail = find_own(GiftCard, request.args.get('card'))
    return render_template('admin_gift_cards.html', cards=cards, balances=balances, q=q, outstanding=outstanding,
                           detail=detail, detail_entries=detail.entries.order_by(GiftCardLedger.id).all() if detail else [],
                           enabled=giftcard_service.enabled())
