import os
import re
import time
import uuid
import pandas as pd
from datetime import datetime
from sqlalchemy import or_
from sqlalchemy.orm import selectinload
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, send_file, g
from app.config import Config
from app.extensions import db
from app.models.user import User, AdminLog, StaffAuditLog, RewardSetting
from app.models.menu import MenuItem, ComboOption, ModifierOption
from app.models.order import Order
from app.models.coupon import Coupon, UserCoupon
from app.models.tenant import Tenant, StaffAccount
from app.services.cv_service import save_and_fix_image
from app.services.order_service import (
    sanitize_discount_value, 
    build_order_analytics, set_item_reward_status
)
from app.services.ai_service import generate_ai_business_advice, _AI_ADVICE_STATE, run_local_slm, fallback_menu_description
from app.routes.common import record_staff_audit, get_or_create_reward_setting
from app.routes.auth import clear_pin_lockout
from app.utils import (
    role_required, rate_limit, js_alert_redirect, safe_int, safe_float, safe_round, validate_pin, validate_phone,
    platform_admin_required, is_platform_admin, get_json_dict
)
from app.services.backup_service import (
    create_full_backup_zip,
    restore_system_backup,
    generate_excel_export,
    upsert_menu_items_from_df,
    clean_phone_number,
    parse_bool,
    parse_per_user_limit
)

ALLOWED_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp'}
admin_bp = Blueprint('admin', __name__)


@admin_bp.before_request
def require_staff_for_admin_routes():
    public_endpoints = {'admin.admin_dashboard', 'admin.admin_logout', 'admin.tenant_register'}
    if request.endpoint in public_endpoints:
        return None
    if not session.get('staff_account_id') or not session.get('tenant_id') or not getattr(g, 'staff_account', None):
        if request.is_json or request.path.startswith('/api/'):
            return jsonify({'success': False, 'message': '請先登入店家員工帳號。'}), 401
        return redirect(url_for('admin.admin_dashboard'))
    return None

# ==============================================================================
# 後台登入與主儀表板
# ==============================================================================
@admin_bp.route('/admin', methods=['GET', 'POST'], endpoint='admin_dashboard')
@rate_limit(max_requests=10, window_seconds=60, key_prefix="admin_login", methods=('POST',))
def admin_dashboard():
    if request.method == 'POST' and 'username' in request.form:
        username = request.form.get('username', '').strip()
        password = request.form.get('password')
        staff = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(username=username).first()
        tenant = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(id=staff.tenant_id).first() if staff else None
        if staff and staff.is_active and tenant and tenant.is_active and staff.check_password(password or ''):
            session.permanent = True
            session['admin_logged_in'] = True
            session['staff_account_id'] = staff.id
            session['tenant_id'] = tenant.id
            session['admin_role'] = staff.role
            session['staff_role'] = staff.role
            session['admin_username'] = staff.username
            g.staff_account = staff
            g.tenant = tenant
            g.tenant_id = tenant.id
            log = AdminLog(tenant_id=tenant.id, username=username, login_at=datetime.now(), ip_address=request.remote_addr or '')
            db.session.add(log)
            db.session.commit()
            session['admin_log_id'] = log.id
            return redirect(url_for('admin.admin_dashboard'))
        return js_alert_redirect("❌ 帳號或密碼錯誤！", status=401)

    if not session.get('staff_account_id'):
        return render_template('admin.html', is_admin=False)

    limit = max(1, request.args.get('limit', 3, type=int))
    # 只載入「今天的訂單 + 所有尚未出餐的訂單」(營運分析只用到這兩類)，不再每次把歷史上全部訂單載進記憶體；
    # 並一次預載訂單明細，避免逐筆查詢 (N+1)。
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    all_orders = (
        Order.query.options(selectinload(Order.items))
        .filter(or_(Order.created_at >= today_start, Order.status == 'Pending'))
        .order_by(Order.id.desc()).all()
    )
    items = MenuItem.query.all()
    reward_items = MenuItem.query.filter_by(is_reward=True).all()
    reward_coupons = Coupon.query.all()
    users = User.query.all()

    analytics = build_order_analytics(all_orders, limit=limit)
    analytics['ai_advice'] = generate_ai_business_advice(analytics)   # 首次載入就顯示建議，不必等第一次輪詢
    today_orders = [o for o in all_orders if o.created_at and o.created_at >= today_start]
    # 讀取當前紅利設定 (若無則自動產生預設值；同時多個請求首次進入也只會建立一筆)
    reward_setting = get_or_create_reward_setting(g.tenant_id)

    try:
        audit_logs = StaffAuditLog.query.order_by(StaffAuditLog.id.desc()).limit(200).all()
    except Exception:
        # 資料表結構問題由啟動/還原時的遷移負責，這裡不在請求途中跑遷移 (遷移內的 commit/rollback 會干擾目前交易)
        db.session.rollback()
        audit_logs = []
    try:
        admin_logs = AdminLog.query.order_by(AdminLog.id.desc()).limit(100).all()
    except Exception:
        db.session.rollback()
        admin_logs = []

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
        reward_setting=reward_setting,
        tenant=g.tenant,
        audit_logs=audit_logs,
        admin_logs=admin_logs,
        is_platform_admin=is_platform_admin(g.staff_account, g.tenant)
    )

