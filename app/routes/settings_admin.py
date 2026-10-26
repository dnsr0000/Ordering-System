"""後台「系統設定」：各項外部服務金鑰與營運規則 (欄位定義見 app/services/settings_service.py)"""
from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user

from app.errors import AppError
from app.extensions import db
from app.services import settings_service
from app.services.audit import record_staff_audit
from app.sync import runtime as sync_runtime
from app.utils import role_required

settings_bp = Blueprint('settings_admin', __name__)
MANAGERS = ('owner', 'manager')

# 系統設定首頁的功能入口：(群組, 名稱, endpoint, 圖示, 說明, 需要的權限 (None = 店長以上))
FEATURE_PAGES = (
    ('A 金流與法規', '電子發票', 'invoices.list_invoices', 'bi-receipt', '開立紀錄、重送、作廢、列印', None),
    ('A 金流與法規', '作廢與退款紀錄', 'refunds.list_refunds', 'bi-arrow-counterclockwise', '退款明細、待人工退款', None),
    ('B 收銀營運', '交班與日結', 'shifts.list_shifts', 'bi-cash-coin', '開班零用金、現金盤點、帳差、日結報表', 'cash_drawer'),
    ('B 收銀營運', '印表機與廚房工作站', 'printing.manage', 'bi-printer', '網路熱感印表機、分站出單、列印紀錄', None),
    ('D 菜單', '菜單進階設定', 'menu_pro.manage', 'bi-clock-history', '時段供應、通路價、過敏原、多語系', None),
    ('D 菜單', '原物料與配方', 'inventory.manage', 'bi-box-seam', '原料、配方、進貨、盤點、低庫存、批號效期', 'inventory'),
    ('D 菜單', '供應商與採購單', 'purchasing.index', 'bi-truck', '低庫存建議採購、送出、收貨入庫', 'inventory'),
    ('D 菜單', '備料預測', 'purchasing.forecast', 'bi-graph-up', '依過去同星期幾預估份數與原料需求', 'inventory'),
    ('D 菜單', '分店叫貨 / 調撥', 'branches.transfers', 'bi-arrow-left-right', '向總部中央廚房或其他分店叫貨、分店間調撥原料', 'inventory'),
    ('D 菜單', '菜單工程分析', 'reports.menu_engineering', 'bi-grid-3x3-gap', '人氣 × 毛利四象限：明星、金牛、潛力、瘦狗', 'reports'),
    ('E 會員行銷', '會員經營', 'members_pro.manage', 'bi-award', '會員等級、生日禮、儲值、LINE 推播', None),
    ('E 會員行銷', '會員分群與自動行銷', 'marketing.index', 'bi-people', 'RFM 分群、久未消費自動發券、排程推播、回購成效', None),
    ('E 會員行銷', '促銷活動', 'promotions.manage', 'bi-tags', '買 X 送 Y、第 N 件折扣、滿額折、滿額贈', None),
    ('E 會員行銷', '顧客評價', 'feedback.index', 'bi-star-half', '餐後滿意度、低分留言、平均星數', None),
    ('E 會員行銷', '禮物卡', 'gift_cards.index', 'bi-gift', '售出的禮物卡、餘額、作廢', None),
    ('E 會員行銷', '寄杯 / 多次券', 'passes.index', 'bi-cup-hot', '寄杯商品、已售出的寄杯與剩餘次數', None),
    ('F 通路', '團體預購', 'group_orders.admin', 'bi-people-fill', '主揪分享連結各自點餐，截止合併成一張預約單；分裝清單', None),
    ('F 通路', '外送平台', 'delivery.manage', 'bi-bicycle', 'Webhook 網址、平台訂單紀錄', None),
    ('F 通路', '外送派車', 'dispatch.board', 'bi-scooter', '自有外送：指派外送員、出發、送達', None),
    ('F 通路', '訂位 / 候位', 'reservations.board', 'bi-calendar-check', '今日訂位、現場候位叫號', 'reservations'),
    ('G 報表與總部', '即時看板', 'live.index', 'bi-speedometer2', '今日各分店營業額、連線狀態、熱銷品項', None),
    ('G 報表與總部', '分店價格 / 上下架', 'branches.menu', 'bi-shop', '總部為各分店設定不同售價或下架餐點', None),
    ('G 報表與總部', '會計匯出', 'reports.accounting', 'bi-journal-text', '日結傳票、發票明細 CSV / Excel (匯入會計軟體)', 'reports'),
    ('G 報表與總部', '員工異常偵測', 'reports.staff_anomalies', 'bi-shield-exclamation', '作廢 / 退款、折扣 / 改價、交班帳差異常排行', 'reports'),
    ('G 報表與總部', '損益表', 'pnl.statement', 'bi-cash-stack', '營收 − 食材成本 − 人事 − 手續費 − 營業費用 = 淨利、近 6 個月趨勢', 'reports'),
    ('G 報表與總部', '營業費用', 'pnl.expenses', 'bi-wallet2', '房租、水電、耗材等費用登錄，固定費用每月一鍵複製', None),
    ('G 報表與總部', '成本 / 售價歷史', 'pnl.cost_history', 'bi-clock-history', '原料成本與餐點售價變動紀錄、成本上漲對毛利的影響', 'reports'),
    ('G 報表與總部', '營運報表', 'reports.index', 'bi-bar-chart-line', '毛利、時段熱度、分店比較、人事成本率、Excel', 'reports'),
    ('G 報表與總部', '排班建議', 'timeclock.staffing', 'bi-people', '依各時段預估訂單數建議人力與班別、預估人事成本率', None),
    ('G 報表與總部', '打卡與排班', 'timeclock.admin', 'bi-person-badge', '出勤紀錄、班表、工時統計', None),
    ('H 維運', '裝置管理', 'devices.admin', 'bi-display', '點餐機 / POS / 廚房看板註冊、在線與版本、遠端鎖定', None),
    ('H 維運', 'API 與 Webhook', 'integrations.index', 'bi-plug', 'API 金鑰、訂單事件 Webhook (串接 ERP / 會計 / 自建 App)', None),
    ('H 維運', '舊資料封存', 'archive.index', 'bi-archive', '超過 N 個月的訂單與日誌搬到封存檔、下載與查詢', None),
    ('H 維運', '系統監控', 'system.status', 'bi-activity', '健康狀態、錯誤紀錄、備份、時鐘誤差', None),
)


