"""修正小組 pos-backend-2 的回歸測試：交班與日結頁 (templates/admin_shifts.html)"""
import re

import pytest

from app.extensions import db
from app.models.shift import CashShift


@pytest.fixture
def no_open_shift(app):
    """確保測試開始與結束時沒有開著的班 (其他測試不受影響)"""
    def close_all():
        with app.app_context():
            for s in CashShift.query.filter_by(status='open').all():
                s.status = 'closed'
            db.session.commit()
    close_all()
    yield
    close_all()


def _shift_row(html, sid):
    m = re.search(r'<tr><td>%d</td>(.*?)</tr>' % sid, html, re.S)
    assert m, f'找不到班別 #{sid} 的列'
    return m.group(1)


def _petty_total(html):
    """日結付款方式表「零用金」欄的合計 (各付款方式加總；-$200 → -200)"""
    cells = re.findall(r'<tr><td>[^<]+</td><td>\$-?\d+</td><td>\$-?\d+</td>\s*<td>(-?)\$(\d+)</td>', html)
    return sum(-int(v) if neg else int(v) for neg, v in cells)


def test_open_shift_row_and_petty_cash_column(app, staff_client, admin_client, no_open_shift):
    """B-114：開班中的列不再顯示應有現金 $0、實點 $0；日結付款方式表列出零用金，三欄能對上淨額"""
    r = staff_client.post('/api/pos/shift/open', json={'opening_cash': 1500})
    assert r.status_code == 200
    sid = r.get_json()['shift']['shift_id']
    # 今天其他測試也可能有零用金收支：以這次支出前的合計為基準
    before = _petty_total(admin_client.get('/admin/shifts').get_data(as_text=True))
    assert staff_client.post('/api/pos/shift/movement',
                             json={'amount': 200, 'kind': 'pay_out', 'note': '買冰塊'}).status_code == 200

    html = admin_client.get('/admin/shifts').get_data(as_text=True)
    row = _shift_row(html, sid)
    # 應有現金用即時值 1500 - 200，實點顯示「-」
    assert '<td>$1300</td><td>-</td>' in row
    assert '$0</td>' not in row
    # 付款方式表多一欄零用金，顯示 -$200
    assert '<th>零用金</th>' in html
    assert _petty_total(html) == before - 200


def test_back_button_for_cash_drawer_only_staff(staff_client, admin_client, no_open_shift):
    """R2-044：沒有系統設定權限的櫃檯員工，頁首返回鈕改回 POS；有權限者仍回系統設定"""
    html = staff_client.get('/admin/shifts').get_data(as_text=True)
    assert '回POS' in html and '回系統設定' not in html
    assert 'href="/pos"' in html
    admin_html = admin_client.get('/admin/shifts').get_data(as_text=True)
    assert '回系統設定' in admin_html
