"""跨檔請求 templates/reservations.html 的回歸測試：店家網址帶 ?shop=、店家取消 / 改回預約中按鈕、訂金退款失敗處理按鈕"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models.reservation import Reservation
from tests.support import default_tenant_id, login_staff


def _add(app, name, status, deposit_status=None):
    with app.app_context():
        tid = default_tenant_id(app)
        r = Reservation(tenant_id=tid, name=name, phone='0911000111', party_size=2,
                        reserved_at=datetime.now().replace(second=0, microsecond=0) + timedelta(hours=3),
                        status=status, source='staff')
        if deposit_status:
            r.deposit_status, r.deposit_amount = deposit_status, 300
        db.session.add(r)
        db.session.commit()
        return r.id


def test_board_links_carry_shop_and_status_buttons(app):
    booked = _add(app, '甲訂位', 'booked')
    no_show = _add(app, '乙未到', 'no_show')
    failed = _add(app, '丙退款失敗', 'cancelled', 'refund_failed')
    staff = login_staff(app, 'fix_res_tpl_staff', role='manager')
    html = staff.get(f'/reservations?day={(datetime.now() + timedelta(hours=3)).date()}').get_data(as_text=True)
    # B-201：取號頁 / 訂位頁網址帶店家代碼
    assert '/waitlist?shop=default' in html
    # B-422：門口平板需在裝置管理註冊
    assert '裝置管理' in html
    # B-092：已訂位有「店家取消 (退訂金)」，未到 / 已取消有「改回預約中」並帶目前狀態
    row = html.split(f'data-reservation-id="{booked}"')[1].split('</tr>')[0]
    assert 'name="by_store" value="1"' in row and '店家取消 (退訂金)' in row
    row = html.split(f'data-reservation-id="{no_show}"')[1].split('</tr>')[0]
    assert '改回預約中' in row and 'name="expected_status" value="no_show"' in row
    # R2-036：退款失敗的訂位有重試 / 已人工退款按鈕
    row = html.split(f'data-reservation-id="{failed}"')[1].split('</tr>')[0]
    assert 'value="deposit_retry"' in row and 'value="deposit_manual"' in row
    assert '訂金退款失敗待處理' in html
