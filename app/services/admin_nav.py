"""
後台共用側欄 (所有 admin-page 頁面)：依工作情境分七組，依角色 / 頁面權限 / 主機角色 (單機、總部、分店) 過濾，
並標示目前所在的頁面。側欄資料由 build_nav() 產生，base.html 在後台頁面 include _admin_sidebar.html。

每個項目：(標籤, 端點, 圖示, 條件)。條件：
- None：登入的員工都可看 (實際頁面仍由各路由的 role_required / permission_required 把關)
- 'perm:xxx'：需要該頁面權限 (current_user.has_permission)
- 'role:owner' / 'role:manager' (店長以上) / 'role:pos' (櫃檯以上) / 'role:kitchen' (廚房以上)
- 'sync:hub' / 'sync:node' / 'sync:store' (單機或分店)
- 'platform'：平台管理者；'register'：允許公開註冊店家
以 dict 參數 tab='orders' 的項目會連到後台首頁的對應分頁 (/admin?tab=orders)。
"""
from flask import current_app, request, url_for
from flask_login import current_user

from app.sync import runtime as sync_runtime

ROLE_RANK = {'kitchen': 1, 'staff': 1, 'manager': 2, 'owner': 3}

NAV_GROUPS = (
    ('today', '今日營運', 'bi-sun', (
        ('總覽', 'admin.admin_dashboard', 'bi-speedometer2', None, {}),
        ('實時訂單', 'admin.admin_dashboard', 'bi-receipt-cutoff', 'perm:reports', {'tab': 'orders'}),
        ('即時看板', 'live.index', 'bi-broadcast', 'role:manager', {}),
        ('櫃檯 POS', 'pos.pos_terminal', 'bi-pc-display-horizontal', 'role:pos', {'_blank': True}),
        ('廚房 KDS', 'kitchen.kitchen_display', 'bi-fire', 'role:kitchen', {'_blank': True}),
        ('取餐核銷', 'pickup.pickup_management', 'bi-bag-check', 'role:pos', {'_blank': True}),
        ('叫號看板', 'pickup.counter_display', 'bi-display', None, {'_blank': True}),
        ('數位看板', 'signage.board', 'bi-tv', None, {'_blank': True}),
    )),
    ('menu', '菜單', 'bi-grid-fill', (
        ('菜單與套餐', 'menu_pro.manage', 'bi-grid-fill', 'perm:menu', {}),
        ('菜單工程分析', 'reports.menu_engineering', 'bi-grid-3x3-gap', 'perm:reports', {}),
        ('原物料與配方', 'inventory.manage', 'bi-box-seam', 'perm:inventory', {}),
        ('供應商與採購', 'purchasing.index', 'bi-truck', 'perm:inventory', {}),
        ('備料預測', 'purchasing.forecast', 'bi-graph-up', 'perm:inventory', {}),
        ('分店叫貨 / 調撥', 'branches.transfers', 'bi-arrow-left-right', 'perm:inventory', {}),
    )),
    ('customers', '顧客與行銷', 'bi-people-fill', (
        ('會員', 'members_pro.manage', 'bi-people-fill', 'perm:members', {}),
        ('回饋商城與優惠券', 'admin.admin_dashboard', 'bi-gift-fill', 'perm:coupons', {'tab': 'rewards'}),
        ('促銷活動', 'promotions.manage', 'bi-tags', 'perm:coupons', {}),
        ('集點 / 寄杯', 'passes.index', 'bi-cup-hot', 'role:manager', {}),
        ('禮物卡', 'gift_cards.index', 'bi-gift', 'role:manager', {}),
        ('分群與自動行銷', 'marketing.index', 'bi-send', 'role:manager', {}),
        ('顧客評價', 'feedback.index', 'bi-star-half', 'perm:feedback', {}),
    )),
    ('channels', '通路', 'bi-signpost-split', (
        ('桌位管理', 'tables.manage', 'bi-qr-code', 'role:manager', {'sync': 'store'}),
        ('外送平台', 'delivery.manage', 'bi-bicycle', 'role:manager', {}),
        ('外送派車', 'dispatch.board', 'bi-scooter', 'role:pos', {}),
        ('訂位 / 候位', 'reservations.board', 'bi-calendar-check', 'perm:reservations', {}),
        ('團體預購', 'group_orders.admin', 'bi-people', 'role:manager', {}),
    )),
    ('people', '人員', 'bi-person-badge', (
        ('員工帳號', 'admin.staff_accounts', 'bi-person-vcard', 'role:owner', {}),
        ('打卡與排班', 'timeclock.admin', 'bi-person-badge', 'role:manager', {}),
        ('可排時段', 'timeclock.availability', 'bi-calendar-week', 'role:manager', {}),
        ('排班建議', 'timeclock.staffing', 'bi-people', 'role:manager', {}),
        ('自動排班', 'timeclock.auto_schedule', 'bi-magic', 'role:manager', {}),
        ('交班與日結', 'shifts.list_shifts', 'bi-cash-coin', 'perm:cash_drawer', {}),
    )),
    ('reports', '報表', 'bi-bar-chart-line', (
        ('營運報表', 'reports.index', 'bi-bar-chart-line', 'perm:reports', {}),
        ('損益表', 'pnl.statement', 'bi-cash-stack', 'perm:reports', {}),
        ('營業費用', 'pnl.expenses', 'bi-wallet2', 'role:manager', {}),
        ('成本 / 售價歷史', 'pnl.cost_history', 'bi-clock-history', 'perm:reports', {}),
        ('會計匯出', 'reports.accounting', 'bi-journal-text', 'perm:reports', {}),
        ('電子發票', 'invoices.list_invoices', 'bi-receipt', 'role:manager', {}),
        ('作廢與退款', 'refunds.list_refunds', 'bi-arrow-counterclockwise', 'role:manager', {}),
        ('員工異常偵測', 'reports.staff_anomalies', 'bi-shield-exclamation', 'perm:reports', {}),
        ('稽核紀錄', 'audit.index', 'bi-shield-check', 'role:manager', {}),
    )),
    ('system', '系統', 'bi-gear', (
        ('系統設定', 'settings_admin.index', 'bi-sliders', 'perm:settings', {'id': 'nav-settings'}),
        ('分店管理', 'sync_admin.nodes', 'bi-diagram-3', 'role:owner', {'sync': 'hub', 'id': 'nav-sync-nodes'}),
        ('分店價格 / 上下架', 'branches.menu', 'bi-shop', 'role:manager', {'sync': 'hub'}),
        ('同步狀態', 'sync_admin.sync_status', 'bi-arrow-repeat', 'role:manager', {'sync': 'node', 'id': 'nav-sync-status'}),
        ('印表機與工作站', 'printing.manage', 'bi-printer', 'role:manager', {}),
        ('裝置管理', 'devices.admin', 'bi-display', 'role:manager', {}),
        ('API 與 Webhook', 'integrations.index', 'bi-plug', 'role:owner', {}),
        ('系統監控與備份', 'system.status', 'bi-activity', 'role:manager', {}),
        ('舊資料封存', 'archive.index', 'bi-archive', 'role:owner', {}),
        ('隱私權告知', 'privacy.settings', 'bi-shield-lock', 'role:manager', {'id': 'nav-privacy'}),
        ('帳號安全', 'security.account', 'bi-shield-lock', None, {'id': 'nav-security'}),
        ('店家管理', 'tenants.index', 'bi-buildings', 'platform', {}),
        ('建立新店家', 'admin.tenant_register', 'bi-shop', 'platform', {'id': 'nav-tenant-register'}),
    )),
)

