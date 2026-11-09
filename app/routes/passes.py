"""寄杯 / 多次券：後台商品設定與售出紀錄、POS 售出"""
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.models.enums import POS_ROLES
from app.errors import AppError
from app.extensions import db, limiter
from app.models.member import MemberPass, PassLedger, PassProduct
from app.models.menu import MenuItem
from app.models.user import User
from app.services import pass_service
from app.services.audit import record_staff_audit
from app.services.approval_service import permission_required
from app.services.order_service import find_by_client_order_id
from app.utils import find_own, get_json_dict, get_own, role_required, safe_int

passes_bp = Blueprint('passes', __name__)


@passes_bp.route('/admin/passes', methods=['GET', 'POST'], endpoint='index')
@permission_required('coupons')
def index():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'save':
            pid = safe_int(request.form.get('product_id'), 0)
            product = get_own(PassProduct, pid, '寄杯商品') if pid else PassProduct(tenant_id=g.tenant_id)
            pass_service.save_product(product, request.form)
            db.session.add(product)
            record_staff_audit('PASS_PRODUCT', details=f'{"編輯" if pid else "新增"}寄杯商品 {product.name} '
                                                       f'({product.uses} 次 ${product.price})')
            db.session.commit()
            flash(f'已儲存寄杯商品「{product.name}」', 'success')
        elif action == 'toggle':
            product = get_own(PassProduct, request.form.get('product_id'), '寄杯商品')
            product.is_active = not product.is_active
            db.session.commit()
            flash(f"寄杯商品「{product.name}」已{'上架' if product.is_active else '下架'} (已售出的寄杯照常可用)", 'success')
        else:
            raise AppError('不支援的操作')
        return redirect(url_for('passes.index'))
    products = PassProduct.query.order_by(PassProduct.id).all()
    q = (request.args.get('q') or '').strip()
    query = MemberPass.query.join(User, User.id == MemberPass.user_id)
    if q:
        query = query.filter((User.phone.contains(q)) | (User.name.contains(q)))
    passes = query.order_by(MemberPass.id.desc()).limit(200).all()
    outstanding = db.session.query(func.coalesce(func.sum(MemberPass.remaining), 0)) \
        .filter(MemberPass.status == 'active').scalar() or 0
    # 未兌換次數的預收金額 (依各張寄杯的平均單價)
    liability = sum(round((mp.price_paid or 0) * max(0, mp.remaining or 0) / mp.total_uses)
                    for mp in MemberPass.query.filter(MemberPass.status == 'active', MemberPass.remaining > 0).all()
                    if mp.total_uses)
    detail = None
    if request.args.get('pass'):
        detail = find_own(MemberPass, request.args.get('pass'))
    entries = PassLedger.query.filter_by(member_pass_id=detail.id).order_by(PassLedger.id).all() if detail else []
    items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
    return render_template('admin_passes.html', products=products, passes=passes, q=q, outstanding=outstanding,
                           liability=liability, detail=detail, entries=entries, items=items)


@passes_bp.route('/api/pos/passes/sell', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('30/minute')
def api_sell():
    """
    POS 售出寄杯：{user_id, product_id, payment_method, cash_received, payment_info, invoice, request_id}。
    request_id：前端每次開啟售出視窗產生 (重送沿用)，存成寄杯購買訂單的 client_order_id (同店唯一索引)；
    回應遺失後重按「售出」時回傳原本那筆，不會重複建立寄杯與收款。
    """
    data = get_json_dict()
    request_id = str(data.get('request_id') or '').strip()[:36] or None
    existing = find_by_client_order_id(g.tenant_id, request_id) if request_id else None
    if existing is not None:
        return _replay_sale(existing)
    user = db.session.get(User, safe_int(data.get('user_id'), 0))
    mp, order = pass_service.sell(g.tenant_id, data.get('product_id'), user, data.get('payment_method', 'Cash'),
                                  current_user.username, cash_received=data.get('cash_received'),
                                  payment_info=data.get('payment_info') or {}, invoice_info=data.get('invoice') or None)
    if request_id:
        order.client_order_id = request_id
    try:
        db.session.commit()
    except IntegrityError:
        # 同一個 request_id 同時送出兩次：後到的撞上唯一索引，回傳先完成的那筆
        db.session.rollback()
        existing = find_by_client_order_id(g.tenant_id, request_id) if request_id else None
        if existing is None:
            raise
        return _replay_sale(existing)
    return _sale_response(mp, order)


def _sale_response(mp, order, replayed=False):
    return jsonify({'success': True, 'pass': mp.to_dict(), 'order_id': order.id, 'total': order.total_price,
                    'cash_change': order.cash_change, 'replayed': replayed,
                    'message': f'已售出寄杯「{mp.name}」{mp.total_uses} 次'})


def _replay_sale(order):
    """重送同一個 request_id：只回傳給原本售出的收銀員，且必須是寄杯購買訂單"""
    mp = MemberPass.query.filter_by(sale_order_id=order.id).first()
    if mp is None or order.cashier_username != current_user.username:
        raise AppError('售出編號重複，請關閉視窗後重新操作', 409)
    return _sale_response(mp, order, replayed=True)
