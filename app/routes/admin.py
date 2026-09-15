import os
import io
import time
import zipfile
import re
import unicodedata
import pandas as pd
from datetime import datetime, timedelta
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_file
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename
from openpyxl.styles import Font

from app.config import Config
from app.extensions import db
from app.models.user import User, AdminLog, CustomerLog, RewardSetting
from app.models.menu import MenuItem, ComboOption
from app.models.order import Order, OrderItem
from app.models.coupon import Coupon, UserCoupon
from app.services.cv_service import save_and_fix_image
from app.services.order_service import (
    update_popular_items, sanitize_discount_value, 
    cancel_order_and_rollback, build_order_analytics
)
from app.services.ai_service import generate_ai_business_advice, _AI_ADVICE_STATE, run_local_slm, fallback_menu_description
from app.services.event_bus import order_event_bus

admin_bp = Blueprint('admin', __name__)

# ==============================================================================
# 後台登入與主儀表板
# ==============================================================================
@admin_bp.route('/admin', methods=['GET', 'POST'], endpoint='admin_dashboard')
def admin_dashboard():
    if request.method == 'POST' and 'username' in request.form:
        username = request.form.get('username')
        password = request.form.get('password')
        if username == Config.ADMIN_USERNAME and check_password_hash(Config.ADMIN_PASSWORD_HASH, password):
            session.permanent = True
            session['admin_logged_in'] = True
            log = AdminLog(username=username, login_at=datetime.now(), ip_address=request.remote_addr or '')
            db.session.add(log)
            db.session.commit()
            session['admin_log_id'] = log.id
            return redirect(url_for('admin.admin_dashboard'))
        return "<script>alert('❌ 帳號或密碼錯誤！'); window.history.back();</script>", 401

    if not session.get('admin_logged_in'):
        return render_template('admin.html', is_admin=False)

    limit = max(1, request.args.get('limit', 3, type=int))
    all_orders = Order.query.order_by(Order.id.desc()).all()
    items = MenuItem.query.all()
    reward_items = MenuItem.query.filter_by(is_reward=True).all()
    reward_coupons = Coupon.query.all()
    users = User.query.all()

    analytics = build_order_analytics(all_orders, limit=limit)
    today = datetime.now().date()
    today_orders = [o for o in all_orders if o.created_at and o.created_at.date() == today]
    # 讀取當前紅利設定 (若無則自動產生預設值)
    reward_setting = RewardSetting.query.first()
    if not reward_setting:
        reward_setting = RewardSetting(is_enabled=True, points_per_dollar=1, max_discount_per_order=0, spend_per_point=100)
        db.session.add(reward_setting)
        db.session.commit()

    return render_template(
        'admin.html',
        is_admin=True,
        orders=today_orders,
        items=items,
        reward_items=reward_items,
        reward_coupons=reward_coupons,
        users=users,
        analytics=analytics,
        current_limit=limit,
        reward_setting=reward_setting
    )

@admin_bp.route('/admin/logout', endpoint='admin_logout')
def admin_logout():
    log_id = session.get('admin_log_id')
    if log_id:
        log = db.session.get(AdminLog, log_id)
        if log and not log.logout_at:
            log.logout_at = datetime.now()
            db.session.commit()
    session.pop('admin_logged_in', None)
    session.pop('admin_log_id', None)
    return redirect(url_for('admin.admin_dashboard'))

# ==============================================================================
# 菜單品項與加購品 CRUD
# ==============================================================================
@admin_bp.route('/admin/add', methods=['POST'])
def add_item():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    name = request.form.get('name')
    price = request.form.get('price')
    if name and price:
        final_price = max(0, round(float(price)))
        can_be_add_on = True if request.form.get('can_be_add_on') else False
        add_on_price = max(0, round(float(request.form.get('add_on_price') or 0)))
        
        if can_be_add_on:
            if add_on_price <= 0:
                return "<script>alert('❌ 已開啟加購品標籤，請輸入大於 0 的加購專屬價！'); window.history.back();</script>", 400
            if add_on_price >= final_price:
                return f"<script>alert('❌ 加購專屬價 (${add_on_price}) 不得高於或等於原單價 (${final_price})！'); window.history.back();</script>", 400

        image = request.files.get('image')
        image_filename = ''
        if image and image.filename != '':
            image_filename = f"menu_{int(time.time())}.jpg"
            filepath = os.path.join(Config.UPLOAD_FOLDER_MENU, image_filename)
            save_and_fix_image(image, filepath)

        is_sold_out = request.form.get('is_sold_out') == '1'
        is_rec = request.form.get('is_recommended') == '1'
        is_new = request.form.get('is_new') == '1'
        is_discount = request.form.get('is_discount') == '1'

        stock_val = max(0, int(request.form.get('stock') or 0))
        total_stock_val = max(stock_val, int(request.form.get('total_stock') or stock_val))

        new_item = MenuItem(
            name=name,
            category=request.form.get('category', '主餐'),
            modifiers=request.form.get('modifiers', 'none'),
            price=final_price,
            stock=stock_val,
            total_stock=total_stock_val,
            description=request.form.get('description', ''),
            image_path=image_filename,
            is_sold_out=is_sold_out,
            is_recommended=is_rec,
            is_manual_popular=is_rec,
            is_discount=is_discount,
            discount_price=max(0, round(float(request.form.get('discount_price') or 0))),
            is_new=is_new,
            can_be_add_on=can_be_add_on,
            add_on_price=add_on_price,
            addon_trigger_type=request.form.get('addon_trigger_type', 'any'),
            addon_trigger_target=request.form.get('addon_trigger_target', '').strip()
        )
        db.session.add(new_item)
        db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='menu'))

@admin_bp.route('/admin/edit/<int:id>', methods=['POST'])
def edit_item(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    item = MenuItem.query.get_or_404(id)
    name = request.form.get('name')
    price = request.form.get('price')
    from_tab = request.form.get('from_tab') or ('rewards' if item.is_reward else 'menu')

    if name and price:
        item_price = max(0, round(float(price)))
        can_be_add_on = bool(request.form.get('can_be_add_on'))
        add_on_price = max(0, round(float(request.form.get('add_on_price') or 0)))
        if can_be_add_on:
            if add_on_price <= 0:
                return "<script>alert('❌ 已開啟加購品標籤，請輸入大於 0 的加購專屬價！'); window.history.back();</script>", 400
            if add_on_price >= item_price:
                return f"<script>alert('❌ 加購專屬價 (${add_on_price}) 不得高於或等於原單價 (${item_price})！'); window.history.back();</script>", 400

        is_reward = bool(request.form.get('is_reward'))
        reward_points = max(0, int(request.form.get('reward_points') or 0))
        reward_discount_points = max(0, int(request.form.get('reward_discount_points') or 0))

        if is_reward:
            if reward_points <= 0:
                return "<script>alert('❌ 啟用紅利兌換時，「兌換所需點數」必須大於 0！'); window.history.back();</script>", 400
            if reward_points > item_price:
                return f"<script>alert('❌ 兌換所需點數 ({reward_points} 點) 不得高於原單價 (${item_price})！'); window.history.back();</script>", 400
            if reward_discount_points > 0 and reward_discount_points >= reward_points:
                return "<script>alert('❌ 「限時優惠點數」必須小於「兌換所需點數」！'); window.history.back();</script>", 400

        item.name = name
        item.category = request.form.get('category', '主餐')
        item.modifiers = request.form.get('modifiers', 'none')
        item.price = item_price
        item.stock = max(0, int(request.form.get('stock') or 0))
        item.total_stock = max(item.stock, int(request.form.get('total_stock') or item.stock))
        item.is_discount = bool(request.form.get('is_discount'))
        item.discount_price = max(0, round(float(request.form.get('discount_price') or 0)))
        item.description = request.form.get('description', '')
        item.is_sold_out = bool(request.form.get('is_sold_out')) or (item.stock == 0)
        item.is_manual_popular = bool(request.form.get('is_recommended'))
        item.is_new = bool(request.form.get('is_new'))
        item.can_be_add_on = can_be_add_on
        item.add_on_price = add_on_price
        item.addon_trigger_type = request.form.get('addon_trigger_type', 'any')
        item.addon_trigger_target = request.form.get('addon_trigger_target', '').strip()
        item.is_reward = is_reward
        item.reward_points = reward_points
        item.reward_discount_points = reward_discount_points

        image = request.files.get('image')
        if image and image.filename != '':
            image_filename = f"menu_{int(time.time())}.jpg"
            filepath = os.path.join(Config.UPLOAD_FOLDER_MENU, image_filename)
            save_and_fix_image(image, filepath)
            if item.image_path:
                old_path = os.path.join(Config.UPLOAD_FOLDER_MENU, item.image_path)
                if os.path.exists(old_path):
                    try: os.remove(old_path)
                    except Exception: pass
            item.image_path = image_filename

        db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab=from_tab))

