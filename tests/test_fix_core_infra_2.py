"""修正小組 core-infra-2 的回歸測試：?shop= 指定店家、停用店家不默默改用預設店家、遷移失敗正常拋例外、廚房會員照片權限"""
import os

import pytest
from sqlalchemy import text

from app.extensions import db
from app.models.tenant import StaffAccount, Tenant
from tests.support import default_tenant_id, get_or_create_tenant, jpeg_bytes, login_staff, make_member


def test_public_pages_accept_shop_query(app):
    """B-201：訂位 / 候位 / 看板 / 打卡等公開頁可用 ?shop=<代號> 指定店家 (新瀏覽器不再落到預設店家)"""
    tid = get_or_create_tenant(app, 'fix-ci2-shop-b')
    for path in ('/reserve', '/waitlist', '/counter', '/signage', '/timeclock'):
        c = app.test_client()
        c.get(f'{path}?shop=fix-ci2-shop-b')
        with c.session_transaction() as sess:
            assert sess.get('tenant_id') == tid, path
    assert app.test_client().get('/reserve?shop=no-such-shop-ci2').status_code == 404


def test_shop_query_does_not_override_logged_in_staff(app):
    """已登入員工一律使用所屬店家，?shop= 不會切換"""
    get_or_create_tenant(app, 'fix-ci2-shop-b')
    c = login_staff(app, 'fix_ci2_staff', 'staff')
    c.get('/pos?shop=fix-ci2-shop-b')
    with c.session_transaction() as sess:
        assert sess.get('tenant_id') == default_tenant_id(app)


def test_remembered_inactive_tenant_is_not_replaced_by_default(app):
    """R2-072：session 記住的店家被停用後回 404，不可默默改用預設店家；重新啟用後自動恢復"""
    tid = get_or_create_tenant(app, 'fix-ci2-off-shop')
    c = app.test_client()
    assert c.get('/shop/fix-ci2-off-shop').status_code != 404
    with app.app_context():
        db.session.get(Tenant, tid).is_active = False
        db.session.commit()
    try:
        assert c.get('/').status_code == 404
        with c.session_transaction() as sess:
            assert sess.get('tenant_id') == tid
        assert c.get('/').status_code == 404   # 重新整理仍是暫停服務，不會改到預設店家
        # 明確指定其他店家仍可使用
        assert c.get('/?shop=default').status_code == 200
        with c.session_transaction() as sess:
            assert sess.get('tenant_id') == default_tenant_id(app)
    finally:
        with app.app_context():
            db.session.get(Tenant, tid).is_active = True
            db.session.commit()


def test_remembered_missing_tenant_is_404_and_forgotten(app):
    """R2-072：記住的店家已不存在 → 404 並清掉記憶；沒有記住店家時才用預設店家"""
    c = app.test_client()
    with c.session_transaction() as sess:
        sess['tenant_id'] = 987654
    assert c.get('/').status_code == 404
    with c.session_transaction() as sess:
        assert 'tenant_id' not in sess
    assert c.get('/').status_code == 200
    with c.session_transaction() as sess:
        assert sess.get('tenant_id') == default_tenant_id(app)


def test_upgrade_database_raises_instead_of_sys_exit(app):
    """B-200：資料庫版本不在本版 revision 清單 → 拋出一般例外 (不是 SystemExit)，備份還原才能回報失敗"""
    from app.services.db_migrate import upgrade_database
    with app.app_context():
        upgrade_database()   # 已是最新版：可重複執行
        current = db.session.execute(text('SELECT version_num FROM alembic_version')).scalar()
        db.session.execute(text("UPDATE alembic_version SET version_num = 'zz_ci2_unknown'"))
        db.session.commit()
        try:
            with pytest.raises(Exception) as excinfo:
                upgrade_database()
            assert not isinstance(excinfo.value, SystemExit)
        finally:
            db.session.rollback()
            db.session.execute(text('UPDATE alembic_version SET version_num = :v'), {'v': current})
            db.session.commit()
        upgrade_database()


def test_kitchen_with_members_permission_can_read_member_photo(app):
    """R2-073：被授權「會員名冊」的廚房帳號可讀會員照片；沒授權的廚房仍 403"""
    fname = 'fix_ci2_kitchen_photo.jpg'
    path = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], fname)
    with open(path, 'wb') as fh:
        fh.write(jpeg_bytes().read())
    make_member(app, '廚房授權照片會員', '0933990222', photo_path=fname)
    try:
        plain = login_staff(app, 'fix_ci2_kitchen_plain', 'kitchen')
        assert plain.get(f'/media/member/{fname}').status_code == 403
        granted = login_staff(app, 'fix_ci2_kitchen_members', 'kitchen')
        with app.app_context():
            staff = StaffAccount.query.filter_by(username='fix_ci2_kitchen_members').first()
            staff.permissions = 'members'
            db.session.commit()
        assert granted.get(f'/media/member/{fname}').status_code == 200
        assert app.test_client().get(f'/media/member/{fname}').status_code == 403
    finally:
        os.remove(path)
