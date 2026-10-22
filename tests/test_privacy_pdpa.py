"""個資法：隱私權告知與後台設定、會員同意紀錄、撤回人臉資料、清除長期未使用的會員"""
import os
from datetime import datetime, timedelta

from tests.support import (
    assert_flash,
    jpeg_bytes,
    login_staff,
)


# ---------------------------------------------------------------------------
# 隱私權告知與同意
# ---------------------------------------------------------------------------
def test_privacy_notice_and_admin_settings(app):
    c = app.test_client()
    page = c.get('/privacy?shop=default').get_data(as_text=True)
    for heading in ('蒐集者名稱', '蒐集之目的', '個人資料之類別', '利用之期間', '您的權利', '不提供個人資料之影響'):
        assert heading in page, heading
    manager = login_staff(app, 'privacy_mgr', 'manager')
    staff = login_staff(app, 'privacy_staff', 'staff')
    assert staff.get('/admin/privacy').status_code == 403
    r = manager.post('/admin/privacy', data={'operator_name': '測試餐飲有限公司 <b>', 'contact_email': 'privacy@example.com',
                                             'contact_phone': '06-1234567', 'retention_months': '18'})
    assert r.status_code == 302
    page = app.test_client().get('/privacy?shop=default').get_data(as_text=True)
    assert '測試餐飲有限公司 &lt;b&gt;' in page and 'privacy@example.com' in page and '滿 18 個月' in page


def test_member_consent_banner_and_face_withdrawal(app):
    import numpy as np

    from app.extensions import db
    from app.models.user import User
    from tests.support import make_member, member_client
    photo = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], 'privacy_face.jpg')
    with open(photo, 'wb') as fh:
        fh.write(jpeg_bytes().read())
    feature = np.zeros(128, dtype=np.float32)
    feature[77] = 1
    uid = make_member(app, '舊會員', '0977000123', pin='2468', photo_path='privacy_face.jpg', feature=feature)
    c = member_client(app, '0977000123', '2468', ip='10.77.0.1')
    assert 'privacy-consent-banner' in c.get('/').get_data(as_text=True), '沒有同意紀錄的舊會員顯示告知'
    c.post('/privacy/consent')
    assert 'privacy-consent-banner' not in c.get('/').get_data(as_text=True)

    page = c.get('/privacy').get_data(as_text=True)
    assert '撤回同意並刪除人臉資料' in page
    c.post('/privacy/face/delete')
    assert_flash(c, '已刪除您的人臉辨識資料')
    with app.app_context():
        user = db.session.get(User, uid)
        assert user.feature is None and user.photo_path == '' and user.face_consented_at is None
        assert user.privacy_consented_at is not None
    assert not os.path.exists(photo)


def test_purge_inactive_members(app):
    from app.extensions import db
    from app.models.order import Order
    from app.models.user import User
    from app.services import privacy_service
    from tests.support import default_tenant_id, make_member
    tid = default_tenant_id(app)
    old_id = make_member(app, '很久沒來', '0977000201')
    new_id = make_member(app, '最近有來', '0977000202')
    with app.app_context():
        long_ago = datetime.now() - timedelta(days=800)
        db.session.get(User, new_id).last_login_at = datetime.now()
        from app.models.sync import PointLedger
        PointLedger.query.filter(PointLedger.user_id == old_id).update({'created_at': long_ago})
        db.session.add(Order(tenant_id=tid, user_id=old_id, total_price=50, table_number='很久沒來', created_at=long_ago))
        db.session.commit()
        names = [u.name for u in privacy_service.purge_inactive_members(tid, 24, dry_run=True)]
        assert '很久沒來' in names and '最近有來' not in names
        assert db.session.get(User, old_id) is not None, '預覽不刪除'
        privacy_service.purge_inactive_members(tid, 24, dry_run=False)
        assert db.session.get(User, old_id) is None and db.session.get(User, new_id) is not None
        kept = Order.query.filter_by(total_price=50, table_number='已刪除會員').first()
        assert kept is not None and kept.user_id is None, '訂單保留但去識別化'
