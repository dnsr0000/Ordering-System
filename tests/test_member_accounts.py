"""會員帳號：註冊與登出、註冊欄位驗證、舊會員補 PIN、後台編輯 / 刪除會員 (點數與庫存以差額套用、只記錄有變更的稽核)"""
import io
import os
import re
import uuid

from app.extensions import db
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.user import StaffAuditLog, User
from tests.support import (
    PHOTO_MEMBER_PHONE,
    assert_flash,
    default_tenant_id,
    jpeg_bytes,
    make_item,
    make_member,
)


def test_register_and_logout(app):
    """註冊贈 20 點、人臉照片不留存、/logout 清除會員工作階段"""
    c = app.test_client()
    c.get('/?shop=default')
    r = c.post('/register', data={'name': '陳大文', 'phone': '0912345678', 'pin': '2468', 'agree_privacy': '1',
                                  'face_consent': '1', 'photo': (jpeg_bytes(), '../../evil_traversal.jpg')},
               content_type='multipart/form-data', headers={'X-Forwarded-For': '10.20.1.1'})
    assert r.status_code == 302
    with app.app_context():
        user = User.query.filter_by(phone='0912345678').first()
        assert user.points == 20
        assert user.photo_path == ''
        assert user.feature is not None
        assert user.privacy_consent_version and user.privacy_consented_at and user.face_consented_at, '同意紀錄'
    assert not [f for f in os.listdir(app.config['UPLOAD_FOLDER_MEMBER']) if f.startswith('member_')]
    assert c.post('/logout').status_code == 302
    with c.session_transaction() as sess:
        assert sess.get('user_id') is None


def test_legacy_member_without_pin_and_enumeration(app):
    """沒有 PIN 的舊會員、空號與錯誤 PIN 一律 401 且訊息相同 (無法列舉會員)"""
    make_member(app, '陳小美', '0912999888', points=150, photo_path='member_legacy.jpg')
    c = app.test_client()
    c.get('/?shop=default')
    legacy = c.post('/phone_login', json={'phone': '0912999888', 'pin': '1234'}, headers={'X-Forwarded-For': '10.20.2.1'})
    no_phone = c.post('/phone_login', json={'phone': '0988777666', 'pin': '1234'}, headers={'X-Forwarded-For': '10.20.2.2'})
    wrong_pin = c.post('/phone_login', json={'phone': '0912345678', 'pin': '9999'}, headers={'X-Forwarded-For': '10.20.2.3'})
    assert legacy.status_code == no_phone.status_code == wrong_pin.status_code == 401
    assert legacy.get_json()['message'] == no_phone.get_json()['message'] == wrong_pin.get_json()['message']


def test_admin_edit_and_delete_member(app, admin_client):
    """後台編輯會員點數/PIN (含稽核)；刪除會員時刪除照片檔、解除訂單關聯並寫稽核"""
    tid = default_tenant_id(app)
    photo = f"member_del_verify_{uuid.uuid4().hex[:8]}.jpg"
    photo_path = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], photo)
    with open(photo_path, 'wb') as fh:
        fh.write(jpeg_bytes().read())
    temp_uid = make_member(app, '待刪會員', '0955111222', points=30, photo_path=photo)
    with app.app_context():
        chen_id = User.query.filter_by(phone='0912345678').first().id
        order = Order(tenant_id=tid, user_id=temp_uid, table_number='A1', total_price=100, status='Pending')
        db.session.add(order)
        db.session.commit()
        order_id = order.id

    r = admin_client.post(f'/admin/edit_user/{chen_id}', data={'name': '陳大文', 'phone': '0912345678', 'points': '300', 'pin': '1357'})
    assert r.status_code == 302
    with app.app_context():
        chen = db.session.get(User, chen_id)
        assert chen.points == 300 and chen.check_pin('1357')
        audit = StaffAuditLog.query.filter_by(action_type='EDIT_USER', target_user_id=chen_id).first()
        assert audit.points_delta == 280

    assert admin_client.post(f'/admin/delete_user/{temp_uid}').status_code == 302
    with app.app_context():
        assert db.session.get(User, temp_uid) is None
        assert not os.path.exists(photo_path)
        linked = db.session.get(Order, order_id)
        assert linked.user_id is None and linked.table_number == '已刪除會員'
        audit = StaffAuditLog.query.filter_by(action_type='DELETE_USER', target_user_id=temp_uid).first()
        assert audit.points_delta == -30
        db.session.delete(linked)
        db.session.commit()


def test_register_validation(app, photo_member):
    c = app.test_client()
    c.get('/?shop=default')
    assert c.get('/register').status_code == 200

    def post(**fields):
        fields = {'agree_privacy': '1', 'face_consent': '1', **fields}
        return c.post('/register', data={k: v for k, v in fields.items() if v is not None},
                      content_type='multipart/form-data')

    def photo():
        return (jpeg_bytes(200, 200, (255, 255, 255)), 'p.jpg')

    cases = [
        (dict(name='A', phone='0933130201', pin='2580'), '請拍攝或上傳照片'),
        (dict(name='', phone='0933130201', pin='2580', photo=photo()), '請填寫姓名'),
        (dict(name='A', phone='0933130201', pin='2580', photo=photo(), agree_privacy=None), '隱私權告知'),
        (dict(name='A', phone='12345', pin='2580', photo=photo()), '手機號碼格式不正確'),
        (dict(name='A', phone='0933130201', pin='12', photo=photo()), 'PIN 碼必須'),
        (dict(name='A', phone=PHOTO_MEMBER_PHONE, pin='2580', photo=photo()), '已註冊過'),
        (dict(name='A', phone='0933130202', pin='2580', photo=(io.BytesIO(b'not-an-image'), 'bad.jpg')), '已損壞'),
        (dict(name='A', phone='0933130203', pin='1234', photo=photo()), '太容易被猜中'),
    ]
    for fields, message in cases:
        assert post(**fields).status_code == 302
        assert_flash(c, message)
    with app.app_context():
        assert User.query.filter(User.phone.in_(['0933130201', '0933130202'])).count() == 0


