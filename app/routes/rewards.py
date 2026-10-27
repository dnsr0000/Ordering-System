from flask import Blueprint, g, jsonify, redirect, render_template, request

from app.errors import AppError
from app.extensions import db, limiter
from app.models.coupon import Coupon
from app.models.enums import VoucherStatus
from app.models.menu import MenuItem, ModifierOption
from app.models.user import RewardRedemption
from app.routes.auth import member_login_url
from app.services import loyalty_service, pass_service, reward_service
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
        reward_coupons=[c for c in Coupon.query.filter_by(is_reward=True, member_only=False).all() if c.period_error() is None],
        held_coupons=_held_summary(member.id),
        modifier_options=ModifierOption.query.filter_by(is_active=True).all(),
        open_vouchers=_open_voucher_summary(member.id),
        stamp=loyalty_service.stamp_card(member),
        expiry=loyalty_service.upcoming_expiry(member),
        referral=_referral(member),
        passes=pass_service.member_passes(member.id),
    )


@rewards_bp.route('/api/member/marketing', methods=['POST'], endpoint='marketing_pref')
@limiter.limit('20/minute')
def marketing_pref():
    """會員自己開啟 / 關閉行銷訊息 (關閉後自動行銷不發券、不推播；會同步到各分店與線上端)"""
    member = _require_member()
    opt_in = bool(get_json_dict().get('opt_in'))
    member.marketing_opt_out = not opt_in
    db.session.commit()
    return jsonify({'success': True, 'opt_in': opt_in,
                    'message': '✅ 已開啟優惠活動訊息' if opt_in else '✅ 已關閉優惠活動訊息，之後不會再收到行銷優惠券與推播'})


def _referral(member):
    """推薦好友 (已啟用時)：第一次開啟時產生推薦碼"""
    if not loyalty_service.referral_config(member.tenant_id).get('referral_enabled'):
        return None
    if not member.referral_code:
        loyalty_service.ensure_referral_code(member)
        db.session.commit()
    return loyalty_service.referral_summary(member)


@rewards_bp.route('/api/member_qr', methods=['GET', 'POST'], endpoint='member_qr')
@limiter.limit('30/minute')
def member_qr():
    """會員動態條碼 (QR 圖檔 data URI)；POST action=rotate 重新產生金鑰讓舊碼 (含截圖) 立即失效"""
    from app.services import member_qr_service, table_service
    member = _require_member()
    if request.method == 'POST':
        if get_json_dict().get('action') != 'rotate':
            raise AppError('不支援的操作')
        member_qr_service.rotate_secret(member)
    token = member_qr_service.make_token(member)
    db.session.commit()
    return jsonify({'success': True, 'token': token, 'qr': table_service.qr_data_uri(token),
                    'expires_in': member_qr_service.seconds_left()})


def _held_summary(user_id):
    """會員目前持有、尚未使用且未過期的券：{coupon_id: {'count': 張數, 'expires': 最快到期時間}}"""
    from app.services import coupon_service
    held = {}
    for uc in coupon_service.usable_coupons(user_id):
        row = held.setdefault(uc.coupon_id, {'count': 0, 'expires': None})
        row['count'] += 1
        end = uc.expired_at
        if end is not None and (row['expires'] is None or end < row['expires']):
            row['expires'] = end
    return held


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
