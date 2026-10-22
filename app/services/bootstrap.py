"""啟動時確保基本資料存在：預設店家、預設負責人帳號、預設加料與紅利設定"""
import logging

from flask import current_app

from app.extensions import db
from app.models.menu import ModifierOption
from app.models.tenant import StaffAccount, Tenant
from app.models.user import RewardSetting

logger = logging.getLogger(__name__)

DEFAULT_MODIFIERS = (('加荷包蛋', 10), ('加起司片', 15))


def seed_tenant_defaults(tenant_id):
    """新店家的預設加料與紅利設定 (已存在則略過)"""
    if not ModifierOption.query.filter_by(tenant_id=tenant_id).count():
        db.session.add_all(ModifierOption(tenant_id=tenant_id, category='addons', name=n, price=p, is_active=True)
                           for n, p in DEFAULT_MODIFIERS)
    if not RewardSetting.query.filter_by(tenant_id=tenant_id).first():
        db.session.add(RewardSetting(tenant_id=tenant_id, is_enabled=True, points_per_dollar=1,
                                     max_discount_per_order=0, spend_per_point=100))


def _announce(cfg, action):
    logger.warning("%s預設負責人帳號。帳號: %s 密碼: %s —— 請立即登入並將帳密寫入安全位置 (或於 .env 設定 ADMIN_USERNAME / ADMIN_PASSWORD)。",
                   action, cfg['ADMIN_USERNAME'], cfg['ADMIN_PASSWORD'])


def ensure_defaults():
    """
    預設店家 (slug=default) 與其負責人：
    - 尚無負責人時以 .env 的帳密 (未設定則隨機產生) 建立。
    - 一般重啟不會改動既有負責人，負責人自行修改的密碼不會被洗掉。
    - RESET_ADMIN_PASSWORD_ON_START=1 時重設密碼並重新啟用 (忘記密碼的救援手段)；
      若帳密是自動產生的，同時把帳號名稱輪替成新產生的名稱。
    """
    cfg = current_app.config
    tenant = Tenant.query.filter_by(slug='default').first()
    if tenant is None:
        tenant = Tenant(name='預設店家', slug='default', is_active=True)
        db.session.add(tenant)
        db.session.flush()
    seed_tenant_defaults(tenant.id)

    owners = StaffAccount.query.filter_by(tenant_id=tenant.id, role='owner', deleted_at=None)
    if cfg['ADMIN_CREDENTIALS_GENERATED']:
        account = owners.order_by(StaffAccount.id).first()
    else:
        account = StaffAccount.query.filter_by(username=cfg['ADMIN_USERNAME'], deleted_at=None).first()

    if account is None:
        db.session.add(StaffAccount(tenant_id=tenant.id, username=cfg['ADMIN_USERNAME'],
                                    password_hash=cfg['ADMIN_PASSWORD_HASH'], role='owner', is_active=True))
        if cfg['ADMIN_CREDENTIALS_GENERATED']:
            _announce(cfg, '已建立')
    elif cfg['RESET_ADMIN_PASSWORD_ON_START']:
        if cfg['ADMIN_CREDENTIALS_GENERATED']:
            account.username = cfg['ADMIN_USERNAME']
            account.role = 'owner'
            _announce(cfg, '已輪替')
        account.password_hash = cfg['ADMIN_PASSWORD_HASH']
        account.is_active = True
        logger.info('已依 RESET_ADMIN_PASSWORD_ON_START 重設預設負責人【%s】的密碼並重新啟用。', account.username)
    db.session.commit()