def test_register_birthday_dropdowns(app):
    """生日是年 / 月 / 日三個下拉選單；後端仍檢查：未來日期、不存在的日期、只選一部分、被竄改的值都擋下 (不建立會員)"""
    from datetime import date, timedelta
    c = app.test_client()
    c.environ_base['REMOTE_ADDR'] = '10.9.8.7'   # 註冊有每分鐘 10 次的頻率限制，與上一個測試分開計數
    c.get('/?shop=default')
    html = c.get('/register').get_data(as_text=True)
    assert all(f'name="birthday_{p}"' in html for p in ('year', 'month', 'day')) and 'js/birthday-select.js' in html
    assert 'type="date"' not in html

    tomorrow = date.today() + timedelta(days=1)
    base = {'name': 'A', 'phone': '0933130204', 'pin': '2580', 'agree_privacy': '1'}
    cases = [
        (dict(birthday_year=tomorrow.year, birthday_month=tomorrow.month, birthday_day=tomorrow.day), '生日不能是未來的日期'),
        (dict(birthday_year='2001', birthday_month='2', birthday_day='30'), '生日日期不存在'),
        (dict(birthday_year='2001', birthday_month='2'), '請完整選擇生日'),
        (dict(birthday_year="1990' OR 1=1", birthday_month='1', birthday_day='1'), '生日日期不存在'),
        (dict(birthday_year='1800', birthday_month='1', birthday_day='1'), '生日日期不存在'),
    ]
    for fields, message in cases:
        assert c.post('/register', data={**base, **fields}).status_code == 302
        assert_flash(c, message)
    with app.app_context():
        assert User.query.filter_by(phone='0933130204').count() == 0


BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_admin_edits_apply_deltas(app, admin_client):
    uid = make_member(app, '模組15編輯會員', '0933150008', points=100)
    iid = make_item(app, '模組15編輯品', price=100, stock=50)
    with app.app_context():
        db.session.get(User, uid).points = 150
        db.session.get(MenuItem, iid).stock = 40
        db.session.commit()
    admin_client.post(f'/admin/edit_user/{uid}', data={'name': '新名字', 'phone': '0933150008', 'points': '100', 'loaded_points': '100'})
    admin_client.post(f'/admin/edit/{iid}', data={'name': '模組15編輯品', 'price': '111', 'stock': '50', 'total_stock': '50',
                                                  'loaded_stock': '50', 'loaded_total_stock': '50'})
    with app.app_context():
        assert db.session.get(User, uid).points == 150
        assert (db.session.get(MenuItem, iid).stock, db.session.get(MenuItem, iid).price) == (40, 111)
    admin_client.post(f'/admin/edit_user/{uid}', data={'name': '新名字', 'phone': '0933150008', 'points': '120', 'loaded_points': '100'})
    admin_client.post(f'/admin/edit/{iid}', data={'name': '模組15編輯品', 'price': '111', 'stock': '60', 'total_stock': '60',
                                                  'loaded_stock': '50', 'loaded_total_stock': '50'})
    with app.app_context():
        assert db.session.get(User, uid).points == 170
        assert db.session.get(MenuItem, iid).stock == 50
    admin_client.post(f'/admin/edit_user/{uid}', data={'name': '新名字', 'phone': '0933150008', 'points': '10'})
    with app.app_context():
        assert db.session.get(User, uid).points == 10


def test_edit_user_audit_only_changes(app, admin_client):
    uid = make_member(app, '模組15稽核會員', '0933150023', points=40)

    def last_audit():
        with app.app_context():
            a = StaffAuditLog.query.filter_by(action_type='EDIT_USER', target_user_id=uid).order_by(StaffAuditLog.id.desc()).first()
            return a.details if a else None

    admin_client.post(f'/admin/edit_user/{uid}', data={'name': '模組15稽核會員', 'phone': '0933150024', 'points': '40', 'loaded_points': '40'})
    d = last_audit()
    assert '手機 0933150023 -> 0933150024' in d and '點數' not in d and 'PIN' not in d
    admin_client.post(f'/admin/edit_user/{uid}', data={'name': '模組15稽核會員', 'phone': '0933150024', 'points': '55',
                                                       'loaded_points': '40', 'pin': '2468'})
    d = last_audit()
    assert '點數 40 -> 55' in d and '重設安全PIN碼' in d
    with app.app_context():
        n = StaffAuditLog.query.filter_by(action_type='EDIT_USER', target_user_id=uid).count()
    admin_client.post(f'/admin/edit_user/{uid}', data={'name': '模組15稽核會員', 'phone': '0933150024', 'points': '55', 'loaded_points': '55'})
    with app.app_context():
        assert StaffAuditLog.query.filter_by(action_type='EDIT_USER', target_user_id=uid).count() == n
    html = open(os.path.join(BASE_DIR, 'templates', '_member_modals.html'), encoding='utf-8').read()   # 會員頁對話框的子樣板
    pin_input = re.search(r'<input[^>]*id="edit-user-pin"[^>]*>', html).group(0)
    assert 'autocomplete="new-password"' in pin_input
