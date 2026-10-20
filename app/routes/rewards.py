import re
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, g
from app.extensions import db
from app.models.user import User, RewardRedemption
from app.models.menu import MenuItem, ModifierOption
from app.models.coupon import Coupon, UserCoupon
from app.services.voucher_service import list_open_vouchers, try_refund_voucher
from app.utils import safe_int, get_json_dict

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
    data = get_json_dict()

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
        db.session.flush()
        # 領取上限二次確認：上面的次數檢查與寫入之間有空窗，同時送出多個請求會一起通過。
        # 扣點的 UPDATE 已取得資料庫寫入鎖，因此這裡重新計數時其他請求的結果已可見；超過上限就整筆取消 (連同扣點一起 rollback)
        if coupon.per_user_limit and coupon.per_user_limit > 0:
            if UserCoupon.query.filter_by(user_id=user_id, coupon_id=coupon.id).count() > coupon.per_user_limit:
                db.session.rollback()
                return jsonify({
                    'success': False,
                    'message': f'❌ 您已達此優惠券的兌換上限（每位會員限兌換 {coupon.per_user_limit} 次）！'
                }), 400
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

    data = get_json_dict()
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

    # 嚴格檢查並核銷尚未使用的 RewardRedemption 憑證，防止無憑證或重複退點洗點數 (#5)。
    # 每張憑證以「原子條件式 UPDATE」退點：若同一時間已被結帳核銷或被另一個退點請求處理，這裡就搶不到，
    # 搶到的張數不足就整筆取消 (rollback)，不會出現「餐點拿了、點數也退了」。
    candidates = list_open_vouchers(g.tenant_id, user_id, menu_item.id)
    candidates.reverse()  # 退點優先退最近兌換的 (與原行為一致)
    refunded_ids = []
    pts_to_refund = 0
    for voucher_id, cost in candidates:
        if len(refunded_ids) >= quantity:
            break
        if try_refund_voucher(g.tenant_id, user_id, voucher_id):
            refunded_ids.append(voucher_id)
            pts_to_refund += cost

    if len(refunded_ids) < quantity:
        db.session.rollback()
        return jsonify({'success': False, 'message': '查無可退還之有效兌換紀錄，無法重複退點！'}), 400

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