@admin_bp.route('/admin/delete/<int:id>')
def delete_item(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    item = MenuItem.query.get_or_404(id)
    if item.image_path:
        filepath = os.path.join(Config.UPLOAD_FOLDER_MENU, item.image_path)
        if os.path.exists(filepath):
            try: os.remove(filepath)
            except Exception: pass

    ComboOption.query.filter_by(item1_id=id).update({ComboOption.item1_id: None})
    ComboOption.query.filter_by(item2_id=id).update({ComboOption.item2_id: None})
    ComboOption.query.filter_by(item3_id=id).update({ComboOption.item3_id: None})
    db.session.delete(item)
    db.session.commit()
    update_popular_items()
    return redirect(url_for('admin.admin_dashboard', tab='menu'))

# ==============================================================================
# 套餐組合 CRUD
# ==============================================================================
def check_side_customizable(item_id, is_checked):
    """檢查配餐是否具備客製化群組，無客製化品項一律強制為 False"""
    if not item_id or not is_checked:
        return False
    item = db.session.get(MenuItem, item_id)
    if not item or not item.modifiers or item.modifiers == 'none':
        return False
    return True

@admin_bp.route('/admin/add_combo', methods=['POST'])
def add_combo():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    
    main_item_id = int(request.form.get('main_item_id'))
    name = str(request.form.get('name', '')).strip()
    additional_price = max(0, int(request.form.get('additional_price') or 0))
    description = request.form.get('description', '')
    
    item1_id = int(request.form.get('item1_id')) if request.form.get('item1_id') else None
    item2_id = int(request.form.get('item2_id')) if request.form.get('item2_id') else None
    item3_id = int(request.form.get('item3_id')) if request.form.get('item3_id') else None

    chosen_sides = [s for s in [item1_id, item2_id, item3_id] if s is not None]
    if len(chosen_sides) != len(set(chosen_sides)):
        db.session.rollback()
        return "<script>alert('❌ 配餐不可重複選擇相同的餐點！'); window.location.href='/admin?tab=menu';</script>"

    #同主餐名稱重複防呆
    existing_name = ComboOption.query.filter_by(
        main_item_id=main_item_id,
        name=name
    ).first()
    if existing_name:
        db.session.rollback()
        return f"<script>alert('❌ 該主餐已存在名為【{name}】的套餐，請使用不同名稱！'); window.location.href='/admin?tab=menu';</script>"

    #同主餐配餐內容重複防呆
    target_sides_set = set(chosen_sides)
    existing_combos = ComboOption.query.filter_by(main_item_id=main_item_id).all()
    for ec in existing_combos:
        ec_sides = [s for s in [ec.item1_id, ec.item2_id, ec.item3_id] if s is not None]
        if set(ec_sides) == target_sides_set:
            db.session.rollback()
            return f"<script>alert('❌ 該主餐已存在包含相同配餐內容的套餐【{ec.name}】，不可重複建立相同組合！'); window.location.href='/admin?tab=menu';</script>"

    item1_customizable = check_side_customizable(item1_id, request.form.get('item1_customizable') == '1')
    item2_customizable = check_side_customizable(item2_id, request.form.get('item2_customizable') == '1')
    item3_customizable = check_side_customizable(item3_id, request.form.get('item3_customizable') == '1')
    can_addon = True if request.form.get('can_addon') == '1' else False
    
    new_combo = ComboOption(
        main_item_id=main_item_id,
        name=name,
        additional_price=additional_price,
        description=description,
        item1_id=item1_id,
        item2_id=item2_id,
        item3_id=item3_id,
        item1_customizable=item1_customizable,
        item2_customizable=item2_customizable,
        item3_customizable=item3_customizable,
        can_addon=can_addon
    )
    db.session.add(new_combo)
    db.session.commit()
    db.session.expire_all() 
    return f"<script>alert('✅ 成功為餐點新增套餐組合！'); window.location.href='/admin?tab=menu';</script>"

@admin_bp.route('/admin/edit_combo/<int:id>', methods=['POST'])
def edit_combo(id):
    """編輯現有套餐組合設定"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
        
    combo = ComboOption.query.get_or_404(id)
    main_item_id = int(request.form.get('main_item_id'))
    name = str(request.form.get('name', '')).strip()
    additional_price = max(0, int(request.form.get('additional_price') or 0))
    description = request.form.get('description', '')

    item1_id = int(request.form.get('item1_id')) if request.form.get('item1_id') else None
    item2_id = int(request.form.get('item2_id')) if request.form.get('item2_id') else None
    item3_id = int(request.form.get('item3_id')) if request.form.get('item3_id') else None

    chosen_sides = [s for s in [item1_id, item2_id, item3_id] if s is not None]
    if len(chosen_sides) != len(set(chosen_sides)):
        db.session.rollback()
        return "<script>alert('❌ 配餐不可重複選擇相同的餐點！'); window.location.href='/admin?tab=menu';</script>"

    #同主餐名稱重複防呆 (排除自己)
    duplicate_name = ComboOption.query.filter(
        ComboOption.id != id,
        ComboOption.main_item_id == main_item_id,
        ComboOption.name == name
    ).first()
    if duplicate_name:
        db.session.rollback()
        return f"<script>alert('❌ 該主餐已有其他名為【{name}】的套餐組合！'); window.location.href='/admin?tab=menu';</script>"

    #同主餐配餐內容重複防呆 (排除自己)
    target_sides_set = set(chosen_sides)
    existing_combos = ComboOption.query.filter(
        ComboOption.id != id,
        ComboOption.main_item_id == main_item_id
    ).all()
    for ec in existing_combos:
        ec_sides = [s for s in [ec.item1_id, ec.item2_id, ec.item3_id] if s is not None]
        if set(ec_sides) == target_sides_set:
            db.session.rollback()
            return f"<script>alert('❌ 該主餐已有相同配餐組合的套餐【{ec.name}】，請勿重複配置！'); window.location.href='/admin?tab=menu';</script>"

    combo.main_item_id = main_item_id
    combo.name = name
    combo.additional_price = additional_price
    combo.description = description
    combo.item1_id = item1_id
    combo.item2_id = item2_id
    combo.item3_id = item3_id
    combo.item1_customizable = check_side_customizable(item1_id, request.form.get('item1_customizable') == '1')
    combo.item2_customizable = check_side_customizable(item2_id, request.form.get('item2_customizable') == '1')
    combo.item3_customizable = check_side_customizable(item3_id, request.form.get('item3_customizable') == '1')
    combo.can_addon = True if request.form.get('can_addon') == '1' else False

    db.session.commit()
    db.session.expire_all()
    return redirect(url_for('admin.admin_dashboard', tab='menu'))

@admin_bp.route('/admin/delete_combo/<int:id>')
def delete_combo(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    combo = ComboOption.query.get_or_404(id)
    db.session.delete(combo)
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='menu'))

# ==============================================================================
# 回饋商城上下架與優惠券管理
# ==============================================================================
@admin_bp.route('/admin/add_reward', methods=['POST'])
def add_reward():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    item_id = request.form.get('item_id')
    item = MenuItem.query.get_or_404(item_id)
    item.is_reward = True
    item.reward_points = max(0, int(request.form.get('reward_points') or 0))
    item.reward_discount_points = max(0, int(request.form.get('reward_discount_points') or 0))
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

@admin_bp.route('/admin/remove_reward/<int:id>')
def remove_reward_item(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    item = MenuItem.query.get_or_404(id)
    item.is_reward = False
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

@admin_bp.route('/admin/add_coupon', methods=['POST'])
def add_coupon():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    code = request.form.get('code', '').strip().upper()
    if Coupon.query.filter_by(code=code).first():
        return "<script>alert('❌ 代碼重複！'); window.history.back();</script>", 400
    dtype = request.form.get('discount_type', 'fixed')
    new_c = Coupon(
        title=request.form.get('title'),
        code=code,
        discount_type=dtype,
        discount_value=sanitize_discount_value(dtype, request.form.get('discount_value') or 0),
        min_spend=max(0.0, float(request.form.get('min_spend') or 0)),
        reward_points=max(0, int(request.form.get('reward_points') or 0)),
        reward_discount_points=max(0, int(request.form.get('reward_discount_points') or 0)),
        is_reward=True
    )
    db.session.add(new_c)
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

@admin_bp.route('/admin/delete_coupon/<int:id>')
def delete_coupon(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    c = Coupon.query.get_or_404(id)
    db.session.delete(c)
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

@admin_bp.route('/admin/edit_coupon/<int:id>', methods=['POST'])
def edit_coupon(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    c = Coupon.query.get_or_404(id)
    code = request.form.get('code', '').strip().upper()
    if Coupon.query.filter(Coupon.code == code, Coupon.id != id).first():
        return "<script>alert('❌ 代碼已被使用！'); window.history.back();</script>", 400
    if c.code != code:
        UserCoupon.query.filter_by(coupon_id=c.id).update({UserCoupon.code: code})
    c.title = request.form.get('title')
    c.code = code
    c.discount_type = request.form.get('discount_type', 'fixed')
    c.discount_value = sanitize_discount_value(c.discount_type, request.form.get('discount_value') or 0)
    c.min_spend = max(0.0, float(request.form.get('min_spend') or 0))
    c.reward_points = max(0, int(request.form.get('reward_points') or 0))
    c.reward_discount_points = max(0, int(request.form.get('reward_discount_points') or 0))
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

# ==============================================================================
# 會員資料維護
# ==============================================================================
@admin_bp.route('/admin/delete_user/<int:id>')
def delete_user(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    user = User.query.get_or_404(id)
    if user.photo_path:
        f = os.path.join(Config.UPLOAD_FOLDER_MEMBER, user.photo_path)
        if os.path.exists(f):
            try: os.remove(f)
            except Exception: pass
    Order.query.filter_by(user_id=id).update({Order.user_id: None, Order.table_number: f"{user.name} (已註銷會員)"}, synchronize_session=False)
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='users'))

@admin_bp.route('/admin/edit_user/<int:id>', methods=['POST'])
def edit_user(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    user = User.query.get_or_404(id)
    user.name = request.form.get('name', user.name)
    user.phone = request.form.get('phone', user.phone)
    user.points = int(request.form.get('points') or 0)
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='users'))

@admin_bp.route('/admin/toggle_user_coupon/<int:uc_id>', methods=['POST'])
def toggle_user_coupon(uc_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'message': '未授權'}), 401
    uc = UserCoupon.query.get_or_404(uc_id)
    uc.is_used = not bool(uc.is_used)
    uc.used_at = datetime.now() if uc.is_used else None
    db.session.commit()
    all_user_coupons = UserCoupon.query.filter_by(user_id=uc.user_id).order_by(UserCoupon.id.asc()).all()
    return jsonify({
        'success': True,
        'user_name': uc.user.name if uc.user else '',
        'coupons': [{'id': c.id, 'title': c.coupon.title if c.coupon else '優惠券', 'code': c.code, 'is_used': bool(c.is_used)} for c in all_user_coupons]
    })

@admin_bp.route('/admin/update_reward_setting', methods=['POST'])
def update_reward_setting():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))

    setting = RewardSetting.query.first()
    if not setting:
        setting = RewardSetting()
        db.session.add(setting)

    setting.is_enabled = bool(request.form.get('is_enabled'))
    setting.points_per_dollar = max(1, int(request.form.get('points_per_dollar') or 1))
    setting.max_discount_per_order = max(0, int(request.form.get('max_discount_per_order') or 0))
    setting.spend_per_point = max(1, int(request.form.get('spend_per_point') or 100))

    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='users'))
# ==============================================================================
# 訂單狀態更新
# ==============================================================================
@admin_bp.route('/admin/update_order_status/<int:id>', methods=['POST'])
def update_order_status(id):
    if not session.get('admin_logged_in'):
        return jsonify({'error': '未授權'}), 401
    order = Order.query.get_or_404(id)
    new_status = (request.get_json() or {}).get('status')
    if new_status == 'Cancelled':
        success, msg = cancel_order_and_rollback(order)
        return jsonify({'message': msg, 'status': 'Cancelled'})
    if new_status in ['Pending', 'Completed', 'PickedUp']:
        order.status = new_status
        if new_status == 'Completed' and not order.completed_at:
            order.completed_at = datetime.now()
        db.session.commit()
        _AI_ADVICE_STATE['last_signature'] = None
        order_event_bus.notify()
        return jsonify({'message': '狀態更新成功', 'status': new_status})
    return jsonify({'error': '無效狀態'}), 400

# ==============================================================================
# 系統備份與還原
# ==============================================================================
@admin_bp.route('/admin/backup_system')
def backup_system():
    if not session.get('admin_logged_in'):
        return "<script>alert('❌ 權限不足，拒絕存取！'); window.location.href='/admin';</script>", 403
    try:
        db.session.execute(db.text("PRAGMA wal_checkpoint(TRUNCATE);"))
        db.session.commit()

        memory_file = io.BytesIO()
        db_path = os.path.join(Config.DATABASE_DIR, 'menu.db')
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            if os.path.exists(db_path):
                zf.write(db_path, arcname='menu.db')
            if os.path.exists(Config.UPLOAD_FOLDER_MEMBER):
                for root, _, files in os.walk(Config.UPLOAD_FOLDER_MEMBER):
                    for file in files:
                        zf.write(os.path.join(root, file), arcname=os.path.join('static', 'member', file).replace('\\', '/'))
            if os.path.exists(Config.UPLOAD_FOLDER_MENU):
                for root, _, files in os.walk(Config.UPLOAD_FOLDER_MENU):
                    for file in files:
                        zf.write(os.path.join(root, file), arcname=os.path.join('static', 'menu', file).replace('\\', '/'))

        memory_file.seek(0)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(memory_file, download_name=f"Kiosk_Backup_{timestamp}.zip", as_attachment=True, mimetype='application/zip')
    except Exception as e:
        return f"<script>alert('❌ 備份失敗：{e}'); window.history.back();</script>", 500


@admin_bp.route('/admin/restore_backup', methods=['POST'])
def restore_backup():
    # 1. 權限檢驗
    if not session.get('admin_logged_in'):
        return "<script>alert('❌ 權限不足：未授權操作！'); window.location.href='/admin';</script>", 403

    file = request.files.get('backup_zip')
    if not file or file.filename == '':
        return "<script>alert('❌ 請選擇備份 ZIP 檔案！'); window.history.back();</script>", 400

    if not file.filename.lower().endswith('.zip'):
        return "<script>alert('❌ 安全審查失敗：檔案類型不合法，僅支援 .zip 壓縮檔！'); window.history.back();</script>", 403

    restore_db = (request.form.get('restore_db') == '1')
    restore_photos = (request.form.get('restore_photos') == '1')
    restore_menu_photos = (request.form.get('restore_menu_photos') == '1')
    overwrite_photos = (request.form.get('overwrite_photos') == '1')

    if not restore_db and not restore_photos and not restore_menu_photos:
        return "<script>alert('❌ 請至少勾選一項欲還原的項目！'); window.history.back();</script>", 400

    db_path = os.path.abspath(os.path.join(Config.DATABASE_DIR, 'menu.db'))
    member_dir = os.path.abspath(Config.UPLOAD_FOLDER_MEMBER)
    menu_dir = os.path.abspath(Config.UPLOAD_FOLDER_MENU)

    allowed_img_exts = {'.jpg', '.jpeg', '.png', '.webp'}
    dangerous_exts = {
        '.exe', '.bat', '.cmd', '.sh', '.py', '.php', '.phtml', 
        '.pl', '.cgi', '.jsp', '.asp', '.aspx', '.js', '.vbs', 
        '.jar', '.scr', '.dll', '.so', '.com', '.msi', '.ps1'
    }

    try:
        with zipfile.ZipFile(file, 'r') as zf:
            infolist = zf.infolist()
            if not infolist:
                return "<script>alert('❌ 審查失敗：壓縮檔為空！'); window.history.back();</script>", 400

            total_uncompressed_size = 0
            has_valid_backup_structure = False

            # 2. 第一階段：靜態安全掃描 (Zip Bomb、Symlink、危險副檔名)
            for info in infolist:
                # 單檔上限檢查
                if info.file_size > Config.MAX_SINGLE_FILE_SIZE:
                    return f"<script>alert('❌ 安全審查失敗：檔案 ({info.filename}) 超出單檔 15MB 限制！'); window.history.back();</script>", 403

                total_uncompressed_size += info.file_size
                if total_uncompressed_size > Config.MAX_TOTAL_EXTRACT_SIZE:
                    return "<script>alert('❌ 安全審查失敗：解壓總容量超過 150MB 上限，疑似 Zip Bomb！'); window.history.back();</script>", 403

                # 符號連結檢查 (防止 Symlink 任意讀取)
                if (info.external_attr >> 16) & 0o120000 == 0o120000:
                    return "<script>alert('❌ 安全審查失敗：檢測到不安全的符號連結 (Symlink)！'); window.history.back();</script>", 403

                norm_name = info.filename.replace('\\', '/')
                base_name = os.path.basename(norm_name)
                _, ext = os.path.splitext(base_name.lower())

                # 危險檔案黑名單阻斷
                if ext in dangerous_exts or base_name.startswith('.'):
                    return f"<script>alert('❌ 安全審查阻斷：壓縮檔內包含非法危險檔案 ({base_name})！'); window.history.back();</script>", 403

                # 判定是否存在合法備份特徵路徑
                if base_name == 'menu.db':
                    has_valid_backup_structure = True
                parts = [p.lower() for p in norm_name.split('/')[:-1]]
                if ('member' in parts or 'menu' in parts) and ext in allowed_img_exts:
                    has_valid_backup_structure = True

            # 若完全沒有符合備份規格的檔案，直接阻斷 (防止隨意上傳無關 ZIP)
            if not has_valid_backup_structure:
                return "<script>alert('❌ 檔案審查失敗：此壓縮檔非本系統之合法備份包 (查無 menu.db 或相片目錄)！'); window.history.back();</script>", 403

            namelist = zf.namelist()
            restored_db_status = False
            restored_photo_count = 0
            restored_menu_photo_count = 0

            # 3. 第二階段：資料庫還原與 Magic Bytes 深度審查
            if restore_db:
                db_entry = next((n for n in namelist if os.path.basename(n.replace('\\', '/')) == 'menu.db'), None)
                if not db_entry:
                    return "<script>alert('❌ 審查失敗：您勾選了還原資料庫，但壓縮檔內查無 menu.db！'); window.history.back();</script>", 403

                db_bytes = zf.read(db_entry)
                # 嚴格驗證 SQLite 檔案標頭 (Header Magic Bytes)
                if not db_bytes.startswith(b'SQLite format 3\x00'):
                    return "<script>alert('❌ 格式審查失敗：menu.db 標頭損壞或非合法 SQLite3 資料庫，拒絕寫入！'); window.history.back();</script>", 403

                # 關閉目前連線再進行實體替換
                db.session.remove()
                db.engine.dispose()
                with open(db_path, 'wb') as f:
                    f.write(db_bytes)
                restored_db_status = True

            # 4. 第三階段：圖片解壓縮與 Zip Slip 防護
            for entry in namelist:
                norm_entry = entry.replace('\\', '/')
                if norm_entry.endswith('/'): 
                    continue

                raw_filename = os.path.basename(norm_entry)
                safe_name = secure_filename(raw_filename)
                if not safe_name:
                    continue

                _, ext = os.path.splitext(safe_name.lower())
                if ext not in allowed_img_exts:
                    continue

                parts = [p.lower() for p in norm_entry.split('/')[:-1]]
                target_dir = None

                if restore_photos and ('member' in parts):
                    target_dir = member_dir
                elif restore_menu_photos and ('menu' in parts):
                    target_dir = menu_dir

                if target_dir:
                    dest_path = os.path.abspath(os.path.join(target_dir, safe_name))
                    
                    # 嚴格 Zip Slip 路徑越界檢查
                    if os.path.commonpath([target_dir, dest_path]) != target_dir:
                        return "<script>alert('❌ 安全審查阻斷：檢測到非法路徑遍歷攻擊 (Zip Slip)！'); window.history.back();</script>", 403

                    if not os.path.exists(dest_path) or overwrite_photos:
                        with open(dest_path, 'wb') as f_out:
                            f_out.write(zf.read(entry))
                        if target_dir == member_dir:
                            restored_photo_count += 1
                        else:
                            restored_menu_photo_count += 1

            # 5. 檢驗實際成果
            total_items = (1 if restored_db_status else 0) + restored_photo_count + restored_menu_photo_count
            if total_items == 0:
                return "<script>alert('❌ 還原失敗：未自壓縮檔中還原任何有效項目！'); window.history.back();</script>", 400

        msg = f"✅ 系統備份還原完成！\\n" \
              f"➤ 資料庫：{'成功更新' if restored_db_status else '未勾選/未變更'}\\n" \
              f"➤ 會員相片：寫入 {restored_photo_count} 張\\n" \
              f"➤ 餐點相片：寫入 {restored_menu_photo_count} 張"
        return f"<script>alert('{msg}'); window.location.href='/admin';</script>", 200

    except zipfile.BadZipFile:
        return "<script>alert('❌ 檔案審查失敗：檔案已損壞或非合法 ZIP 壓縮檔！'); window.history.back();</script>", 403
    except Exception as e:
        return f"<script>alert('❌ 還原過程發生錯誤：{e}'); window.history.back();</script>", 500

# ==============================================================================
# Excel 匯出 (包含全部 8 個工作表與格式自適應)
# ==============================================================================
@admin_bp.route('/admin/export_excel', methods=['POST'])
def export_excel():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
    selected_tables = request.form.getlist('tables')
    if not selected_tables:
        return "<script>alert('❌ 請至少勾選一個資料表！'); window.history.back();</script>", 400

    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')
    start_date = datetime.strptime(start_date_str, '%Y-%m-%d') if start_date_str else None
    end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1, seconds=-1) if end_date_str else None

    orders_query = Order.query
    if start_date: orders_query = orders_query.filter(Order.created_at >= start_date)
    if end_date: orders_query = orders_query.filter(Order.created_at <= end_date)
    filtered_orders = orders_query.all()
    filtered_order_ids = [o.id for o in filtered_orders]

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        if 'User' in selected_tables:
            user_data = []
            for u in User.query.all():
                login_str = u.last_login_at.strftime('%Y-%m-%d %H:%M:%S') if u.last_login_at else '尚未記錄'
                if not u.last_login_at:
                    logout_str = '尚未記錄'
                elif not u.last_logout_at or u.last_logout_at < u.last_login_at:
                    logout_str = '尚未登出'
                else:
                    logout_str = u.last_logout_at.strftime('%Y-%m-%d %H:%M:%S')
                user_data.append({
                    'ID': u.id, '姓名(Name)': u.name, '電話(Phone)': u.phone, '紅利點數(Points)': u.points,
                    '最後登入時間(Last Login)': login_str, '最後登出時間(Last Logout)': logout_str
                })
            pd.DataFrame(user_data if user_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='會員資料(User)', index=False)

        if 'MenuItem' in selected_tables:
            menu_data = [{
                'ID': i.id, '餐點名稱(Name)': i.name, '分類(Category)': i.category, '單價(Price)': i.price,
                '客製化群組(Modifiers)': i.modifiers or 'none', '商品描述(Description)': i.description or '',
                '是否特價(Is Discount)': '是' if i.is_discount else '否', '特價金額(Discount Price)': i.discount_price,
                '熱門推薦(Recommended)': '是' if (i.is_manual_popular or i.is_recommended) else '否',
                '新品上市(Is New)': '是' if i.is_new else '否', '開放紅利兌換(Is Reward)': '是' if i.is_reward else '否',
                '兌換所需點數(Reward Points)': i.reward_points, '限時特惠點數(Reward Discount Points)': i.reward_discount_points
            } for i in MenuItem.query.all()]
            pd.DataFrame(menu_data if menu_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='菜單品項(MenuItem)', index=False)

        if 'Coupon' in selected_tables:
            coupon_data = [{
                'ID': c.id, '代碼(Code)': c.code, '標題(Title)': c.title, '折抵類型(Discount Type)': c.discount_type,
                '折抵值(Discount Value)': c.discount_value, '門檻(Min Spend)': c.min_spend,
                '兌換所需點數(Reward Points)': c.reward_points, '限時特惠點數(Reward Discount Points)': c.reward_discount_points,
                '上架回饋商城(Is Reward)': '是' if c.is_reward else '否'
            } for c in Coupon.query.all()]
            pd.DataFrame(coupon_data if coupon_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='優惠券(Coupon)', index=False)

        if 'UserCoupon' in selected_tables:
            user_coupons_data = [{
                'ID': uc.id, '關聯會員ID': uc.user_id, '優惠代碼(Code)': uc.code, 
                '是否已使用(Is Used)': '是' if uc.is_used else '否',
                '領取時間': uc.created_at.strftime('%Y-%m-%d %H:%M') if uc.created_at else ''
            } for uc in UserCoupon.query.all()]
            pd.DataFrame(user_coupons_data if user_coupons_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='會員持券(UserCoupon)', index=False)

        if 'Order' in selected_tables:
            order_data = []
            for o in filtered_orders:
                log = db.session.get(CustomerLog, o.customer_log_id) if getattr(o, 'customer_log_id', None) else None
                cust_identifier = o.user_id if o.user_id else (o.table_number if o.table_number else f"訪客-{o.pickup_number:03d}")
                login_dt = log.login_at if (log and log.login_at) else (o.user.last_login_at if (o.user and o.user.last_login_at) else None)

                if login_dt:
                    login_str = login_dt.strftime('%Y-%m-%d %H:%M:%S')
                    sec = int((o.created_at - login_dt).total_seconds()) if o.created_at else 0
                    duration_str = f"{sec // 60}分{sec % 60}秒" if sec > 0 else "即時下單"
                else:
                    login_str = "未記錄"
                    duration_str = "無進場紀錄"

                order_data.append({
                    'ID': o.id, '取餐編號(Pickup No)': f"#{o.pickup_number:03d}" if o.pickup_number else f"#{o.id}",
                    '身分/會員ID(Customer ID)': cust_identifier, '進場時間(Login At)': login_str, '點餐耗時(Duration)': duration_str,
                    '下單時間(Order Created)': o.created_at.strftime('%Y-%m-%d %H:%M:%S') if o.created_at else '',
                    '出餐時間(Completed At)': o.completed_at.strftime('%Y-%m-%d %H:%M:%S') if o.completed_at else '未出餐',
                    '總金額(Total)': o.total_price, '付款方式(Payment)': o.payment_method, '狀態(Status)': o.status, '使用紅利': o.points_used
                })
            pd.DataFrame(order_data if order_data else [{'資料': '目前無資料'}]).to_excel(writer, sheet_name='訂單總覽(Order)', index=False, startrow=1)

        if 'OrderItem' in selected_tables:
            order_items = OrderItem.query.filter(OrderItem.order_id.in_(filtered_order_ids)).all() if filtered_order_ids else []
            data = [{
                'ID': oi.id, '關聯訂單ID(Order ID)': oi.order_id, '品名(Item Name)': oi.item_name,
                '單價(Price)': oi.price, '數量(Qty)': oi.quantity, '客製化(Customization)': oi.customization
            } for oi in order_items]
            pd.DataFrame(data if data else [{'資料': '目前無明細'}]).to_excel(writer, sheet_name='訂單明細(OrderItem)', index=False, startrow=1)

        if 'Analytics' in selected_tables:
            valid_orders = [o for o in filtered_orders if o.status != 'Cancelled']
            tot_revenue = sum(o.total_price or 0 for o in valid_orders)
            total_points = sum(o.points_used or 0 for o in valid_orders)
            analytics_data = [
                {'指標 (Metric)': '區間總訂單數', '數值 (Value)': len(filtered_orders)},
                {'指標 (Metric)': '有效訂單數', '數值 (Value)': len(valid_orders)},
                {'指標 (Metric)': '總營收', '數值 (Value)': tot_revenue},
                {'指標 (Metric)': '總折扣折抵', '數值 (Value)': sum(o.discount_amount or 0 for o in valid_orders)},
                {'指標 (Metric)': '紅利使用總額 (Points Used)', '數值 (Value)': total_points},
                {'指標 (Metric)': '平均客單價', '數值 (Value)': round(tot_revenue / len(valid_orders), 2) if valid_orders else 0}
            ]
            pd.DataFrame(analytics_data).to_excel(writer, sheet_name='區間營運總覽(Analytics)', index=False, startrow=1)

            # 區間熱銷排行 (依銷售數量排序，排除紅利免費兌換品項)
            item_stats = {}
            for o in valid_orders:
                for item in o.items:
                    if '[點數兌換]' in item.item_name or item.item_name.startswith('🎁') or item.customization == '紅利免費兌換':
                        continue
                    if item.item_name not in item_stats:
                        item_stats[item.item_name] = {'qty': 0, 'rev': 0}
                    item_stats[item.item_name]['qty'] += (item.quantity or 1)
                    item_stats[item.item_name]['rev'] += ((item.price or 0) * (item.quantity or 1))

            sales_data = [
                {'餐點名稱 (Item Name)': name, '銷售數量 (Qty)': stats['qty'], '創造營收 (Revenue)': stats['rev']}
                for name, stats in sorted(item_stats.items(), key=lambda x: x[1]['qty'], reverse=True)
            ]
            pd.DataFrame(sales_data if sales_data else [{'資料': '所選日期範圍內無銷售資料'}]).to_excel(writer, sheet_name='區間熱銷排行(ItemSales)', index=False, startrow=1)

        if 'AdminLog' in selected_tables:
            logs_query = AdminLog.query
            if start_date: logs_query = logs_query.filter(AdminLog.login_at >= start_date)
            if end_date: logs_query = logs_query.filter(AdminLog.login_at <= end_date)
            log_data = [{
                'ID': lg.id, '管理員帳號': lg.username,
                '登入時間': lg.login_at.strftime('%Y-%m-%d %H:%M:%S') if lg.login_at else '',
                '登出時間': lg.logout_at.strftime('%Y-%m-%d %H:%M:%S') if lg.logout_at else '尚未登出',
                'IP 位址': lg.ip_address or '本機'
            } for lg in logs_query.order_by(AdminLog.id.desc()).all()]
            pd.DataFrame(log_data if log_data else [{'資料': '目前無紀錄'}]).to_excel(writer, sheet_name='管理員進出日誌(AdminLog)', index=False, startrow=1)

        # 日期區間標題與欄寬自適應格式化 (中文全形字元加權計算，避免欄位過窄)
        display_start = start_date_str if start_date_str else '全部區間 (All)'
        display_end = end_date_str if end_date_str else '全部區間 (All)'
        date_range_text = f"報表資料區間：{display_start} ~ {display_end}"

        date_dependent_sheets = {
            '訂單總覽(Order)', '訂單明細(OrderItem)',
            '區間營運總覽(Analytics)', '區間熱銷排行(ItemSales)',
            '管理員進出日誌(AdminLog)'
        }

        def calculate_display_width(val):
            if val is None:
                return 0
            text = str(val)
            width = 0.0
            for char in text:
                status = unicodedata.east_asian_width(char)
                if status in ('F', 'W') or ord(char) >= 0x2600 or ord(char) >= 0x1F000:
                    width += 2.2
                else:
                    width += 1.1
            return int(width)

        for sheet_name, ws in writer.sheets.items():
            if sheet_name in date_dependent_sheets:
                ws.cell(row=1, column=1, value=date_range_text).font = Font(bold=True, color="0055aa")

            for col in ws.columns:
                max_len = 0
                column_letter = col[0].column_letter
                for cell in col:
                    if sheet_name in date_dependent_sheets and cell.row == 1 and cell.column == 1:
                        continue
                    cell_len = calculate_display_width(cell.value)
                    if cell_len > max_len:
                        max_len = cell_len
                ws.column_dimensions[column_letter].width = max(max_len + 4, 12)

    output.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    date_suffix = f"_{start_date_str}_to_{end_date_str}" if start_date_str and end_date_str else ""
    filename = f"Kiosk_Export_{timestamp}{date_suffix}.xlsx"
    return send_file(output, download_name=filename, as_attachment=True, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')

# ==============================================================================
# Excel 匯入 (菜單匯入與多工作表智慧匯入)
# ==============================================================================
@admin_bp.route('/admin/import_excel', methods=['POST'])
def import_menu_excel():
    """Excel 批量匯入菜單 (支援自身匯出的檔案與自訂格式)"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
        
    file = request.files.get('file')
    if not file or file.filename == '':
        return "<script>alert('請選擇 Excel/CSV 檔案！'); window.history.back();</script>", 400

    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(file)
        else:
            excel_file = pd.ExcelFile(file)
            target_sheet = None
            for sheet in excel_file.sheet_names:
                if '菜單' in sheet or 'MenuItem' in sheet or 'menu' in sheet.lower():
                    target_sheet = sheet
                    break
            df = pd.read_excel(excel_file, sheet_name=target_sheet if target_sheet else 0)

        column_mapping = {
            '餐點名稱(Name)': 'name', '名稱(Name)': 'name', '餐點名稱': 'name', '名稱': 'name', 'name': 'name',
            '分類(Category)': 'category', '分類': 'category', 'category': 'category',
            '單價(Price)': 'price', '單價': 'price', '原單價': 'price', 'price': 'price',
            '客製化類型(Modifiers)': 'modifiers', '客製化群組(Modifiers)': 'modifiers', '客製化類型': 'modifiers', '客製化': 'modifiers', 'modifiers': 'modifiers',
            '商品描述(Description)': 'description', '簡介': 'description', '描述': 'description', 'description': 'description',
            '熱門推薦(Recommended)': 'is_recommended', '熱門(Popular)': 'is_recommended', '熱門推薦': 'is_recommended', 'is_recommended': 'is_recommended',
            '新品上市(Is New)': 'is_new', '新品': 'is_new', 'is_new': 'is_new',
            '是否特價(Is Discount)': 'is_discount', '是否特價': 'is_discount', '特價': 'is_discount', 'is_discount': 'is_discount',
            '特價金額(Discount Price)': 'discount_price', '特價金額': 'discount_price', 'discount_price': 'discount_price',
            '開放紅利兌換(Is Reward)': 'is_reward', '是否開放紅利兌換': 'is_reward', 'is_reward': 'is_reward',
            '兌換所需點數(Reward Points)': 'reward_points', '兌換點數': 'reward_points', 'reward_points': 'reward_points',
            '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '特惠點數': 'reward_discount_points', 'reward_discount_points': 'reward_discount_points'
        }
        df.rename(columns=column_mapping, inplace=True)

        #必要欄位防呆
        if 'name' not in df.columns or 'price' not in df.columns:
            return "<script>alert('檔案缺少必要欄位：「餐點名稱」或「單價」！'); window.history.back();</script>", 400

        #安全的布林轉換
        def parse_bool(val):
            if pd.isna(val):
                return False
            s = str(val).strip().lower()
            return s in ['1', 'true', '是', 'yes', 'y']

        for _, row in df.iterrows():
            name = str(row.get('name', '')).strip()
            if not name or pd.isna(row.get('name')) or name == '目前無資料':
                continue

            category = str(row.get('category', '主餐')).strip() if not pd.isna(row.get('category')) else '主餐'
            
            try: price = max(0, round(float(row.get('price', 0))))
            except (ValueError, TypeError): price = 0

            try: discount_price = max(0, round(float(row.get('discount_price', 0))))
            except (ValueError, TypeError): discount_price = 0

            try: reward_points = max(0, int(row.get('reward_points', 0)))
            except (ValueError, TypeError): reward_points = 0

            try: reward_discount_points = max(0, int(row.get('reward_discount_points', 0)))
            except (ValueError, TypeError): reward_discount_points = 0

            modifiers = str(row.get('modifiers', 'none')).strip() if not pd.isna(row.get('modifiers')) else 'none'
            if modifiers not in ['none', 'ice_sugar', 'spicy', 'addons']:
                modifiers = 'none'

            description = str(row.get('description', '')).strip() if not pd.isna(row.get('description')) else ''
            
            is_recommended = parse_bool(row.get('is_recommended'))
            is_new = parse_bool(row.get('is_new'))
            is_discount = parse_bool(row.get('is_discount'))
            is_reward = parse_bool(row.get('is_reward'))

            new_item = MenuItem(
                name=name,
                category=category,
                price=price,
                modifiers=modifiers,
                description=description,
                image_path='',
                is_recommended=False,
                is_manual_popular=is_recommended,
                is_discount=is_discount, 
                discount_price=discount_price,
                is_new=is_new,
                is_reward=is_reward,
                reward_points=reward_points,
                reward_discount_points=reward_discount_points
            )
            db.session.add(new_item)

        db.session.commit()
        update_popular_items()
        return redirect(url_for('admin.admin_dashboard', tab='menu'))

    except Exception as e:
        db.session.rollback()
        return f"<script>alert('匯入解析失敗：{e}'); window.history.back();</script>", 500

@admin_bp.route('/admin/import_smart', methods=['POST'])
def import_smart():
    """智慧匯入功能：自動辨識 Excel 中的工作表與純英文/中文欄位，並匯入/更新對應資料表"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))

    file = request.files.get('file')
    if not file or file.filename == '':
        return "<script>alert('❌ 請選擇 Excel 檔案！'); window.history.back();</script>", 400

    try:
        excel_file = pd.ExcelFile(file)
        imported_counts = {'User': 0, 'MenuItem': 0, 'Coupon': 0}

        #安全的布林轉換與手機號碼清洗
        def parse_bool(val):
            if pd.isna(val): return False
            s = str(val).strip().lower()
            return s in ['1', 'true', '是', 'yes', 'y']

        def clean_phone_number(val):
            """清理與格式化手機號碼，避免 Excel 浮點數轉換問題"""
            if pd.isna(val): return ""
            s = str(val).strip()
            if s.endswith('.0'):
                s = s[:-2]
            if len(s) == 9 and s.startswith('9'):
                s = '0' + s
            return s

        for sheet in excel_file.sheet_names:
            df = pd.read_excel(excel_file, sheet_name=sheet)
            if df.empty: continue

            cols_lower = [str(c).lower() for c in df.columns]

            # 1. 辨識是否為「會員資料 (User)」
            if 'User' in sheet or '會員' in sheet or '電話(Phone)' in df.columns or 'phone' in cols_lower or '電話' in df.columns:
                col_map = {
                    '姓名(Name)': 'name', '姓名': 'name', 'name': 'name',
                    '電話(Phone)': 'phone', '電話': 'phone', 'phone': 'phone',
                    '紅利點數(Points)': 'points', '紅利點數': 'points', 'points': 'points'
                }
                df_user = df.rename(columns=col_map)
                
                if 'name' in df_user.columns and 'phone' in df_user.columns:
                    for _, row in df_user.iterrows():
                        name = str(row.get('name', '')).strip()
                        phone = clean_phone_number(row.get('phone', ''))
                        if not name or not phone or pd.isna(row.get('name')) or name == '目前無資料': 
                            continue
                        
                        try: points = int(row.get('points', 0))
                        except: points = 0
                        
                        existing = User.query.filter_by(phone=phone).first()
                        if existing:
                            existing.name = name
                            existing.points = points
                        else:
                            new_user = User(
                                name=name, 
                                phone=phone, 
                                photo_path='', 
                                feature=None, 
                                points=points if points > 0 else 20, 
                                last_login_at=None
                            )
                            db.session.add(new_user)
                        imported_counts['User'] += 1

            # 2. 辨識是否為「菜單品項 (MenuItem)」
            elif 'MenuItem' in sheet or '菜單' in sheet or '單價(Price)' in df.columns or 'price' in cols_lower or '單價' in df.columns:
                col_map = {
                    '餐點名稱(Name)': 'name', '名稱(Name)': 'name', '餐點名稱': 'name', 'name': 'name',
                    '分類(Category)': 'category', '分類': 'category', 'category': 'category',
                    '單價(Price)': 'price', '單價': 'price', 'price': 'price',
                    '剩餘數量(Stock)': 'stock', '剩餘數量': 'stock', '庫存': 'stock', 'stock': 'stock',
                    '總量(Total Stock)': 'total_stock', '總量': 'total_stock', 'total_stock': 'total_stock',
                    '客製化群組(Modifiers)': 'modifiers', '客製化類型': 'modifiers', 'modifiers': 'modifiers',
                    '商品描述(Description)': 'description', '簡介': 'description', 'description': 'description',
                    '熱門推薦(Recommended)': 'is_recommended', '熱門(Popular)': 'is_recommended', 'is_recommended': 'is_recommended',
                    '新品上市(Is New)': 'is_new', '新品': 'is_new', 'is_new': 'is_new',
                    '是否特價(Is Discount)': 'is_discount', '是否特價': 'is_discount', 'is_discount': 'is_discount',
                    '特價金額(Discount Price)': 'discount_price', '特價金額': 'discount_price', 'discount_price': 'discount_price',
                    '開放紅利兌換(Is Reward)': 'is_reward', '是否開放紅利兌換': 'is_reward', 'is_reward': 'is_reward',
                    '兌換所需點數(Reward Points)': 'reward_points', '兌換點數': 'reward_points', 'reward_points': 'reward_points',
                    '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '特惠點數': 'reward_discount_points', 'reward_discount_points': 'reward_discount_points'
                }
                df_menu = df.rename(columns=col_map)
                
                if 'name' in df_menu.columns and 'price' in df_menu.columns:
                    for _, row in df_menu.iterrows():
                        name = str(row.get('name', '')).strip()
                        if not name or pd.isna(row.get('name')) or name == '目前無資料': 
                            continue
                        
                        category = str(row.get('category', '主餐')).strip() if not pd.isna(row.get('category')) else '主餐'
                        try: price = max(0, round(float(row.get('price', 0))))
                        except: price = 0
                        try: discount_price = max(0, round(float(row.get('discount_price', 0))))
                        except: discount_price = 0
                        try: reward_points = max(0, int(row.get('reward_points', 0)))
                        except: reward_points = 0
                        try: reward_discount_points = max(0, int(row.get('reward_discount_points', 0)))
                        except: reward_discount_points = 0
                        try: stock = max(0, int(row.get('stock', 50)))
                        except: stock = 50
                        try: total_stock = max(stock, int(row.get('total_stock', 50)))
                        except: total_stock = 50

                        modifiers = str(row.get('modifiers', 'none')).strip() if not pd.isna(row.get('modifiers')) else 'none'
                        if modifiers not in ['none', 'ice_sugar', 'spicy', 'addons']: 
                            modifiers = 'none'

                        description = str(row.get('description', '')).strip() if not pd.isna(row.get('description')) else ''
                        
                        is_rec = parse_bool(row.get('is_recommended'))
                        is_new = parse_bool(row.get('is_new'))
                        is_disc = parse_bool(row.get('is_discount'))
                        is_rew = parse_bool(row.get('is_reward'))

                        existing = MenuItem.query.filter_by(name=name).first()
                        if existing:
                            #完整同步所有欄位，非僅同步價格與分類
                            existing.category = category
                            existing.price = price
                            existing.discount_price = discount_price
                            existing.reward_points = reward_points
                            existing.reward_discount_points = reward_discount_points
                            existing.modifiers = modifiers
                            existing.description = description
                            existing.is_manual_popular = is_rec
                            existing.is_new = is_new
                            existing.is_discount = is_disc
                            existing.is_reward = is_rew
                            existing.stock = stock
                            existing.total_stock = total_stock
                            existing.is_sold_out = (stock <= 0)
                        else:
                            new_item = MenuItem(
                                name=name, 
                                category=category, 
                                price=price, 
                                modifiers=modifiers, 
                                description=description,
                                image_path='', 
                                is_recommended=False, 
                                is_manual_popular=is_rec,
                                is_discount=is_disc, 
                                discount_price=discount_price, 
                                is_new=is_new,
                                is_reward=is_rew, 
                                reward_points=reward_points, 
                                reward_discount_points=reward_discount_points,
                                stock=stock,
                                total_stock=total_stock,
                                is_sold_out=(stock <= 0),
                                can_be_add_on=False,
                                add_on_price=0,
                                addon_trigger_type='any',
                                addon_trigger_target=''
                            )
                            db.session.add(new_item)
                        imported_counts['MenuItem'] += 1

            # 3. 辨識是否為「優惠券 (Coupon)」
            elif 'Coupon' in sheet or '優惠券' in sheet or '代碼(Code)' in df.columns or 'code' in cols_lower or '代碼' in df.columns:
                col_map = {
                    '代碼(Code)': 'code', '代碼': 'code', 'code': 'code', '優惠代碼': 'code',
                    '標題(Title)': 'title', '標題': 'title', 'title': 'title', '優惠券名稱': 'title', '名稱': 'title',
                    '折抵類型(Discount Type)': 'discount_type', '折抵類型': 'discount_type', '折抵方式': 'discount_type', 'discount_type': 'discount_type',
                    '折抵值(Discount Value)': 'discount_value', '折抵值': 'discount_value', '折抵金額': 'discount_value', 'discount_value': 'discount_value',
                    '門檻(Min Spend)': 'min_spend', '門檻': 'min_spend', '最低消費門檻': 'min_spend', '使用門檻': 'min_spend', 'min_spend': 'min_spend',
                    '兌換所需點數(Reward Points)': 'reward_points', '兌換所需點數': 'reward_points', '兌換點數': 'reward_points', '點數': 'reward_points', 'reward_points': 'reward_points',
                    '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '限時特惠點數': 'reward_discount_points', '特惠點數': 'reward_discount_points', 'reward_discount_points': 'reward_discount_points',
                    '上架回饋商城(Is Reward)': 'is_reward', '是否上架': 'is_reward', '上架商城': 'is_reward', 'is_reward': 'is_reward'
                }
                df_coupon = df.rename(columns=col_map)
                
                if 'code' in df_coupon.columns and 'title' in df_coupon.columns:
                    for _, row in df_coupon.iterrows():
                        code = str(row.get('code', '')).strip().upper()
                        title = str(row.get('title', '')).strip()
                        if not code or not title or pd.isna(row.get('code')) or code == '目前無資料': 
                            continue
                        
                        dtype = str(row.get('discount_type', 'fixed')).strip()
                        if dtype not in ['fixed', 'percent']: 
                            dtype = 'fixed'
                        raw_dval = row.get('discount_value', 0)
                        dvalue = sanitize_discount_value(dtype, raw_dval)
                        try: min_sp = max(0.0, float(row.get('min_spend', 0)))
                        except: min_sp = 0.0
                        try: reward_points = max(0, int(row.get('reward_points', 0)))
                        except: reward_points = 0
                        try: reward_discount_points = max(0, int(row.get('reward_discount_points', 0)))
                        except: reward_discount_points = 0
                        
                        if 'is_reward' in df_coupon.columns and not pd.isna(row.get('is_reward')):
                            is_reward = parse_bool(row.get('is_reward'))
                        else:
                            is_reward = True if reward_points > 0 else True

                        existing = Coupon.query.filter_by(code=code).first()
                        if existing:
                            #完整同步優惠券欄位
                            existing.title = title
                            existing.discount_type = dtype
                            existing.discount_value = dvalue
                            existing.min_spend = min_sp
                            existing.reward_points = reward_points
                            existing.reward_discount_points = reward_discount_points
                            existing.is_reward = is_reward
                        else:
                            new_coupon = Coupon(
                                code=code,
                                title=title,
                                discount_type=dtype,
                                discount_value=dvalue,
                                min_spend=min_sp,
                                reward_points=reward_points,
                                reward_discount_points=reward_discount_points,
                                is_reward=is_reward
                            )
                            db.session.add(new_coupon)
                        imported_counts['Coupon'] += 1

        db.session.commit()
        update_popular_items()
        msg = f"✅ 智慧匯入完成！\\n" \
              f"共處理更新與新增：\\n" \
              f"➤ 會員 (User): {imported_counts['User']} 筆\\n" \
              f"➤ 菜單 (MenuItem): {imported_counts['MenuItem']} 筆\\n" \
              f"➤ 優惠券 (Coupon): {imported_counts['Coupon']} 筆"
        
        return f"<script>alert('{msg}'); window.location.href='/admin';</script>"

    except Exception as e:
        db.session.rollback()
        return f"<script>alert('❌ 檔案解析失敗，請確認檔案格式是否正確！\\n錯誤訊息: {e}'); window.history.back();</script>", 500

# ==============================================================================
# 前端即時輪詢與 AI 文案生成 API (絕對路徑保持 /api/...)
# ==============================================================================
@admin_bp.route('/api/admin_live_orders')
def api_admin_live_orders():
    db.session.expire_all()
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    today_orders = Order.query.filter(Order.created_at >= today_start, Order.created_at <= today_end).order_by(Order.id.desc()).all()

    orders_data = [{
        'id': o.id, 'pickup_number': o.pickup_number if getattr(o, 'pickup_number', None) else o.id,
        'user_name': o.user.name if o.user else '非會員', 'order_type': o.order_type,
        'payment_method': o.payment_method, 'need_cutlery': bool(o.need_cutlery) if getattr(o, 'need_cutlery', None) is not None else True,
        'note': o.note or '', 'created_at': o.created_at.strftime('%m-%d %H:%M') if o.created_at else '',
        'status': o.status, 'discount_amount': o.discount_amount or 0, 'total_price': o.total_price,
        'items': [{'item_name': i.item_name, 'quantity': i.quantity, 'price': i.price, 'customization': i.customization or ''} for i in o.items]
    } for o in today_orders]

    menu_items_data = [{
        'id': item.id, 'stock': item.stock if item.stock is not None else 0,
        'total_stock': item.total_stock if item.total_stock is not None else 50,
        'is_sold_out': bool(item.is_sold_out) or ((item.stock or 0) <= 0)
    } for item in MenuItem.query.all()]

    analytics = build_order_analytics(Order.query.order_by(Order.id.desc()).all(), limit=3)
    analytics['ai_advice'] = generate_ai_business_advice(analytics)

    return jsonify({'success': True, 'orders': orders_data, 'menu_items': menu_items_data, 'analytics': analytics})

@admin_bp.route('/api/generate_item_description', methods=['POST'])
def api_generate_item_description():
    """AI 智慧菜單文案生成路由 (AI Copywriter - 具備三層自動降級機制)"""
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'message': '未授權管理者'}), 401

    data = request.get_json() or {}
    name = str(data.get('name', '')).strip()
    category = str(data.get('category', '')).strip()

    if not name:
        return jsonify({'success': False, 'message': '請先輸入餐點名稱！'}), 400

    prompt = f"""
你是一位專業的餐飲品牌文案策劃師。
請為以下餐點撰寫一段誘人、生動且具體的美食商品介紹：
- 餐點名稱：{name}
- 餐點分類：{category if category else '特色美饌'}

【生成要求】：
1. 繁體中文，篇幅約 2~3 句話（簡潔有力，70字以內）。
2. 聚焦於「口感層次」、「料理手法」或「風味特色」（如：酥脆焦香、慢火細熬、清爽甘甜）。
3. 嚴禁包含字數編號 (如 (1)、(2))、引號、Markdown 符號或思考草稿，直接輸出文案本身。
"""

    api_key_clean = (Config.GEMINI_API_KEY or "").strip().strip("'").strip('"')

    # --------------------------------------------------------------------------
    # 第一層：嘗試呼叫 Google Gemini 3.6 Flash
    is_in_cooldown = time.time() < _AI_ADVICE_STATE.get('cooldown_until', 0)
    
    if api_key_clean and api_key_clean not in ["YOUR_API_KEY", "1234", "none"] and not is_in_cooldown:
        try:
            import requests
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key_clean}"
            system_instruction = "你是一位專業的美食文案師。請直接輸出最終的繁體中文介紹，嚴格禁止輸出任何思考過程、草稿檢查、字數驗證或英文自我問答。"
            payload = {
                "contents": [{"parts": [{"text": f"{system_instruction}\n\n{prompt}"}]}],
                "generationConfig": {
                    "maxOutputTokens": 1200,
                    "temperature": 0.2
                }
            }
            print("[*] 正在向 Gemini (gemini-3.6-flash) 請求生成餐點文案...") 

            resp = requests.post(url, headers={"Content-Type": "application/json", "x-goog-api-key": api_key_clean}, json=payload, timeout=15)
            res_json = resp.json() if resp.status_code == 200 else {}
            candidates = res_json.get('candidates', [])

            if resp.status_code == 200 and candidates:
                if 'content' in candidates[0]:
                    parts = candidates[0]['content'].get('parts', [])
                    if parts:
                        text = parts[0].get('text', '').strip()

                        text = re.sub(r'(?i)(?:words|chars|perfect|focus on|texture|cooking|flavor|\b(?:yes|no)\b)[^。\n]*[。\n]?', '', text)
                        text = re.sub(r'[a-zA-Z\?\/]+', '', text)
                        cleaned_desc = re.sub(r'[\(\[\（]\s*\d+\s*[\)\]\）]', '', text)
                        cleaned_desc = cleaned_desc.replace('"', '').replace("'", '').replace('「', '').replace('」', '').replace('*', '').replace('`', '').strip()
                        cleaned_desc = re.sub(r'^[，,。\s]+', '', cleaned_desc)

                        if cleaned_desc and not cleaned_desc.endswith(('。', '！', '!')):
                            cleaned_desc += '。'

                        if len(cleaned_desc) >= 12:
                            print("[V] Gemini 文案生成成功！") 
                            return jsonify({'success': True, 'description': cleaned_desc})
            elif resp.status_code in [401, 403, 429]:
                _AI_ADVICE_STATE['cooldown_until'] = time.time() + 60
                print(f"[!] Gemini 回應代碼 ({resp.status_code})，啟動 60 秒冷卻並切換本地第二層...") 
            else:
                print(f"[!] Gemini 回應代碼 ({resp.status_code})，準備切換至本地第二層...") 
        except Exception as e:
            print(f"[!] Gemini 連線異常 ({e})，準備切換至本地第二層...") 

    # --------------------------------------------------------------------------
    # 第二層：嘗試呼叫本機 Qwen2.5-1.5B (GGUF 本地大模型)
    print("[*] 正在使用本機 Qwen2.5-1.5B 產生商品文案...") 
    local_prompt = f"請為餐點【{name}】（分類：{category or '美饌'}）撰寫一段 50 字以內的誘人繁體中文商品介紹，直接輸出文案。"
    local_desc = run_local_slm(local_prompt, max_tokens=120)
    
    if local_desc and len(local_desc) >= 12:
        cleaned_local = re.sub(r'[\(\[\（]\s*\d+\s*[\)\]\）]', '', local_desc)
        cleaned_local = cleaned_local.replace('"', '').replace("'", '').replace('「', '').replace('」', '').strip()
        if not cleaned_local.endswith(('。', '！', '!')):
            cleaned_local += '。'
        
        print(f"[V] 本地 SLM 文案生成完成：{cleaned_local}") 
        return jsonify({'success': True, 'description': cleaned_local})
    else:
        print("[!] 本地 SLM 輸出長度不足或為空，退回第三層動態辭庫...") 

    # --------------------------------------------------------------------------
    # 第三層
    print("[*] 啟用本地第三層動態辭庫生成文案。") 
    fallback_desc = fallback_menu_description(name, category)
    return jsonify({'success': True, 'description': fallback_desc})