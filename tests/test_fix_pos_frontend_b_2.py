"""POS 前端回歸檢查 (修正小組 pos-frontend-b-2)：儲值防重複送出、刷卡末四碼、核准重試次數、客顯外送費"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding='utf-8') as fh:
        return fh.read()


def _block(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


def test_topup_submit_guards_double_submit_and_clears_amount():
    """B-230：儲值送出期間擋重複送出、帶 request_id，成功後清空金額"""
    src = _read('static', 'js', 'pos', 'shift.js')
    handler = _block(src, "$('posTopupForm').addEventListener('submit'", 'Kiosk.on(document')
    assert 'topupBusy' in handler and 'btn.disabled = true' in handler
    assert 'request_id' in handler
    assert "$('posTopupAmount').value = ''" in handler


def test_shift_movement_submit_disables_button():
    """B-242：零用金異動送出期間停用按鈕"""
    src = _read('static', 'js', 'pos', 'shift.js')
    handler = _block(src, "$('posShiftMoveForm').addEventListener('submit'", "$('posShiftCloseForm')")
    assert 'moveBusy' in handler and 'btn.disabled = true' in handler and 'finally' in handler


def test_topup_and_gift_dialogs_reset_card_fields():
    """R2-083：儲值 / 禮物卡視窗重開時付款方式回現金、清空授權碼"""
    src = _read('static', 'js', 'pos', 'shift.js')
    assert "resetCardFields('posTopup')" in _block(src, 'function openTopup', '\n}')
    assert "resetCardFields('posGift')" in _block(src, 'function openGiftCards', '\n}')
    assert "$(`${prefix}Method`).value = 'Cash'" in src


def test_card_last4_collected_when_required():
    """B-236：桌位 / 分開結帳、儲值、禮物卡售出在必填末四碼時送出 card_last4"""
    pro = _read('static', 'js', 'pos', 'pro.js')
    details = _block(pro, 'export async function paymentDetails', "if (method === 'Line Pay'")
    assert 'cfg.cardRequireLast4' in details and 'card_last4' in details
    shift = _read('static', 'js', 'pos', 'shift.js')
    assert shift.count('card_last4: last4') == 2


def test_approval_loops_do_not_ask_for_discarded_credentials():
    """B-241：最後一輪失敗時不再跳視窗要帳密 / PIN"""
    pro = _read('static', 'js', 'pos', 'pro.js')
    for fn, ask in (('withApproval', 'askApproval('), ('withMemberVerify', 'askMemberPin(')):
        body = _block(pro, f'export async function {fn}', 'finally')
        assert re.search(r'if \(attempt === 3\) break;[^\n]*\n\s*const \w+ = await ' + re.escape(ask), body), fn


def test_customer_display_shows_delivery_fee():
    """B-237：客顯在有外送費時顯示外送費列"""
    src = _read('static', 'js', 'pos-display.js')
    assert 'cdDeliveryRow' in src and 't.delivery' in src


def test_gift_card_sell_sends_idempotent_request_id():
    """R2-029：禮物卡售出帶 request_id，失敗重按沿用，成功後換新"""
    src = _read('static', 'js', 'pos', 'shift.js')
    handler = _block(src, "$('posGiftSellForm').addEventListener('submit'", "$('posGiftLookupForm')")
    assert 'request_id: requestId' in handler
    assert 'Kiosk.newOrderId()' in handler
    assert handler.index('giftRequest = null') > handler.index("api('/api/pos/gift_cards/sell'")


def test_shift_default_denominations_match_backend():
    """R2-042：交班預設面額與 shift_service.DENOMINATIONS 一致"""
    from app.services.shift_service import DENOMINATIONS
    src = _read('static', 'js', 'pos', 'shift.js')
    m = re.search(r'let denominations = \[([^\]]*)\]', src)
    assert tuple(int(x) for x in m.group(1).split(',')) == tuple(DENOMINATIONS)
