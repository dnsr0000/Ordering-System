import re
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, g, session
from app.extensions import db, acquire_distributed_lock
from app.models.user import User, CustomerLog, RewardSetting, RewardRedemption
from app.models.menu import MenuItem, ComboOption, ModifierOption
from app.models.order import Order, OrderItem
from app.models.coupon import Coupon, UserCoupon
from app.services.order_service import (
    update_popular_items, sanitize_discount_value, calc_promo_discount
)
from app.services.event_bus import order_event_bus
from app.services.ai_service import invalidate_ai_advice
from app.routes.common import record_staff_audit
from app.routes.auth import clear_pin_lockout
from app.utils import (
    role_required, rate_limit, safe_int,
    ALLOWED_PAYMENT_METHODS, ALLOWED_ORDER_TYPES
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
    code = str(data.get('promo_code', '')).strip().upper()
    user_id = safe_int(data.get('user_id'), 0) or None
    try:
        subtotal = float(data.get('subtotal', 0))
    except (ValueError, TypeError):
        subtotal = 0.0

    if not code:
        return jsonify({'valid': False, 'discount': 0, 'message': '請輸入優惠代碼！'})

    coupon = None
    if user_id:
        user_coupon = UserCoupon.query.filter_by(user_id=user_id, code=code, is_used=False).first()
        if user_coupon:
            coupon = user_coupon.coupon

    if not coupon:
        public_coupon = Coupon.query.filter_by(code=code).first()
        if public_coupon and (public_coupon.reward_points == 0 and not public_coupon.is_reward):
            coupon = public_coupon
        elif public_coupon and public_coupon.is_reward:
            if not user_id:
                return jsonify({'valid': False, 'discount': 0, 'message': '此為會員專屬紅利券，請先於 POS 綁定會員！'})
            else:
                return jsonify({'valid': False, 'discount': 0, 'message': '該會員尚未持有此優惠券，或該券已核銷！'})
        else:
            return jsonify({'valid': False, 'discount': 0, 'message': '無效的優惠代碼！'})

    if subtotal < coupon.min_spend:
        return jsonify({'valid': False, 'discount': 0, 'message': f'未達使用門檻！需消費滿 ${int(coupon.min_spend)} 元才可折抵。'})

    discount = calc_promo_discount(coupon, subtotal)
    if coupon.discount_type == 'fixed':
        msg = f'已折抵現金 ${int(discount)} 元！'
    else:
        safe_percent = sanitize_discount_value('percent', coupon.discount_value)
        msg = f'已套用 {round(safe_percent * 10, 1)} 折優惠，折抵 ${int(discount)} 元！'

    return jsonify({'valid': True, 'discount': discount, 'message': msg})


@pos_bp.route('/api/pos/checkout', methods=['POST'])
@role_required('owner', 'manager', 'staff')
@rate_limit(max_requests=30, window_seconds=60, key_prefix="pos_checkout", methods=('POST',))
def api_pos_checkout():
    """
    櫃檯 POS 原子結帳端點：
    - 嚴格檢驗 payment_method 與 order_type 白名單
    - 完整持久化 order_source='POS'、cashier_username、cash_received、cash_change 至 Order 資料表
    - 同步寫入 CustomerLog 與 StaffAuditLog 稽核日誌，確保收銀對帳與會員扣點可追責
    """
    data = request.get_json() or {}
    raw_items = data.get('items', [])
    payment_method = str(data.get('payment_method', 'Cash')).strip()
    order_type = str(data.get('order_type', '內用')).strip()

    if payment_method not in ALLOWED_PAYMENT_METHODS:
        return jsonify({'success': False, 'error': f'不支援的付款方式：{payment_method}'}), 400
    if order_type not in ALLOWED_ORDER_TYPES:
        return jsonify({'success': False, 'error': f'不支援的用餐方式：{order_type}'}), 400

    need_cutlery = bool(data.get('need_cutlery', False)) if order_type == '外帶' else False
    note = str(data.get('note', '')).strip()[:200]
    promo_code = str(data.get('promo_code', '')).strip().upper()
    use_points = max(0, safe_int(data.get('use_points', 0), 0))
    table_input = str(data.get('table_number', '')).strip()[:40]
    cash_received = max(0, safe_int(data.get('cash_received', 0), 0))

    if not raw_items or not isinstance(raw_items, list):
        return jsonify({'success': False, 'error': '點餐清單為空！'}), 400

    req_user_id = safe_int(data.get('user_id'), 0)
    user = db.session.get(User, req_user_id) if req_user_id > 0 else None
    if not user or user.tenant_id != g.tenant_id:
        user = None
        user_id = None
    else:
        user_id = user.id

    all_active_modifiers = ModifierOption.query.filter_by(is_active=True).all()
    modifier_db_map = {mod.id: mod for mod in all_active_modifiers}
    modifier_names_set = {mod.name for mod in all_active_modifiers}

    verified_items = []
    item_demands = {}
    reward_item_demands = []  # (menu_item, quantity, unit_pts)

    for client_item in raw_items:
        if not isinstance(client_item, dict):
            continue
        raw_name = str(client_item.get('name', '')).strip()
        raw_custom = str(client_item.get('customization', '')).strip()
        quantity = max(1, safe_int(client_item.get('quantity', 1), 1))
        is_add_on = bool(client_item.get('is_add_on', False))
        raw_client_id = str(client_item.get('id', ''))
        is_claimed_reward = ('[點數兌換]' in raw_name) or ('reward_' in raw_client_id) or bool(client_item.get('is_reward'))
        clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', raw_name).strip()

        menu_item = None
        item_db_id = safe_int(client_item.get('menu_item_id') or client_item.get('id'), 0)
        if item_db_id > 0:
            menu_item = MenuItem.query.filter_by(id=item_db_id, tenant_id=g.tenant_id).first()
        if not menu_item:
            menu_item = MenuItem.query.filter_by(name=clean_name).first()

        if not menu_item:
            return jsonify({'success': False, 'error': f'餐點【{clean_name}】不存在於系統中！'}), 400

        item_demands[menu_item.id] = item_demands.get(menu_item.id, 0) + quantity

        extra_modifier_price = 0
        verified_modifier_labels = []
        raw_modifier_ids = client_item.get('modifier_ids')
        if not isinstance(raw_modifier_ids, list):
            raw_modifier_ids = []
        clean_modifier_ids = [int(mid) for mid in raw_modifier_ids if str(mid).isdigit()]

        if clean_modifier_ids:
            is_combo = bool(re.search(r'\[套餐:\s*([^\]]+)\]', raw_custom))
            if menu_item.modifiers != 'addons' and not is_combo and not menu_item.can_be_add_on:
                return jsonify({'success': False, 'error': f'餐點【{menu_item.name}】不支援客製化加料！'}), 400

            for mid in clean_modifier_ids:
                if mid in modifier_db_map:
                    mod = modifier_db_map[mid]
                    extra_modifier_price += mod.price
                    verified_modifier_labels.append(f"{mod.name}(+{mod.price})")
                else:
                    return jsonify({'success': False, 'error': '包含無效或已停用的加料品項！'}), 400

        safe_custom_parts = []
        for part in [p.strip() for p in raw_custom.split('、') if p.strip()]:
            if any(name in part for name in modifier_names_set):
                continue
            cleaned_part = re.sub(r'\(\+\d+\)', '', part).strip()
            if cleaned_part:
                safe_custom_parts.append(cleaned_part)

        safe_custom_parts.extend(verified_modifier_labels)
        safe_customization = "、".join(safe_custom_parts) if safe_custom_parts else ""

        combo_option_id = None
        display_name = menu_item.name
        if is_claimed_reward:
            if not user_id:
                return jsonify({'success': False, 'error': '請先綁定會員才可使用紅利點數兌換商品！'}), 400
            if not menu_item.is_reward:
                return jsonify({'success': False, 'error': f'餐點【{menu_item.name}】未開放紅利兌換！'}), 400
            unit_pts = menu_item.reward_discount_points if (
                menu_item.reward_discount_points and menu_item.reward_discount_points > 0
            ) else menu_item.reward_points
            if not unit_pts or unit_pts <= 0:
                return jsonify({'success': False, 'error': f'餐點【{menu_item.name}】未設定有效兌換點數！'}), 400

            reward_item_demands.append((menu_item, quantity, unit_pts))
            unit_price = extra_modifier_price
            display_name = f"🎁 [點數兌換] {menu_item.name}"
            if not safe_customization:
                safe_customization = '紅利免費兌換'
        elif is_add_on:
            if not menu_item.can_be_add_on:
                return jsonify({'success': False, 'error': f'餐點【{menu_item.name}】非合法加購品項！'}), 400
            unit_price = menu_item.add_on_price + extra_modifier_price
        else:
            base_price = menu_item.discount_price if (menu_item.is_discount and menu_item.discount_price > 0) else menu_item.price
            combo_extra_price = 0
            combo_match = re.search(r'\[套餐:\s*([^\]]+)\]', raw_custom)
            if combo_match:
                combo_name = combo_match.group(1).strip()
                combo_opt = ComboOption.query.filter_by(main_item_id=menu_item.id, name=combo_name).first()
                if combo_opt:
                    combo_extra_price = combo_opt.additional_price
                    combo_option_id = combo_opt.id
                    for side_id in (combo_opt.item1_id, combo_opt.item2_id, combo_opt.item3_id):
                        if side_id:
                            item_demands[side_id] = item_demands.get(side_id, 0) + quantity
            unit_price = base_price + combo_extra_price + extra_modifier_price

        verified_items.append({
            'menu_item_id': menu_item.id,
            'combo_option_id': combo_option_id,
            'menu_item_name': menu_item.name,
            'display_name': display_name,
            'price': int(unit_price),
            'quantity': quantity,
            'customization': safe_customization
        })

    if not verified_items:
        return jsonify({'success': False, 'error': '無有效結帳品項！'}), 400

    subtotal = sum(v['price'] * v['quantity'] for v in verified_items)
    cashier_name = getattr(g.staff_account, 'username', None) or session.get('admin_username', 'POS')

    try:
        with acquire_distributed_lock(f"lock:order_checkout:{g.tenant_id}", timeout=8, blocking_timeout=5):
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
            max_pickup = db.session.query(db.func.max(Order.pickup_number)).filter(
                Order.tenant_id == g.tenant_id,
                Order.created_at >= today_start,
                Order.created_at <= today_end
            ).scalar()
            next_pickup = (max_pickup % 999) + 1 if (max_pickup and max_pickup > 0) else 1

            # 1. 原子扣減主餐與套餐配餐庫存
            for item_id, demanded_qty in item_demands.items():
                result = db.session.execute(
                    db.text("UPDATE menu_item SET stock = stock - :qty, is_sold_out = CASE WHEN stock - :qty <= 0 THEN 1 ELSE is_sold_out END WHERE id = :id AND tenant_id = :tenant_id AND stock >= :qty"),
                    {"id": item_id, "qty": demanded_qty, "tenant_id": g.tenant_id}
                )
                if result.rowcount == 0:
                    db.session.rollback()
                    m_item = db.session.get(MenuItem, item_id)
                    curr = m_item.stock if m_item else 0
                    return jsonify({
                        'success': False,
                        'insufficient_stock': True,
                        'error': f'餐點或配餐【{m_item.name if m_item else ""}】庫存剩 {curr} 份，庫存不足！'
                    }), 400

            # 2. POS 直接扣減紅利兌換商品點數並建立核銷紀錄
            created_redemptions = []
            total_reward_item_pts = 0
            for m_item, qty, unit_pts in reward_item_demands:
                pts_needed = unit_pts * qty
                pts_res = db.session.execute(
                    db.text("UPDATE user SET points = points - :pts WHERE id = :id AND tenant_id = :tenant_id AND points >= :pts"),
                    {"id": user_id, "pts": pts_needed, "tenant_id": g.tenant_id}
                )
                if pts_res.rowcount == 0:
                    db.session.rollback()
                    return jsonify({'success': False, 'error': f'會員紅利點數不足（需 {pts_needed} 點），無法兌換【{m_item.name}】！'}), 400
                total_reward_item_pts += pts_needed
                for _ in range(qty):
                    r_obj = RewardRedemption(
                        tenant_id=g.tenant_id,
                        user_id=user_id,
                        menu_item_id=m_item.id,
                        points_cost=unit_pts,
                        is_used=True,
                        is_refunded=False
                    )
                    db.session.add(r_obj)
                    created_redemptions.append(r_obj)

            # 3. 優惠券核銷
            promo_discount = 0
            target_user_coupon = None
            if promo_code and user_id:
                target_user_coupon = UserCoupon.query.filter_by(user_id=user_id, code=promo_code, is_used=False).first()
                if target_user_coupon and subtotal >= target_user_coupon.coupon.min_spend:
                    cp = target_user_coupon.coupon
                    promo_discount = calc_promo_discount(cp, subtotal)
                    coupon_update = db.session.execute(
                        db.text("UPDATE user_coupon SET is_used = 1, used_at = :now WHERE id = :id AND tenant_id = :tenant_id AND is_used = 0"),
                        {"id": target_user_coupon.id, "now": datetime.now(), "tenant_id": g.tenant_id}
                    )
                    if coupon_update.rowcount == 0:
                        db.session.rollback()
                        return jsonify({'success': False, 'error': '該優惠券已被核銷！'}), 400

            if promo_code and not target_user_coupon:
                pub_cp = Coupon.query.filter_by(code=promo_code, is_reward=False, reward_points=0).first()
                if pub_cp and subtotal >= pub_cp.min_spend:
                    promo_discount = calc_promo_discount(pub_cp, subtotal)

            # 4. 紅利點數現金折抵與回饋
            setting = RewardSetting.query.first()
            ppd = setting.points_per_dollar if (setting and setting.points_per_dollar > 0) else 1
            max_discount_limit = setting.max_discount_per_order if setting else 0
            spend_threshold = setting.spend_per_point if (setting and setting.spend_per_point > 0) else 100
            is_points_enabled = setting.is_enabled if setting else True

            remaining_amount = max(0, subtotal - promo_discount)
            points_used = 0
            points_discount = 0

            if user_id and use_points > 0 and is_points_enabled:
                curr_pts_row = db.session.execute(
                    db.text("SELECT points FROM user WHERE id = :id AND tenant_id = :tenant_id"),
                    {"id": user_id, "tenant_id": g.tenant_id}
                ).fetchone()
                avail_points = int(curr_pts_row[0] or 0) if curr_pts_row else 0

                max_cash = remaining_amount
                if max_discount_limit > 0:
                    max_cash = min(max_cash, max_discount_limit)

                actual_points = min(avail_points, use_points)
                points_discount = min(actual_points // ppd, max_cash)
                points_used = int(points_discount * ppd)

                if points_used > 0:
                    pts_update = db.session.execute(
                        db.text("UPDATE user SET points = points - :used WHERE id = :id AND tenant_id = :tenant_id AND points >= :used"),
                        {"id": user_id, "used": points_used, "tenant_id": g.tenant_id}
                    )
                    if pts_update.rowcount == 0:
                        db.session.rollback()
                        return jsonify({'success': False, 'error': '會員紅利點數不足！'}), 400

            final_price = int(max(0, remaining_amount - points_discount))
            total_discount = int(promo_discount + points_discount)
            points_earned = int(final_price // spend_threshold) if user_id else 0

            # 檢查現金實收金額不得小於應付總額
            if payment_method == 'Cash' and cash_received > 0 and cash_received < final_price:
                db.session.rollback()
                return jsonify({'success': False, 'error': f'實收現金 (${cash_received}) 小於應付總額 (${final_price})！'}), 400

            actual_received = cash_received if (payment_method == 'Cash' and cash_received >= final_price) else final_price
            cash_change = max(0, actual_received - final_price) if payment_method == 'Cash' else 0

            if user_id and points_earned > 0:
                db.session.execute(
                    db.text("UPDATE user SET points = points + :earned WHERE id = :id AND tenant_id = :tenant_id"),
                    {"id": user_id, "earned": points_earned, "tenant_id": g.tenant_id}
                )

            if table_input:
                table_display = f"{table_input} ({user.name})" if user and user.name not in table_input else table_input
            elif user:
                table_display = f"POS-{user.name}"
            else:
                table_display = "櫃檯POS"

            now_dt = datetime.now()
            # 5. 建立 CustomerLog 紀錄，讓 POS 點餐紀錄與會員進場日誌完整銜接
            items_summary_str = ", ".join([f"{v['display_name']} x{v['quantity']}" for v in verified_items])
            pos_cust_log = CustomerLog(
                tenant_id=g.tenant_id,
                user_type='POS會員' if user else 'POS訪客',
                identifier=f"{user.name} ({user.phone})" if user else table_display[:100],
                login_at=now_dt,
                logout_at=now_dt,
                ordered_items=f"[POS 取餐#{next_pickup}] {items_summary_str}",
                ip_address=request.remote_addr or ''
            )
            db.session.add(pos_cust_log)
            db.session.flush()

            new_order = Order(
                tenant_id=g.tenant_id,
                user_id=user_id,
                table_number=table_display[:50],
                total_price=final_price,
                payment_method=payment_method,
                order_type=order_type,
                order_source='POS',
                cashier_username=cashier_name,
                cash_received=actual_received,
                cash_change=cash_change,
                need_cutlery=need_cutlery,
                note=note,
                status='Pending',
                points_used=points_used,
                points_earned=points_earned,
                discount_amount=total_discount,
                coupon_code=promo_code if promo_code else '',
                pickup_number=next_pickup,
                customer_log_id=pos_cust_log.id,
                created_at=now_dt
            )
            db.session.add(new_order)
            db.session.flush()

            for r_obj in created_redemptions:
                r_obj.order_id = new_order.id

            receipt_items = []
            for v in verified_items:
                order_item = OrderItem(
                    order_id=new_order.id,
                    menu_item_id=v['menu_item_id'],
                    combo_option_id=v['combo_option_id'],
                    item_name=v['display_name'],
                    price=v['price'],
                    quantity=v['quantity'],
                    customization=v['customization']
                )
                db.session.add(order_item)
                receipt_items.append({
                    'name': v['display_name'],
                    'price': v['price'],
                    'quantity': v['quantity'],
                    'subtotal': v['price'] * v['quantity'],
                    'customization': v['customization']
                })

            # 6. 寫入 StaffAuditLog 稽核日誌（特別記錄收銀金額與會員點數異動）
            net_pts_delta = points_earned - (points_used + total_reward_item_pts)
            record_staff_audit(
                action_type='POS_CREATE_ORDER',
                target_order_id=new_order.id,
                target_user_id=user_id,
                points_delta=net_pts_delta,
                details=(
                    f"POS 結帳單號 #{new_order.id} (取餐 #{next_pickup:03d}) | "
                    f"顧客:{table_display} | 付款:{payment_method} 應收:${final_price} 實收:${actual_received} 找零:${cash_change} | "
                    f"兌換扣點:{total_reward_item_pts} 折抵扣點:{points_used} 贈點:{points_earned} 優惠碼:{promo_code or '無'}"
                )
            )
            db.session.commit()

    except TimeoutError:
        return jsonify({'success': False, 'error': '系統結帳佇列繁忙，請重試！'}), 503
    except Exception as e:
        db.session.rollback()
        print(f"[!] POS 結帳異常: {e}")
        return jsonify({'success': False, 'error': '結帳交易發生異常，請重試！'}), 500

    db.session.expire_all()
    remaining_points = 0
    if user_id:
        refreshed_user = db.session.get(User, user_id)
        remaining_points = refreshed_user.points if refreshed_user else 0

    update_popular_items(force=False)
    invalidate_ai_advice(g.tenant_id)
    order_event_bus.notify()

    return jsonify({
        'success': True,
        'order_id': new_order.id,
        'pickup_number': next_pickup,
        'table_number': table_display,
        'user_name': user.name if user else table_display,
        'subtotal': int(subtotal),
        'discount_amount': total_discount,
        'points_used': points_used,
        'points_earned': points_earned,
        'total_price': final_price,
        'cash_received': actual_received,
        'cash_change': cash_change,
        'payment_method': payment_method,
        'order_type': order_type,
        'order_source': 'POS',
        'need_cutlery': need_cutlery,
        'note': note,
        'created_at': new_order.created_at.strftime('%Y-%m-%d %H:%M:%S') if new_order.created_at else '',
        'cashier': cashier_name,
        'items': receipt_items,
        'remaining_points': remaining_points
    })
