import time
import secrets
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from app.extensions import db
from app.models.user import User, RewardRedemption
from app.models.menu import MenuItem
from app.models.coupon import Coupon, UserCoupon

rewards_bp = Blueprint('rewards', __name__)

@rewards_bp.route('/rewards', endpoint='rewards_store')
def rewards_store():
    if not session.get('user_id'):
        return redirect(url_for('auth.face_login'))
    
    user = db.session.get(User, session['user_id'])
    if not user:
        return redirect(url_for('auth.logout'))
    reward_items = MenuItem.query.filter_by(is_reward=True).all()
    reward_coupons = Coupon.query.filter_by(is_reward=True).all()
    session['user_points'] = user.points
    return render_template('rewards.html', user=user, reward_items=reward_items, reward_coupons=reward_coupons)

@rewards_bp.route('/api/redeem_reward', methods=['POST'])
def redeem_reward():
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '請先登入會員！'}), 401
    user_id = session['user_id']
    data = request.get_json() or {}

    if 'coupon_id' in data:
        coupon = db.session.get(Coupon, data.get('coupon_id'))
        if not coupon:
            return jsonify({'success': False, 'message': '無效的優惠券！'}), 400
        req_points = coupon.reward_discount_points if coupon.reward_discount_points > 0 else coupon.reward_points
        res = db.session.execute(db.text("UPDATE user SET points = points - :pts WHERE id = :id AND points >= :pts"), {"id": user_id, "pts": req_points})
        if res.rowcount == 0:
            db.session.rollback()
            return jsonify({'success': False, 'message': f'紅利不足！需要 {req_points} 點。'}), 400

        user_coupon = UserCoupon(user_id=user_id, coupon_id=coupon.id, code=coupon.code, is_used=False)
        db.session.add(user_coupon)
        db.session.commit()
        refreshed_user = db.session.get(User, user_id)
        session['user_points'] = refreshed_user.points
        return jsonify({'success': True, 'is_coupon': True, 'promo_code': coupon.code, 'message': f'🎉 成功兌換【{coupon.title}】！', 'remaining_points': refreshed_user.points})

    item = db.session.get(MenuItem, data.get('item_id'))
    if not item or not item.is_reward or item.is_sold_out or (item.stock is not None and item.stock <= 0):
        return jsonify({'success': False, 'message': '商品不存在或已售罄！'}), 400

    req_points = item.reward_discount_points if item.reward_discount_points > 0 else item.reward_points
    res = db.session.execute(db.text("UPDATE user SET points = points - :pts WHERE id = :id AND points >= :pts"), {"id": user_id, "pts": req_points})
    if res.rowcount == 0:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'紅利不足！需要 {req_points} 點。'}), 400

    db.session.commit()
    refreshed_user = db.session.get(User, user_id)
    session['user_points'] = refreshed_user.points
    customization = str(data.get('customization', '')).strip()
    extra_price = int(data.get('extra_price', 0))
    redemption_token = secrets.token_urlsafe(32)
    db.session.add(RewardRedemption(
        token=redemption_token,
        user_id=user_id,
        menu_item_id=item.id,
        points=req_points,
        status='reserved'
    ))
    db.session.commit()

    return jsonify({
        'success': True,
        'is_coupon': False,
        'message': f'🎉 成功兌換【{item.name}】！',
        'remaining_points': refreshed_user.points,
        'redeemed_item': {
            'id': f'reward_{item.id}_{int(time.time())}',
            'reward_token': redemption_token,
            'is_reward_item': True,
            'name': f'🎁 [點數兌換] {item.name}',
            'price': extra_price,
            'customization': customization if customization else '紅利免費兌換',
            'quantity': 1
        }
    })

@rewards_bp.route('/api/refund_reward', methods=['POST'])
def refund_reward():
    user_id = session.get('user_id')
    data = request.get_json() or {}
    token = str(data.get('reward_token', '')).strip()
    if not user_id or not token:
        return jsonify({'success': False, 'message': '無效的兌換紀錄！'}), 400

    redemption = RewardRedemption.query.filter_by(
        token=token, user_id=user_id, status='reserved'
    ).with_for_update().first()
    if not redemption:
        return jsonify({'success': False, 'message': '兌換品已使用或已返還。'}), 400

    user = db.session.get(User, user_id)
    user.points += redemption.points
    redemption.status = 'refunded'
    db.session.commit()
    session['user_points'] = user.points
    return jsonify({
        'success': True,
        'remaining_points': user.points,
        'message': f'已返還 {redemption.points} 點紅利。'
    })