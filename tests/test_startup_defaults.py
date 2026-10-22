"""首次啟動：預設店家、負責人帳號與預設加料、SECRET_KEY 產生與保存、預設負責人帳密的重設規則"""
import os
import shutil
import tempfile

import app.config as config_module
from app.extensions import db
from app.models.menu import ModifierOption
from app.models.tenant import StaffAccount, Tenant
from app.models.user import RewardSetting
from app.services.bootstrap import ensure_defaults
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME


def test_default_tenant_owner_and_modifiers(app):
    """預設店家、負責人帳號與預設加料自動建立"""
    with app.app_context():
        tenant = Tenant.query.filter_by(slug='default').first()
        assert tenant is not None
        owner = StaffAccount.query.filter_by(tenant_id=tenant.id, username=ADMIN_USERNAME).first()
        assert owner is not None and owner.role == 'owner' and owner.check_password(ADMIN_PASSWORD)
        assert ModifierOption.query.filter_by(tenant_id=tenant.id).count() >= 2
        assert RewardSetting.query.filter_by(tenant_id=tenant.id).count() == 1


def test_secret_key_generated_and_persisted(monkeypatch):
    tmp = tempfile.mkdtemp(prefix='kiosk_secret_')
    try:
        monkeypatch.setattr(config_module, 'BASE_DIR', tmp)
        monkeypatch.delenv('SECRET_KEY', raising=False)
        k1, k2 = config_module._load_or_create_secret_key(), config_module._load_or_create_secret_key()
        assert k1 == k2 and len(k1) >= 64
        assert os.path.exists(os.path.join(tmp, 'database', '.secret_key'))
        monkeypatch.setenv('SECRET_KEY', 'from-env-value')
        assert config_module._load_or_create_secret_key() == 'from-env-value'
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_default_owner_credentials(app):
    """重啟不洗掉負責人自行改的密碼；救援開關才重設；自動產生帳密時輪替帳號名稱"""
    from werkzeug.security import generate_password_hash
    cfg = app.config
    saved = {k: cfg[k] for k in ('ADMIN_USERNAME', 'ADMIN_PASSWORD', 'ADMIN_PASSWORD_HASH',
                                 'ADMIN_CREDENTIALS_GENERATED', 'RESET_ADMIN_PASSWORD_ON_START')}

    def owner(name=ADMIN_USERNAME):
        return StaffAccount.query.filter_by(username=name).first()

    try:
        with app.app_context():
            owner().set_password('Changed!Pass1234')
            db.session.commit()
            ensure_defaults()
            assert owner().check_password('Changed!Pass1234')
            cfg['RESET_ADMIN_PASSWORD_ON_START'] = True
            ensure_defaults()
            assert owner().check_password(ADMIN_PASSWORD)
            cfg.update(RESET_ADMIN_PASSWORD_ON_START=False, ADMIN_USERNAME='owner-generated-test', ADMIN_PASSWORD='Generated!Pass2026',
                       ADMIN_PASSWORD_HASH=generate_password_hash('Generated!Pass2026'), ADMIN_CREDENTIALS_GENERATED=True)
            ensure_defaults()
            assert owner() is not None and owner('owner-generated-test') is None
            cfg['RESET_ADMIN_PASSWORD_ON_START'] = True
            ensure_defaults()
            rotated = owner('owner-generated-test')
            assert rotated.role == 'owner' and rotated.check_password('Generated!Pass2026') and owner() is None
    finally:
        with app.app_context():
            rotated = owner('owner-generated-test')
            if rotated:
                rotated.username = ADMIN_USERNAME
                rotated.set_password(ADMIN_PASSWORD)
                db.session.commit()
        cfg.update(saved)
