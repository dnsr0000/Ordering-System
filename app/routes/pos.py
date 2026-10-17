import re
from flask import Blueprint, render_template, request, jsonify, g, session
from app.extensions import db
from app.models.user import User, RewardSetting
from app.models.menu import MenuItem, ModifierOption
from app.models.coupon import Coupon, UserCoupon
from app.services.order_service import verify_promo_code, process_checkout
from app.routes.common import record_staff_audit
from app.routes.auth import clear_pin_lockout
from app.utils import (
    role_required,
    rate_limit,
    safe_int
)

pos_bp = Blueprint('pos', __name__)
PIN_REGEX = re.compile(r'^\d{4,6}$')


def _serialize_pos_member(user):
    avail_cps = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
    coupons_list = []
    for uc in avail_cps:
        cp = uc.coupon
        if cp:
            d_desc = f"[折${int(cp.discount_value)}]" if cp.discount_type == 'fixed' else f"[{round(cp.discount_value * 10, 1)}折]"
            coupons_list.append({
                'user_coupon_id': uc.id,
                'code': uc.code,
                'title': f"{d_desc} {cp.title}",
                'min_spend': cp.min_spend
            })
    return {
        'id': user.id,
        'name': user.name,
        'phone': user.phone,
        'points': user.points or 0,
        'has_pin': bool(user.pin_hash),
        'coupons': coupons_list
    }


@pos_bp.route('/pos', endpoint='pos_terminal')
@role_required('owner', 'manager', 'staff')
def pos_terminal():
    """櫃檯 POS 收銀點餐主介面"""
    items = MenuItem.query.all()
    categories = sorted(list(set(item.category for item in items if item.category)))
    modifier_options = ModifierOption.query.filter_by(is_active=True).all()
    coupons = Coupon.query.all()

    reward_setting = RewardSetting.query.first()
    if not reward_setting:
        reward_setting = RewardSetting(
            is_enabled=True,
            points_per_dollar=1,
            max_discount_per_order=0,
            spend_per_point=100
        )
        db.session.add(reward_setting)
        db.session.commit()

    return render_template(
        'pos.html',
        items=items,
        categories=categories,
        modifier_options=modifier_options,
        coupons=coupons,
        reward_setting=reward_setting,
        tenant=g.tenant,
        staff=g.staff_account
    )


@pos_bp.route('/api/pos/member_lookup', methods=['POST'])
@role_required('owner', 'manager', 'staff')
@rate_limit(max_requests=45, window_seconds=60, key_prefix="pos_member_lookup", methods=('POST',))
def api_pos_member_lookup():
    """
    櫃檯人員透過手機號碼或姓名查詢本店會員：
    - 若傳入明確 member_id，直接回傳該筆會員
    - 若查詢關鍵字僅符合 1 筆，直接回傳該會員
    - 若模糊比對符合多筆 (>1)，回傳 candidates 清單讓店員於前端明確點選，避免綁錯同名會員扣錯點數
    """
    data = request.get_json() or {}
    exact_id = safe_int(data.get('member_id'), 0)
    if exact_id > 0:
        user = db.session.get(User, exact_id)
        if not user or user.tenant_id != g.tenant_id:
            return jsonify({'success': False, 'message': '查無此會員！'}), 404
        return jsonify({
            'success': True,
            'multiple': False,
            'member': _serialize_pos_member(user)
        })

    q = str(data.get('query', '')).strip()
    if not q:
        return jsonify({'success': False, 'message': '請輸入會員手機號碼或姓名！'}), 400

    # 1. 若為完整手機號碼精準命中，直接回傳（同店電話具唯一索引）
    exact_phone_user = User.query.filter_by(phone=q).first()
    if exact_phone_user:
        return jsonify({
            'success': True,
            'multiple': False,
            'member': _serialize_pos_member(exact_phone_user)
        })

    # 2. 模糊搜尋姓名或電話片段，最多取 20 筆
    matched_users = User.query.filter(
        (User.phone.contains(q)) | (User.name.contains(q))
    ).order_by(User.id.asc()).limit(20).all()

    if not matched_users:
        return jsonify({'success': False, 'message': f'查無符合「{q}」的本店會員！'}), 404

    if len(matched_users) == 1:
        return jsonify({
            'success': True,
            'multiple': False,
            'member': _serialize_pos_member(matched_users[0])
        })

    return jsonify({
        'success': True,
        'multiple': True,
        'candidates': [_serialize_pos_member(u) for u in matched_users]
    })


