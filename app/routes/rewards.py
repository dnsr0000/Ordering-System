import re
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, g
from app.extensions import db
from app.models.user import User, RewardRedemption
from app.models.menu import MenuItem, ModifierOption
from app.models.coupon import Coupon, UserCoupon
from app.utils import safe_int

rewards_bp = Blueprint('rewards', __name__)


@rewards_bp.route('/rewards', endpoint='rewards_store')
def rewards_store():
    if not session.get('user_id'):
        return redirect(url_for('auth.face_login'))

    user = db.session.get(User, session['user_id'])
    if not user or user.tenant_id != g.tenant_id:
        return redirect(url_for('auth.logout'))
    reward_items = MenuItem.query.filter_by(is_reward=True).all()
    reward_coupons = Coupon.query.filter_by(is_reward=True).all()
    modifier_options = ModifierOption.query.filter_by(is_active=True).all()
    session['user_points'] = user.points
    return render_template('rewards.html', user=user, reward_items=reward_items, reward_coupons=reward_coupons, modifier_options=modifier_options)


@rewards_bp.route('/api/redeem_reward', methods=['POST'])
def redeem_reward():
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '請先登入會員！'}), 401
    user_id = session['user_id']
    data = request.get_json() or {}

    coupon_id = data.get('coupon_id') or (data.get('id') if data.get('type') == 'coupon' else None)
    if coupon_id is not None:
        coupon = Coupon.query.filter_by(id=safe_int(coupon_id), tenant_id=g.tenant_id).first()
        if not coupon or not coupon.is_reward:
            return jsonify({'success': False, 'message': '無效的優惠券！'}), 400

        if coupon.per_user_limit and coupon.per_user_limit > 0:
            redeemed_count = UserCoupon.query.filter_by(user_id=user_id, coupon_id=coupon.id).count()
            if redeemed_count >= coupon.per_user_limit:
                return jsonify({
                    'success': False,
                    'message': f'❌ 您已達此優惠券的兌換上限（每位會員限兌換 {coupon.per_user_limit} 次）！'
                }), 400

        req_points = coupon.reward_discount_points if (coupon.reward_discount_points and coupon.reward_discount_points > 0) else coupon.reward_points
        res = db.session.execute(
            db.text("UPDATE user SET points = points - :pts WHERE id = :id AND tenant_id = :tenant_id AND points >= :pts"),
            {"id": user_id, "pts": req_points, "tenant_id": g.tenant_id}
        )
        if res.rowcount == 0:
            db.session.rollback()
            return jsonify({'success': False, 'message': f'紅利不足！需要 {req_points} 點。'}), 400

        user_coupon = UserCoupon(user_id=user_id, coupon_id=coupon.id, code=coupon.code, is_used=False)
        db.session.add(user_coupon)
        db.session.commit()
        db.session.expire_all()
        refreshed_user = db.session.get(User, user_id)
        session['user_points'] = refreshed_user.points
        return jsonify({
            'success': True,
            'is_coupon': True,
            'promo_code': coupon.code,
            'message': f'🎉 成功兌換【{coupon.title}】！',
            'remaining_points': refreshed_user.points
        })

    item_id = data.get('item_id') or data.get('id')
    item = MenuItem.query.filter_by(id=safe_int(item_id), tenant_id=g.tenant_id).first()
    if not item or not item.is_reward or item.is_sold_out or (item.stock is not None and item.stock <= 0):
        return jsonify({'success': False, 'message': '商品不存在或已售罄！'}), 400

    req_points = item.reward_discount_points if (item.reward_discount_points and item.reward_discount_points > 0) else item.reward_points
    res = db.session.execute(
        db.text("UPDATE user SET points = points - :pts WHERE id = :id AND tenant_id = :tenant_id AND points >= :pts"),
        {"id": user_id, "pts": req_points, "tenant_id": g.tenant_id}
    )
    if res.rowcount == 0:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'紅利不足！需要 {req_points} 點。'}), 400

    redemption = RewardRedemption(
        tenant_id=g.tenant_id,
        user_id=user_id,
        menu_item_id=item.id,
        points_cost=req_points,
        is_used=False,
        is_refunded=False
    )
    db.session.add(redemption)
    db.session.commit()
    db.session.expire_all()
    refreshed_user = db.session.get(User, user_id)
    session['user_points'] = refreshed_user.points

    raw_modifier_ids = data.get('modifier_ids')
    if not isinstance(raw_modifier_ids, list):
        raw_modifier_ids = []
    clean_modifier_ids = [int(m) for m in raw_modifier_ids if str(m).isdigit()]

    extra_price = 0
    verified_labels = []
    if clean_modifier_ids:
        mods = ModifierOption.query.filter(
            ModifierOption.id.in_(clean_modifier_ids),
            ModifierOption.is_active == True
        ).all()
        for mod in mods:
            extra_price += mod.price
            verified_labels.append(f"{mod.name}(+{mod.price})")

    customization = str(data.get('customization', '')).strip()
    safe_parts = [re.sub(r'\(\+\d+\)', '', p.strip()) for p in customization.split('、') if p.strip()]
    safe_parts = [p for p in safe_parts if p] + verified_labels
    final_custom = "、".join(safe_parts) if safe_parts else '紅利免費兌換'

    return jsonify({
        'success': True,
        'is_coupon': False,
        'message': f'🎉 成功兌換【{item.name}】！',
        'remaining_points': refreshed_user.points,
        'redeemed_item': {
            'id': f'reward_{item.id}_{redemption.id}',
            'redemption_id': redemption.id,
            'reward_item_id': item.id,
            'name': f'🎁 [點數兌換] {item.name}',
            'price': extra_price,
            'points_cost': req_points,
            'is_reward': True,
            'customization': final_custom,
            'quantity': 1,
            'modifier_ids': clean_modifier_ids
        }
    })


