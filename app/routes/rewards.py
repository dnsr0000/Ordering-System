from flask import Blueprint, g, jsonify, render_template, request

from app.errors import AppError
from app.extensions import db, limiter
from app.models.coupon import Coupon
from app.models.enums import VoucherStatus
from app.models.menu import MenuItem, ModifierOption
from app.models.user import RewardRedemption
from app.services import branch_service, i18n_service, loyalty_service, pass_service, reward_service
from app.services.customer_session import current_member
from app.utils import get_json_dict, member_required, safe_int

rewards_bp = Blueprint('rewards', __name__)


def _require_member():
    member = current_member()
    if member is None:
        raise AppError('請先登入會員！', 401)
    return member


@rewards_bp.route('/rewards', endpoint='rewards_store')
@member_required()
def rewards_store(member):
    from app.services.customer_session import needs_pin_confirm
    branch_service.activate_for_request(g.tenant_id)   # 分店下架與結帳一致 (線上端依顧客選的取餐分店)
    return render_template(
        'rewards.html',
        user=member, needs_pin=needs_pin_confirm(),
        # 本分店未販售、不在販售期間 / 供應時段的兌換品不顯示 (兌換後也無法結帳)
        reward_items=[i for i in MenuItem.query.filter_by(is_reward=True).all()
                      if not i.is_branch_hidden and i.service_status()[0]],
        reward_coupons=[c for c in Coupon.query.filter_by(is_reward=True, member_only=False).all()
                        if c.period_error() is None and c.redeem_points > 0],
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
                    **_message('✅ 已開啟優惠活動訊息' if opt_in else '✅ 已關閉優惠活動訊息，之後不會再收到行銷優惠券與推播')})


def _message(message):
    """成功回應的訊息：中文 message 加上目前顧客語言的 message_i18n (英 / 日文介面顯示用；沒有對照時同原文)"""
    return {'message': message, 'message_i18n': i18n_service.translate_message(message)}


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
    from app.services.customer_session import require_confirmed
    member = _require_member()
    data = get_json_dict() if request.method == 'POST' else {}
    require_confirmed(member, data.get('pin'), '出示會員條碼')   # 條碼等同本人出示 (可動用儲值金)：人臉登入要再輸入 PIN
    if request.method == 'POST' and data.get('action') not in (None, '', 'rotate', 'confirm'):
        raise AppError('不支援的操作')
    if data.get('action') == 'rotate':
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
        # 兌換商品已被刪除 (menu_item_id 為 NULL) 的憑證彙總成 item_id 0 一列，仍可退回點數
        key = v.menu_item_id or 0
        row = summary.get(key)
        if row is None:
            item = db.session.get(MenuItem, key) if key else None
            row = summary[key] = {'item_id': key, 'name': item.name if item else '(已下架商品)',
                                  'quantity': 0, 'points': 0}
        row['quantity'] += 1
        row['points'] += v.points_cost or 0
    return list(summary.values())


@rewards_bp.route('/api/redeem_reward', methods=['POST'])
def redeem_reward():
    from app.services import customer_session
    member = _require_member()
    data = get_json_dict()
    if customer_session.needs_pin_confirm():   # 人臉登入 / 門市 LINE 登入：兌換 (花點數) 前要再輸入 PIN
        customer_session.require_confirmed(member, data.get('pin'), '兌換紅利')
    if data.get('coupon_id') is not None:
        coupon = reward_service.redeem_coupon(g.tenant_id, member.id, safe_int(data['coupon_id']))
        return jsonify({
            'success': True, 'is_coupon': True, 'promo_code': coupon.code,
            **_message(f'🎉 成功兌換【{coupon.title}】！'),
            'remaining_points': reward_service.member_points(member.id),
        })

    raw_mods = data.get('modifier_ids') or []
    if not isinstance(raw_mods, list):
        raise AppError('加料資料格式不正確，請重新選擇！')
    modifier_ids = [safe_int(m) for m in raw_mods if str(m).isdigit()]
    branch_service.activate_for_request(g.tenant_id)   # 分店下架的兌換品不可兌換 (與結帳一致)
    item, voucher, cart_line = reward_service.redeem_item(
        g.tenant_id, member.id, safe_int(data.get('item_id')), modifier_ids, data.get('customization'))
    customer_session.remember_voucher(voucher)   # 分店 / 線上端登出或換人登入時，自動退還本工作階段兌換、未結帳的憑證
    return jsonify({
        'success': True, 'is_coupon': False,
        **_message(f'🎉 成功兌換【{item.name}】！'),
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
    return jsonify({'success': True, **_message(f'已退還 {points} 點數'), 'current_points': current})