def _section(key):
    section = settings_service.SECTIONS.get(key)
    if section is None:
        abort(404)
    has_secret = any(f.type == 'secret' for f in section.fields)
    if has_secret and current_user.role != 'owner':
        raise AppError('此設定含金鑰，只有負責人可以檢視與修改', 403)
    return section


@settings_bp.route('/admin/settings', endpoint='index')
@role_required(*MANAGERS)
def index():
    endpoints = current_app.view_functions
    pages = [dict(group=g_, name=n, url=url_for(e) if e in endpoints else '', icon=i, desc=d)
             for g_, n, e, i, d, _perm in FEATURE_PAGES]
    sections = [dict(key=s.key, title=s.title, group=s.group, icon=s.icon, description=s.description,
                     enabled=settings_service.is_enabled(s.key) if s.enabled_field else None,
                     owner_only=any(f.type == 'secret' for f in s.fields),
                     source=settings_service.source(s.key) if sync_runtime.is_node() else '',
                     hq=settings_service.hq_managed(s.key))
                for s in settings_service.SECTIONS.values()]
    return render_template('admin_settings.html', groups=settings_service.GROUPS, pages=pages, sections=sections)


@settings_bp.route('/admin/settings/<key>', methods=['GET', 'POST'], endpoint='edit')
@role_required(*MANAGERS)
def edit(key):
    section = _section(key)
    if request.method == 'POST':
        if request.form.get('action') == 'revert_hq':
            if not sync_runtime.is_node():
                abort(404)
            settings_service.revert_to_hq(key)
            record_staff_audit('SETTINGS_CHANGE', details=f'系統設定「{section.title}」改回使用總部設定')
            db.session.commit()
            flash(f'✅「{section.title}」已改回使用總部設定', 'success')
            return redirect(url_for('settings_admin.edit', key=key))
        values = settings_service.parse_form(key, request.form)
        settings_service.put(key, values, changed_by=current_user.username)
        published = sync_runtime.is_hub() and settings_service.publish_hq(key, changed_by=current_user.username) is not None
        changed = ', '.join(f.label for f in section.fields if f.type != 'secret')
        record_staff_audit('SETTINGS_CHANGE', details=f'修改系統設定「{section.title}」({changed[:300]})'
                                                      + ('；已下發到所有分店' if published else ''))
        db.session.commit()
        flash(f'✅ 已儲存「{section.title}」' + ('，並下發到所有分店 (分店自訂的除外)' if published else ''), 'success')
        return redirect(url_for('settings_admin.edit', key=key))
    managed = settings_service.hq_managed(key)
    return render_template('admin_setting_form.html', section=section, values=settings_service.form_view(key),
                           can_test=key in settings_service.TESTERS, hq_managed=managed,
                           setting_source=settings_service.source(key), is_node=sync_runtime.is_node(),
                           is_hub=sync_runtime.is_hub())


@settings_bp.route('/admin/settings/<key>/test', methods=['POST'], endpoint='test')
@role_required(*MANAGERS)
def test(key):
    _section(key)
    tester = settings_service.TESTERS.get(key)
    if tester is None:
        abort(404)
    try:
        message = tester(settings_service.get(key))
        flash(f'✅ 測試成功：{message}', 'success')
    except AppError as err:
        db.session.rollback()
        flash(f'❌ 測試失敗：{err.message}', 'danger')
    return redirect(url_for('settings_admin.edit', key=key))
