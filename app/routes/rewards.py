from flask import Blueprint, g, jsonify, redirect, render_template

from app.errors import AppError
from app.extensions import db
from app.models.coupon import Coupon
from app.models.enums import VoucherStatus
from app.models.menu import MenuItem, ModifierOption
from app.models.user import RewardRedemption
from app.routes.auth import member_login_url
from app.services import reward_service
from app.services.customer_session import current_member
from app.utils import get_json_dict, safe_int

rewards_bp = Blueprint('rewards', __name__)


def _require_member():
    member = current_member()
    if member is None:
        raise AppError('請先登入會員！', 401)
    return member


@rewards_bp.route('/rewards', endpoint='rewards_store')
def rewards_store():
    member = current_member()
    if member is None:
        return redirect(member_login_url())
    return render_template(
        'rewards.html',
        user=member,
        reward_items=MenuItem.query.filter_by(is_reward=True).all(),
        reward_coupons=Coupon.query.filter_by(is_reward=True).all(),
        modifier_options=ModifierOption.query.filter_by(is_active=True).all(),
        open_vouchers=_open_voucher_summary(member.id),
    )


def _open_voucher_summary(user_id):
    """會員已兌換、尚未結帳使用的商品憑證 (依商品彙總)，供紅利商城頁面「退回兌換」使用"""
    summary = {}
    vouchers = (RewardRedemption.query.filter_by(user_id=user_id, status=VoucherStatus.OPEN)
                .order_by(RewardRedemption.id.asc()).all())
    for v in vouchers:
        if v.menu_item_id is None:
            continue
        row = summary.get(v.menu_item_id)
        if row is None:
            item = db.session.get(MenuItem, v.menu_item_id)
            row = summary[v.menu_item_id] = {'item_id': v.menu_item_id, 'name': item.name if item else '(已下架商品)',
                                             'quantity': 0, 'points': 0}
        row['quantity'] += 1
        row['points'] += v.points_cost or 0
    return list(summary.values())


@rewards_bp.route('/api/redeem_reward', methods=['POST'])
def redeem_reward():
    member = _require_member()
    data = get_json_dict()
    if data.get('coupon_id') is not None:
        coupon = reward_service.redeem_coupon(g.tenant_id, member.id, safe_int(data['coupon_id']))
        return jsonify({
            'success': True, 'is_coupon': True, 'promo_code': coupon.code,
            'message': f'🎉 成功兌換【{coupon.title}】！',
            'remaining_points': reward_service.member_points(member.id),
        })

    modifier_ids = [safe_int(m) for m in data.get('modifier_ids') or [] if str(m).isdigit()]
    item, _voucher, cart_line = reward_service.redeem_item(
        g.tenant_id, member.id, safe_int(data.get('item_id')), modifier_ids, data.get('customization'))
    return jsonify({
        'success': True, 'is_coupon': False,
        'message': f'🎉 成功兌換【{item.name}】！',
        'remaining_points': reward_service.member_points(member.id),
        'redeemed_item': cart_line,
    })


@rewards_bp.route('/api/refund_reward_item', methods=['POST'])
def refund_reward_item():
    member = _require_member()
    data = get_json_dict()
    points = reward_service.refund_item(g.tenant_id, member.id, safe_int(data.get('item_id')),
                                        max(1, safe_int(data.get('quantity'), 1)))
    current = reward_service.member_points(member.id)
    return jsonify({'success': True, 'message': f'已退還 {points} 點數', 'current_points': current})