# 舊標頭按鈕的 id (測試與自動化腳本用)，對應到側欄項目
LEGACY_IDS = {
    ('menu_pro.manage', ''): 'nav-menu-pro', ('promotions.manage', ''): 'nav-promotions',
    ('members_pro.manage', ''): 'nav-members-pro', ('feedback.index', ''): 'nav-feedback', ('live.index', ''): 'nav-live',
    ('tables.manage', ''): 'nav-tables',
}


def _allowed(cond, extra):
    user = current_user
    role = getattr(user, 'role', '')
    if cond is None:
        ok = True
    elif cond.startswith('perm:'):
        ok = user.has_permission(cond[5:])
    elif cond == 'role:owner':
        ok = role == 'owner'
    elif cond == 'role:manager':
        ok = ROLE_RANK.get(role, 0) >= 2
    elif cond == 'role:pos':
        ok = role in ('owner', 'manager', 'staff')
    elif cond == 'role:kitchen':
        ok = role in ('owner', 'manager', 'kitchen')
    elif cond == 'platform':
        from app.utils import is_platform_admin
        ok = is_platform_admin()
    else:
        ok = False
    if not ok:
        return False
    sync = extra.get('sync')
    if sync:
        role_now = sync_runtime.role()
        if sync == 'store' and role_now == 'hub':
            return False
        if sync in ('hub', 'node') and role_now != sync:
            return False
    return True


def _active(endpoint, extra):
    if request.endpoint != endpoint:
        # 設定區塊編輯頁、子頁面 (同一個 blueprint) 也算在該項目之下
        return endpoint == 'settings_admin.index' and (request.blueprint == 'settings_admin')
    tab = extra.get('tab')
    current_tab = request.args.get('tab') or ''
    if endpoint == 'admin.admin_dashboard':
        return (tab or '') == current_tab
    return True


def build_nav():
    """[{key, label, icon, items: [{label, url, icon, active, blank, id}]}]，只含目前使用者可看的項目與非空群組"""
    if not getattr(current_user, 'is_authenticated', False):
        return []
    endpoints = current_app.view_functions
    groups = []
    for key, label, icon, items in NAV_GROUPS:
        rows = []
        for item_label, endpoint, item_icon, cond, extra in items:
            if endpoint not in endpoints or not _allowed(cond, extra):
                continue
            if endpoint == 'admin.tenant_register' and current_app.config.get('ALLOW_TENANT_REGISTER'):
                continue   # 開放公開註冊時由登入頁提供，側欄不重複
            kwargs = {'tab': extra['tab']} if extra.get('tab') else {}
            rows.append({
                'label': item_label, 'icon': item_icon, 'url': url_for(endpoint, **kwargs),
                'active': _active(endpoint, extra), 'blank': bool(extra.get('_blank')),
                'id': extra.get('id') or LEGACY_IDS.get((endpoint, ''), ''),
            })
        if rows:
            groups.append({'key': key, 'label': label, 'icon': icon, 'items': rows})
    return groups


def register(app):
    @app.context_processor
    def _inject_admin_nav():
        return {'admin_nav': build_nav}
