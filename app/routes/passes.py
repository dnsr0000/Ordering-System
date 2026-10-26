"""寄杯 / 多次券：後台商品設定與售出紀錄、POS 售出"""
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from app.errors import AppError
from app.extensions import db, limiter
from app.models.member import MemberPass, PassLedger, PassProduct
from app.models.menu import MenuItem
from app.models.user import User
from app.services import pass_service
from app.services.audit import record_staff_audit
from app.utils import get_json_dict, role_required, safe_int

passes_bp = Blueprint('passes', __name__)
MANAGERS = ('owner', 'manager')
POS_ROLES = ('owner', 'manager', 'staff')


@passes_bp.route('/admin/passes', methods=['GET', 'POST'], endpoint='index')
@role_required(*MANAGERS)
def index():
    if request.method == 'POST':
        action = request.form.get('action')
        if action == 'save':
            pid = safe_int(request.form.get('product_id'), 0)
            product = db.session.get(PassProduct, pid) if pid else PassProduct(tenant_id=g.tenant_id)
            if product is None or product.tenant_id != g.tenant_id:
                raise AppError('查無此寄杯商品', 404)
            pass_service.save_product(product, request.form)
            db.session.add(product)
            record_staff_audit('PASS_PRODUCT', details=f'{"編輯" if pid else "新增"}寄杯商品 {product.name} '
                                                       f'({product.uses} 次 ${product.price})')
            db.session.commit()
            flash(f'✅ 已儲存寄杯商品「{product.name}」', 'success')
        elif action == 'toggle':
            product = db.session.get(PassProduct, safe_int(request.form.get('product_id'), 0))
            if product is None or product.tenant_id != g.tenant_id:
                raise AppError('查無此寄杯商品', 404)
            product.is_active = not product.is_active
            db.session.commit()
            flash(f"✅ 寄杯商品「{product.name}」已{'上架' if product.is_active else '下架'} (已售出的寄杯照常可用)", 'success')
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
        detail = db.session.get(MemberPass, safe_int(request.args.get('pass'), 0))
        if detail is not None and detail.tenant_id != g.tenant_id:
            detail = None
    entries = PassLedger.query.filter_by(member_pass_id=detail.id).order_by(PassLedger.id).all() if detail else []
    items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all()
    return render_template('admin_passes.html', products=products, passes=passes, q=q, outstanding=outstanding,
                           liability=liability, detail=detail, entries=entries, items=items)


@passes_bp.route('/api/pos/passes/sell', methods=['POST'])
@role_required(*POS_ROLES)
@limiter.limit('30/minute')
def api_sell():
    """POS 售出寄杯：{user_id, product_id, payment_method, cash_received, payment_info, invoice}"""
    data = get_json_dict()
    user = db.session.get(User, safe_int(data.get('user_id'), 0))
    mp, order = pass_service.sell(g.tenant_id, data.get('product_id'), user, data.get('payment_method', 'Cash'),
                                  current_user.username, cash_received=data.get('cash_received'),
                                  payment_info=data.get('payment_info') or {}, invoice_info=data.get('invoice') or None)
    db.session.commit()
    return jsonify({'success': True, 'pass': mp.to_dict(), 'order_id': order.id, 'total': order.total_price,
                    'cash_change': order.cash_change, 'message': f'已售出寄杯「{mp.name}」{mp.total_uses} 次'})