@admin_bp.route('/admin/logout', endpoint='admin_logout')
def admin_logout():
    log_id = session.get('admin_log_id')
    if log_id:
        log = db.session.get(AdminLog, log_id)
        if log and not log.logout_at:
            log.logout_at = datetime.now()
            db.session.commit()
    for key in ('admin_logged_in', 'admin_log_id', 'staff_account_id', 'admin_role', 'admin_username'):
        session.pop(key, None)
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/tenant/register', methods=['GET', 'POST'], endpoint='tenant_register')
def tenant_register():
    if request.method == 'GET':
        return render_template('tenant_register.html')

    shop_name = request.form.get('shop_name', '').strip()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    slug = request.form.get('slug', '').strip().lower()
    slug = re.sub(r'[^a-z0-9-]+', '-', slug).strip('-')

    if not shop_name or not username or len(password) < 10 or not slug:
        return js_alert_redirect("請填妥資料；密碼至少 10 個字元。", status=400)
    if Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug=slug).first():
        return js_alert_redirect("店家網址代號已被使用。", status=409)
    if StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(username=username).first():
        return js_alert_redirect("員工帳號已被使用。", status=409)

    try:
        tenant = Tenant(name=shop_name, slug=slug, is_active=True)
        db.session.add(tenant)
        db.session.flush()
        g.tenant = tenant
        g.tenant_id = tenant.id
        owner = StaffAccount(tenant_id=tenant.id, username=username, role='owner', is_active=True)
        owner.set_password(password)
        db.session.add(owner)
        db.session.add_all([
            ModifierOption(tenant_id=tenant.id, category='addons', name='加荷包蛋', price=10, is_active=True),
            ModifierOption(tenant_id=tenant.id, category='addons', name='加起司片', price=15, is_active=True),
            RewardSetting(tenant_id=tenant.id, is_enabled=True, points_per_dollar=1, max_discount_per_order=0, spend_per_point=100)
        ])
        db.session.commit()
    except Exception:
        db.session.rollback()
        return js_alert_redirect("建立店家失敗，請檢查資料後重試。", status=500)

    session.clear()
    session.permanent = True
    session['staff_account_id'] = owner.id
    session['tenant_id'] = tenant.id
    session['admin_logged_in'] = True
    session['admin_role'] = owner.role
    session['admin_username'] = owner.username
    return redirect(url_for('admin.admin_dashboard'))


@admin_bp.route('/admin/staff', methods=['GET', 'POST'], endpoint='staff_accounts')
@role_required('owner')
def staff_accounts():
    if request.method == 'POST':
        action = request.form.get('action', 'create')
        if action == 'toggle_active':
            target_id = safe_int(request.form.get('account_id'), 0)
            acc = StaffAccount.query.filter_by(id=target_id, tenant_id=g.tenant_id).first_or_404()
            if acc.id == g.staff_account.id:
                return js_alert_redirect('❌ 無法停用您目前登入的負責人帳號！', url_for('admin.staff_accounts'), 400)
            acc.is_active = not bool(acc.is_active)
            record_staff_audit('STAFF_TOGGLE_ACTIVE', details=f"{'啟用' if acc.is_active else '停用'}員工帳號：{acc.username} ({acc.role})")
            db.session.commit()
            return redirect(url_for('admin.staff_accounts'))

        if action == 'reset_password':
            target_id = safe_int(request.form.get('account_id'), 0)
            new_pwd = request.form.get('new_password', '')
            if len(new_pwd) < 10:
                return js_alert_redirect('❌ 新密碼長度至少需 10 個字元！', url_for('admin.staff_accounts'), 400)
            acc = StaffAccount.query.filter_by(id=target_id, tenant_id=g.tenant_id).first_or_404()
            acc.set_password(new_pwd)
            record_staff_audit('STAFF_RESET_PASSWORD', details=f"重設員工帳號密碼：{acc.username}")
            db.session.commit()
            return js_alert_redirect(f'✅ 已成功重設員工【{acc.username}】的密碼！', url_for('admin.staff_accounts'), 200)

        if action == 'delete_account':
            target_id = safe_int(request.form.get('account_id'), 0)
            acc = StaffAccount.query.filter_by(id=target_id, tenant_id=g.tenant_id).first_or_404()
            if acc.id == g.staff_account.id:
                return js_alert_redirect('❌ 無法刪除您目前登入的負責人帳號！', url_for('admin.staff_accounts'), 400)
            record_staff_audit(
                'STAFF_DELETE_ACCOUNT',
                details=f"刪除員工帳號：{acc.username} ({acc.role})"
            )
            db.session.delete(acc)
            db.session.commit()
            return redirect(url_for('admin.staff_accounts'))

        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'staff')
        if role not in {'manager', 'staff', 'kitchen'} or not username or len(password) < 10:
            return js_alert_redirect('❌ 帳號必填，密碼至少 10 個字元，且角色需合法！', url_for('admin.staff_accounts'), 400)
        if StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(username=username).first():
            return js_alert_redirect('❌ 該員工帳號名稱已被使用！', url_for('admin.staff_accounts'), 409)
        account = StaffAccount(tenant_id=g.tenant_id, username=username, role=role, is_active=True)
        account.set_password(password)
        db.session.add(account)
        record_staff_audit('STAFF_CREATE', details=f"新增員工帳號：{username} (角色:{role})")
        db.session.commit()
        return redirect(url_for('admin.staff_accounts'))

    accounts = StaffAccount.query.filter_by(tenant_id=g.tenant_id).order_by(StaffAccount.id.asc()).all()
    return render_template(
        'staff_accounts.html',
        accounts=accounts,
        tenant=g.tenant,
        current_staff_id=g.staff_account.id
    )

# ==============================================================================
# 菜單品項與加購品 CRUD
# ==============================================================================
def _save_menu_image(image):
    """
    儲存上傳的菜單圖片，回傳 (資料庫相對路徑, 錯誤訊息)。
    - 檔名使用隨機 UUID (舊版用秒級時間戳，同一秒上傳兩張會互相覆蓋)
    - 圖片損毀/無法處理時回傳錯誤，呼叫端不可再把不存在的路徑寫進資料庫
    - 內容一律重新編碼為 JPEG，因此副檔名固定為 .jpg
    """
    _, ext = os.path.splitext((image.filename or '').lower())
    if ext not in ALLOWED_IMAGE_EXTS:
        return None, '❌ 僅支援 JPG、PNG、WEBP 圖片格式！'
    tenant_id = getattr(g, 'tenant_id', None) or session.get('tenant_id')
    store_upload_dir = os.path.join(Config.UPLOAD_FOLDER_MENU, str(tenant_id))
    os.makedirs(store_upload_dir, exist_ok=True)
    image_filename = f"menu_{uuid.uuid4().hex[:16]}.jpg"
    if not save_and_fix_image(image, os.path.join(store_upload_dir, image_filename)):
        return None, '❌ 上傳的圖片已損壞或格式不支援，請重新選擇清晰的 JPG/PNG/WEBP 圖片！'
    # 存進資料庫的相對路徑：例如 "1/menu_ab12cd34ef56ab78.jpg"
    return f"{tenant_id}/{image_filename}", None


