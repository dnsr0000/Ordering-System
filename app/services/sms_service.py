"""
簡訊與手機驗證碼 (後台「系統設定 → 簡訊驗證碼」)。
- 三竹簡訊：https://smsapi.mitake.com.tw/api/mtk/SmSend (帳號、密碼、門號、內容)
- 每日簡訊 EVERY8D：https://api.e8d.tw/API21/HTTP/sendSMS.ashx (UID、PWD、DEST、MSG)
- 只記錄：不發送，簡訊內容寫入系統監控事件 (測試用)
驗證碼 6 碼、10 分鐘有效、最多嘗試 5 次，只存雜湊；同一支手機 60 秒內只能寄一次、每小時最多 5 次。
"""
import logging
import re
import secrets
from datetime import datetime, timedelta

import requests
from werkzeug.security import check_password_hash, generate_password_hash

from app.errors import AppError
from app.extensions import db
from app.models.member import OtpCode
from app.models.tenant import Tenant
from app.services import monitor_service, settings_service
from app.utils import validate_phone

logger = logging.getLogger(__name__)

MITAKE_URL = 'https://smsapi.mitake.com.tw/api/mtk/SmSend'
EVERY8D_URL = 'https://api.e8d.tw/API21/HTTP/sendSMS.ashx'
OTP_TTL = timedelta(minutes=10)
OTP_MAX_ATTEMPTS = 5
RESEND_AFTER = timedelta(seconds=60)
MAX_PER_HOUR = 5
# 全店每小時 / 每天的驗證碼上限 (後台可調)：換 IP、換號碼也不能無限制消耗店家的簡訊點數 (SMS pumping)
DEFAULT_TENANT_HOURLY_LIMIT = 60
DEFAULT_TENANT_DAILY_LIMIT = 300
VERIFIED_TTL = timedelta(minutes=30)


def is_enabled(tenant_id=None):
    return settings_service.get('sms', tenant_id).get('provider', 'none') != 'none'


def _post(url, **kwargs):
    """實際送出 (測試時以 monkeypatch 取代)；回傳 (狀態碼, 文字)"""
    resp = requests.post(url, timeout=15, **kwargs)
    return resp.status_code, resp.text


def send_sms(tenant_id, phone, text):
    cfg = settings_service.get('sms', tenant_id)
    provider = cfg.get('provider', 'none')
    if provider == 'none':
        raise AppError('尚未設定簡訊服務')
    if provider == 'log':
        monitor_service.record_event('info', f'[簡訊測試 → {phone}] {text}', 'sms')
        return 'logged'
    try:
        if provider == 'mitake':
            status, body = _post(MITAKE_URL, params={'CharsetURL': 'UTF-8'}, data={
                'username': cfg.get('username'), 'password': cfg.get('password'), 'dstaddr': phone, 'smbody': text})
            codes = re.findall(r'statuscode=(\w)', body or '')
            ok = status == 200 and bool(codes) and codes[0] in '0124'   # 0~4：已送達簡訊中心 / 已送達手機
        else:
            status, body = _post(EVERY8D_URL, data={'UID': cfg.get('username'), 'PWD': cfg.get('password'),
                                                    'SB': '', 'MSG': text, 'DEST': phone, 'ST': ''})
            ok = status == 200 and body.strip() and not body.strip().startswith('-')
    except requests.RequestException as exc:
        raise AppError(f'簡訊發送失敗：{exc}', 502)
    if not ok:
        logger.warning('簡訊發送失敗 (%s)：%s', provider, body[:200])
        raise AppError('簡訊發送失敗，請稍後再試或洽櫃檯', 502)
    return 'sent'


