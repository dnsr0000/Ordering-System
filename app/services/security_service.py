"""
員工帳號安全：兩步驟驗證 (TOTP，RFC 6238) 與登入工作階段管理。

兩步驟驗證
- 金鑰 20 bytes (base32)，30 秒一組 6 位數，容許前後各一個時間窗；同一組驗證碼不能重複使用 (記錄最後使用的時間窗)。
- 啟用時產生 10 組一次性備用碼 (只存雜湊)，手機遺失時使用；用完一組就失效。
- 「系統設定 → 帳號安全」可要求負責人 / 店長必須啟用；未啟用前登入後只能進入「帳號安全」頁設定。

工作階段
- 每次以帳密 (與兩步驟驗證) 登入建立一筆 StaffSession (裝置、IP、最後活動時間)，可個別登出；
- 「登出所有裝置」：遞增帳號的 session_epoch (同步到各分店)，所有舊的登入一律失效；
- 閒置超過設定分鐘數自動登出 (0 = 不限制，門市收銀 / 廚房螢幕通常整天開著)。
"""
import base64
import hashlib
import hmac
import json
import secrets
import struct
import time
import urllib.parse
from datetime import datetime, timedelta

from flask import request, session
from werkzeug.security import check_password_hash, generate_password_hash

from app.errors import AppError
from app.extensions import db

TOTP_STEP = 30
TOTP_DIGITS = 6
BACKUP_CODE_COUNT = 10
SESSION_KEY = 'staff_sid'
PENDING_KEY = 'pending_2fa'
PENDING_TTL = 300           # 輸入密碼後 5 分鐘內要完成兩步驟驗證
TOUCH_INTERVAL = 60         # 最後活動時間每分鐘最多寫入一次


# ---------------------------------------------------------------------------
# TOTP
# ---------------------------------------------------------------------------
def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode('ascii').rstrip('=')


def _key(secret):
    s = str(secret or '').strip().replace(' ', '').upper()
    return base64.b32decode(s + '=' * (-len(s) % 8))


