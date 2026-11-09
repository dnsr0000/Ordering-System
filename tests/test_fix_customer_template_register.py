"""
跨檔請求 (customer-template-register)：顧客點餐頁 / 註冊頁樣板與 customer.css 的修正
- B-132 線上端開啟線上付款時不列點餐機付款方式 (改為隱藏欄位 + 說明)
- B-127 manifest 連結帶 ?shop=
- B-256 售完的快速加入卡片仍有 data-id (補貨後 live.js 才找得到)
- R2-045 註冊頁顯示 LINE 待綁定提示，須勾選 (帶一次性權杖) 才綁定，可取消
- R2-050 face_enabled=False 時登入頁直接顯示手機 + PIN、註冊頁沒有人臉區塊
- B-481 收據 / 歷史視窗的表單標籤改深色字
"""
import os
import re
from datetime import datetime

from tests.support import default_tenant_id, guest_client, make_item

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def test_b132_hub_online_payment_hides_kiosk_methods(app, monkeypatch):
    make_item(app, 'CTR 線上付款餐', price=80)
    c = guest_client(app)
    html = c.get('/').get_data(as_text=True)
    assert '<select id="paymentMethod"' in html and 'id="online-pay-hint"' not in html   # 門市照舊
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    monkeypatch.setitem(app.config, 'ONLINE_PAYMENT', True)
    r = c.get('/?shop=default')
    html = r.get_data(as_text=True)
    assert r.status_code == 200, html[:300]
    assert '<input type="hidden" id="paymentMethod" value="Credit Card">' in html
    assert '<select id="paymentMethod"' not in html and 'id="online-pay-hint"' in html


def test_b127_manifest_link_carries_shop(app):
    html = guest_client(app).get('/').get_data(as_text=True)
    m = re.search(r'<link rel="manifest" href="([^"]+)"', html)
    assert m and 'shop=default' in m.group(1), m and m.group(1)


def test_b256_sold_out_quick_card_keeps_data_id():
    with open(os.path.join(ROOT, 'templates', 'customer.html'), encoding='utf-8') as fh:
        src = fh.read()
    macro = src[src.index('{% macro quick_card'):src.index('{% endmacro %}', src.index('{% macro quick_card'))]
    assert 'data-id="{{ item.id }}" {% if not sold_out %}data-action="add"' in macro


def test_r2_045_register_page_line_pending_needs_opt_in(app):
    tid = default_tenant_id(app)
    c = app.test_client()
    c.get('/?shop=default')
    assert 'id="linePendingHint"' not in c.get('/register').get_data(as_text=True)
    with c.session_transaction() as sess:
        sess['line_pending'] = {'user_id': 'U-ctr-pending', 'name': 'CTR LINE', 'tenant_id': tid,
                                'at': datetime.now().isoformat(), 'token': 'tok-ctr'}
    html = c.get('/register').get_data(as_text=True)
    assert 'id="linePendingHint"' in html and 'CTR LINE' in html
    box = re.search(r'<input[^>]*id="bindLineToken"[^>]*>', html).group(0)
    assert 'form="registerForm"' in box and 'name="bind_line_token"' in box and 'value="tok-ctr"' in box
    assert 'checked' not in box, '預設不勾：不知情的下一位顧客不會綁上別人的 LINE'
    assert 'id="linePendingDiscard"' in html and '/auth/line/pending/discard' in html


def test_r2_050_face_disabled_shows_phone_login_and_no_face_register(app):
    with app.test_request_context('/'):
        from flask import g, render_template
        from app.services import privacy_service
        g.tenant_id = default_tenant_id(app)
        from app.models.tenant import Tenant
        from app.extensions import db
        g.tenant = db.session.get(Tenant, g.tenant_id)
        login = render_template('register.html', login_mode=True, face_enabled=False)
        reg = render_template('register.html', login_mode=False, face_enabled=False,
                              privacy=privacy_service.notice_context(g.tenant_id), referral_enabled=False, referral_code='')
    assert 'id="face-tab"' not in login and 'id="loginVideo"' not in login
    assert 'id="phone-login-pane"' in login and 'tab-pane fade show active" id="phone-login-pane"' in login
    assert 'id="online-login-hint"' not in login, '門市人臉不可用時不顯示「線上點餐」提示'
    assert 'id="faceConsent"' not in reg and 'id="facePhotoSection"' not in reg


def test_hub_member_login_keeps_online_hint(app, monkeypatch):
    monkeypatch.setitem(app.config, 'SYNC_ROLE', 'hub')
    c = app.test_client()
    html = c.get('/member_login?shop=default').get_data(as_text=True)
    assert 'id="online-login-hint"' in html and 'id="face-tab"' not in html


def test_b481_receipt_modal_labels_dark():
    with open(os.path.join(ROOT, 'static', 'css', 'customer.css'), encoding='utf-8') as fh:
        css = fh.read()
    assert re.search(r'#receiptModal \.form-check-label,[^{]*\{\s*color:\s*#0f172a;', css)
