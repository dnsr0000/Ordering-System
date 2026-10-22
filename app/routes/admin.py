import os
import re
import secrets
from datetime import datetime

from flask import (Blueprint, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from flask_login import current_user, login_user, logout_user
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db, ip_key, limiter
from app.models.coupon import Coupon, UserCoupon, parse_discount_value
from app.models.enums import DiscountType, OrderSource
from app.models.menu import ComboOption, MenuItem
from app.models.order import Order
from app.models.tenant import StaffAccount, Tenant
from app.models.user import AdminLog, StaffAuditLog, User
from app.routes.sync_admin import node_name_map
from app.services import ai_service, analytics_service, backup_service, excel_service, menu_service
from app.services.audit import record_staff_audit
from app.services import auth_throttle
from app.services.auth_throttle import clear_pin_lockout
from app.services.bootstrap import seed_tenant_defaults
from app.services.order_service import todays_range
from app.services.points_service import change_points
from app.services.reward_service import get_or_create_reward_setting
from app.services.upload_paths import remove_upload, upload_file_path
from app.utils import (WEAK_PIN_MESSAGE, form_flag, get_json_dict, is_platform_admin, is_weak_pin,
                       platform_admin_required, role_required, safe_int, validate_phone, validate_pin)

admin_bp = Blueprint('admin', __name__)
MANAGERS = ('owner', 'manager')
STAFF_ROLES = {'manager', 'staff', 'kitchen'}


def _dashboard(tab=None):
    return url_for('admin.admin_dashboard', tab=tab) if tab else url_for('admin.admin_dashboard')


def _ai_signature(orders):
    """今日訂單狀態摘要：有變動才重新產生 AI 營運建議"""
    start, _end = todays_range()
    return ','.join(f'{o.id}:{o.status}' for o in sorted(orders, key=lambda o: o.id) if o.created_at >= start)


def _menu_item_form_data(item):
    """後台「編輯品項」視窗的預設值 (以 JSON 嵌入頁面，取代按鈕上的大量 data-* 屬性)"""
    return {
        'id': item.id, 'name': item.name, 'category': item.category or '', 'modifiers': item.modifiers or 'none',
        'price': item.price or 0, 'stock': item.stock or 0, 'total_stock': item.total_stock or 0,
        'is_sold_out': bool(item.is_sold_out), 'is_discount': bool(item.is_discount),
        'discount_price': item.discount_price or 0, 'description': item.description or '',
        'image_path': item.image_path or '', 'is_recommended': bool(item.is_manual_popular or item.is_recommended),
        'is_new': bool(item.is_new), 'is_reward': bool(item.is_reward), 'reward_points': item.reward_points or 0,
        'reward_discount_points': item.reward_discount_points or 0, 'can_be_add_on': bool(item.can_be_add_on),
        'add_on_price': item.add_on_price or 0, 'addon_trigger_type': item.addon_trigger_type or 'any',
        'addon_trigger_target': item.addon_trigger_target or '',
    }


def _user_coupon_row(uc):
    """會員持券列 (後台票券管理視窗與 toggle_user_coupon 回應共用)"""
    return dict(uc.to_dict(), title=uc.coupon.title if uc.coupon else '優惠券')


def _analytics(orders, limit):
    data = analytics_service.build_order_analytics(orders, limit=limit)
    data['ai_advice'] = ai_service.generate_ai_business_advice(data, g.tenant_id, _ai_signature(orders))
    return data


# ==============================================================================
# 登入 / 儀表板 / 店家註冊 / 員工帳號
# ==============================================================================
@admin_bp.route('/admin', methods=['GET', 'POST'], endpoint='admin_dashboard')
@limiter.limit('10/minute', methods=['POST'], key_func=ip_key)
def admin_dashboard():
    if request.method == 'POST':
        return _staff_login()

    if not current_user.is_authenticated:
        return render_template('admin.html', is_admin=False)
    if current_user.role not in MANAGERS:
        # 後台含會員個資與稽核紀錄：櫃檯與廚房直接進入各自的工作畫面
        return redirect(url_for('kitchen.kitchen_display' if current_user.role == 'kitchen' else 'pos.pos_terminal'))

    limit = max(1, request.args.get('limit', 3, type=int))
    orders = analytics_service.load_dashboard_orders()
    start, _end = todays_range()
    items = MenuItem.query.all()
    users = User.query.order_by(User.id).all()
    member_dir = current_app.config['UPLOAD_FOLDER_MEMBER']
    return render_template(
        'admin.html',
        is_admin=True,
        orders=[o for o in orders if o.created_at and o.created_at >= start],
        items=items,
        reward_items=[i for i in items if i.is_reward],
        reward_coupons=Coupon.query.all(),
        users=users,
        # 只顯示實際存在的會員照片 (檔案遺失時改顯示預設圖，避免瀏覽器出現 404)
        member_photo_ids={u.id for u in users if os.path.isfile(upload_file_path(member_dir, u.photo_path) or '')},
        admin_menu_items=[_menu_item_form_data(i) for i in items],
        admin_user_coupons={u.id: [_user_coupon_row(uc) for uc in sorted(u.user_coupons, key=lambda c: c.id)] for u in users},
        analytics=_analytics(orders, limit),
        current_limit=limit,
        reward_setting=get_or_create_reward_setting(g.tenant_id),
        tenant=g.tenant,
        audit_logs=StaffAuditLog.query.order_by(StaffAuditLog.id.desc()).limit(200).all(),
        admin_logs=AdminLog.query.order_by(AdminLog.id.desc()).limit(100).all(),
        is_platform_admin=is_platform_admin(),
        # 整庫備份/還原只適用於 SQLite (backup_service.require_sqlite)；其他資料庫隱藏並顯示說明
        backup_available=db.engine.dialect.name == 'sqlite',
        db_dialect='PostgreSQL' if db.engine.dialect.name == 'postgresql' else db.engine.dialect.name,
    )


def _staff_login():
    username = request.form.get('username', '').strip()
    password = request.form.get('password') or ''
    keys = auth_throttle.staff_keys(username, request.remote_addr or '-')
    remaining = auth_throttle.lock_remaining(keys.values())
    if remaining:  # 鎖定期間連正確密碼也不放行
        minutes = max(1, int((remaining.total_seconds() + 59) // 60))
        flash(f'⚠️ 密碼錯誤次數過多，此帳號已暫時鎖定，請於 {minutes} 分鐘後再試。', 'danger')
        return render_template('admin.html', is_admin=False), 429
    staff = StaffAccount.query.filter_by(username=username).first()
    if staff is None:
        auth_throttle.spend_hash_time(password)
    if staff is None or not staff.check_password(password) or not staff.can_login:
        auth_throttle.record_staff_failure(keys)  # 帳號不存在也同樣計數，無法藉此列舉帳號
        flash('❌ 帳號或密碼錯誤！', 'danger')
        return render_template('admin.html', is_admin=False), 401
    auth_throttle.clear_keys(keys['device'], keys['account'])
    # 換新的工作階段：清掉登入前的顧客 / 其他員工資料，只保留目前店家
    tenant_id = session.get('tenant_id')
    session.clear()
    if tenant_id is not None:
        session['tenant_id'] = tenant_id
    login_user(staff)
    session.permanent = True
    log = AdminLog(tenant_id=staff.tenant_id, username=staff.username, login_at=datetime.now(),
                   ip_address=request.remote_addr or '')
    g.tenant_id = staff.tenant_id
    db.session.add(log)
    db.session.commit()
    session['admin_log_id'] = log.id
    return redirect(_dashboard())


@admin_bp.route('/admin/logout', methods=['POST'], endpoint='admin_logout')
def admin_logout():
    log = db.session.get(AdminLog, session.get('admin_log_id') or 0)
    if log is not None and not log.logout_at:
        log.logout_at = datetime.now()
        db.session.commit()
    logout_user()
    tenant_id = session.get('tenant_id')
    session.clear()
    if tenant_id is not None:
        session['tenant_id'] = tenant_id
    return redirect(_dashboard())


@admin_bp.route('/tenant/register', methods=['GET', 'POST'], endpoint='tenant_register')
@limiter.limit('10/hour', methods=['POST'], key_func=ip_key)
def tenant_register():
    if request.method == 'GET':
        return render_template('tenant_register.html')
    shop_name = request.form.get('shop_name', '').strip()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    slug = re.sub(r'[^a-z0-9-]+', '-', request.form.get('slug', '').strip().lower()).strip('-')
    if not shop_name or not username or len(password) < 10 or not slug:
        raise AppError('請填妥資料；密碼至少 10 個字元。')
    if Tenant.query.filter_by(slug=slug).first():
        raise AppError('店家網址代號已被使用。', 409)
    if StaffAccount.query.filter_by(username=username).first():
        raise AppError('員工帳號已被使用。', 409)

    tenant = Tenant(name=shop_name, slug=slug, is_active=True)
    db.session.add(tenant)
    db.session.flush()
    g.tenant, g.tenant_id = tenant, tenant.id
    owner = StaffAccount(tenant_id=tenant.id, username=username, role='owner', is_active=True)
    owner.set_password(password)
    db.session.add(owner)
    seed_tenant_defaults(tenant.id)
    db.session.commit()
    session.clear()
    login_user(owner)
    session.permanent = True
    return redirect(_dashboard())


@admin_bp.route('/admin/staff', methods=['GET', 'POST'], endpoint='staff_accounts')
@role_required('owner')
def staff_accounts():
    if request.method == 'GET':
        accounts = StaffAccount.query.filter_by(tenant_id=g.tenant_id, deleted_at=None).order_by(StaffAccount.id).all()
        return render_template('staff_accounts.html', accounts=accounts, tenant=g.tenant, current_staff_id=current_user.id)

    back = url_for('admin.staff_accounts')
    action = request.form.get('action', 'create')
    if action == 'create':
        username = request.form.get('username', '').strip()
        role = request.form.get('role', 'staff')
        password = request.form.get('password', '')
        if role not in STAFF_ROLES or not username or len(password) < 10:
            raise AppError('❌ 帳號必填，密碼至少 10 個字元，且角色需合法！', redirect_to=back)
        if StaffAccount.query.filter_by(username=username).first():
            raise AppError('❌ 該員工帳號名稱已被使用！', 409, redirect_to=back)
        account = StaffAccount(tenant_id=g.tenant_id, username=username, role=role, is_active=True)
        account.set_password(password)
        db.session.add(account)
        record_staff_audit('STAFF_CREATE', details=f"新增員工帳號：{username} (角色:{role})")
        db.session.commit()
        return redirect(back)

    acc = StaffAccount.query.filter_by(id=safe_int(request.form.get('account_id')), tenant_id=g.tenant_id,
                                       deleted_at=None).first_or_404()
    if action in ('toggle_active', 'delete_account') and acc.id == current_user.id:
        raise AppError('❌ 無法停用或刪除您目前登入的負責人帳號！', redirect_to=back)
    if action == 'toggle_active':
        acc.is_active = not acc.is_active
        record_staff_audit('STAFF_TOGGLE_ACTIVE', details=f"{'啟用' if acc.is_active else '停用'}員工帳號：{acc.username} ({acc.role})")
    elif action == 'reset_password':
        new_pwd = request.form.get('new_password', '')
        if len(new_pwd) < 10:
            raise AppError('❌ 新密碼長度至少需 10 個字元！', redirect_to=back)
        acc.set_password(new_pwd)
        record_staff_audit('STAFF_RESET_PASSWORD', details=f"重設員工帳號密碼：{acc.username}")
        flash(f'✅ 已成功重設員工【{acc.username}】的密碼！', 'success')
    elif action == 'delete_account':
        # 軟刪除：保留帳號列 (稽核可追溯、id 不重用)，立即停用、使密碼失效並釋出帳號名稱
        record_staff_audit('STAFF_DELETE_ACCOUNT', details=f"刪除員工帳號：{acc.username} ({acc.role})，帳號ID={acc.id}")
        acc.is_active = False
        acc.deleted_at = datetime.now()
        acc.username = f"{acc.username[:90]}#deleted-{acc.id}"
        acc.set_password(secrets.token_urlsafe(32))
    else:
        raise AppError('不支援的操作', redirect_to=back)
    db.session.commit()
    return redirect(back)


# ==============================================================================
# 菜單、套餐、紅利商品
# ==============================================================================
def _store_uploaded_image(item):
    image = request.files.get('image')
    if not image or not image.filename:
        return
    upload_dir = current_app.config['UPLOAD_FOLDER_MENU']
    new_path = menu_service.save_menu_image(upload_dir, g.tenant_id, image)
    if item.image_path:  # 新圖存好後才刪舊圖，圖片損毀時不會把原圖弄丟
        remove_upload(upload_dir, item.image_path)
    item.image_path = new_path


@admin_bp.route('/admin/add', methods=['POST'])
@role_required(*MANAGERS)
def add_item():
    item = menu_service.apply_menu_form(MenuItem(tenant_id=g.tenant_id, image_path=''), request.form, form_flag)
    _store_uploaded_image(item)
    db.session.add(item)
    db.session.commit()
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/edit/<int:id>', methods=['POST'])
@role_required(*MANAGERS)
def edit_item(id):
    item = db.get_or_404(MenuItem, id)
    menu_service.apply_menu_form(item, request.form, form_flag)
    menu_service.set_item_reward_status(item, form_flag('is_reward'), request.form.get('reward_points'),
                                        request.form.get('reward_discount_points'))
    _store_uploaded_image(item)
    db.session.commit()
    return redirect(_dashboard(request.form.get('from_tab') or ('rewards' if item.is_reward else 'menu')))


@admin_bp.route('/admin/delete/<int:id>', methods=['POST'])
@role_required(*MANAGERS)
def delete_item(id):
    item = db.get_or_404(MenuItem, id)
    if item.image_path:
        remove_upload(current_app.config['UPLOAD_FOLDER_MENU'], item.image_path)
    record_staff_audit('DELETE_MENU_ITEM', details=f'刪除菜單品項：{item.name} (${item.price})')
    db.session.delete(item)
    db.session.commit()
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/add_combo', methods=['POST'])
@role_required(*MANAGERS)
def add_combo():
    combo = menu_service.save_combo(ComboOption(tenant_id=g.tenant_id), request.form, form_flag)
    db.session.add(combo)
    record_staff_audit('COMBO_CHANGE', details=f"新增套餐：{combo.name} (主餐 #{combo.main_item_id}，加價 ${combo.additional_price})")
    db.session.commit()
    flash('✅ 成功為餐點新增套餐組合！', 'success')
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/edit_combo/<int:id>', methods=['POST'])
@role_required(*MANAGERS)
def edit_combo(id):
    combo = menu_service.save_combo(db.get_or_404(ComboOption, id), request.form, form_flag)
    record_staff_audit('COMBO_CHANGE', details=f"編輯套餐 #{combo.id}：{combo.name} (主餐 #{combo.main_item_id}，加價 ${combo.additional_price})")
    db.session.commit()
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/delete_combo/<int:id>', methods=['POST'])
@role_required(*MANAGERS)
def delete_combo(id):
    combo = db.get_or_404(ComboOption, id)
    record_staff_audit('COMBO_CHANGE', details=f"刪除套餐 #{combo.id}：{combo.name}")
    db.session.delete(combo)
    db.session.commit()
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/add_reward', methods=['POST'])
@role_required(*MANAGERS)
def add_reward():
    item = db.get_or_404(MenuItem, safe_int(request.form.get('item_id')))
    menu_service.set_item_reward_status(item, True, request.form.get('reward_points'), request.form.get('reward_discount_points'))
    db.session.commit()
    return redirect(_dashboard('rewards'))


@admin_bp.route('/admin/remove_reward/<int:id>', methods=['POST'])
@role_required(*MANAGERS)
def remove_reward_item(id):
    menu_service.set_item_reward_status(db.get_or_404(MenuItem, id), False)
    db.session.commit()
    return redirect(_dashboard('rewards'))


# ==============================================================================
# 優惠券
# ==============================================================================
def _apply_coupon_form(coupon):
    code = request.form.get('code', '').strip().upper()
    title = (request.form.get('title') or '').strip()
    if not code or not title:
        raise AppError('❌ 請輸入優惠券名稱與代碼！')
    if Coupon.query.filter(Coupon.code == code, Coupon.id != coupon.id).first():
        raise AppError('❌ 代碼已被使用！')
    if coupon.id is not None and coupon.code != code:  # 改代碼時一併更新會員已領取的券
        db.session.execute(update(UserCoupon).where(UserCoupon.coupon_id == coupon.id).values(code=code)
                           .execution_options(synchronize_session=False))
    dtype = request.form.get('discount_type', DiscountType.FIXED)
    coupon.code, coupon.title = code, title
    coupon.discount_type = dtype if dtype in (DiscountType.FIXED, DiscountType.PERCENT) else DiscountType.FIXED
    coupon.discount_value = parse_discount_value(coupon.discount_type, request.form.get('discount_value'))
    coupon.min_spend = max(0, safe_int(request.form.get('min_spend'), 0))
    coupon.reward_points = max(0, safe_int(request.form.get('reward_points'), 0))
    coupon.reward_discount_points = max(0, safe_int(request.form.get('reward_discount_points'), 0)) if coupon.reward_points else 0
    coupon.is_reward = coupon.reward_points > 0
    coupon.per_user_limit = excel_service.parse_per_user_limit(request.form.get('per_user_limit'))
    return coupon


def _coupon_desc(c):
    return f"{c.title} ({c.code}，{c.discount_label}，門檻${c.min_spend}，兌換點數:{c.reward_points})"


@admin_bp.route('/admin/add_coupon', methods=['POST'])
@role_required(*MANAGERS)
def add_coupon():
    coupon = _apply_coupon_form(Coupon(tenant_id=g.tenant_id))
    db.session.add(coupon)
    record_staff_audit('COUPON_CHANGE', details=f"新增優惠券：{_coupon_desc(coupon)}")
    db.session.commit()
    return redirect(_dashboard('rewards'))


@admin_bp.route('/admin/edit_coupon/<int:id>', methods=['POST'])
@role_required(*MANAGERS)
def edit_coupon(id):
    coupon = db.get_or_404(Coupon, id)
    before = _coupon_desc(coupon)
    _apply_coupon_form(coupon)
    record_staff_audit('COUPON_CHANGE', details=f"編輯優惠券：{before} → {_coupon_desc(coupon)}")
    db.session.commit()
    return redirect(_dashboard('rewards'))


@admin_bp.route('/admin/delete_coupon/<int:id>', methods=['POST'])
@role_required(*MANAGERS)
def delete_coupon(id):
    coupon = db.get_or_404(Coupon, id)
    record_staff_audit('COUPON_CHANGE', details=f"刪除優惠券：{coupon.title} ({coupon.code})，連同會員已領取的 {len(coupon.user_coupons)} 張持券")
    db.session.delete(coupon)
    db.session.commit()
    return redirect(_dashboard('rewards'))


# ==============================================================================
# 會員
# ==============================================================================
@admin_bp.route('/admin/delete_user/<int:id>', methods=['POST'])
@role_required(*MANAGERS)
def delete_user(id):
    user = db.get_or_404(User, id)
    if user.photo_path:
        remove_upload(current_app.config['UPLOAD_FOLDER_MEMBER'], user.photo_path)
    # 訂單保留 (外鍵為 SET NULL)，桌號註記為已註銷會員
    db.session.execute(update(Order).where(Order.user_id == id).values(table_number=f"{user.name} (已註銷會員)"[:50])
                       .execution_options(synchronize_session=False))
    record_staff_audit('DELETE_USER', target_user_id=user.id, points_delta=-(user.points or 0),
                       details=f"刪除會員：{user.name} ({user.phone}, 原點數:{user.points})")
    db.session.delete(user)
    db.session.commit()
    return redirect(_dashboard('users'))


@admin_bp.route('/admin/edit_user/<int:id>', methods=['POST'])
@role_required(*MANAGERS)
def edit_user(id):
    user = db.get_or_404(User, id)
    old = (user.name, user.phone, user.points or 0)
    name = (request.form.get('name') or user.name).strip()[:100]
    phone = (request.form.get('phone') or user.phone).strip()
    pin = (request.form.get('pin') or '').strip()
    if not validate_phone(phone):
        raise AppError('❌ 手機號碼格式不正確！請輸入 09 開頭的 10 碼數字。')
    dup = User.query.filter(User.phone == phone, User.id != user.id).first()
    if dup is not None:
        raise AppError(f'❌ 手機號碼 {phone} 已被其他會員【{dup.name}】使用！', 409)
    if pin and not validate_pin(pin):
        raise AppError('❌ 重設之安全 PIN 碼必須為 4 至 6 位數字！')
    if pin and is_weak_pin(pin, phone):
        raise AppError(WEAK_PIN_MESSAGE)

    user.name, user.phone = name, phone
    # 點數：表單帶有開啟視窗時的點數 (loaded_points) 時只套用差額，不蓋掉編輯期間顧客剛消費的點數
    submitted = max(0, safe_int(request.form.get('points'), 0))
    loaded = request.form.get('loaded_points')
    if loaded not in (None, ''):
        delta = submitted - max(0, safe_int(loaded, 0))
        if delta:
            change_points(g.tenant_id, user.id, delta, 'admin', clamp=True)
            db.session.refresh(user, ['points'])
    else:
        user.points = submitted
    if pin:
        user.set_pin(pin)

    changes = []
    if user.name != old[0]:
        changes.append(f"姓名 {old[0]} -> {user.name}")
    if user.phone != old[1]:
        changes.append(f"手機 {old[1]} -> {user.phone}")
    if user.points != old[2]:
        changes.append(f"點數 {old[2]} -> {user.points}")
    if pin:
        changes.append("重設安全PIN碼")
    if changes:
        record_staff_audit('EDIT_USER', target_user_id=user.id, points_delta=user.points - old[2],
                           details=f"編輯會員 #{user.id}：" + "；".join(changes))
    db.session.commit()
    if pin:
        clear_pin_lockout(g.tenant_id, user.phone)
    return redirect(_dashboard('users'))


@admin_bp.route('/admin/toggle_user_coupon/<int:uc_id>', methods=['POST'])
@role_required(*MANAGERS)
def toggle_user_coupon(uc_id):
    uc = db.get_or_404(UserCoupon, uc_id)
    uc.is_used = not uc.is_used
    uc.used_at = datetime.now() if uc.is_used else None
    record_staff_audit('TOGGLE_USER_COUPON', target_user_id=uc.user_id,
                       details=f"手動將會員券 {uc.code} (持券 #{uc.id}) 切換為「{'已使用' if uc.is_used else '未使用'}」")
    db.session.commit()
    coupons = UserCoupon.query.filter_by(user_id=uc.user_id).order_by(UserCoupon.id).all()
    return jsonify({'success': True, 'user_name': uc.user.name if uc.user else '',
                    'coupons': [_user_coupon_row(c) for c in coupons]})


@admin_bp.route('/admin/update_reward_setting', methods=['POST'])
@role_required(*MANAGERS)
def update_reward_setting():
    s = get_or_create_reward_setting(g.tenant_id)
    before = (s.is_enabled, s.points_per_dollar, s.max_discount_per_order, s.spend_per_point)
    s.is_enabled = form_flag('is_enabled')
    s.points_per_dollar = max(1, safe_int(request.form.get('points_per_dollar'), 1))
    s.max_discount_per_order = max(0, safe_int(request.form.get('max_discount_per_order'), 0))
    s.spend_per_point = max(1, safe_int(request.form.get('spend_per_point'), 1))
    after = (s.is_enabled, s.points_per_dollar, s.max_discount_per_order, s.spend_per_point)
    if before != after:
        record_staff_audit('UPDATE_REWARD_SETTING', details=f"紅利設定 (啟用/幾點折$1/單筆折抵上限/每幾元送1點)：{before} → {after}")
    db.session.commit()
    return redirect(_dashboard('users'))


# ==============================================================================
# 備份、匯出入
# ==============================================================================
@admin_bp.route('/admin/backup_system', methods=['POST'])
@role_required('owner')
@platform_admin_required
def backup_system():
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    return send_file(backup_service.create_full_backup_zip(), download_name=f"Kiosk_Full_Backup_{stamp}.zip",
                     as_attachment=True, mimetype='application/zip')


@admin_bp.route('/admin/restore_backup', methods=['POST'])
@role_required('owner')
@platform_admin_required
def restore_backup():
    file = request.files.get('backup_zip')
    if not file or not file.filename:
        raise AppError('整庫還原會覆蓋現有資料；請選取有效備份檔案。')
    if not file.filename.lower().endswith('.zip'):
        raise AppError('❌ 僅支援 .zip 壓縮格式！')
    flags = {k: request.form.get(k) == '1' for k in ('restore_db', 'restore_photos', 'restore_menu_photos', 'overwrite_photos')}
    if not (flags['restore_db'] or flags['restore_photos'] or flags['restore_menu_photos']):
        raise AppError('❌ 請至少勾選一項欲還原的項目！')
    try:
        db_restored, photos, menu_photos = backup_service.restore_system_backup(file, **flags)
    except AppError:
        raise
    except Exception as e:  # noqa: BLE001  (訊息可能含備份包內使用者可控的檔名，由模板自動跳脫)
        raise AppError(f'❌ 還原過程發生錯誤：{e}', 500)
    flash(f"✅ 系統備份還原完成！資料庫：{'已還原 (還原前的資料庫已另存為 database/menu.db.bak-時間)' if db_restored else '未更動'}；"
          f"會員相片 {photos} 張；餐點相片 {menu_photos} 張", 'success')
    return redirect(_dashboard())


@admin_bp.route('/admin/export_excel', methods=['POST'])
@role_required(*MANAGERS)
def export_excel():
    tables = request.form.getlist('tables')
    if not tables:
        raise AppError('❌ 請至少勾選一個資料表！')
    start, end = request.form.get('start_date'), request.form.get('end_date')
    suffix = f"_{start}_to_{end}" if start and end else ''
    return send_file(excel_service.generate_excel_export(tables, start, end), as_attachment=True,
                     download_name=f"Kiosk_Export_{datetime.now():%Y%m%d_%H%M%S}{suffix}.xlsx",
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@admin_bp.route('/admin/import_smart', methods=['POST'])
@role_required(*MANAGERS)
def import_smart():
    file = request.files.get('file')
    if not file or not file.filename:
        raise AppError('❌ 請選擇 Excel / CSV 檔案！')
    counts = excel_service.import_workbook(file)
    flash(f"✅ 智慧匯入完成！會員 {counts['User']} 筆、菜單 {counts['MenuItem']} 筆、優惠券 {counts['Coupon']} 筆", 'success')
    return redirect(_dashboard('menu'))


# ==============================================================================
# 前端即時資料與 AI 文案
# ==============================================================================
@admin_bp.route('/api/admin_live_orders')
@role_required(*MANAGERS)
def api_admin_live_orders():
    orders = analytics_service.load_dashboard_orders()
    start, end = todays_range()
    today = [o for o in orders if o.created_at and start <= o.created_at < end]
    limit = max(1, min(100, request.args.get('limit', 3, type=int) or 3))
    node_names = node_name_map()
    return jsonify({
        'success': True,
        'orders': [{
            'id': o.id, 'pickup_number': o.display_pickup_number,
            # 線上訂單在取餐分店收到前尚未編取餐號；線上端顯示取餐分店
            'pickup_pending': o.order_source == OrderSource.ONLINE and not (o.pickup_number or 0) > 0,
            'pickup_node_name': node_names.get(o.node_uuid, '') if o.node_uuid else '',
            'payment_status': o.payment_status or '',
            'user_name': o.user.name if o.user else (o.table_number or '非會員'),
            'order_type': o.order_type, 'order_source': o.order_source or 'Kiosk',
            'cashier_username': o.cashier_username or '',
            'cash_received': o.cash_received if o.cash_received is not None else o.total_price,
            'cash_change': o.cash_change or 0, 'payment_method': o.payment_method,
            'need_cutlery': bool(o.need_cutlery), 'note': o.note or '',
            'created_at': o.created_at.strftime('%m-%d %H:%M'), 'status': o.status,
            'discount_amount': o.discount_amount or 0, 'total_price': o.total_price,
            'items': [i.to_dict() for i in o.items],
        } for o in today],
        'menu_items': [{'id': m.id, 'stock': m.stock or 0, 'total_stock': m.total_stock or 0,
                        'is_sold_out': not m.is_available} for m in MenuItem.query.all()],
        'analytics': _analytics(orders, limit),
    })


@admin_bp.route('/api/generate_item_description', methods=['POST'])
@role_required(*MANAGERS)
def api_generate_item_description():
    data = get_json_dict()
    name = str(data.get('name', '')).strip()
    if not name:
        raise AppError('請先輸入餐點名稱！')
    return jsonify({'success': True, 'description': ai_service.generate_menu_description(name, str(data.get('category', '')).strip())})