@rewards_bp.route('/api/refund_reward_item', methods=['POST'])
def refund_reward_item():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'message': '請先登入會員！'}), 401

    data = request.get_json() or {}
    item_id = safe_int(data.get('item_id'), 0)
    raw_name = str(data.get('name', '')).strip()
    quantity = max(1, safe_int(data.get('quantity', 1), 1))

    menu_item = None
    if item_id > 0:
        menu_item = MenuItem.query.filter_by(id=item_id, tenant_id=g.tenant_id).first()
    if not menu_item and raw_name:
        clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', raw_name).strip()
        menu_item = MenuItem.query.filter_by(name=clean_name, tenant_id=g.tenant_id).first()

    if not menu_item or not menu_item.is_reward:
        return jsonify({'success': False, 'message': '查無此兌換商品或非有效兌換品項！'}), 400

    # 嚴格檢查並核銷尚未使用的 RewardRedemption 憑證，防止無憑證或重複退點洗點數 (#5)
    vouchers = RewardRedemption.query.filter_by(
        user_id=user_id,
        menu_item_id=menu_item.id,
        is_used=False,
        is_refunded=False
    ).order_by(RewardRedemption.id.desc()).limit(quantity).all()

    if len(vouchers) < quantity:
        return jsonify({'success': False, 'message': '查無可退還之有效兌換紀錄，無法重複退點！'}), 400

    pts_to_refund = sum(v.points_cost for v in vouchers)
    for v in vouchers:
        v.is_refunded = True

    db.session.execute(
        db.text("UPDATE user SET points = points + :pts WHERE id = :id AND tenant_id = :tenant_id"),
        {"pts": pts_to_refund, "id": user_id, "tenant_id": g.tenant_id}
    )
    db.session.commit()
    db.session.expire_all()
    user = db.session.get(User, user_id)
    session['user_points'] = user.points if user else 0

    return jsonify({
        'success': True,
        'message': f'已退還 {pts_to_refund} 點數',
        'current_points': user.points if user else 0,
        'new_points': user.points if user else 0
    })
