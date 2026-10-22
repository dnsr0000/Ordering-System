"""端對端：會員註冊 (上傳照片)、手機 + PIN / 人臉登入、紅利商城兌換優惠券與商品、退回兌換"""
import json
import random
import uuid

import numpy as np
import pytest
from playwright.sync_api import expect

import app.routes.auth as auth_routes
from app.extensions import db
from app.models.coupon import Coupon, UserCoupon
from app.models.user import RewardRedemption, User
from tests.support import (
    default_tenant_id,
    jpeg_bytes,
    make_item,
    make_member,
    seed_demo,
)

pytestmark = pytest.mark.e2e


@pytest.fixture(autouse=True)
def demo(app):
    return seed_demo(app)


def unique_phone():
    return f'09{random.randint(0, 10 ** 8 - 1):08d}'


def member_by_phone(app, phone):
    with app.app_context():
        user = User.query.filter_by(phone=phone).first()
        return None if user is None else {'id': user.id, 'name': user.name, 'points': user.points,
                                          'photo_path': user.photo_path, 'has_feature': user.feature is not None}


def member_points(app, user_id):
    with app.app_context():
        return db.session.get(User, user_id).points


def api_member_login(page, phone, pin):
    """以 API 登入會員 (不開啟點餐首頁)，cookie 與瀏覽器分頁共用"""
    page.request.get('/?shop=default')
    resp = page.request.post('/phone_login', data={'phone': phone, 'pin': pin})
    assert resp.ok, resp.text()


def test_register_with_uploaded_photo(page, app):
    suffix = uuid.uuid4().hex[:6]
    name = f"新會員<b>{suffix}</b>'測試'"
    phone = unique_phone()
    page.goto('/register')
    page.fill('#regName', name)
    page.fill('#regPhone', phone)
    page.fill('#regPin', '1357')
    page.check('#faceConsent')
    page.set_input_files('#photoInput', files=[{'name': f'<i>face_{suffix}</i>.jpg', 'mimeType': 'image/jpeg',
                                                'buffer': jpeg_bytes(200, 200).getvalue()}])
    # 檔名以純文字顯示
    expect(page.locator('#photoStatus')).to_contain_text(f'<i>face_{suffix}</i>.jpg')
    page.check('#agreePrivacy')
    page.get_by_role('button', name='註冊會員').click()
    page.wait_for_url('**/')
    page.wait_for_load_state('networkidle')

    member = member_by_phone(app, phone)
    assert member is not None, '註冊後應建立會員'
    assert member['name'] == name and member['points'] == 20 and member['has_feature']
    assert member['photo_path'].startswith('member_')


def test_register_duplicate_phone_shows_flash(page, app):
    phone = unique_phone()
    make_member(app, '已存在會員', phone, pin='2468')
    page.goto('/register')
    page.fill('#regName', '重複手機')
    page.fill('#regPhone', phone)
    page.fill('#regPin', '1357')
    page.check('#faceConsent')
    page.set_input_files('#photoInput', files=[{'name': 'p.jpg', 'mimeType': 'image/jpeg',
                                                'buffer': jpeg_bytes(200, 200).getvalue()}])
    page.check('#agreePrivacy')
    page.get_by_role('button', name='註冊會員').click()
    # 409 會 flash 後導回註冊頁
    expect(page.locator('.flash-stack')).to_contain_text('此手機號碼已註冊過')
    expect(page.get_by_role('button', name='註冊會員')).to_be_enabled()


def test_phone_and_face_login(page, app, monkeypatch):
    suffix = uuid.uuid4().hex[:6]
    phone = unique_phone()
    feature = np.zeros(128, dtype=np.float32)
    feature[(int(suffix, 16) % 100) + 20] = 1.0
    name = f"<script>alert('{suffix}')</script>會員"
    user_id = make_member(app, name, phone, points=88, pin='8642', feature=feature)

    # 手機 + PIN 登入
    page.goto('/face_login')
    page.get_by_role('tab', name='手機號碼登入').click()
    page.fill('#loginPhoneInput', phone)
    page.fill('#loginPinInput', '8642')
    page.get_by_role('button', name='手機號碼登入').click()
    page.wait_for_url('**/')
    page.wait_for_load_state('networkidle')
    assert page.request.get('/rewards').ok

    # 登出後改用人臉辨識登入 (測試環境沒有相機：截取空白畫面，特徵擷取以假特徵取代)
    page.request.post('/logout')
    monkeypatch.setattr(auth_routes, 'extract_feature', lambda _path: feature.copy())
    page.goto('/face_login')
    page.locator('#btn-login-snap').click()
    page.wait_for_url('**/')
    page.wait_for_load_state('networkidle')
    page.goto('/rewards')
    expect(page.locator('#memberName')).to_have_text(name)
    assert member_points(app, user_id) == 88


