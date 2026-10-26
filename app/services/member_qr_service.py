"""
會員動態條碼：會員在手機「紅利商城」開啟 QR 碼，櫃檯以掃碼槍 (或手動輸入) 掃描即可帶出會員，不必報手機號碼。

- 內容為 KM1.<會員 uuid>.<時間窗>.<簽章>；時間窗 60 秒，簽章為以會員專屬金鑰 (qr_secret，會同步到各分店) 計算的 HMAC。
- 最近 3 個時間窗內有效 (約 3 分鐘，並容許主機時間差 1 分鐘)，截圖外流也很快失效；會員可按「重新產生」讓舊碼立即失效。
"""
import hashlib
import hmac
import math
import re
import secrets
import time

from app.errors import AppError
from app.models.user import User

PREFIX = 'KM1'
WINDOW = 60
VALID_WINDOWS = 3
TOKEN_RE = re.compile(r'^KM1\.([0-9a-fA-F-]{36})\.(\d{1,12})\.([0-9a-f]{20})$')


def ensure_secret(user):
    """第一次使用時產生會員金鑰 (由呼叫端 commit)"""
    if not user.qr_secret:
        user.qr_secret = secrets.token_hex(20)
    return user.qr_secret


def rotate_secret(user):
    user.qr_secret = secrets.token_hex(20)
    return user.qr_secret


def _sign(secret, user_uuid, window):
    return hmac.new(secret.encode('utf-8'), f'{user_uuid}.{window}'.encode('utf-8'), hashlib.sha256).hexdigest()[:20]


def make_token(user, at=None):
    window = int((at if at is not None else time.time()) // WINDOW)
    return f'{PREFIX}.{user.uuid}.{window}.{_sign(ensure_secret(user), user.uuid, window)}'


def seconds_left(at=None):
    now = at if at is not None else time.time()
    return max(1, math.ceil(WINDOW - now % WINDOW))


def looks_like_token(text):
    return str(text or '').strip().startswith(PREFIX + '.')


def resolve(text, at=None):
    """驗證掃描到的會員條碼並回傳會員 (本店，依目前店家自動過濾)；無效或過期拋出 AppError"""
    m = TOKEN_RE.match(str(text or '').strip())
    if not m:
        raise AppError('無法辨識的會員條碼', 400)
    user_uuid, window, sig = m.group(1).lower(), int(m.group(2)), m.group(3)
    now = int((at if at is not None else time.time()) // WINDOW)
    if not (now - VALID_WINDOWS + 1 <= window <= now + 1):
        raise AppError('會員條碼已過期：請顧客重新開啟會員條碼後再掃描', 410)
    user = User.query.filter_by(uuid=user_uuid).first()
    if user is None or not user.qr_secret or not hmac.compare_digest(_sign(user.qr_secret, user.uuid, window), sig):
        raise AppError('會員條碼無效 (可能剛重新產生，或資料尚未同步到本店)：請改用手機號碼查詢', 404)
    return user
