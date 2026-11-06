"""後台共用版面：側欄七個群組依角色 / 權限 / 主機角色過濾、所在頁面標示、總覽分頁 (含今日數字與熱門排行)、
菜單 / 會員 / 稽核各自的頁面、系統設定左欄與搜尋資料、側欄固定 id"""
import re

from app.extensions import db
from app.models.tenant import StaffAccount


def _groups(html):
    return re.findall(r'data-nav-group="([a-z]+)"', html)


def _nav_labels(html):
    nav = html.split('id="adminSidebar"')[1].split('</nav>')[0]
    return re.findall(r'class="admin-nav-link[^"]*"[^>]*>\s*<i class="bi [^"]+"></i><span>([^<]+)</span>', nav)


def test_sidebar_groups_follow_role(app, admin_client, manager_client, staff_client, kitchen_client):
    html = admin_client.get('/admin').get_data(as_text=True)
    assert 'id="adminSidebar"' in html and 'id="adminTopbar"' in html and 'has-admin-nav' in html
    assert _groups(html) == ['today', 'menu', 'customers', 'channels', 'people', 'reports', 'system'], '負責人看到七個群組'
    labels = _nav_labels(html)
    for must in ('總覽', '菜單與套餐', '員工帳號', '系統設定', '自動排班', '稽核紀錄', 'API 與 Webhook', '帳號安全'):
        assert must in labels, must
    assert '分店管理' not in labels and '同步狀態' not in labels, '單機模式沒有分店同步項目'
    assert 'id="nav-settings"' in html and 'id="nav-live"' in html and 'id="nav-menu-pro"' in html, '側欄固定 id 保留給測試 / 腳本'
    assert 'navbar-admin' not in html, '舊的整排標頭按鈕已移除'

    html = manager_client.get('/admin').get_data(as_text=True)
    labels = _nav_labels(html)
    assert '員工帳號' not in labels and 'API 與 Webhook' not in labels and '系統設定' in labels, '店長：沒有負責人專屬項目'

    # 櫃檯 (預設權限 cash_drawer / reservations / wallet / delivery，沒有後台分頁) 直接導向 POS；授權 menu 後可進後台
    assert staff_client.get('/admin').status_code == 302
    with app.app_context():
        acc = StaffAccount.query.filter_by(username='pos_user').first()
        acc.permissions = 'menu'
        db.session.commit()
    try:
        html = staff_client.get('/admin').get_data(as_text=True)
        groups = _groups(html)
        labels = _nav_labels(html)
        assert 'reports' not in groups and 'system' in groups, '櫃檯沒有報表群組；系統群組只剩帳號安全'
        assert labels.count('帳號安全') == 1 and '系統設定' not in labels and '會員' not in labels
        assert '菜單與套餐' in labels and '外送派車' in labels and '交班與日結' in labels
        assert 'id="nav-back-work"' in html, '非店長有「回工作畫面」'
    finally:
        with app.app_context():
            acc = StaffAccount.query.filter_by(username='pos_user').first()
            acc.permissions = ''
            db.session.commit()
    assert kitchen_client.get('/admin').status_code == 302, '廚房帳號沒有後台分頁 → 導向 KDS'


def test_sidebar_marks_current_page_and_hides_for_login(app, admin_client):
    html = admin_client.get('/admin/inventory').get_data(as_text=True)
    active = re.findall(r'class="admin-nav-link active"[^>]*>\s*<i class="bi [^"]+"></i><span>([^<]+)</span>', html)
    assert active == ['原物料與配方']
    html = admin_client.get('/admin/menu').get_data(as_text=True)
    assert re.findall(r'class="admin-nav-link active"[^>]*>\s*<i class="bi [^"]+"></i><span>([^<]+)</span>', html) == ['菜單與套餐']
    html = admin_client.get('/admin/members').get_data(as_text=True)
    assert re.findall(r'class="admin-nav-link active"[^>]*>\s*<i class="bi [^"]+"></i><span>([^<]+)</span>', html) == ['會員']
    html = admin_client.get('/admin/settings/pos').get_data(as_text=True)
    assert re.findall(r'class="admin-nav-link active"[^>]*>\s*<i class="bi [^"]+"></i><span>([^<]+)</span>', html) == ['系統設定']
    login = app.test_client().get('/admin').get_data(as_text=True)
    assert 'id="adminSidebar"' not in login and 'name="password"' in login, '未登入的登入頁沒有側欄'
    pos = admin_client.get('/pos').get_data(as_text=True)
    assert 'id="adminSidebar"' not in pos, '工作畫面 (POS) 不套後台殼'