@admin_bp.route('/admin/add', methods=['POST'])
@role_required('owner', 'manager')
def add_item():
    name = request.form.get('name')
    price = request.form.get('price')
    if name and price:
        price_num = safe_float(price, None)
        if price_num is None:
            return js_alert_redirect('❌ 單價格式不正確，請輸入數字！', status=400)
        final_price = max(0, round(price_num))
        can_be_add_on = True if request.form.get('can_be_add_on') else False
        add_on_price = max(0, safe_round(request.form.get('add_on_price')))
        
        if can_be_add_on:
            if add_on_price <= 0:
                return js_alert_redirect("❌ 已開啟加購品標籤，請輸入大於 0 的加購專屬價！", status=400)
            if add_on_price >= final_price:
                return js_alert_redirect(f"❌ 加購專屬價 (${add_on_price}) 不得高於或等於原單價 (${final_price})！", status=400)

        image = request.files.get('image')
        db_image_path = ''

        if image and image.filename != '':
            db_image_path, img_err = _save_menu_image(image)
            if img_err:
                return js_alert_redirect(img_err, status=400)

        is_sold_out = request.form.get('is_sold_out') == '1'
        is_rec = request.form.get('is_recommended') == '1'
        is_new = request.form.get('is_new') == '1'
        is_discount = request.form.get('is_discount') == '1'

        stock_val = max(0, safe_int(request.form.get('stock'), 0))
        total_stock_val = max(stock_val, safe_int(request.form.get('total_stock'), stock_val))

        new_item = MenuItem(
            tenant_id=g.tenant_id,
            name=name,
            category=request.form.get('category', '主餐'),
            modifiers=request.form.get('modifiers', 'none'),
            price=final_price,
            stock=stock_val,
            total_stock=total_stock_val,
            description=request.form.get('description', ''),
            image_path=db_image_path,
            is_sold_out=is_sold_out,
            is_recommended=is_rec,
            is_manual_popular=is_rec,
            is_discount=is_discount,
            discount_price=max(0, safe_round(request.form.get('discount_price'))),
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
@role_required('owner', 'manager')
def edit_item(id):
    item = MenuItem.query.get_or_404(id)
    name = request.form.get('name')
    price = request.form.get('price')
    from_tab = request.form.get('from_tab') or ('rewards' if item.is_reward else 'menu')

    if name and price:
        price_num = safe_float(price, None)
        if price_num is None:
            return js_alert_redirect('❌ 單價格式不正確，請輸入數字！', status=400)
        item_price = max(0, round(price_num))
        can_be_add_on = bool(request.form.get('can_be_add_on'))
        add_on_price = max(0, safe_round(request.form.get('add_on_price')))
        if can_be_add_on:
            if add_on_price <= 0:
                return js_alert_redirect("❌ 已開啟加購品標籤，請輸入大於 0 的加購專屬價！", status=400)
            if add_on_price >= item_price:
                return js_alert_redirect(f"❌ 加購專屬價 (${add_on_price}) 不得高於或等於原單價 (${item_price})！", status=400)

        is_reward = bool(request.form.get('is_reward'))
        reward_points = max(0, safe_int(request.form.get('reward_points'), 0))
        reward_discount_points = max(0, safe_int(request.form.get('reward_discount_points'), 0))

        # 庫存：表單帶有「載入編輯視窗當下的庫存」(loaded_stock) 時只套用「差額」，
        # 不再用過期的表單值蓋掉編輯期間已被顧客買走的數量 (例如只想改價格，結果庫存被重設回舊數字)。
        # 沒帶 loaded_stock (舊表單/API 呼叫) 時維持原本「以表單值為準」的行為。
        submitted_stock = max(0, safe_int(request.form.get('stock'), 0))
        loaded_stock_raw = request.form.get('loaded_stock')
        if loaded_stock_raw not in (None, ''):
            stock_delta = submitted_stock - max(0, safe_int(loaded_stock_raw, 0))
            if stock_delta != 0:
                db.session.execute(
                    db.text("UPDATE menu_item SET stock = MAX(0, stock + :d) WHERE id = :id AND tenant_id = :t"),
                    {"d": stock_delta, "id": item.id, "t": g.tenant_id}
                )
                db.session.refresh(item)
        else:
            item.stock = submitted_stock
        submitted_total = max(0, safe_int(request.form.get('total_stock'), item.stock or 0))
        loaded_total_raw = request.form.get('loaded_total_stock')
        if loaded_total_raw not in (None, '') and submitted_total == max(0, safe_int(loaded_total_raw, 0)):
            submitted_total = item.total_stock or 0   # 總量沒被改動：保留目前資料庫的值

        # 基本品項欄位更新
        item.name = name
        item.category = request.form.get('category', '主餐')
        item.modifiers = request.form.get('modifiers', 'none')
        item.price = item_price
        item.total_stock = max(item.stock or 0, submitted_total)
        item.is_discount = bool(request.form.get('is_discount'))
        item.discount_price = max(0, safe_round(request.form.get('discount_price')))
        item.description = request.form.get('description', '')
        item.is_sold_out = bool(request.form.get('is_sold_out')) or (item.stock == 0)
        item.is_manual_popular = bool(request.form.get('is_recommended'))
        item.is_new = bool(request.form.get('is_new'))
        item.can_be_add_on = can_be_add_on
        item.add_on_price = add_on_price
        item.addon_trigger_type = request.form.get('addon_trigger_type', 'any')
        item.addon_trigger_target = request.form.get('addon_trigger_target', '').strip()

        # 委派 Service 統一處理紅利上下架規則、價格防呆與稽核日誌
        r_res, r_code = set_item_reward_status(
            item_id=item.id,
            is_reward=is_reward,
            reward_points=reward_points,
            reward_discount_points=reward_discount_points,
            tenant_id=g.tenant_id
        )
        if r_code != 200:
            db.session.rollback()
            return js_alert_redirect(f"❌ {r_res['message']}", status=400)

        image = request.files.get('image')
        if image and image.filename != '':
            new_image_path, img_err = _save_menu_image(image)
            if img_err:
                db.session.rollback()
                return js_alert_redirect(img_err, status=400)
            # 新圖片確定存好之後才刪舊圖，圖片損毀時不會把原本的圖弄丟
            if item.image_path:
                old_path = os.path.join(Config.UPLOAD_FOLDER_MENU, item.image_path)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass
            item.image_path = new_image_path

        db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab=from_tab))

