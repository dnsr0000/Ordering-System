import re
import secrets
import time
from datetime import date, datetime

from flask import (Blueprint, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from flask_login import current_user, login_user, logout_user
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.errors import AppError
from app.extensions import db, ip_key, limiter
from app.models.coupon import Coupon, UserCoupon, parse_discount_value
from app.models.enums import CREATABLE_STAFF_ROLES, DiscountType, MANAGER_ROLES, OrderSource
from app.models.menu import ComboOption, MenuItem
from app.models.order import Order
from app.models.tenant import StaffAccount, Tenant
from app.models.user import AdminLog, User
from app.routes.sync_admin import node_name_map
from app.services import (ai_service, analytics_service, backup_service, coupon_service, excel_service, line_service,
                          menu_service, wallet_service)
from app.services.approval_service import permission_required
from app.services.audit import record_staff_audit
from app.services import auth_throttle
from app.services.auth_throttle import clear_pin_lockout
from app.services.bootstrap import seed_tenant_defaults
from app.services.event_bus import order_event_bus
from app.services.order_service import todays_range
from app.services.points_service import change_points
from app.services.reward_service import get_or_create_reward_setting
from app.services.upload_paths import remove_upload
from app.sync import runtime as sync_runtime
from app.utils import (WEAK_PIN_MESSAGE, form_flag, get_json_dict, get_own, is_platform_admin, is_weak_pin,
                       platform_admin_required, role_required, safe_float, safe_int, send_xlsx, validate_phone,
                       validate_pin)

admin_bp = Blueprint('admin', __name__)

# 表單整數欄位的上限：PostgreSQL INTEGER 最大約 21 億，超過時線上端 500、分店 SQLite 存下後上傳會讓整批同步失敗
MAX_AMOUNT = 1_000_000        # 金額 (元)，與 menu_service.MAX_AMOUNT / coupon.MAX_FIXED_DISCOUNT 一致
MAX_POINTS = 100_000_000      # 點數
# 新建員工帳號名稱長度上限：admin_log.username 在已部署的資料庫 (未升級前) 只有 50 字，較長的帳號在 PostgreSQL 上無法登入
MAX_USERNAME_LEN = 50
AI_ADVICE_MIN_INTERVAL = 20 * 60   # 有雲端 AI 金鑰時，營運建議最短更新間隔 (秒)；每次呼叫都會計費


def bounded_int(val, default, lo, hi):
    """表單整數：格式錯誤時用 default，再夾在 lo ~ hi 之間 (寫入資料庫 Integer 欄位前使用)"""
    return min(hi, max(lo, safe_int(val, default)))


# 動作完成後導回的頁面：菜單、會員、稽核是獨立頁面，其餘 (rewards…) 是後台首頁的分頁
TAB_PAGES = {'menu': 'menu_pro.manage', 'users': 'members_pro.manage', 'audit': 'audit.index', 'analytics': 'admin.admin_dashboard'}


def _dashboard(tab=None):
    if tab in TAB_PAGES:
        return url_for(TAB_PAGES[tab])
    return url_for('admin.admin_dashboard', tab=tab) if tab else url_for('admin.admin_dashboard')


def data_modal_context():
    """匯出 / 匯入對話框 (_data_modals.html) 需要的變數：整庫備份 / 還原只適用於 SQLite，其他資料庫隱藏並顯示說明"""
    return dict(is_platform_admin=is_platform_admin(), backup_available=db.engine.dialect.name == 'sqlite',
                db_dialect='PostgreSQL' if db.engine.dialect.name == 'postgresql' else db.engine.dialect.name)


def _dashboard_summary():
    """總覽分頁的待處理事項：未結帳桌、進行中的外送、低庫存原料、發票 / 列印 / 退款異常、今日訂位"""
    from datetime import timedelta
    from app.models.enums import OrderStatus
    from app.models.inventory import Ingredient
    from app.models.reservation import Reservation
    from app.models.payment import Invoice, InvoiceStatus, Refund
    from app.models.printing import PrintJob
    from app.services import inventory_service, table_service
    start, end = todays_range()
    active = Order.query.filter(Order.created_at >= start - timedelta(days=1), Order.status != OrderStatus.CANCELLED)
    # 以桌為單位、不限日期 (與 POS 的未結帳桌位清單 table_service.unpaid_tables 相同條件)
    unpaid_tables = table_service.unpaid_orders_query() \
        .with_entities(db.func.count(db.distinct(Order.dining_table_id))).scalar() or 0
    deliveries = active.filter(Order.delivery_status.in_(('pending', 'assigned', 'out'))).count()
    low_stock = sum(1 for i in Ingredient.query.filter_by(is_active=True).all() if i.is_low) \
        if inventory_service.enabled(g.tenant_id) else 0
    try:
        reservations = Reservation.query.filter(Reservation.reserved_at >= start, Reservation.reserved_at < end,
                                                Reservation.status.in_(('booked', 'confirmed'))).count()
    except Exception:  # noqa: BLE001  (訂位模組欄位變動時不影響總覽)
        db.session.rollback()
        reservations = 0
    # 只取總覽需要的異常計數；完整的 maintenance_service.health() 含 AI 金鑰檢查 (可能同步連外部服務)，只在系統健康頁使用
    health = {
        'invoice_pending': Invoice.query.filter(Invoice.status == InvoiceStatus.PENDING).count(),
        'invoice_failed': Invoice.query.filter(Invoice.status == InvoiceStatus.FAILED).count(),
        'print_failed': PrintJob.query.filter(PrintJob.status == 'failed').count(),
        'print_queued': PrintJob.query.filter(PrintJob.status == 'queued').count(),
        'refund_manual': Refund.query.filter(Refund.status.in_(('manual', 'failed'))).count(),
    }
    return {'unpaid_tables': unpaid_tables, 'deliveries_active': deliveries, 'low_stock': low_stock,
            'reservations_today': reservations, 'health': health}


def _ai_signature(orders):
    """
    今日訂單狀態摘要：有變動才重新產生營運建議。
    有雲端 AI 金鑰時每次產生都會計費 (並消耗每月預算)：改為今日有訂單後最多每 AI_ADVICE_MIN_INTERVAL 秒更新一次；
    沒有金鑰時用本機規則引擎 (不計費)，維持訂單一有變動就更新。
    """
    start, _end = todays_range()
    today = sorted((o for o in orders if o.created_at >= start), key=lambda o: o.id)
    if ai_service.credentials(g.tenant_id):
        return f"{start:%Y%m%d}:{bool(today)}:{int(time.time() // AI_ADVICE_MIN_INTERVAL)}"
    return ','.join(f'{o.id}:{o.status}' for o in today)


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
    from app.models.enums import PAGE_PERMISSIONS
    tabs = [p for p in PAGE_PERMISSIONS if current_user.has_permission(p)]
    if current_user.role not in MANAGER_ROLES and not tabs:
        # 只被授權「顧客評價 / 客服」或「系統設定」的櫃檯 / 廚房：進入該頁 (有後台側欄可切換其他可用頁面)，
        # 不要導回 POS / KDS 讓負責人勾選的權限在畫面上找不到入口
        for perm, endpoint in (('feedback', 'feedback.index'), ('settings', 'settings_admin.index')):
            if current_user.has_permission(perm):
                return redirect(url_for(endpoint))
        # 後台含會員個資與稽核紀錄：櫃檯與廚房 (沒有被授權任何後台頁面時) 直接進入各自的工作畫面
        if current_user.role != 'kitchen':
            return redirect(url_for('pos.pos_terminal'))
        if not sync_runtime.is_hub():
            return redirect(url_for('kitchen.kitchen_display'))
        # 線上端沒有廚房看板 (/kitchen 回 404)：顯示只有總覽的後台首頁
        flash('線上端沒有廚房看板，請到分店開啟 KDS', 'info')
    if current_user.role in MANAGER_ROLES:
        tabs = list(PAGE_PERMISSIONS)

    limit = max(1, request.args.get('limit', 3, type=int))
    orders = analytics_service.load_dashboard_orders()
    if 'reports' in tabs:
        analytics = _analytics(orders, limit)
    else:
        # 營收 / 客單價 / 熱銷金額 / 營運建議屬於「營運報表」權限：不呼叫 AI，也不把營收資料交給樣板
        analytics = analytics_service.build_order_analytics(orders, limit=limit)
        analytics.update(today_revenue=0, avg_order_value=0, ai_advice='')
        analytics['top_items'] = [{k: v for k, v in t.items() if k != 'revenue'} for t in analytics.get('top_items') or []]
    items = MenuItem.query.order_by(MenuItem.category, MenuItem.name).all() if 'coupons' in tabs else []
    return render_template(
        'admin.html',
        is_admin=True,
        items=items,
        reward_items=[i for i in items if i.is_reward],
        reward_coupons=Coupon.query.all() if 'coupons' in tabs else [],
        coupon_batches=coupon_service.recent_batches(g.tenant_id) if 'coupons' in tabs else [],
        coupon_targets=coupon_service.TARGET_LABELS,
        member_tiers=wallet_service.tiers(g.tenant_id),
        line_push_ready=line_service.messaging_enabled(),
        today=date.today(),
        analytics=analytics,
        current_limit=limit,
        reward_setting=get_or_create_reward_setting(g.tenant_id),
        tenant=g.tenant,
        dashboard=_dashboard_summary(),
        tabs=tabs,   # 可見的分頁 (櫃檯 / 廚房依授權的頁面權限)
        **data_modal_context(),
    )


def _staff_login():
    username = request.form.get('username', '').strip()
    password = request.form.get('password') or ''
    keys = auth_throttle.staff_keys(username, request.remote_addr or '-')
    remaining = auth_throttle.staff_lock_remaining(keys)
    if remaining:  # 鎖定期間連正確密碼也不放行 (已註冊的店內裝置只受同 IP 的鎖定)
        minutes = max(1, int((remaining.total_seconds() + 59) // 60))
        flash(f'密碼錯誤次數過多，此帳號已暫時鎖定，請於 {minutes} 分鐘後再試。', 'danger')
        return render_template('admin.html', is_admin=False), 429
    staff = StaffAccount.query.filter_by(username=username).first()
    if staff is None:
        auth_throttle.spend_hash_time(password)
    if staff is None or not staff.check_password(password) or not staff.can_login:
        auth_throttle.record_staff_failure(keys)  # 帳號不存在也同樣計數，無法藉此列舉帳號
        flash('帳號或密碼錯誤！', 'danger')
        return render_template('admin.html', is_admin=False), 401
    if not staff.totp_enabled:
        # 啟用兩步驟驗證的帳號在驗證碼通過後才清除 (security.two_factor)：否則重新輸入一次密碼就能把驗證碼的錯誤次數歸零
        auth_throttle.clear_keys(keys['device'], keys['account'])
    if staff.totp_enabled:   # 兩步驟驗證：密碼正確後再輸入驗證 App 的 6 位數
        from app.services import security_service
        tenant_id = session.get('tenant_id')
        session.clear()
        if tenant_id is not None:
            session['tenant_id'] = tenant_id
        session[security_service.PENDING_KEY] = {'id': staff.id, 'v': staff.session_version, 'at': time.time()}
        return redirect(url_for('security.two_factor'))
    return complete_staff_login(staff)


def complete_staff_login(staff):
    """帳密 (與兩步驟驗證) 通過後：換新的工作階段並記錄登入"""
    from app.services import security_service
    # 換新的工作階段：清掉登入前的顧客 / 其他員工資料，只保留目前店家
    tenant_id = session.get('tenant_id')
    session.clear()
    if tenant_id is not None:
        session['tenant_id'] = tenant_id
    login_user(staff)
    session.permanent = True
    security_service.start_session(staff)
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
    from app.services import security_service
    security_service.end_current_session()
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
    # 門市主機預設不開放公開註冊 (任何連得到主機的人都能建店並取得負責人權限)；平台管理者登入後仍可建立
    if not current_app.config.get('ALLOW_TENANT_REGISTER') and not is_platform_admin():
        if request.method == 'GET':
            flash('本主機未開放公開建立店家：請以平台管理者 (預設店家負責人) 登入後再建立，或在 .env 設定 ALLOW_TENANT_REGISTER=1。',
                  'warning')
            return redirect(url_for('admin.admin_dashboard'))
        raise AppError('本主機未開放公開建立店家 (ALLOW_TENANT_REGISTER=0)', 403)
    if request.method == 'GET':
        return render_template('tenant_register.html')
    shop_name = request.form.get('shop_name', '').strip()
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '')
    slug = re.sub(r'[^a-z0-9-]+', '-', request.form.get('slug', '').strip().lower()).strip('-')
    if not shop_name or not username or len(password) < 10 or not slug:
        raise AppError('請填妥資料；密碼至少 10 個字元。')
    if len(shop_name) > 120 or len(slug) > 80 or len(username) > MAX_USERNAME_LEN:
        raise AppError(f'店名最多 120 字、網址代號最多 80 字、負責人帳號最多 {MAX_USERNAME_LEN} 字。')
    if Tenant.query.filter_by(slug=slug).first():
        raise AppError('店家網址代號已被使用。', 409)
    if StaffAccount.query.filter_by(username=username).first():
        raise AppError('員工帳號已被使用。', 409)

    try:
        tenant = Tenant(name=shop_name, slug=slug, is_active=True)
        db.session.add(tenant)
        db.session.flush()
        g.tenant, g.tenant_id = tenant, tenant.id
        owner = StaffAccount(tenant_id=tenant.id, username=username, role='owner', is_active=True)
        owner.set_password(password)
        db.session.add(owner)
        seed_tenant_defaults(tenant.id)
        db.session.commit()
    except IntegrityError:   # 兩人同時以同一個代號 / 帳號註冊：先查後插之間被搶先
        db.session.rollback()
        raise AppError('店家網址代號或員工帳號已被使用。', 409)
    if current_user.is_authenticated:   # 平台管理者替別人建店：不切換成新店的負責人
        flash(f'已建立店家「{tenant.name}」(代號 {tenant.slug})，負責人帳號 {owner.username}', 'success')
        return redirect(_dashboard())
    # 與一般登入相同：建立工作階段紀錄 (閒置逾時 / 單一裝置登出) 並寫入登入日誌 (不沿用註冊前瀏覽的店家)
    session.pop('tenant_id', None)
    return complete_staff_login(owner)


def _verify_current_credentials(acc, back):
    """變更自己的密碼前確認本人：目前的密碼，已啟用兩步驟驗證時再加驗證碼 (或備用碼)"""
    if not acc.check_password(request.form.get('current_password') or ''):
        raise AppError('❌ 變更自己的密碼需輸入正確的目前密碼！', redirect_to=back)
    if acc.totp_enabled:
        from app.services import security_service
        if not security_service.verify_second_factor(acc, (request.form.get('totp_code') or '').strip()):
            raise AppError('❌ 變更自己的密碼需輸入正確的兩步驟驗證碼 (或備用碼)！', redirect_to=back)


def _clear_staff_lockout(username):
    """解除員工帳號的登入鎖定 (帳號層級與各 IP 的裝置層級；鍵名與 auth_throttle.staff_keys 相同)"""
    from app.models.system import AuthThrottle
    keys = auth_throttle.staff_keys(username, '')   # IP 留白：device 鍵即為「staff:dev:<帳號>:」前綴
    db.session.execute(delete(AuthThrottle).where(or_(
        AuthThrottle.key == keys['account'],
        AuthThrottle.key.startswith(keys['device'], autoescape=True),
    )).execution_options(synchronize_session=False))


@admin_bp.route('/admin/staff', methods=['GET', 'POST'], endpoint='staff_accounts')
@role_required('owner')
def staff_accounts():
    if request.method == 'GET':
        from app.models.enums import PERMISSIONS, ROLE_DEFAULT_PERMISSIONS
        accounts = StaffAccount.query.filter_by(tenant_id=g.tenant_id, deleted_at=None).order_by(StaffAccount.id).all()
        return render_template('staff_accounts.html', accounts=accounts, tenant=g.tenant, current_staff_id=current_user.id,
                               permissions=PERMISSIONS, sync_node=sync_runtime.is_node(),   # 分店：負責人帳號只能在線上端修改
                               role_defaults={k: sorted(v) for k, v in ROLE_DEFAULT_PERMISSIONS.items()})

    back = url_for('admin.staff_accounts')
    action = request.form.get('action', 'create')
    if action == 'create':
        username = request.form.get('username', '').strip()
        role = request.form.get('role', 'staff')
        password = request.form.get('password', '')
        if role not in CREATABLE_STAFF_ROLES or not username or len(password) < 10:
            raise AppError('❌ 帳號必填，密碼至少 10 個字元，且角色需合法！', redirect_to=back)
        if len(username) > MAX_USERNAME_LEN:
            raise AppError(f'❌ 員工帳號最多 {MAX_USERNAME_LEN} 個字元！', redirect_to=back)
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
    if action in ('toggle_active', 'delete_account', 'reset_password') and acc.role == 'owner' and sync_runtime.is_node():
        # 負責人的帳密 / 啟用狀態只能在線上端修改：分店改了也會在同步時被線上端整列拒收，線上端仍接受舊密碼
        raise AppError('❌ 負責人帳號請到線上端修改 (分店的修改不會同步到線上端)', 403, redirect_to=back)
    if action == 'toggle_active':
        acc.is_active = not acc.is_active
        if not acc.is_active:
            from app.services import security_service, staffing_service
            # 遞增 session_epoch (同步的「安全版本」)：分店斷線期間的舊版本 (仍為啟用) 之後同步上來也不能把帳號改回啟用
            security_service.logout_everywhere(acc, '帳號已被停用')
            # 只刪今天起的班表，保留可排時段 (重新啟用後仍可自動排班)
            staffing_service.clear_future_schedules(g.tenant_id, acc.id)
        record_staff_audit('STAFF_TOGGLE_ACTIVE', details=f"{'啟用' if acc.is_active else '停用'}員工帳號：{acc.username} ({acc.role})")
    elif action == 'reset_password':
        new_pwd = request.form.get('new_password', '')
        if len(new_pwd) < 10:
            raise AppError('❌ 新密碼長度至少需 10 個字元！', redirect_to=back)
        if acc.id == current_user.id:
            # 變更自己的密碼需重新驗證：拿到未登出工作階段的人不能藉此永久奪走帳號
            _verify_current_credentials(acc, back)
        acc.set_password(new_pwd)
        from app.services import security_service
        # 遞增 session_epoch：分店斷線期間的舊密碼版本之後同步上來也不會蓋回新密碼
        security_service.logout_everywhere(acc, '密碼已被重設')
        _clear_staff_lockout(acc.username)   # 被連錯鎖定的員工拿到新密碼後可立即登入
        record_staff_audit('STAFF_RESET_PASSWORD', details=f"重設員工帳號密碼：{acc.username}")
        flash(f'已成功重設員工【{acc.username}】的密碼！', 'success')
    elif action == 'logout_all':
        from app.services import security_service
        security_service.logout_everywhere(acc, f'由 {current_user.username} 強制登出')
        record_staff_audit('STAFF_LOGOUT_ALL', details=f"強制登出員工 {acc.username} 的所有裝置")
        flash(f'【{acc.username}】已在所有裝置登出', 'success')
    elif action == 'reset_2fa':
        if acc.role == 'owner':
            raise AppError('❌ 負責人帳號的兩步驟驗證請以備用碼登入後自行重設', 403, redirect_to=back)
        acc.totp_secret, acc.totp_enabled, acc.totp_backup_codes, acc.totp_last_counter = '', False, '[]', 0
        record_staff_audit('STAFF_2FA_RESET', details=f"重設員工 {acc.username} 的兩步驟驗證")
        flash(f'已重設【{acc.username}】的兩步驟驗證', 'success')
    elif action == 'permissions':
        from app.models.enums import PERMISSIONS
        perms = sorted({p for p in request.form.getlist('perm') if p in PERMISSIONS})
        before = acc.permissions or ''
        acc.permissions = ','.join(perms)
        record_staff_audit('STAFF_PERMISSIONS', details=f"員工 {acc.username} 額外權限：{before or '無'} → {acc.permissions or '無'}")
        flash(f'已更新【{acc.username}】的權限', 'success')
    elif action == 'wage':
        wage = safe_int(request.form.get('hourly_wage'), 0)
        if not 0 <= wage <= 100000:
            raise AppError('時薪需介於 0 ~ 100,000', redirect_to=back)
        acc.hourly_wage = wage
        record_staff_audit('STAFF_WAGE', details=f"設定員工 {acc.username} 時薪 ${wage}")
        flash(f'已設定【{acc.username}】時薪 ${wage}', 'success')
    elif action == 'delete_account':
        # 軟刪除：保留帳號列 (稽核可追溯、id 不重用)，立即停用、使密碼失效並釋出帳號名稱
        record_staff_audit('STAFF_DELETE_ACCOUNT', details=f"刪除員工帳號：{acc.username} ({acc.role})，帳號ID={acc.id}")
        acc.is_active = False
        acc.deleted_at = datetime.now()
        acc.username = f"{acc.username[:90]}#deleted-{acc.id}"
        acc.set_password(secrets.token_urlsafe(32))
        from app.services import security_service, staffing_service
        # 遞增 session_epoch：分店的舊版本 (未刪除) 之後同步上來也不能還原帳號
        security_service.logout_everywhere(acc, '帳號已被刪除')
        # 刪除今天起的班表與該員工全部可排時段
        staffing_service.clear_future_schedules(g.tenant_id, acc.id, availability=True)
    else:
        raise AppError('不支援的操作', redirect_to=back)
    db.session.commit()
    return redirect(back)


# ==============================================================================
# 菜單、套餐、紅利商品
# ==============================================================================
def _store_uploaded_image(item):
    """存好上傳的新圖並換上 item.image_path；回傳被取代的舊圖路徑 (沒有換圖時回傳 None)，由 _commit_item_image 處理"""
    image = request.files.get('image')
    if not image or not image.filename:
        return None
    upload_dir = current_app.config['UPLOAD_FOLDER_MENU']
    old_path = item.image_path or ''
    item.image_path = menu_service.save_menu_image(upload_dir, g.tenant_id, image)
    return old_path


def _commit_item_image(item, old_path):
    """
    commit；換了圖時 commit 成功後才刪舊圖。交易失敗時資料庫仍指向舊圖：刪掉剛存的新圖 (不留孤兒檔)，舊圖保留。
    old_path 為 None 代表沒有換圖。
    """
    upload_dir = current_app.config['UPLOAD_FOLDER_MENU']
    new_path = item.image_path if old_path is not None else None
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        if new_path and new_path != old_path:
            remove_upload(upload_dir, new_path)
        raise
    if old_path and old_path != new_path:
        _remove_menu_image_if_unused(old_path, item.id)


def _remove_menu_image_if_unused(path, own_id):
    """
    刪除餐點圖片檔：還有其他餐點 (不分店家，排除 own_id) 使用同一路徑時保留。
    線上端可能有修正前上傳、跨店共用的圖片路徑，刪除或換圖時不可刪掉別家店的圖片。
    """
    other = db.session.execute(select(MenuItem.id).where(MenuItem.image_path == path, MenuItem.id != own_id)
                               .execution_options(skip_tenant_filter=True).limit(1)).first()
    if other is None:
        remove_upload(current_app.config['UPLOAD_FOLDER_MENU'], path)


@admin_bp.route('/admin/add', methods=['POST'])
@permission_required('menu')
def add_item():
    item = menu_service.apply_menu_form(MenuItem(tenant_id=g.tenant_id, image_path=''), request.form, form_flag)
    old_image = _store_uploaded_image(item)
    db.session.add(item)
    _commit_item_image(item, old_image)
    order_event_bus.notify(g.tenant_id)   # 數位看板 / 點餐機即時更新售完與價格
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/edit/<int:id>', methods=['POST'])
@permission_required('menu')
def edit_item(id):
    item = db.get_or_404(MenuItem, id)
    menu_service.apply_menu_form(item, request.form, form_flag)
    if current_user.has_permission('coupons'):
        # 紅利商城上下架屬於「優惠券 / 紅利商城」權限 (同 add_reward / remove_reward)；只有菜單權限時保留原設定
        menu_service.set_item_reward_status(item, form_flag('is_reward'), request.form.get('reward_points'),
                                            request.form.get('reward_discount_points'))
    old_image = _store_uploaded_image(item)
    _commit_item_image(item, old_image)
    order_event_bus.notify(g.tenant_id)   # 數位看板 / 點餐機即時更新售完與價格
    return redirect(_dashboard(request.form.get('from_tab') or ('rewards' if item.is_reward else 'menu')))


@admin_bp.route('/admin/delete/<int:id>', methods=['POST'])
@permission_required('menu')
def delete_item(id):
    item = db.get_or_404(MenuItem, id)
    # 寄杯綁定品項：刪除品項會連帶刪掉寄杯商品 (資料庫 CASCADE)，會員手上的寄杯也對不到品項而無法兌換 (預收的商品禮券)
    from app.models.member import MemberPass, PassProduct
    held = MemberPass.query.filter(MemberPass.menu_item_id == item.id, MemberPass.status == 'active',
                                   MemberPass.remaining > 0).count()
    if held:
        raise AppError(f'有 {held} 位會員的寄杯還沒兌換完 (綁定此品項)，不能刪除；請改用「停售 / 下架」')
    if PassProduct.query.filter_by(menu_item_id=item.id, is_active=True).first() is not None:
        raise AppError('此品項有販售中的寄杯商品，請先到「寄杯」停售該商品再刪除品項 (或改用下架)')
    # 其他套餐的固定配餐 / 任選選項：刪除會被資料庫連帶拿掉，套餐卻照原加價販售 (多收錢少給餐)
    from app.models.menu import ComboOptionItem
    combos = sorted({side.combo.name for side in ComboOptionItem.query.filter_by(menu_item_id=item.id).all()
                     if side.combo is not None and side.combo.main_item_id != item.id})
    if combos:
        raise AppError(f"此品項是套餐「{'」、「'.join(combos[:5])}」{' 等' if len(combos) > 5 else ''}的配餐 / 任選選項，"
                       f"不能刪除；請先編輯套餐移除此品項，或改用「停售 / 下架」")
    if item.image_path:
        _remove_menu_image_if_unused(item.image_path, item.id)
    record_staff_audit('DELETE_MENU_ITEM', details=f'刪除菜單品項：{item.name} (${item.price})')
    db.session.delete(item)
    db.session.commit()
    order_event_bus.notify(g.tenant_id)   # 數位看板 / 點餐機即時更新售完與價格
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/add_combo', methods=['POST'])
@permission_required('menu')
def add_combo():
    combo = menu_service.save_combo(ComboOption(tenant_id=g.tenant_id), request.form, form_flag)
    db.session.add(combo)
    record_staff_audit('COMBO_CHANGE', details=f"新增套餐：{combo.name} (主餐 #{combo.main_item_id}，加價 ${combo.additional_price})")
    db.session.commit()
    flash('成功為餐點新增套餐組合！', 'success')
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/edit_combo/<int:id>', methods=['POST'])
@permission_required('menu')
def edit_combo(id):
    combo = menu_service.save_combo(db.get_or_404(ComboOption, id), request.form, form_flag)
    record_staff_audit('COMBO_CHANGE', details=f"編輯套餐 #{combo.id}：{combo.name} (主餐 #{combo.main_item_id}，加價 ${combo.additional_price})")
    db.session.commit()
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/delete_combo/<int:id>', methods=['POST'])
@permission_required('menu')
def delete_combo(id):
    combo = db.get_or_404(ComboOption, id)
    record_staff_audit('COMBO_CHANGE', details=f"刪除套餐 #{combo.id}：{combo.name}")
    db.session.delete(combo)
    db.session.commit()
    return redirect(_dashboard('menu'))


@admin_bp.route('/admin/add_reward', methods=['POST'])
@permission_required('coupons')
def add_reward():
    item = db.get_or_404(MenuItem, safe_int(request.form.get('item_id')))
    menu_service.set_item_reward_status(item, True, request.form.get('reward_points'), request.form.get('reward_discount_points'))
    db.session.commit()
    return redirect(_dashboard('rewards'))


@admin_bp.route('/admin/remove_reward/<int:id>', methods=['POST'])
@permission_required('coupons')
def remove_reward_item(id):
    menu_service.set_item_reward_status(db.get_or_404(MenuItem, id), False)
    db.session.commit()
    return redirect(_dashboard('rewards'))


# ==============================================================================
# 優惠券
# ==============================================================================
def _apply_coupon_form(coupon):
    code = request.form.get('code', '').strip().upper()
    title = (request.form.get('title') or '').strip()[:100]
    if not code or not title:
        raise AppError('❌ 請輸入優惠券名稱與代碼！')
    code = coupon_service.validate_code(code)
    if Coupon.query.filter(Coupon.code == code, Coupon.id != coupon.id).first():
        raise AppError('❌ 代碼已被使用！')
    if coupon.id is not None and coupon.code != code:  # 改代碼時一併更新會員已領取的券
        db.session.execute(update(UserCoupon).where(UserCoupon.coupon_id == coupon.id).values(code=code)
                           .execution_options(synchronize_session=False))
    dtype = request.form.get('discount_type', DiscountType.FIXED)
    coupon.code, coupon.title = code, title
    coupon.discount_type = dtype if dtype in (DiscountType.FIXED, DiscountType.PERCENT) else DiscountType.FIXED
    raw_value = request.form.get('discount_value')
    value = parse_discount_value(coupon.discount_type, raw_value)
    if coupon.discount_type == DiscountType.PERCENT:
        # 0 / 負數 / 100 以上都不是有效折數 (舊版會夾成 1 → 0.1 折，等於折掉 99%)
        if value is None or safe_float(raw_value, 0) <= 0 or not 1 <= value <= 99:
            raise AppError('❌ 折數請輸入 0.1 ~ 9.9 折 (例如 85、8.5 或 0.85 都代表 85 折)！')
    elif not value or value <= 0:
        raise AppError('❌ 折抵金額必須大於 0！')
    coupon.discount_value = value
    coupon.min_spend = bounded_int(request.form.get('min_spend'), 0, 0, MAX_AMOUNT)
    coupon.reward_points = bounded_int(request.form.get('reward_points'), 0, 0, MAX_POINTS)
    coupon.reward_discount_points = bounded_int(request.form.get('reward_discount_points'), 0, 0, MAX_POINTS) \
        if coupon.reward_points else 0
    coupon.member_only = bool(request.form.get('member_only'))
    if coupon.member_only:   # 會員專屬券只能發放，不在紅利商城兌換
        coupon.reward_points = coupon.reward_discount_points = 0
    coupon.is_reward = coupon.reward_points > 0
    coupon.per_user_limit = excel_service.parse_per_user_limit(request.form.get('per_user_limit'))
    coupon.valid_from = coupon_service.parse_date(request.form.get('valid_from'))
    coupon.valid_until = coupon_service.parse_date(request.form.get('valid_until'))
    if coupon.valid_from and coupon.valid_until and coupon.valid_until < coupon.valid_from:
        raise AppError('❌ 活動結束日不可早於開始日！')
    coupon.valid_days = min(3650, max(0, safe_int(request.form.get('valid_days'), 0)))
    coupon.login_required = bool(request.form.get('login_required'))
    coupon.uses_per_member = min(1000, max(0, safe_int(request.form.get('uses_per_member'), 0)))
    coupon.max_discount = min(1000000, max(0, safe_int(request.form.get('max_discount'), 0)))
    return coupon


def _coupon_desc(c):
    kind = '會員專屬' if c.member_only else f'兌換點數:{c.reward_points}'
    limits = f"，限制:{c.usage_label}" if c.usage_label else ''
    return f"{c.title} ({c.code}，{c.discount_label}，門檻${c.min_spend}，{kind}，期限:{c.period_label}{limits})"


def _release_until_snapshot(coupon, old_until):
    """
    延長或取消活動結束日時：舊版發放會把當時的結束日 (23:59:59) 寫進持券的 expires_at，
    這些未使用的持券改用「領取後 N 天」的到期時間 (沒有設定天數或較早時清空，只受活動期間限制，由 status 動態比對新的 valid_until)。
    新發的券已不寫入結束日快照，這裡只處理既有資料。
    """
    from datetime import time as dt_time
    snap = datetime.combine(old_until, dt_time(23, 59, 59))
    for uc in UserCoupon.query.filter_by(coupon_id=coupon.id, is_used=False, expires_at=snap).all():
        new = coupon.expires_for(uc.created_at) if uc.created_at else None
        uc.expires_at = new if (new is not None and new > snap) else None


@admin_bp.route('/admin/add_coupon', methods=['POST'])
@permission_required('coupons')
def add_coupon():
    coupon = _apply_coupon_form(Coupon(tenant_id=g.tenant_id))
    db.session.add(coupon)
    record_staff_audit('COUPON_CHANGE', details=f"新增優惠券：{_coupon_desc(coupon)}")
    db.session.commit()
    return redirect(_dashboard('rewards'))


@admin_bp.route('/admin/edit_coupon/<int:id>', methods=['POST'])
@permission_required('coupons')
def edit_coupon(id):
    coupon = db.get_or_404(Coupon, id)
    before = _coupon_desc(coupon)
    old_until = coupon.valid_until
    _apply_coupon_form(coupon)
    if old_until and (coupon.valid_until is None or coupon.valid_until > old_until):
        _release_until_snapshot(coupon, old_until)
    record_staff_audit('COUPON_CHANGE', details=f"編輯優惠券：{before} → {_coupon_desc(coupon)}")
    db.session.commit()
    return redirect(_dashboard('rewards'))


@admin_bp.route('/admin/delete_coupon/<int:id>', methods=['POST'])
@permission_required('coupons')
def delete_coupon(id):
    coupon = db.get_or_404(Coupon, id)
    # 會員用點數兌換、還沒用掉的券：刪券會連帶刪掉持券 (資料庫 CASCADE)，會員付出的點數也不會退回
    now = datetime.now()
    redeemed = sum(1 for uc in coupon.user_coupons
                   if uc.source == 'reward' and uc.status(now) in ('available', 'upcoming'))
    if redeemed:
        raise AppError(f'❌ 有 {redeemed} 張會員用點數兌換、尚未使用的券 (刪除後點數不會退回)，不能刪除；'
                       f'請等會員使用完畢或券到期後再刪除', 409, redirect_to=_dashboard('rewards'))
    # 自動行銷活動引用這張券：刪券後外鍵被清空 (SET NULL)，只發券的活動之後每次執行都 0 人收到，狀態卻仍顯示啟用
    from app.models.marketing import MarketingCampaign
    campaign = MarketingCampaign.query.filter(MarketingCampaign.coupon_id == coupon.id,
                                              MarketingCampaign.status.in_(('active', 'paused', 'running'))).first()
    if campaign is not None:
        raise AppError(f'❌ 自動行銷活動「{campaign.name}」仍在使用這張優惠券，請先修改或刪除該活動！', 409,
                       redirect_to=_dashboard('rewards'))
    record_staff_audit('COUPON_CHANGE', details=f"刪除優惠券：{coupon.title} ({coupon.code})，連同會員已領取的 {len(coupon.user_coupons)} 張持券")
    db.session.delete(coupon)
    db.session.commit()
    return redirect(_dashboard('rewards'))


# ==============================================================================
# 會員
# ==============================================================================
@admin_bp.route('/admin/delete_user/<int:id>', methods=['POST'])
@permission_required('members')
def delete_user(id):
    user = db.get_or_404(User, id)
    from app.services import privacy_service, support_service
    # 已付款的儲值金 / 寄杯是店家對會員的負債：刪除會員會連帶刪掉異動帳與寄杯 (資料庫 CASCADE)，需先退費或用完
    if (user.wallet_balance or 0) > 0:
        raise AppError(f'❌ 會員【{user.name}】儲值金尚有 ${user.wallet_balance} 餘額，請先辦理退費或使用完畢再刪除', 409,
                       redirect_to=_dashboard('users'))
    from app.models.member import MemberPass
    if MemberPass.query.filter(MemberPass.user_id == user.id, MemberPass.status == 'active',
                               MemberPass.remaining > 0).first() is not None:
        raise AppError(f'❌ 會員【{user.name}】還有未兌換完的寄杯，請先辦理退費或使用完畢再刪除', 409,
                       redirect_to=_dashboard('users'))
    # 與會員自助刪除相同的去識別化：人臉特徵與照片、訂單上的姓名 (訂單保留，外鍵為 SET NULL)、外送地址、客服對話
    privacy_service.withdraw_face(user)
    privacy_service._deidentify_member_records(user)
    record_staff_audit('DELETE_USER', target_user_id=user.id, points_delta=-(user.points or 0),
                       details=f"刪除會員：{user.name} ({user.phone}, 原點數:{user.points})")
    support_service.forget_member(user.id)
    db.session.delete(user)
    db.session.commit()
    return redirect(_dashboard('users'))


@admin_bp.route('/admin/edit_user/<int:id>', methods=['POST'])
@permission_required('members')
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
    raw_points = bounded_int(request.form.get('points'), 0, -MAX_POINTS, MAX_POINTS)
    submitted = max(0, raw_points)
    loaded = request.form.get('loaded_points')
    if loaded not in (None, ''):
        # 開窗點數不可夾成 0：跨分店同時折抵後點數可能為負，差額要以實際的負數計算才會調到店員輸入的點數
        loaded_points = bounded_int(loaded, 0, -MAX_POINTS, MAX_POINTS)
        delta = 0 if raw_points == loaded_points else submitted - loaded_points   # 點數欄沒改 (含負點) 時不調整
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


@admin_bp.route('/admin/coupons/<int:coupon_id>/issue', methods=['POST'], endpoint='issue_coupon')
@permission_required('coupons')
def issue_coupon(coupon_id):
    """批量發放會員券"""
    coupon = get_own(Coupon, coupon_id, '優惠券')
    f = request.form
    users, desc, missing = coupon_service.resolve_targets(g.tenant_id, f.get('target'), safe_int(f.get('tier_id'), 0),
                                                          f.get('phones'))
    expire_mode = f.get('expire_mode') or 'coupon'
    expires_on = coupon_service.parse_date(f.get('expires_on')) if expire_mode == 'date' else None
    if expire_mode == 'date' and expires_on is None:
        raise AppError('❌ 請選擇到期日', redirect_to=_dashboard('rewards'))
    valid_days = bounded_int(f.get('valid_days'), 0, 1, 3650) if expire_mode == 'days' else None
    result = coupon_service.issue(g.tenant_id, coupon, users, safe_int(f.get('quantity'), 1), expires_on=expires_on,
                                  valid_days=valid_days, staff=current_user.username)
    until = f"，到期 {result['expires_at']:%Y/%m/%d}" if result['expires_at'] else '，無到期日'
    record_staff_audit('COUPON_ISSUE', details=f"批量發放「{coupon.title}」({coupon.code}) 給{desc}：{result['members']} 位會員共 "
                                               f"{result['issued']} 張{until}；超過每人上限略過 {len(result['skipped'])} 位；"
                                               f"批次 {result['batch_id'][:8]}")
    db.session.commit()
    notified = 0
    if f.get('notify_line') and result['issued']:
        notified = coupon_service.notify_line(g.tenant_id, result['user_ids'], coupon, result['expires_at'])
    msg = f"✅ 已發放「{coupon.title}」給 {result['members']} 位會員，共 {result['issued']} 張{until}"
    if result['skipped']:
        msg += f"；{len(result['skipped'])} 位已達每人上限未發放"
    if missing:
        msg += f"；找不到會員的手機：{'、'.join(missing[:10])}{' 等' if len(missing) > 10 else ''}"
    if notified:
        msg += f"；已 LINE 通知 {notified} 位"
    flash(msg, 'success' if result['issued'] else 'warning')
    return redirect(_dashboard('rewards'))


@admin_bp.route('/admin/coupon_batches/<batch_id>/revoke', methods=['POST'], endpoint='revoke_coupon_batch')
@permission_required('coupons')
def revoke_coupon_batch(batch_id):
    """撤回一次發放中尚未使用的券 (發錯對象時使用；已使用的保留)"""
    n = coupon_service.revoke_batch(g.tenant_id, str(batch_id)[:36])
    record_staff_audit('COUPON_REVOKE', details=f'撤回批次 {str(batch_id)[:8]} 尚未使用的會員券 {n} 張')
    db.session.commit()
    flash(f'已撤回 {n} 張尚未使用的券 (已使用的保留)', 'success')
    return redirect(_dashboard('rewards'))


@admin_bp.route('/admin/users/<int:user_id>/coupons', endpoint='user_coupons')
@permission_required('members')
def user_coupons(user_id):
    """會員票券視窗的資料 (開啟時才載入)"""
    user = db.get_or_404(User, user_id)
    return jsonify({'success': True, 'user_name': user.name, 'coupons': coupon_service.admin_view(user),
                    'unused': coupon_service.unused_count(user)})


@admin_bp.route('/admin/toggle_user_coupon/<int:uc_id>', methods=['POST'])
@permission_required('members')
def toggle_user_coupon(uc_id):
    uc = db.get_or_404(UserCoupon, uc_id)
    uc.is_used = not uc.is_used
    uc.used_at = datetime.now() if uc.is_used else None
    record_staff_audit('TOGGLE_USER_COUPON', target_user_id=uc.user_id,
                       details=f"手動將會員券 {uc.code} (持券 #{uc.id}) 切換為「{'已使用' if uc.is_used else '未使用'}」")
    db.session.commit()
    db.session.refresh(uc.user)
    return jsonify({'success': True, 'user_name': uc.user.name if uc.user else '',
                    'coupons': coupon_service.admin_view(uc.user), 'unused': coupon_service.unused_count(uc.user)})


@admin_bp.route('/admin/update_reward_setting', methods=['POST'])
@permission_required('coupons')
def update_reward_setting():
    s = get_or_create_reward_setting(g.tenant_id)
    before = (s.is_enabled, s.points_per_dollar, s.max_discount_per_order, s.spend_per_point)
    s.is_enabled = form_flag('is_enabled')
    s.points_per_dollar = bounded_int(request.form.get('points_per_dollar'), 1, 1, MAX_POINTS)
    s.max_discount_per_order = bounded_int(request.form.get('max_discount_per_order'), 0, 0, MAX_AMOUNT)
    s.spend_per_point = bounded_int(request.form.get('spend_per_point'), 1, 1, MAX_AMOUNT)
    after = (s.is_enabled, s.points_per_dollar, s.max_discount_per_order, s.spend_per_point)
    if before != after:
        record_staff_audit('UPDATE_REWARD_SETTING', details=f"紅利設定 (啟用/幾點折$1/單筆折抵上限/每幾元送1點)：{before} → {after}")
    db.session.commit()
    return redirect(_dashboard('rewards'))   # 表單在「回饋商城與優惠券」分頁 (只有優惠券權限的員工進不了會員頁)


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
    flash(f"系統備份還原完成！資料庫：{'已還原 (還原前的資料庫已另存為 database/menu.db.bak-時間)' if db_restored else '未更動'}；"
          f"會員相片 {photos} 張；餐點相片 {menu_photos} 張", 'success')
    return redirect(_dashboard())


# 各工作表需要的權限；None = 只限店長以上 (進出日誌含 IP、員工操作稽核：與稽核頁 audit.index 相同)。不在表內的一律不匯出
EXPORT_TABLE_PERMISSION = {'User': 'members', 'UserCoupon': 'members', 'Coupon': 'coupons', 'MenuItem': 'menu', 'Order': 'reports',
                           'OrderItem': 'reports', 'Analytics': 'reports', 'AdminLog': None}


def _can_export(table):
    if table not in EXPORT_TABLE_PERMISSION:
        return False
    perm = EXPORT_TABLE_PERMISSION[table]
    return current_user.role in MANAGER_ROLES if perm is None else current_user.has_permission(perm)


def _export_date(value, label):
    """匯出區間日期 (YYYY-MM-DD)；格式錯誤回 400，不讓 strptime 的 ValueError 變成 500"""
    if not value:
        return value
    try:
        datetime.strptime(value, '%Y-%m-%d')
    except ValueError:
        raise AppError(f'❌ {label}格式錯誤，請使用 YYYY-MM-DD！')
    return value


@admin_bp.route('/admin/export_excel', methods=['POST'])
@permission_required('menu')
def export_excel():
    # 資料表依頁面權限過濾：只有「菜單」權限的櫃檯不能匯出會員 / 訂單 (匯出按鈕在菜單分頁，整個路由以 menu 權限進入)
    tables = [t for t in request.form.getlist('tables') if _can_export(t)]
    if not tables:
        raise AppError('❌ 請至少勾選一個資料表 (且您有該資料的頁面權限)！')
    start = _export_date(request.form.get('start_date'), '開始日期')
    end = _export_date(request.form.get('end_date'), '結束日期')
    suffix = f"_{start}_to_{end}" if start and end else ''
    return send_xlsx(excel_service.generate_excel_export(tables, start, end),
                     f"Kiosk_Export_{datetime.now():%Y%m%d_%H%M%S}{suffix}.xlsx")


@admin_bp.route('/admin/import_smart', methods=['POST'])
@permission_required('menu')
def import_smart():
    file = request.files.get('file')
    if not file or not file.filename:
        raise AppError('❌ 請選擇 Excel / CSV 檔案！')
    # 會員 / 優惠券工作表需要對應的頁面權限 (只有菜單權限的櫃檯不能匯入會員資料)
    counts = excel_service.import_workbook(file, allow_users=current_user.has_permission('members'),
                                           allow_coupons=current_user.has_permission('coupons'))
    flash(f"智慧匯入完成！會員 {counts['User']} 筆、菜單 {counts['MenuItem']} 筆、優惠券 {counts['Coupon']} 筆", 'success')
    order_event_bus.notify(g.tenant_id)
    return redirect(_dashboard('menu'))


# ==============================================================================
# 前端即時資料與 AI 文案
# ==============================================================================
@admin_bp.route('/api/admin_live_orders')
@permission_required('reports')
def api_admin_live_orders():
    from app.models.enums import OrderStatus
    orders = analytics_service.load_dashboard_orders()
    start, end = todays_range()
    # 今天建立、預約今天取餐，以及前幾天遺留的製作中訂單 (總覽「超過預估時間仍未出餐」連到這個看板)
    today = [o for o in orders if (o.created_at and start <= o.created_at < end)
             or (o.pickup_at and start <= o.pickup_at < end)
             or (o.status == OrderStatus.PENDING and o.created_at and o.created_at < start)]
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
            'refunded_amount': o.refunded_amount or 0,
            'items': [i.to_dict() for i in o.items],
        } for o in today],
        'menu_items': [{'id': m.id, 'stock': m.stock or 0, 'total_stock': m.total_stock or 0,
                        'is_sold_out': not m.is_available} for m in MenuItem.query.all()],
        'analytics': _analytics(orders, limit),
    })


@admin_bp.route('/api/admin/menu_items/<int:id>/stock')
@permission_required('menu')
def api_menu_item_stock(id):
    """菜單頁「編輯品項」開啟時取目前庫存：沒有「報表」權限的人頁面不會即時同步，用載入當下的庫存當差額基準會存錯"""
    item = get_own(MenuItem, id, '品項')
    return jsonify({'success': True, 'id': item.id, 'stock': item.stock or 0, 'total_stock': item.total_stock or 0,
                    'is_sold_out': bool(item.is_sold_out)})


@admin_bp.route('/api/generate_item_description', methods=['POST'])
@permission_required('menu')
def api_generate_item_description():
    data = get_json_dict()
    name = str(data.get('name', '')).strip()
    if not name:
        raise AppError('請先輸入餐點名稱！')
    return jsonify({'success': True, 'description': ai_service.generate_menu_description(name, str(data.get('category', '')).strip())})
