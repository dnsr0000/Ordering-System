"""會員人臉與照片：特徵比對與驗證、人臉登入錯誤處理、同一張臉不可重複註冊、上傳圖片限制、照片存取權限"""
import io
import os
import pickle

import numpy as np
import pytest
from PIL import Image
from werkzeug.datastructures import FileStorage

from app.extensions import db
from app.models.user import (
    FEATURE_MAGIC_PREFIX,
    User,
    decode_feature_bytes,
    encode_feature_array,
)
from app.services.cv_service import (
    MAX_UPLOAD_IMAGE_PIXELS,
    find_best_match_vectorized,
    save_and_fix_image,
)
from app.services.legacy_upgrade import decode_legacy_feature
from tests.support import (
    assert_flash,
    jpeg_bytes,
    make_item,
    make_member,
    other_shop_owner_client,
)


def test_member_photos_require_staff(app, staff_client):
    """會員照片 (/media/member/) 匿名 403、同店員工 200；舊的 /static/member/ 一律不提供"""
    folder = app.config['UPLOAD_FOLDER_MEMBER']
    path = os.path.join(folder, 'privacy_test_face.jpg')
    with open(path, 'wb') as fh:
        fh.write(jpeg_bytes().read())
    make_member(app, '照片權限會員', '0922000205', photo_path='privacy_test_face.jpg')
    try:
        assert app.test_client().get('/media/member/privacy_test_face.jpg').status_code == 403
        assert staff_client.get('/media/member/privacy_test_face.jpg').status_code == 200
        assert staff_client.get('/static/member/privacy_test_face.jpg').status_code == 404
    finally:
        os.remove(path)


def test_vectorized_face_match(app):
    """矩陣餘弦相似度比對：本人命中、正交向量不通過門檻"""
    with app.app_context():
        user = User.query.filter_by(phone='0912345678').first()
        matched, score = find_best_match_vectorized(user.feature.copy(), [user], threshold=0.5)
        assert matched.id == user.id and score > 0.99
        other = np.zeros(128, dtype=np.float32)
        other[99] = 1.0
        assert find_best_match_vectorized(other, [user], threshold=0.5)[0] is None


def test_face_login_errors_and_temp_cleanup(app, monkeypatch):
    """人臉登入：缺照片、沒有人臉 (使用真實偵測模型) 皆回 400，且暫存照片被刪除"""
    import os

    import app.routes.auth as auth_routes
    from app.services.cv_service import extract_feature
    monkeypatch.setattr(auth_routes, 'extract_feature', extract_feature)
    c = app.test_client()
    c.get('/?shop=default')
    assert c.get('/face_login').status_code == 200
    r = c.post('/face_login', data={})
    assert r.status_code == 400 and r.get_json()['success'] is False
    r = c.post('/face_login', data={'photo': (jpeg_bytes(200, 200, (255, 255, 255)), 'blank.jpg')}, content_type='multipart/form-data')
    assert r.status_code == 400 and '人臉' in r.get_json()['message']
    assert not [f for f in os.listdir(app.config['UPLOAD_FOLDER_MEMBER']) if f.startswith('temp_')]


def test_feature_vectors_are_validated(app):
    """維度不符 / NaN / 截斷的特徵一律無效；混入壞資料時仍能比對出正確會員"""
    good = np.linspace(0.1, 0.9, 128, dtype=np.float32)
    nan = good.copy()
    nan[3] = np.nan
    assert decode_feature_bytes(FEATURE_MAGIC_PREFIX + np.zeros(64, np.float32).tobytes()) is None
    assert decode_feature_bytes(FEATURE_MAGIC_PREFIX + nan.tobytes()) is None
    assert decode_feature_bytes(FEATURE_MAGIC_PREFIX + good.tobytes()[:-2]) is None
    assert decode_legacy_feature(pickle.dumps(np.zeros(64, np.float32), protocol=4)) is None
    assert np.allclose(decode_feature_bytes(encode_feature_array(good)), good)
    with pytest.raises(ValueError):
        encode_feature_array(np.zeros(64, np.float32))

    class U:
        def __init__(self, f):
            self.feature = f
    users = [U(np.zeros(64, np.float32)), U(nan), U(None), U(good.copy())]
    matched, score = find_best_match_vectorized(good, users, threshold=0.5)
    assert matched is users[3] and score > 0.99

    bad_id = make_member(app, '壞維度會員', '0933140101', feature=FEATURE_MAGIC_PREFIX + np.zeros(64, np.float32).tobytes())
    good_id = make_member(app, '好特徵會員', '0933140102', feature=good.copy())
    with app.app_context():
        rows = User.query.filter(User.id.in_([bad_id, good_id])).all()
        assert next(r for r in rows if r.id == bad_id).feature is None
        assert find_best_match_vectorized(good, rows, threshold=0.5)[0].id == good_id
        User.query.filter(User.id.in_([bad_id, good_id])).delete()
        db.session.commit()