@admin_bp.route('/admin/delete/<int:id>', methods=['POST'])
@role_required('owner', 'manager')
def delete_item(id):
    item = MenuItem.query.get_or_404(id)
    if item.image_path:
        filepath = os.path.join(Config.UPLOAD_FOLDER_MENU, item.image_path)
        if os.path.exists(filepath):
            try: os.remove(filepath)
            except Exception: pass

    ComboOption.query.filter_by(item1_id=id).update({ComboOption.item1_id: None})
    ComboOption.query.filter_by(item2_id=id).update({ComboOption.item2_id: None})
    ComboOption.query.filter_by(item3_id=id).update({ComboOption.item3_id: None})
    record_staff_audit('DELETE_MENU_ITEM', details=f'刪除菜單品項：{item.name} (${item.price})')
    db.session.delete(item)
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='menu'))

# ==============================================================================
# 套餐組合 CRUD
# ==============================================================================
def _validate_combo_refs(main_item_id, name, side_ids):
    """驗證套餐表單：名稱必填，主餐與配餐必須都是「本店」存在的餐點 (不可引用別家店的餐點 ID)"""
    if not name:
        return '❌ 請輸入套餐名稱！'
    if not main_item_id:
        return '❌ 請選擇主餐！'
    ids = {i for i in [main_item_id, *side_ids] if i}
    if MenuItem.query.filter(MenuItem.id.in_(ids)).count() != len(ids):
        return '❌ 套餐包含不存在或不屬於本店的餐點，請重新選擇！'
    return None


def check_side_customizable(item_id, is_checked):
    """檢查配餐是否具備客製化群組，無客製化品項一律強制為 False"""
    if not item_id or not is_checked:
        return False
    item = db.session.get(MenuItem, item_id)
    if not item or not item.modifiers or item.modifiers == 'none':
        return False
    return True

@admin_bp.route('/admin/add_combo', methods=['POST'])
@role_required('owner', 'manager')
def add_combo():
    #if not session.get('admin_logged_in'):
    #    return redirect(url_for('admin.admin_dashboard'))
    
    main_item_id = safe_int(request.form.get('main_item_id'), 0)
    name = str(request.form.get('name', '')).strip()
    additional_price = max(0, safe_int(request.form.get('additional_price'), 0))
    description = request.form.get('description', '')
    
    item1_id = safe_int(request.form.get('item1_id'), 0) or None
    item2_id = safe_int(request.form.get('item2_id'), 0) or None
    item3_id = safe_int(request.form.get('item3_id'), 0) or None

    combo_err = _validate_combo_refs(main_item_id, name, [item1_id, item2_id, item3_id])
    if combo_err:
        return js_alert_redirect(combo_err, '/admin?tab=menu')

    chosen_sides = [s for s in [item1_id, item2_id, item3_id] if s is not None]
    if len(chosen_sides) != len(set(chosen_sides)):
        db.session.rollback()
        return js_alert_redirect("❌ 配餐不可重複選擇相同的餐點！", '/admin?tab=menu')

    #同主餐名稱重複防呆
    existing_name = ComboOption.query.filter_by(
        main_item_id=main_item_id,
        name=name
    ).first()
    if existing_name:
        db.session.rollback()
        return js_alert_redirect(f"❌ 該主餐已存在名為【{name}】的套餐，請使用不同名稱！", '/admin?tab=menu')

    #同主餐配餐內容重複防呆
    target_sides_set = set(chosen_sides)
    existing_combos = ComboOption.query.filter_by(main_item_id=main_item_id).all()
    for ec in existing_combos:
        ec_sides = [s for s in [ec.item1_id, ec.item2_id, ec.item3_id] if s is not None]
        if set(ec_sides) == target_sides_set:
            db.session.rollback()
            return js_alert_redirect(f"❌ 該主餐已存在包含相同配餐內容的套餐【{ec.name}】，不可重複建立相同組合！", '/admin?tab=menu')

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
    record_staff_audit('COMBO_CHANGE', details=f"新增套餐：{name} (主餐 #{main_item_id}，加價 ${additional_price})")
    db.session.commit()
    db.session.expire_all() 
    return js_alert_redirect("✅ 成功為餐點新增套餐組合！", '/admin?tab=menu')