def hotp(secret, counter):
    digest = hmac.new(_key(secret), struct.pack('>Q', counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack('>I', digest[offset:offset + 4])[0] & 0x7FFFFFFF) % (10 ** TOTP_DIGITS)
    return f'{code:0{TOTP_DIGITS}d}'


def totp_now(secret, at=None):
    return hotp(secret, int((at if at is not None else time.time()) // TOTP_STEP))


def match_totp(secret, code, last_counter=0, at=None):
    """驗證碼正確且未使用過時回傳該時間窗 (之後存為 last_counter)，否則回傳 None"""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code) != TOTP_DIGITS or not secret:
        return None
    now = int((at if at is not None else time.time()) // TOTP_STEP)
    for counter in (now - 1, now, now + 1):
        if counter > (last_counter or 0) and hmac.compare_digest(hotp(secret, counter), code):
            return counter
    return None


def provisioning_uri(secret, username, issuer):
    label = urllib.parse.quote(f'{issuer}:{username}')
    query = urllib.parse.urlencode({'secret': secret, 'issuer': issuer, 'digits': TOTP_DIGITS, 'period': TOTP_STEP})
    return f'otpauth://totp/{label}?{query}'


def new_backup_codes():
    """回傳 (明碼清單, 存檔用的雜湊 JSON)"""
    codes = [f'{secrets.randbelow(10 ** 8):08d}' for _ in range(BACKUP_CODE_COUNT)]
    codes = [f'{c[:4]}-{c[4:]}' for c in codes]
    return codes, json.dumps([generate_password_hash(c.replace('-', '')) for c in codes])


def use_backup_code(staff, code):
    """一次性備用碼：正確則移除該組並回傳 True"""
    code = ''.join(ch for ch in str(code or '') if ch.isdigit())
    if len(code) != 8:
        return False
    try:
        hashes = json.loads(staff.totp_backup_codes or '[]')
    except ValueError:
        hashes = []
    for h in hashes:
        if check_password_hash(h, code):
            hashes.remove(h)
            staff.totp_backup_codes = json.dumps(hashes)
            return True
    return False


def remaining_backup_codes(staff):
    try:
        return len(json.loads(staff.totp_backup_codes or '[]'))
    except ValueError:
        return 0


def verify_second_factor(staff, code):
    """登入第二步 / 停用時確認：TOTP 或備用碼。成功回傳使用的方式 ('totp' / 'backup')，失敗回傳 None"""
    counter = match_totp(staff.totp_secret, code, staff.totp_last_counter or 0)
    if counter is not None:
        staff.totp_last_counter = counter
        return 'totp'
    if use_backup_code(staff, code):
        return 'backup'
    return None


def required_for(staff, tenant_id=None):
    """系統設定要求這個角色必須啟用兩步驟驗證"""
    from app.services import settings_service
    roles = settings_service.get('security', tenant_id or staff.tenant_id).get('require_2fa_roles') or []
    return staff.role in roles


# ---------------------------------------------------------------------------
# 工作階段
# ---------------------------------------------------------------------------
def _hash(token):
    return hashlib.sha256(token.encode('utf-8')).hexdigest()


def _device_label(ua):
    ua = ua or ''
    os_name = next((n for k, n in (('Windows', 'Windows'), ('Android', 'Android'), ('iPhone', 'iPhone'), ('iPad', 'iPad'),
                                   ('Mac OS', 'macOS'), ('Linux', 'Linux')) if k in ua), '其他裝置')
    browser = next((n for k, n in (('Edg/', 'Edge'), ('Chrome/', 'Chrome'), ('Firefox/', 'Firefox'), ('Safari/', 'Safari'))
                    if k in ua), '瀏覽器')
    return f'{browser} / {os_name}'


def start_session(staff):
    """登入成功：建立工作階段紀錄並記在 Flask session"""
    from app.models.tenant import StaffSession
    token = secrets.token_urlsafe(32)
    ua = (request.headers.get('User-Agent') or '')[:300]
    now = datetime.now()
    db.session.add(StaffSession(staff_account_id=staff.id, token_hash=_hash(token), created_at=now, last_seen=now,
                                ip_address=(request.remote_addr or '')[:50], user_agent=ua, device=_device_label(ua)))
    session[SESSION_KEY] = token
    session['staff_sid_seen'] = time.time()


def current_session_row():
    from app.models.tenant import StaffSession
    token = session.get(SESSION_KEY)
    if not token:
        return None
    return StaffSession.query.filter_by(token_hash=_hash(token)).first()


def session_is_valid(staff):
    """user_loader 呼叫：工作階段已被登出 / 閒置逾時 → False。舊版登入 (沒有紀錄) 視為有效，「登出所有裝置」仍會讓它失效。"""
    token = session.get(SESSION_KEY)
    if not token:
        return True
    row = current_session_row()
    if row is None or row.revoked_at is not None or row.staff_account_id != staff.id:
        return False
    idle = idle_minutes(staff)
    if idle and row.last_seen and datetime.now() - row.last_seen > timedelta(minutes=idle):
        row.revoked_at, row.revoked_reason = datetime.now(), f'閒置超過 {idle} 分鐘'
        db.session.commit()
        return False
    if time.time() - float(session.get('staff_sid_seen') or 0) > TOUCH_INTERVAL:
        session['staff_sid_seen'] = time.time()
        row.last_seen = datetime.now()
        db.session.commit()
    return True


def idle_minutes(staff):
    from app.services import settings_service
    try:
        return int(settings_service.get('security', staff.tenant_id).get('staff_idle_minutes') or 0)
    except RuntimeError:
        return 0


def end_current_session(reason='登出'):
    row = current_session_row()
    if row is not None and row.revoked_at is None:
        row.revoked_at, row.revoked_reason = datetime.now(), reason
    session.pop(SESSION_KEY, None)


def active_sessions(staff):
    from app.models.tenant import StaffSession
    return StaffSession.query.filter_by(staff_account_id=staff.id, revoked_at=None) \
        .order_by(StaffSession.last_seen.desc()).all()


def revoke_session(staff, session_id, reason='手動登出'):
    from app.models.tenant import StaffSession
    row = db.session.get(StaffSession, session_id)
    if row is None or row.staff_account_id != staff.id:
        raise AppError('查無此登入裝置', 404)
    if row.revoked_at is None:
        row.revoked_at, row.revoked_reason = datetime.now(), reason
    return row


def logout_everywhere(staff, reason='登出所有裝置'):
    """所有裝置一律登出 (包含其他分店)：遞增 session_epoch 並註銷本機的工作階段紀錄。由呼叫端 commit。"""
    from app.models.tenant import StaffSession
    staff.session_epoch = (staff.session_epoch or 0) + 1
    now = datetime.now()
    StaffSession.query.filter_by(staff_account_id=staff.id, revoked_at=None) \
        .update({'revoked_at': now, 'revoked_reason': reason}, synchronize_session=False)