def send_otp(tenant_id, phone, purpose, ip=''):
    if purpose not in ('register', 'reset_pin'):
        raise AppError('不支援的驗證用途')
    if not validate_phone(phone):
        raise AppError('手機號碼格式不正確！請輸入 09 開頭的 10 碼數字。')
    now = datetime.now()
    recent = OtpCode.query.filter(OtpCode.phone == phone, OtpCode.created_at >= now - timedelta(hours=1)) \
        .order_by(OtpCode.id.desc()).all()
    if recent and now - recent[0].created_at < RESEND_AFTER:
        wait = int((RESEND_AFTER - (now - recent[0].created_at)).total_seconds()) + 1
        raise AppError(f'驗證碼已寄出，請 {wait} 秒後再重新發送', 429)
    if len(recent) >= MAX_PER_HOUR:
        raise AppError('這支手機一小時內已寄送太多次驗證碼，請稍後再試', 429)
    _check_tenant_quota(tenant_id, now)
    code = f'{secrets.randbelow(1000000):06d}'
    db.session.add(OtpCode(tenant_id=tenant_id, phone=phone, purpose=purpose, code_hash=generate_password_hash(code),
                           expires_at=now + OTP_TTL, ip_address=ip[:50]))
    shop = db.session.get(Tenant, tenant_id)
    template = settings_service.get('sms', tenant_id).get('template') or '驗證碼 {code}'
    db.session.flush()
    send_sms(tenant_id, phone, template.replace('{code}', code).replace('{shop}', shop.name if shop else ''))
    return code


def _check_tenant_quota(tenant_id, now):
    """全店的驗證碼寄送量：超過上限時拒絕並通知店家 (可能是被拿來灌簡訊)"""
    cfg = settings_service.get('sms', tenant_id)
    hourly = int(cfg.get('tenant_hourly_limit') or DEFAULT_TENANT_HOURLY_LIMIT)
    daily = int(cfg.get('tenant_daily_limit') or DEFAULT_TENANT_DAILY_LIMIT)
    base = OtpCode.query.filter(OtpCode.tenant_id == tenant_id).execution_options(skip_tenant_filter=True)
    last_hour = base.filter(OtpCode.created_at >= now - timedelta(hours=1)).count()
    last_day = base.filter(OtpCode.created_at >= now - timedelta(days=1)).count()
    if last_hour >= hourly or last_day >= daily:
        monitor_service.alert(f'簡訊驗證碼已達全店上限 (一小時 {last_hour}/{hourly}、一天 {last_day}/{daily})，暫停發送；'
                              f'若非活動期間請確認是否被惡意灌簡訊 (系統設定 → 簡訊驗證 可調整上限)',
                              key=f'sms-quota-{tenant_id}', tenant_id=tenant_id, level='warning', source='sms')
        raise AppError('本店簡訊驗證碼暫時額滿，請稍後再試或洽櫃檯', 429)


def verify_otp(phone, purpose, code):
    """驗證並標記為已驗證 (之後 30 分鐘內可用來完成註冊 / 重設 PIN)；錯誤拋 AppError"""
    now = datetime.now()
    otp = OtpCode.query.filter(OtpCode.phone == phone, OtpCode.purpose == purpose, OtpCode.consumed_at.is_(None),
                               OtpCode.expires_at >= now).order_by(OtpCode.id.desc()).first()
    if otp is None:
        raise AppError('驗證碼已過期或不存在，請重新發送', 400)
    if otp.attempts >= OTP_MAX_ATTEMPTS:
        raise AppError('驗證碼錯誤次數過多，請重新發送', 429)
    otp.attempts += 1
    if not check_password_hash(otp.code_hash, str(code or '').strip()):
        db.session.commit()
        raise AppError('驗證碼不正確', 400)
    otp.verified_at = now
    return otp


def consume_verified(phone, purpose, code=None):
    """註冊 / 重設 PIN 時：有效的驗證 (提供 code 則當場驗證) → 標記已使用；回傳是否通過"""
    now = datetime.now()
    if code:
        otp = verify_otp(phone, purpose, code)
    else:
        otp = OtpCode.query.filter(OtpCode.phone == phone, OtpCode.purpose == purpose, OtpCode.consumed_at.is_(None),
                                   OtpCode.verified_at >= now - VERIFIED_TTL).order_by(OtpCode.id.desc()).first()
        if otp is None:
            raise AppError('請先完成手機簡訊驗證', 400, need_otp=True)
    otp.consumed_at = now
    return True


@settings_service.register_tester('sms')
def _test(cfg):
    if cfg.get('provider') == 'none':
        raise AppError('請先選擇簡訊服務')
    if cfg.get('provider') == 'log':
        return '目前為「只記錄」模式：驗證碼會寫在系統監控頁，不會真的發送'
    if not cfg.get('username') or not cfg.get('password'):
        raise AppError('請填寫簡訊帳號與密碼')
    return '帳號密碼已填寫 (為避免產生費用，測試不實際發送；請以註冊流程發送一封驗證碼確認)'