@admin_bp.route('/admin/edit_combo/<int:id>', methods=['POST'])
@role_required('owner', 'manager')
def edit_combo(id):
    """編輯現有套餐組合設定"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin.admin_dashboard'))
        
    combo = ComboOption.query.get_or_404(id)
    main_item_id = safe_int(request.form.get('main_item_id'), 0)
    name = str(request.form.get('name', '')).strip()
    additional_price = max(0, safe_int(request.form.get('additional_price'), 0))
    description = request.form.get('description', '')

    item1_id = safe_int(request.form.get('item1_id'), 0) or None
    item2_id = safe_int(request.form.get('item2_id'), 0) or None
    item3_id = safe_int(request.form.get('item3_id'), 0) or None

    combo_err = _validate_combo_refs(main_item_id, name, [item1_id, item2_id, item3_id])
    if combo_err:
        return js_alert_redirect(combo_err, '/admin?tab=menu')

    chosen_sides = [s for s in [item1_id, item2_id, item3_id] if s is not None]
    if len(chosen_sides) != len(set(chosen_sides)):
        db.session.rollback()
        return js_alert_redirect("❌ 配餐不可重複選擇相同的餐點！", '/admin?tab=menu')

    #同主餐名稱重複防呆 (排除自己)
    duplicate_name = ComboOption.query.filter(
        ComboOption.id != id,
        ComboOption.main_item_id == main_item_id,
        ComboOption.name == name
    ).first()
    if duplicate_name:
        db.session.rollback()
        return js_alert_redirect(f"❌ 該主餐已有其他名為【{name}】的套餐組合！", '/admin?tab=menu')

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
            return js_alert_redirect(f"❌ 該主餐已有相同配餐組合的套餐【{ec.name}】，請勿重複配置！", '/admin?tab=menu')

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

    record_staff_audit('COMBO_CHANGE', details=f"編輯套餐 #{combo.id}：{name} (主餐 #{main_item_id}，加價 ${additional_price})")
    db.session.commit()
    db.session.expire_all()
    return redirect(url_for('admin.admin_dashboard', tab='menu'))

@admin_bp.route('/admin/delete_combo/<int:id>', methods=['POST'])
@role_required('owner', 'manager')
def delete_combo(id):
    combo = ComboOption.query.get_or_404(id)
    record_staff_audit('COMBO_CHANGE', details=f"刪除套餐 #{combo.id}：{combo.name}")
    db.session.delete(combo)
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='menu'))

# ==============================================================================
# 回饋商城上下架與優惠券管理
# ==============================================================================
@admin_bp.route('/admin/add_reward', methods=['POST'])
@role_required('owner', 'manager')
def add_reward():
    item_id = request.form.get('item_id')
    reward_points = request.form.get('reward_points')
    reward_discount_points = request.form.get('reward_discount_points')

    res, status_code = set_item_reward_status(
        item_id=item_id,
        is_reward=True,
        reward_points=reward_points,
        reward_discount_points=reward_discount_points,
        tenant_id=g.tenant_id
    )
    if status_code != 200:
        return jsonify(res), status_code
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

@admin_bp.route('/admin/remove_reward/<int:id>', methods=['POST'])
@role_required('owner', 'manager')
def remove_reward_item(id):
    res, status_code = set_item_reward_status(
        item_id=id,
        is_reward=False,
        tenant_id=g.tenant_id
    )
    if status_code != 200:
        return jsonify(res), status_code
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

@admin_bp.route('/admin/add_coupon', methods=['POST'])
@role_required('owner', 'manager')
def add_coupon():
    code = request.form.get('code', '').strip().upper()
    title = (request.form.get('title') or '').strip()
    if not code or not title:
        return js_alert_redirect("❌ 請輸入優惠券名稱與代碼！", status=400)
    if Coupon.query.filter_by(code=code).first():
        return js_alert_redirect("❌ 代碼重複！", status=400)
    dtype = request.form.get('discount_type', 'fixed')
    req_pts = max(0, safe_int(request.form.get('reward_points'), 0))
    new_c = Coupon(
        title=title,
        code=code,
        discount_type=dtype,
        discount_value=sanitize_discount_value(dtype, request.form.get('discount_value') or 0),
        min_spend=max(0.0, safe_float(request.form.get('min_spend'), 0.0)),
        reward_points=req_pts,
        reward_discount_points=max(0, safe_int(request.form.get('reward_discount_points'), 0)) if req_pts > 0 else 0,
        per_user_limit=parse_per_user_limit(request.form.get('per_user_limit')),
        is_reward=(req_pts > 0)
    )
    db.session.add(new_c)
    record_staff_audit('COUPON_CHANGE', details=f"新增優惠券：{title} ({code}，{dtype}={new_c.discount_value}，門檻${new_c.min_spend}，兌換點數:{req_pts})")
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

@admin_bp.route('/admin/delete_coupon/<int:id>', methods=['POST'])
@role_required('owner', 'manager')
def delete_coupon(id):
    c = Coupon.query.get_or_404(id)
    record_staff_audit('COUPON_CHANGE', details=f"刪除優惠券：{c.title} ({c.code})，連同會員已領取的 {len(c.user_coupons)} 張持券")
    db.session.delete(c)
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

@admin_bp.route('/admin/edit_coupon/<int:id>', methods=['POST'])
@role_required('owner', 'manager')
def edit_coupon(id):
    c = Coupon.query.get_or_404(id)
    code = request.form.get('code', '').strip().upper()
    title = (request.form.get('title') or '').strip()
    if not code or not title:
        return js_alert_redirect("❌ 請輸入優惠券名稱與代碼！", status=400)
    if Coupon.query.filter(Coupon.code == code, Coupon.id != id).first():
        return js_alert_redirect("❌ 代碼已被使用！", status=400)
    if c.code != code:
        UserCoupon.query.filter_by(coupon_id=c.id).update({UserCoupon.code: code})
    old_desc = f"{c.title} ({c.code}，{c.discount_type}={c.discount_value}，門檻${c.min_spend}，兌換點數:{c.reward_points})"
    c.title = title
    c.code = code
    c.discount_type = request.form.get('discount_type', 'fixed')
    c.discount_value = sanitize_discount_value(c.discount_type, request.form.get('discount_value') or 0)
    c.min_spend = max(0.0, safe_float(request.form.get('min_spend'), 0.0))
    c.reward_points = max(0, safe_int(request.form.get('reward_points'), 0))
    c.reward_discount_points = max(0, safe_int(request.form.get('reward_discount_points'), 0)) if c.reward_points > 0 else 0
    c.is_reward = (c.reward_points > 0)
    c.per_user_limit = parse_per_user_limit(request.form.get('per_user_limit'))
    record_staff_audit('COUPON_CHANGE', details=f"編輯優惠券：{old_desc} → {c.title} ({c.code}，{c.discount_type}={c.discount_value}，門檻${c.min_spend}，兌換點數:{c.reward_points})")
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='rewards'))

# ==============================================================================
# 會員資料維護
# ==============================================================================
@admin_bp.route('/admin/delete_user/<int:id>', methods=['POST'])
@role_required('owner', 'manager')
def delete_user(id):
    user = User.query.get_or_404(id)
    if user.photo_path:
        f = os.path.join(Config.UPLOAD_FOLDER_MEMBER, user.photo_path)
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass
    Order.query.filter_by(user_id=id).update({Order.user_id: None, Order.table_number: f"{user.name} (已註銷會員)"}, synchronize_session=False)
    record_staff_audit('DELETE_USER', target_user_id=user.id, points_delta=-(user.points or 0), details=f"刪除會員：{user.name} ({user.phone}, 原點數:{user.points})")
    db.session.delete(user)
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='users'))


@admin_bp.route('/admin/edit_user/<int:id>', methods=['POST'])
@role_required('owner', 'manager')
def edit_user(id):
    user = User.query.get_or_404(id)
    old_points = int(user.points or 0)
    old_name, old_phone = user.name, user.phone
    new_name = (request.form.get('name') or user.name).strip()
    new_phone = (request.form.get('phone') or user.phone).strip()
    if not validate_phone(new_phone):
        return js_alert_redirect('❌ 手機號碼格式不正確！請輸入 09 開頭的 10 碼數字。', status=400)
    dup_user = User.query.filter(User.phone == new_phone, User.id != user.id).first()
    if dup_user:
        return js_alert_redirect(f'❌ 手機號碼 {new_phone} 已被其他會員【{dup_user.name}】使用！', status=409)
    new_pin = (request.form.get('pin') or '').strip()
    if new_pin and not validate_pin(new_pin):
        return js_alert_redirect('❌ 重設之安全 PIN 碼必須為 4 至 6 位數字！', status=400)
    user.name = new_name[:100]
    user.phone = new_phone

    # 點數：表單帶有「載入編輯視窗當下的點數」(loaded_points) 時只套用「差額」，
    # 不會用過期的數字蓋掉編輯期間顧客剛消費賺到或剛花掉的點數 (只想改姓名/電話也不會動到點數)。
    # 沒帶 loaded_points (舊表單/API 呼叫) 時維持原本「以表單值為準」的行為。
    submitted_points = max(0, safe_int(request.form.get('points'), 0))
    loaded_points_raw = request.form.get('loaded_points')
    if loaded_points_raw not in (None, ''):
        points_delta_req = submitted_points - max(0, safe_int(loaded_points_raw, 0))
        if points_delta_req != 0:
            db.session.execute(
                db.text("UPDATE user SET points = MAX(0, points + :d) WHERE id = :id AND tenant_id = :t"),
                {"d": points_delta_req, "id": user.id, "t": g.tenant_id}
            )
            db.session.expire(user, ['points'])
    else:
        user.points = submitted_points
    if new_pin:
        user.set_pin(new_pin)
        clear_pin_lockout(g.tenant_id, user.phone)
    pts_diff = int(user.points or 0) - old_points

    # 稽核只記「真的有變動」的欄位 (舊版固定寫「點數 A -> A」，改電話時反而看不出電話改了什麼)
    changes = []
    if user.name != old_name:
        changes.append(f"姓名 {old_name} -> {user.name}")
    if user.phone != old_phone:
        changes.append(f"手機 {old_phone} -> {user.phone}")
    if pts_diff != 0:
        changes.append(f"點數 {old_points} -> {int(user.points or 0)}")
    if new_pin:
        changes.append("重設安全PIN碼")
    if changes:
        record_staff_audit(
            action_type='EDIT_USER',
            target_user_id=user.id,
            points_delta=pts_diff,
            details=f"編輯會員 #{user.id}：" + "；".join(changes)
        )
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='users'))

@admin_bp.route('/admin/toggle_user_coupon/<int:uc_id>', methods=['POST'])
@role_required('owner', 'manager')
def toggle_user_coupon(uc_id):
    uc = UserCoupon.query.get_or_404(uc_id)
    uc.is_used = not bool(uc.is_used)
    uc.used_at = datetime.now() if uc.is_used else None
    record_staff_audit(
        'TOGGLE_USER_COUPON', target_user_id=uc.user_id,
        details=f"手動將會員券 {uc.code} (持券 #{uc.id}) 切換為「{'已使用' if uc.is_used else '未使用'}」"
    )
    db.session.commit()
    all_user_coupons = UserCoupon.query.filter_by(user_id=uc.user_id).order_by(UserCoupon.id.asc()).all()
    return jsonify({
        'success': True,
        'user_name': uc.user.name if uc.user else '',
        'coupons': [{'id': c.id, 'title': c.coupon.title if c.coupon else '優惠券', 'code': c.code, 'is_used': bool(c.is_used)} for c in all_user_coupons]
    })

@admin_bp.route('/admin/update_reward_setting', methods=['POST'])
@role_required('owner', 'manager')
def update_reward_setting():
    setting = get_or_create_reward_setting(g.tenant_id)
    before = (setting.is_enabled, setting.points_per_dollar, setting.max_discount_per_order, setting.spend_per_point)

    setting.is_enabled = bool(request.form.get('is_enabled'))
    setting.points_per_dollar = max(1, safe_int(request.form.get('points_per_dollar'), 1))
    setting.max_discount_per_order = max(0, safe_int(request.form.get('max_discount_per_order'), 0))
    setting.spend_per_point = max(1, safe_int(request.form.get('spend_per_point'), 1))

    after = (setting.is_enabled, setting.points_per_dollar, setting.max_discount_per_order, setting.spend_per_point)
    if before != after:
        record_staff_audit(
            'UPDATE_REWARD_SETTING',
            details=(f"紅利設定 (啟用/幾點折$1/單筆折抵上限/每幾元送1點)：{before} → {after}")
        )
    db.session.commit()
    return redirect(url_for('admin.admin_dashboard', tab='users'))

@admin_bp.route('/admin/restore_backup', methods=['POST'])
@role_required('owner')
@platform_admin_required
def restore_backup():
    file = request.files.get('backup_zip')
    if not file or file.filename == '':
        return jsonify({
            'success': False,
            'message': '整庫還原會覆蓋現有資料；請選取有效備份檔案。'
        }), 403

    if not file.filename.lower().endswith('.zip'):
        return js_alert_redirect("❌ 僅支援 .zip 壓縮格式！", status=400)

    restore_db = (request.form.get('restore_db') == '1')
    restore_photos = (request.form.get('restore_photos') == '1')
    restore_menu_photos = (request.form.get('restore_menu_photos') == '1')
    overwrite_photos = (request.form.get('overwrite_photos') == '1')

    if not restore_db and not restore_photos and not restore_menu_photos:
        return js_alert_redirect("❌ 請至少勾選一項欲還原的項目！", status=400)

    try:
        db_restored, photo_count, menu_photo_count = restore_system_backup(
            file, restore_db, restore_photos, restore_menu_photos, overwrite_photos
        )
        msg = (
            f"✅ 系統備份還原完成！\n➤ 資料庫：{'已還原 (還原前的資料庫已另存為 database/menu.db.bak-時間)' if db_restored else '未更動'}"
            f"\n➤ 會員相片：{photo_count} 張\n➤ 餐點相片：{menu_photo_count} 張"
        )
        return js_alert_redirect(msg, '/admin', status=200)
    except Exception as e:
        # 錯誤訊息可能含備份包內的檔名 (使用者可控)，一律經 js_alert_redirect 跳脫後輸出
        return js_alert_redirect(f'❌ 還原過程發生錯誤：{e}', status=500)

# Excel 匯出 (包含全部 8 個工作表與格式自適應)
@admin_bp.route('/admin/export_excel', methods=['POST'])
@role_required('owner', 'manager')
def export_excel():
    """Excel 營業報表匯出端點（純報表匯出，單一職責）"""
    selected_tables = request.form.getlist('tables')
    if not selected_tables:
        return js_alert_redirect("❌ 請至少勾選一個資料表！", status=400)

    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')

    output = generate_excel_export(selected_tables, start_date_str, end_date_str)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    date_suffix = f"_{start_date_str}_to_{end_date_str}" if start_date_str and end_date_str else ""
    filename = f"Kiosk_Export_{timestamp}{date_suffix}.xlsx"
    return send_file(
        output,
        download_name=filename,
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@admin_bp.route('/admin/import_smart', methods=['POST'])
@role_required('owner', 'manager')
def import_smart():
    """智慧匯入端點：單一交易保障、多表原子寫入"""
    file = request.files.get('file')
    if not file or file.filename == '':
        return js_alert_redirect("❌ 請選擇 Excel / CSV 檔案！", status=400)

    try:
        # 1. 支援直接上傳單一 CSV 菜單檔案
        if file.filename.lower().endswith('.csv'):
            df = pd.read_csv(file)
            ok, msg_or_count = upsert_menu_items_from_df(df, update_existing=True, auto_commit=True)
            if not ok:
                return js_alert_redirect(f"❌ {msg_or_count}", status=400)
            return redirect(url_for('admin.admin_dashboard', tab='menu'))

        # 2. 處理多工作表 Excel (.xlsx, .xls)
        excel_file = pd.ExcelFile(file)
        imported_counts = {'User': 0, 'MenuItem': 0, 'Coupon': 0}

        for sheet in excel_file.sheet_names:
            df = pd.read_excel(excel_file, sheet_name=sheet)
            if df.empty:
                continue

            cols_lower = [str(c).lower() for c in df.columns]

            # 辨識「會員資料 (User)」
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
                        points_raw = row.get('points')
                        points_given = False
                        try:
                            if points_raw is not None and not pd.isna(points_raw):
                                points = max(0, int(float(points_raw)))
                                points_given = True
                            else:
                                points = 0
                        except (ValueError, TypeError, OverflowError):
                            points = 0

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
                                # 檔案有提供點數就照填 (包含 0 點)；沒提供該欄位才給新會員預設 20 點
                                points=points if points_given else 20,
                                last_login_at=None
                            )
                            db.session.add(new_user)
                        imported_counts['User'] += 1

            # 辨識「菜單品項 (MenuItem)」：以單一交易模式處理，錯誤時即刻攔截
            elif 'MenuItem' in sheet or '菜單' in sheet or '單價(Price)' in df.columns or 'price' in cols_lower or '單價' in df.columns:
                ok, res = upsert_menu_items_from_df(df, update_existing=True, auto_commit=False)
                if not ok:
                    db.session.rollback()
                    return js_alert_redirect(f"❌ 菜單工作表【{sheet}】解析失敗：{res}", status=400)
                imported_counts['MenuItem'] += res

            # 辨識「優惠券 (Coupon)」
            elif 'Coupon' in sheet or '優惠券' in sheet or '代碼(Code)' in df.columns or 'code' in cols_lower or '代碼' in df.columns:
                col_map = {
                    '代碼(Code)': 'code', '代碼': 'code', 'code': 'code', '優惠代碼': 'code',
                    '標題(Title)': 'title', '標題': 'title', 'title': 'title', '優惠券名稱': 'title', '名稱': 'title',
                    '折抵類型(Discount Type)': 'discount_type', '折抵類型': 'discount_type', '折抵方式': 'discount_type', 'discount_type': 'discount_type',
                    '折抵值(Discount Value)': 'discount_value', '折抵值': 'discount_value', '折抵金額': 'discount_value', 'discount_value': 'discount_value',
                    '門檻(Min Spend)': 'min_spend', '門檻': 'min_spend', '最低消費門檻': 'min_spend', '使用門檻': 'min_spend', 'min_spend': 'min_spend',
                    '兌換所需點數(Reward Points)': 'reward_points', '兌換所需點數': 'reward_points', '兌換點數': 'reward_points', '點數': 'reward_points', 'reward_points': 'reward_points',
                    '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '限時特惠點數': 'reward_discount_points', '特惠點數': 'reward_discount_points', 'reward_discount_points': 'reward_discount_points',
                    '上架回饋商城(Is Reward)': 'is_reward', '是否上架': 'is_reward', '上架商城': 'is_reward', 'is_reward': 'is_reward',
                    '每人兌換上限(Per User Limit)': 'per_user_limit', '每人兌換上限': 'per_user_limit', '兌換上限': 'per_user_limit', 'per_user_limit': 'per_user_limit'
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
                        per_user_limit = parse_per_user_limit(row.get('per_user_limit', 0))
                        is_reward = parse_bool(row.get('is_reward')) if ('is_reward' in df_coupon.columns and not pd.isna(row.get('is_reward'))) else (reward_points > 0)

                        existing = Coupon.query.filter_by(code=code).first()
                        if existing:
                            existing.title = title
                            existing.discount_type = dtype
                            existing.discount_value = dvalue
                            existing.min_spend = min_sp
                            existing.reward_points = reward_points
                            existing.reward_discount_points = reward_discount_points
                            existing.is_reward = is_reward
                            existing.per_user_limit = per_user_limit
                        else:
                            new_coupon = Coupon(
                                code=code,
                                title=title,
                                discount_type=dtype,
                                discount_value=dvalue,
                                min_spend=min_sp,
                                reward_points=reward_points,
                                reward_discount_points=reward_discount_points,
                                is_reward=is_reward,
                                per_user_limit=per_user_limit
                            )
                            db.session.add(new_coupon)
                        imported_counts['Coupon'] += 1

        # 多工作表全數解析成功後，一次性原子提交
        db.session.commit()
        msg = f"✅ 智慧匯入完成！\n" \
              f"共處理更新與新增：\n" \
              f"➤ 會員 (User): {imported_counts['User']} 筆\n" \
              f"➤ 菜單 (MenuItem): {imported_counts['MenuItem']} 筆\n" \
              f"➤ 優惠券 (Coupon): {imported_counts['Coupon']} 筆"
        return js_alert_redirect(msg, '/admin')

    except Exception as e:
        db.session.rollback()
        return js_alert_redirect(f"❌ 檔案解析失敗，請確認檔案格式是否正確！\n錯誤訊息: {e}", status=500)
    
@admin_bp.route('/admin/backup_system', methods=['POST'])
@role_required('owner')
@platform_admin_required
def backup_system():
    """平台管理者專屬之完整系統資料庫與照片備份端點 (備份包含所有店家資料，其他店家負責人不可使用)"""
    try:
        memory_file = create_full_backup_zip()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return send_file(
            memory_file,
            download_name=f"Kiosk_Full_Backup_{timestamp}.zip",
            as_attachment=True,
            mimetype='application/zip'
        )
    except Exception as e:
        return js_alert_redirect(f'❌ 備份失敗：{e}', status=500)

# 前端即時輪詢與 AI 文案生成 API (絕對路徑保持 /api/...)
@admin_bp.route('/api/admin_live_orders')
def api_admin_live_orders():
    db.session.expire_all()
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    # 分析只需要「今天 + 待製作」的訂單；預載明細避免 N+1。(舊版每次輪詢都載入歷史全部訂單)
    analytics_orders = (
        Order.query.options(selectinload(Order.items))
        .filter(or_(Order.created_at >= today_start, Order.status == 'Pending'))
        .order_by(Order.id.desc()).all()
    )
    today_orders = [o for o in analytics_orders if o.created_at and today_start <= o.created_at <= today_end]

    orders_data = [{
        'id': o.id, 'pickup_number': o.pickup_number if getattr(o, 'pickup_number', None) else o.id,
        'user_name': o.user.name if o.user else (o.table_number or '非會員'),
        'order_type': o.order_type,
        'order_source': getattr(o, 'order_source', 'Kiosk') or 'Kiosk',
        'cashier_username': getattr(o, 'cashier_username', '') or '',
        'cash_received': getattr(o, 'cash_received', None) if getattr(o, 'cash_received', None) is not None else o.total_price,
        'cash_change': getattr(o, 'cash_change', 0) or 0,
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

    # 熱門餐點排行的顯示筆數與頁面上的設定一致 (頁面以 ?limit= 帶入)
    ranking_limit = max(1, min(100, request.args.get('limit', 3, type=int) or 3))
    analytics = build_order_analytics(analytics_orders, limit=ranking_limit)
    analytics['ai_advice'] = generate_ai_business_advice(analytics)

    return jsonify({'success': True, 'orders': orders_data, 'menu_items': menu_items_data, 'analytics': analytics})

@admin_bp.route('/api/generate_item_description', methods=['POST'])
@role_required('owner', 'manager')
def api_generate_item_description():
    """AI 智慧菜單文案生成路由 (AI Copywriter - 具備三層自動降級機制)"""

    data = get_json_dict()
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
            url = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent"
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