def test_member_photo_access(app, admin_client):
    """會員照片只由 /media/member 提供：匿名 403、他店 404、本店 200；靜態路徑的各種變形都拿不到"""
    folder = app.config['UPLOAD_FOLDER_MEMBER']
    path = os.path.join(folder, 'm15_guard_photo.jpg')
    with open(path, 'wb') as fh:
        fh.write(jpeg_bytes().read())
    make_member(app, '模組15照片會員', '0933150017', photo_path='m15_guard_photo.jpg')
    try:
        anon = app.test_client()
        assert anon.get('/media/member/m15_guard_photo.jpg').status_code == 403
        for url in ('/static/member/m15_guard_photo.jpg', '/static/./member/m15_guard_photo.jpg', '/static/MEMBER/m15_guard_photo.jpg'):
            assert admin_client.get(url).status_code == 404, url
        assert admin_client.get('/media/member/m15_guard_photo.jpg').status_code == 200
        assert admin_client.get('/media/member/M15_GUARD_PHOTO.jpg').status_code == 200
        assert admin_client.get('/media/member/../menu/x.jpg').status_code == 404
        other = other_shop_owner_client(app)
        assert other.get('/media/member/m15_guard_photo.jpg').status_code == 404
    finally:
        os.remove(path)


def test_same_face_cannot_register_twice(app, monkeypatch):
    import app.routes.auth as auth_routes
    face = {'v': np.eye(128, dtype=np.float32)[7]}
    monkeypatch.setattr(auth_routes, 'extract_feature', lambda _p: face['v'].copy())
    c = app.test_client()
    c.get('/?shop=default')

    def register(phone):
        return c.post('/register', data={'name': '模組15註冊', 'phone': phone, 'pin': '2580', 'agree_privacy': '1',
                                         'face_consent': '1', 'photo': (jpeg_bytes(200, 200), 'p.jpg')},
                      content_type='multipart/form-data')

    assert register('0933150018').status_code == 302
    c.post('/logout')
    register('0933150019')
    assert_flash(c, '此人臉已註冊')
    face['v'] = np.eye(128, dtype=np.float32)[9]
    assert register('0933150020').status_code == 302
    with app.app_context():
        assert User.query.filter_by(phone='0933150019').count() == 0
        assert User.query.filter_by(phone='0933150020').count() == 1


def test_upload_image_limits(app):
    dest = os.path.join(app.config['DATABASE_DIR'], 'img_limit_test.jpg')

    def upload(img, fmt):
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        buf.seek(0)
        return FileStorage(stream=buf, filename=f'x.{fmt.lower()}')

    side = int(MAX_UPLOAD_IMAGE_PIXELS ** 0.5) + 200
    assert save_and_fix_image(upload(Image.new('RGB', (side, side)), 'PNG'), dest) is False
    assert save_and_fix_image(upload(Image.new('RGB', (64, 64)), 'GIF'), dest) is False
    assert save_and_fix_image(upload(Image.new('RGB', (320, 240)), 'JPEG'), dest) is True
    os.remove(dest)


def test_media_routes_only_serve_images(app, kitchen_client, admin_client):
    """/media/menu 只提供圖片副檔名；會員照片廚房看不到"""
    folder = app.config['UPLOAD_FOLDER_MENU']
    with open(os.path.join(folder, 'evil.js'), 'w') as fh:
        fh.write('alert(1)')
    try:
        assert app.test_client().get('/media/menu/evil.js').status_code == 404
    finally:
        os.remove(os.path.join(folder, 'evil.js'))
    make_member(app, '照片權限會員', '0933990011', photo_path='member_kitchen_check.jpg')
    with open(os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], 'member_kitchen_check.jpg'), 'wb') as fh:
        fh.write(jpeg_bytes().read())
    assert kitchen_client.get('/media/member/member_kitchen_check.jpg').status_code == 403
    assert admin_client.get('/media/member/member_kitchen_check.jpg').status_code == 200


def test_delete_never_leaves_upload_folder(app, admin_client):
    """資料庫裡的圖片路徑被竄改成 ../ 也不會刪到上傳資料夾以外的檔案"""
    from app.extensions import db
    from app.models.menu import MenuItem
    canary = os.path.join(app.config['DATABASE_DIR'], 'canary.txt')
    with open(canary, 'w') as fh:
        fh.write('keep')
    rel = os.path.relpath(canary, app.config['UPLOAD_FOLDER_MENU'])
    item_id = make_item(app, '竄改路徑餐點')
    with app.app_context():
        db.session.get(MenuItem, item_id).image_path = rel
        db.session.commit()
    assert admin_client.post(f'/admin/delete/{item_id}').status_code == 302
    assert os.path.exists(canary)
    os.remove(canary)