def test_rewards_redeem_coupon_item_and_refund(page, app):
    suffix = uuid.uuid4().hex[:6]
    tid = default_tenant_id(app)
    item_name = f"兌換雞排<script>alert('{suffix}')</script>"
    item_id = make_item(app, item_name, price=80, stock=10, category='點心', is_reward=True, reward_points=30,
                        modifiers='spicy')
    coupon_title = f'紅利券 <b>"{suffix}"</b>'
    with app.app_context():
        coupon = Coupon(tenant_id=tid, code=f'E2ER{suffix}'.upper(), title=coupon_title, discount_type='fixed',
                        discount_value=30, min_spend=100, reward_points=15, is_reward=True, per_user_limit=1)
        db.session.add(coupon)
        db.session.commit()
        coupon_id = coupon.id
    phone = unique_phone()
    member_name = f"點數會員 </script><b>{suffix}</b>"
    user_id = make_member(app, member_name, phone, points=100, pin='1122')

    api_member_login(page, phone, '1122')
    page.goto('/rewards')
    expect(page.locator('#memberName')).to_have_text(member_name)
    expect(page.locator('#currentPoints')).to_have_text('100')

    # ---- 兌換優惠券
    coupon_card = page.locator('.reward-item').filter(has_text=coupon_title)
    expect(coupon_card.get_by_text(coupon_title, exact=True)).to_be_visible()
    expect(coupon_card).to_contain_text('現折 $30 元')
    coupon_card.get_by_role('button', name='立即兌換').click()
    modal = page.locator('#redeemModal')
    expect(modal).to_be_visible()
    expect(modal.locator('#modalItemName')).to_have_text(coupon_title)
    expect(modal.locator('#modalAlertText')).to_contain_text(f'E2ER{suffix}'.upper())
    modal.get_by_role('button', name='確認兌換').click()
    expect(page.locator('#currentPoints')).to_have_text('85')
    assert member_points(app, user_id) == 85
    with app.app_context():
        assert UserCoupon.query.filter_by(user_id=user_id, coupon_id=coupon_id).count() == 1
    # 每人限換 1 次：重新整理後按鈕變成「已達兌換上限」
    coupon_card = page.locator('.reward-item').filter(has_text=coupon_title)
    expect(coupon_card.get_by_role('button', name='已達兌換上限')).to_be_disabled()

    # ---- 兌換商品 (選擇辣度)
    item_card = page.locator('.reward-item').filter(has_text=item_name)
    expect(item_card.get_by_text(item_name)).to_be_visible()
    item_card.get_by_role('button', name='立即兌換').click()
    expect(modal.locator('#modalItemName')).to_have_text(item_name)
    modal.locator('#reward-mod-spicy').select_option('中辣')
    modal.get_by_role('button', name='確認兌換').click()
    expect(page.locator('#currentPoints')).to_have_text('55')
    assert member_points(app, user_id) == 55
    # 重新整理後出現「已兌換、尚未使用」清單
    voucher_row = page.locator(f'#openVouchers [data-item-id="{item_id}"]')
    expect(voucher_row.locator('.voucher-name')).to_have_text(item_name)
    cart = json.loads(page.evaluate("localStorage.getItem('kiosk_cart') || '[]'"))
    assert any(l['menu_item_id'] == item_id and l['is_reward'] and l['customization'] == '中辣' for l in cart)

    # ---- 退回兌換：點數恢復、憑證標為已退回、購物車移除
    voucher_row.get_by_role('button', name='退回 1 份').click()
    expect(page.locator(f'#openVouchers [data-item-id="{item_id}"]')).to_have_count(0)
    expect(page.locator('#currentPoints')).to_have_text('85')
    assert member_points(app, user_id) == 85
    with app.app_context():
        assert RewardRedemption.query.filter_by(user_id=user_id, status='refunded').count() == 1
    cart = json.loads(page.evaluate("localStorage.getItem('kiosk_cart') || '[]'"))
    assert not any(l.get('menu_item_id') == item_id and l.get('is_reward') for l in cart)


def test_rewards_insufficient_points_disables_buttons(page, app):
    suffix = uuid.uuid4().hex[:6]
    item_name = f'高價兌換品{suffix}'
    make_item(app, item_name, price=500, stock=5, is_reward=True, reward_points=9999)
    phone = unique_phone()
    make_member(app, f'少點數{suffix}', phone, points=5, pin='3344')
    api_member_login(page, phone, '3344')
    page.goto('/rewards')
    card = page.locator('.reward-item').filter(has_text=item_name)
    expect(card.get_by_role('button', name='點數不足')).to_be_disabled()



def test_register_without_face(page, app):
    """人臉辨識為選填：不勾選即不顯示拍照區，只用手機 + PIN 註冊；未勾選隱私權告知無法送出"""
    phone = unique_phone()
    page.goto('/register')
    expect(page.locator('#facePhotoSection')).to_be_hidden()
    page.fill('#regName', '不刷臉會員')
    page.fill('#regPhone', phone)
    page.fill('#regPin', '1357')
    page.get_by_role('button', name='註冊會員').click()  # 瀏覽器擋下 (隱私權告知必勾)
    assert member_by_phone(app, phone) is None
    page.check('#agreePrivacy')
    page.get_by_role('button', name='註冊會員').click()
    page.wait_for_url('**/')
    member = member_by_phone(app, phone)
    assert member is not None and not member['has_feature'] and member['photo_path'] == ''
