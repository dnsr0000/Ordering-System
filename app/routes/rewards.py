import time
import re
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from app.extensions import db
from app.models.user import User
from app.models.menu import MenuItem, ModifierOption
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
    modifier_options = ModifierOption.query.filter_by(is_active=True).all()
    session['user_points'] = user.points
    return render_template('rewards.html', user=user, reward_items=reward_items, reward_coupons=reward_coupons,modifier_options=modifier_options)

@rewards_bp.route('/api/redeem_reward', methods=['POST'])
def redeem_reward():
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '請先登入會員！'}), 401
    user_id = session['user_id']
    data = request.get_json() or {}

    # 1. 優惠券兌換邏輯
    if 'coupon_id' in data:
        coupon = db.session.get(Coupon, data.get('coupon_id'))
        if not coupon:
            return jsonify({'success': False, 'message': '無效的優惠券！'}), 400

        # 檢核每位會員兌換上限
        if coupon.per_user_limit and coupon.per_user_limit > 0:
            redeemed_count = UserCoupon.query.filter_by(user_id=user_id, coupon_id=coupon.id).count()
            if redeemed_count >= coupon.per_user_limit:
                return jsonify({
                    'success': False, 
                    'message': f'❌ 您已達此優惠券的兌換上限（每位會員限兌換 {coupon.per_user_limit} 次）！'
                }), 400

        req_points = coupon.reward_discount_points if coupon.reward_discount_points > 0 else coupon.reward_points
        res = db.session.execute(
            db.text("UPDATE user SET points = points - :pts WHERE id = :id AND points >= :pts"), 
            {"id": user_id, "pts": req_points}
        )
        if res.rowcount == 0:
            db.session.rollback()
            return jsonify({'success': False, 'message': f'紅利不足！需要 {req_points} 點。'}), 400

        user_coupon = UserCoupon(user_id=user_id, coupon_id=coupon.id, code=coupon.code, is_used=False)
        db.session.add(user_coupon)
        db.session.commit()
        refreshed_user = db.session.get(User, user_id)
        session['user_points'] = refreshed_user.points
        return jsonify({
            'success': True, 
            'is_coupon': True, 
            'promo_code': coupon.code, 
            'message': f'🎉 成功兌換【{coupon.title}】！', 
            'remaining_points': refreshed_user.points
        })

    # 2. 餐點兌換邏輯
    item = db.session.get(MenuItem, data.get('item_id'))
    if not item or not item.is_reward or item.is_sold_out or (item.stock is not None and item.stock <= 0):
        return jsonify({'success': False, 'message': '商品不存在或已售罄！'}), 400

    req_points = item.reward_discount_points if item.reward_discount_points > 0 else item.reward_points
    res = db.session.execute(
        db.text("UPDATE user SET points = points - :pts WHERE id = :id AND points >= :pts"), 
        {"id": user_id, "pts": req_points}
    )
    if res.rowcount == 0:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'紅利不足！需要 {req_points} 點。'}), 400

    # 提交交易並同步 Session點數
    db.session.commit()
    refreshed_user = db.session.get(User, user_id)
    session['user_points'] = refreshed_user.points

    # 處理 modifier_ids 查表計價
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

    # 清洗客製化文字
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
            'id': f'reward_{item.id}_{int(time.time())}',
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
    """購物車刪除商城兌換品項時，全額返還已扣除之點數 (支援 ID 與品名雙重反查)"""
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'message': '請先登入會員！'}), 401

    data = request.get_json() or {}
    item_id = data.get('item_id')
    raw_name = str(data.get('name', '')).strip()
    fallback_points = int(data.get('points', 0))
    quantity = max(1, int(data.get('quantity', 1)))

    menu_item = None
    # 1. 優先透過 ID 查詢
    if item_id:
        try:
            menu_item = db.session.get(MenuItem, int(item_id))
        except (ValueError, TypeError):
            menu_item = None

    # 2. 兜底保障：若 ID 遺失或查無，剔除前綴後以品名反查
    if not menu_item and raw_name:
        clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', raw_name).strip()
        menu_item = MenuItem.query.filter_by(name=clean_name).first()

    pts_to_refund = 0
    if menu_item:
        unit_pts = menu_item.reward_discount_points if (menu_item.reward_discount_points and menu_item.reward_discount_points > 0) else menu_item.reward_points
        pts_to_refund = unit_pts * quantity

    # 3. 前端攜帶點數兜底
    if pts_to_refund <= 0 and fallback_points > 0:
        pts_to_refund = fallback_points * quantity

    if pts_to_refund <= 0:
        return jsonify({'success': False, 'message': '無法核對此兌換商品的退點額度！'}), 400

    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'success': False, 'message': '查無此會員！'}), 404

    # 直接使用 ORM 物件更新點數，防止快取不同步
    user.points = (user.points or 0) + pts_to_refund
    db.session.commit()

    session['user_points'] = user.points

    return jsonify({
        'success': True,
        'message': f'已成功退還 {pts_to_refund} 點紅利點數！',
        'current_points': user.points
    })