@pos_bp.route('/api/pos/set_member_pin', methods=['POST'])
@role_required('owner', 'manager', 'staff')
@rate_limit(max_requests=15, window_seconds=60, key_prefix="pos_set_pin", methods=('POST',))
def api_pos_set_member_pin():
    """櫃檯店員當面核對會員身分後，協助設定或重設會員 4~6 碼手機登入 PIN 碼並解除鎖定"""
    data = request.get_json() or {}
    user_id = safe_int(data.get('user_id'), 0)
    pin = str(data.get('pin', '')).strip()

    if user_id <= 0 or not PIN_REGEX.match(pin):
        return jsonify({'success': False, 'message': '請輸入 4 至 6 位數字的安全 PIN 碼！'}), 400

    user = db.session.get(User, user_id)
    if not user or user.tenant_id != g.tenant_id:
        return jsonify({'success': False, 'message': '查無此會員！'}), 404

    user.set_pin(pin)
    clear_pin_lockout(g.tenant_id, user.phone)
    record_staff_audit(
        action_type='POS_SET_MEMBER_PIN',
        target_user_id=user.id,
        details=f"櫃檯協助會員 {user.name} ({user.phone}) 設定/重設安全 PIN 碼並解除鎖定"
    )
    db.session.commit()
    return jsonify({'success': True, 'message': f'已成功為會員【{user.name}】設定安全 PIN 碼！'})

@pos_bp.route('/api/pos/verify_promo', methods=['POST'])
@role_required('owner', 'manager', 'staff')
def api_pos_verify_promo():
    """POS 專用促銷碼與會員持有優惠券驗證"""
    data = request.get_json() or {}
    code = data.get('promo_code', '')
    user_id = safe_int(data.get('user_id'), 0) or None
    subtotal = data.get('subtotal', 0)

    result = verify_promo_code(
        code=code,
        subtotal=subtotal,
        user_id=user_id,
        is_pos=True,
        tenant_id=g.tenant_id
    )
    return jsonify({
        'valid': result['valid'],
        'discount': result['discount'],
        'message': result['message']
    })
@pos_bp.route('/api/pos/checkout', methods=['POST'])
@role_required('owner', 'manager', 'staff')
@rate_limit(max_requests=30, window_seconds=60, key_prefix="pos_checkout", methods=('POST',))
def api_pos_checkout():
    """
    櫃檯 POS 原子結帳端點：
    委派 order_service.process_checkout 處理原子交易、扣庫存與稽核日誌
    """
    data = request.get_json() or {}
    cashier_name = getattr(g.staff_account, 'username', None) or session.get('admin_username', 'POS')

    result, status_code = process_checkout(
        items_data=data.get('items', []),
        payment_method=data.get('payment_method', 'Cash'),
        order_type=data.get('order_type', '內用'),
        need_cutlery=data.get('need_cutlery', False),
        note=data.get('note', ''),
        promo_code=data.get('promo_code', ''),
        use_points=data.get('use_points', 0),
        table_input=data.get('table_number', ''),
        cash_received=data.get('cash_received', 0),
        user_id=data.get('user_id'),
        order_source='POS',
        cashier_username=cashier_name,
        client_ip=request.remote_addr or '',
        tenant_id=g.tenant_id
    )
    return jsonify(result), status_code