"""修正小組 pos-frontend-b-1：POS 掛單 / 桌位結帳 / 當日訂單 (pos/live.js) 與作廢退款 / 分開結帳 (pos/split.js) 的回歸檢查

前端沒有瀏覽器測試環境時以原始碼檢查關鍵行為 (每個檢查對應一個 bug 編號)。
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src(name):
    with open(os.path.join(ROOT, 'static', 'js', 'pos', name), encoding='utf-8') as fh:
        return fh.read()


def _func(src, name):
    """取出 function name(...) { ... } 的內容 (以下一個頂層宣告為界)"""
    m = re.search(r'^(?:export )?(?:async )?function ' + name + r'\b.*?(?=^(?:export |async |function |const |let |\$\(|Kiosk\.on)|\Z)',
                  src, re.S | re.M)
    assert m, f'找不到 {name}'
    return m.group(0)


def test_hold_order_blocked_while_submitting_and_resets_terminal():
    """B-238 / R2-080 / B-232：送出中不掛單；掛單後比照結帳完成清空終端機 (發票、加急、實收、授權碼、外送地址)"""
    body = _func(_src('live.js'), 'holdOrder')
    assert 'state.submitting' in body and ".modal.show" in body
    assert 'resetTerminal()' in body
    for key in ('rush:', 'invoiceType:', 'invoiceValue:', "'外送到府'", 'home:'):
        assert key in body, key


def test_recall_hold_restores_home_delivery_rush_and_invoice():
    """B-232 / R2-080：取回掛單時還原外送到府 (地址 / 電話 / 區域)、加急與發票設定"""
    body = _func(_src('live.js'), 'recallHold')
    assert "'posTypeHome'" in body and 'posHomeAddress' in body and 'posHomePhone' in body and 'posHomeZone' in body
    assert "$('posRush').checked = Boolean(target.rush)" in body
    assert 'target.invoiceType' in body and 'onInvoiceTypeChange()' in body


def test_render_tables_keeps_cashier_inputs():
    """B-233：同步重繪桌位卡片時保留付款方式、實收與發票欄位"""
    body = _func(_src('live.js'), 'renderTables')
    assert 'kept' in body and 'TABLE_INPUTS' in body
    assert body.index('kept') < body.index('setHtml(grid, tables.map')
    assert 'tableCardChanged(' in body


def test_settle_table_sends_order_ids_expected_total_and_own_invoice():
    """B-234 / B-235：整桌結帳只結畫面上的訂單、附預期金額；發票讀桌位卡片自己的欄位，不讀購物車區"""
    src = _src('live.js')
    body = _func(src, 'settleTable')
    assert 'order_ids: orderIds' in body and 'expected_total: expectedTotal' in body
    assert body.index('const orderIds') < body.index('await paymentDetails')
    assert 'invoicePayload()' not in src, '整桌結帳不可再讀收銀主畫面的發票設定'
    assert '.table-inv-type' in body
    assert 'table-inv-type' in _func(src, 'renderTables')


def test_apply_stock_keeps_service_window():
    """B-239：applyStock 不再只用售完狀態覆寫 is_available (伺服器回報不在供應時段時不可加入)"""
    body = _func(_src('live.js'), 'applyStock')
    assert 'it.is_available = !soldOut && m.in_service !== false' in body


def test_reprint_uses_server_subtotal_service_charge_invoice_points():
    """B-240：補印收據使用伺服器的商品小計、服務費、發票與本次紅利"""
    body = _func(_src('live.js'), 'reprint')
    assert 'o.subtotal' in body
    for key in ('service_charge:', 'invoice:', 'points_earned:', 'delivery_fee:'):
        assert key in body, key


def test_cancel_unpaid_order_goes_through_approval():
    """R2-081：取消訂單包在 withApproval 中，後端要求主管核准時會跳出核准視窗"""
    body = _func(_src('live.js'), 'updateOrderStatus')
    assert 'withApproval(' in body and 'err.cancelled' in body


def test_refund_form_cannot_be_submitted_twice():
    """B-231：作廢 / 退款表單送出中不可再送，按鈕停用並帶 request_id"""
    src = _src('split.js')
    m = re.search(r"\$\('posRefundForm'\)\.addEventListener\('submit'.*?\n\}\);", src, re.S)
    assert m
    body = m.group(0)
    assert 'refundSubmitting) return' in body
    assert 'submitBtn.disabled = true' in body and 'finally' in body
    assert body.count('request_id') >= 3


def test_staff_cannot_pick_void_for_picked_up_when_role_forbids():
    """B-243：角色不能作廢已取餐訂單時 (canVoidPickedUp === false) 停用「作廢整單」"""
    body = _func(_src('split.js'), 'openRefund')
    assert "cfg.canVoidPickedUp === false" in body
    assert "$('posRefundVoid').disabled = unpaid || noVoid" in body


def test_open_split_resets_invoice_fields():
    """B-244：分開結帳視窗開啟時清空發票類型與載具"""
    body = _func(_src('split.js'), 'openSplit')
    assert "$('posSplitInvoiceType').value = ''" in body
    assert "$('posSplitInvoiceValue').value = ''" in body


def test_move_table_requires_selection():
    """B-245：換桌沒有勾選訂單時不送出 (後端空清單代表整桌移動)"""
    body = _func(_src('split.js'), 'moveTable')
    assert 'if (!orderIds.length)' in body
    assert body.index('if (!orderIds.length)') < body.index('/move')


def test_split_waits_for_line_pay_modal_between_payments():
    """R2-082：分開結帳多筆 LINE Pay 時，等上一個付款碼視窗完全關閉後才開下一個，取消時顯示訊息"""
    src = _src('split.js')
    loop = src[src.index("$('posSplitForm').addEventListener('submit'"):]
    assert loop.index('await linePayModalHidden()') < loop.index('await paymentDetails(')
    assert "'hidden.bs.modal'" in _func(src, 'linePayModalHidden')
    assert "$('posSplitMsg').textContent = '已取消付款資料輸入" in loop


def test_table_settle_confirms_and_forces_when_online_pay_pending():
    """R2-011：顧客正在手機線上結帳 (409 online_pay_pending) 時詢問店員，確定後帶 force 重送；整桌與分開結帳共用"""
    split = _src('split.js')
    helper = _func(split, 'settleTableApi')
    assert 'err.data && err.data.online_pay_pending' in helper
    assert 'confirm(err.message)' in helper and 'err.cancelled = true' in helper
    assert 'force: true' in helper
    submit = split[split.index("$('posSplitForm').addEventListener('submit'"):]
    assert 'await settleTableApi(splitTable.table_id' in submit and 'err.cancelled' in submit
    live = _src('live.js')
    assert "import { settleTableApi } from './split.js'" in live
    body = _func(live, 'settleTable')
    assert 'await settleTableApi(btn.dataset.id' in body
    assert 'if (!err.cancelled) alert(' in body and 'btn.disabled = false' in body


def test_live_orders_show_pay_at_pickup_badge_and_button():
    """B-005：當日訂單列表接上 orders.js 的「到店付款・未收款」標籤與「收款」按鈕"""
    src = _src('live.js')
    assert "import { pickupPayButton, unpaidBadge } from './orders.js'" in src
    body = _func(src, 'renderLiveOrders')
    assert '${unpaidBadge(o)}' in body and '${pickupPayButton(o)}' in body
    assert '>未結帳</span>' not in body


def test_pos_modules_within_line_limit():
    for name in ('live.js', 'split.js'):
        assert len(_src(name).split('\n')) <= 500, name
