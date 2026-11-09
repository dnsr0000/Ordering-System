"""啟動時確保基本資料存在：預設店家、預設負責人帳號、預設加料與紅利設定"""
import logging
import sys
from datetime import datetime

from flask import current_app

from app.extensions import db
from app.models.menu import ModifierOption
from app.models.tenant import StaffAccount, Tenant
from app.models.user import RewardSetting

logger = logging.getLogger(__name__)

DEFAULT_MODIFIERS = (('加荷包蛋', 10), ('加起司片', 15))
# 預設資料的修改時間固定為很舊的時間：同步時 (以 updated_at 判斷新舊) 任何人改過的值都比預設值新，
# 新分店第一次同步時不會以「剛安裝」的預設值覆蓋線上端已設定的紅利規則與加料
SEED_UPDATED_AT = datetime(2000, 1, 1)


def seed_tenant_defaults(tenant_id, modifiers=True):
    """新店家的預設加料與紅利設定 (已存在則略過)；modifiers=False 時不建立預設加料"""
    if modifiers and not ModifierOption.query.filter_by(tenant_id=tenant_id).count():
        db.session.add_all(ModifierOption(tenant_id=tenant_id, category='addons', name=n, price=p, is_active=True,
                                          updated_at=SEED_UPDATED_AT)
                           for n, p in DEFAULT_MODIFIERS)
    if not RewardSetting.query.filter_by(tenant_id=tenant_id).first():
        db.session.add(RewardSetting(tenant_id=tenant_id, is_enabled=True, points_per_dollar=1,
                                     max_discount_per_order=0, spend_per_point=100, updated_at=SEED_UPDATED_AT))


def _announce(cfg, action, show_password=True):
    """
    自動產生的帳密只印在主控台 (stderr)，不經 logging：記錄檔 (database/logs/app.log) 會長期保留並輪替備份，
    不可留下平台管理者的明文密碼。記錄檔只寫一行提示。
    """
    secret = f" 密碼: {cfg['ADMIN_PASSWORD']}" if show_password else ' 密碼: (.env 設定的 ADMIN_PASSWORD)'
    print(f"⚠️ {action}預設負責人帳號。帳號: {cfg['ADMIN_USERNAME']}{secret} —— 請立即登入並將帳密寫入安全位置 "
          f"(或於 .env 設定 ADMIN_USERNAME / ADMIN_PASSWORD)。", file=sys.stderr, flush=True)
    logger.warning('%s預設負責人帳號【%s】，密碼只顯示在啟動畫面 (主控台)，不寫入記錄檔；請立即登入並修改密碼。',
                   action, cfg['ADMIN_USERNAME'])


def ensure_defaults():
    """
    預設店家 (slug=default) 與其負責人：
    - 尚無負責人時以 .env 的帳密 (未設定則隨機產生) 建立。
    - 一般重啟不會改動既有負責人，負責人自行修改的密碼不會被洗掉。
    - RESET_ADMIN_PASSWORD_ON_START=1 時重設密碼並重新啟用 (忘記密碼的救援手段)；
      若帳密是自動產生的，同時把帳號名稱輪替成新產生的名稱。
      只填 ADMIN_PASSWORD (帳號留空) 時沿用既有負責人的帳號名稱，密碼重設為填的值。
    - 明確設定的 ADMIN_USERNAME 只對應預設店家的帳號：同名帳號屬於其他店家 (或已刪除) 時不建立也不重設，只記錄錯誤。
    """
    cfg = current_app.config
    tenant = Tenant.query.filter_by(slug='default').first()
    if tenant is None:
        tenant = Tenant(name='預設店家', slug='default', is_active=True)
        db.session.add(tenant)
        db.session.flush()
    # 分店的預設加料由線上端下發：自行建立的預設加料會在同步時讓總部刪除的加料重新出現
    seed_tenant_defaults(tenant.id, modifiers=cfg.get('SYNC_ROLE') != 'node')

    generated = cfg['ADMIN_CREDENTIALS_GENERATED']
    password_generated = generated and cfg.get('ADMIN_PASSWORD_GENERATED', True)
    owners = StaffAccount.query.filter_by(tenant_id=tenant.id, role='owner', deleted_at=None)
    if generated:
        account = owners.order_by(StaffAccount.id).first()
    else:
        # 帳號名稱全系統唯一 (含已刪除的帳號)：不可改到其他店家的帳號
        account = StaffAccount.query.filter_by(username=cfg['ADMIN_USERNAME']).first()
        if account is not None and (account.tenant_id != tenant.id or account.deleted_at is not None):
            logger.error('ADMIN_USERNAME【%s】已被其他店家或已刪除的帳號使用，未建立 / 重設預設負責人；請改用其他帳號名稱。',
                         cfg['ADMIN_USERNAME'])
            db.session.commit()
            return

    if account is None:
        db.session.add(StaffAccount(tenant_id=tenant.id, username=cfg['ADMIN_USERNAME'],
                                    password_hash=cfg['ADMIN_PASSWORD_HASH'], role='owner', is_active=True))
        if generated:
            _announce(cfg, '已建立', show_password=password_generated)
    elif cfg['RESET_ADMIN_PASSWORD_ON_START']:
        if password_generated:
            account.username = cfg['ADMIN_USERNAME']
            account.role = 'owner'
            _announce(cfg, '已輪替')
        account.password_hash = cfg['ADMIN_PASSWORD_HASH']
        account.is_active = True
        logger.info('已依 RESET_ADMIN_PASSWORD_ON_START 重設預設負責人【%s】的密碼並重新啟用。', account.username)
    db.session.commit()