def test_dashboard_home_tab_and_pending_items(app, admin_client, manager_client):
    html = admin_client.get('/admin').get_data(as_text=True)
    assert 'id="home-tab"' in html and 'id="home-pane"' in html and 'id="homeKpis"' in html
    assert html.index('id="home-pane"') < html.index('id="orders-pane"'), '總覽是第一個分頁'
    assert 'id="homePending"' in html and 'id="homeQuickLinks"' in html and 'id="homeTopItems"' in html
    assert '今日訂單' in html and '今日營收' in html and '常用功能' in html
    assert all(f'id="stat-{k}"' in html for k in ('today-orders', 'today-revenue', 'pending-count', 'eta-minutes', 'top-items',
                                                      'avg-order-time', 'ai-advice')), '營運分析的數字併入總覽並即時更新'
    assert 'id="orders-pane"' in html and 'id="rewards-pane"' in html and 'reward-setting-card' in html, '實時訂單與回饋商城 (含紅利折抵設定) 留在首頁'
    for gone in ('analytics', 'menu', 'users', 'audit'):
        assert f'id="{gone}-pane"' not in html, f'{gone} 分頁已併入其他頁面'
    assert 'id="unifiedExportModal"' in html and 'id="unifiedImportModal"' in html, '匯入匯出保留'
    assert 'id="home-pane"' in manager_client.get('/admin').get_data(as_text=True)


def test_menu_and_member_pages(app, admin_client):
    """菜單、會員各自一頁 (品項清單、套餐與進階設定同一頁；匯入 / 匯出只在後台總覽)"""
    menu = admin_client.get('/admin/menu').get_data(as_text=True)
    assert 'id="menuItemTable"' in menu and 'id="comboTable"' in menu and 'id="addItemModal"' in menu and 'menuTranslateForm' in menu
    assert 'name="avail_start"' in menu, '品項清單、套餐與進階設定同一頁'
    assert 'unifiedImportModal' not in menu and 'unifiedExportModal' not in menu, '匯入 / 匯出只放在後台總覽'
    assert admin_client.get('/admin/members').status_code == 200
    members = admin_client.get('/admin/members').get_data(as_text=True)
    assert 'id="memberTable"' in members and 'id="memberTiersCard"' in members and 'id="editUserModal"' in members


def test_settings_hub_two_columns_and_search_data(app, admin_client):
    html = admin_client.get('/admin/settings').get_data(as_text=True)
    assert 'id="settingsNav"' in html and 'id="settingsSearch"' in html and 'id="settingsCards"' in html
    assert 'js/settings-hub.js' in html
    assert 'data-setting-key="einvoice"' in html and 'data-setting-key="chatbot"' in html
    assert '顧客通知' in html and '收款' in html and '財務' in html, '子群組標題'
    assert re.search(r'data-search="[^"]*綠界[^"]*"', html), '搜尋關鍵字含同義詞'
    assert '相關功能頁' in html and '原物料與配方' in html
    form = admin_client.get('/admin/settings/kds').get_data(as_text=True)
    assert 'id="settingsNav"' in form and 'class="settings-nav-link active"' in form and 'data-setting-key="kds"' in form
    assert 'id="settingForm"' in form and 'name="warn_minutes"' in form, '表單原樣保留'
