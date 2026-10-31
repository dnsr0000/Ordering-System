"""
PIN 登入的失敗鎖定 (資料庫保存，重啟不會清空、多行程共用)。三個層級：
- 裝置：同一 IP 對同一手機連續錯誤 DEVICE_MAX_FAILS 次 → 鎖定
- 帳號：同一手機不分來源 IP 累計 ACCOUNT_MAX_FAILS 次 → 鎖定，且每次再鎖定時間加倍 (攻擊者輪換 IP 也無法繞過)
- IP ：同一 IP 對任意手機累計 IP_MAX_FAILS 次 → 鎖定 (防噴灑式猜測；門市 kiosk 共用 IP，門檻較寬)
不論手機號碼是否存在都同樣計數，無法藉由鎖定行為列舉會員。

員工帳號密碼登入另有兩層 (帳號名稱全系統唯一)：同一 IP 對同一帳號 STAFF_DEVICE_MAX_FAILS 次、
同一帳號不分 IP 累計 STAFF_ACCOUNT_MAX_FAILS 次 → 鎖定 LOCK_DURATION (不加倍，避免被惡意鎖死太久)。
"""
from datetime import datetime, timedelta
from functools import lru_cache

from sqlalchemy import delete, or_
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models.system import AuthThrottle

DEVICE_MAX_FAILS = 5
ACCOUNT_MAX_FAILS = 10
IP_MAX_FAILS = 30
FAIL_WINDOW = timedelta(minutes=15)
LOCK_DURATION = timedelta(minutes=15)
MAX_LOCK_DURATION = timedelta(hours=24)
STAFF_DEVICE_MAX_FAILS = 5
STAFF_ACCOUNT_MAX_FAILS = 10


def pin_keys(tenant_id, phone, ip):
    return {
        'device': f"pin:dev:{tenant_id}:{phone}:{ip}",
        'account': f"pin:acct:{tenant_id}:{phone}",
        'ip': f"pin:ip:{tenant_id}:{ip}",
    }


def lock_remaining(keys):
    """任一鍵仍在鎖定中時回傳剩餘時間 (timedelta)，否則回傳 None"""
    now = datetime.now()
    rows = AuthThrottle.query.filter(AuthThrottle.key.in_(list(keys)), AuthThrottle.locked_until > now).all()
    return max((r.locked_until - now for r in rows), default=None)


def record_failure(key, max_fails, escalate=False):
    now = datetime.now()
    row = db.session.get(AuthThrottle, key)
    if row is None:
        row = AuthThrottle(key=key, fails=0, strikes=0, window_start=now)
        try:
            with db.session.begin_nested():
                db.session.add(row)
        except IntegrityError:  # 另一個請求剛建立同一個鍵
            row = db.session.get(AuthThrottle, key)
    not_locked = row.locked_until is None or row.locked_until <= now
    if not_locked and (row.window_start is None or now - row.window_start > FAIL_WINDOW):
        row.fails, row.window_start = 0, now
    row.fails += 1
    if row.fails >= max_fails:
        row.strikes += 1
        duration = LOCK_DURATION * (2 ** (row.strikes - 1)) if escalate else LOCK_DURATION
        row.locked_until = now + min(duration, MAX_LOCK_DURATION)
        row.fails, row.window_start = 0, now


def record_pin_failure(keys, ip_only=False):
    record_failure(keys['ip'], IP_MAX_FAILS)
    if not ip_only:
        record_failure(keys['device'], DEVICE_MAX_FAILS)
        record_failure(keys['account'], ACCOUNT_MAX_FAILS, escalate=True)
    db.session.commit()


def clear_keys(*keys):
    db.session.execute(delete(AuthThrottle).where(AuthThrottle.key.in_(keys)))
    db.session.commit()


def clear_pin_lockout(tenant_id, phone):
    """櫃檯協助設定/重設 PIN 時解除該會員的裝置與帳號鎖定 (不影響 IP 層級的噴灑防護)"""
    phone = str(phone).strip()
    db.session.execute(delete(AuthThrottle).where(or_(
        AuthThrottle.key == f"pin:acct:{tenant_id}:{phone}",
        AuthThrottle.key.like(f"pin:dev:{tenant_id}:{phone}:%"),
    )))
    db.session.commit()


def staff_keys(username, ip):
    name = (username or '').strip().lower()[:80]
    return {'device': f"staff:dev:{name}:{ip}", 'account': f"staff:acct:{name}"}


def staff_lock_remaining(keys):
    """
    員工帳號的鎖定狀態。帳號層級 (不分 IP 累計) 的鎖定是防止換 IP 猜密碼，但也讓知道帳號名稱的人
    (例如打卡看板、收據上的收銀員) 能故意連錯把店員鎖在外面；因此在後台「裝置管理」註冊過的店內裝置
    (POS、點餐機、廚房) 只受裝置層級 (同 IP 同帳號 5 次) 的鎖定，不受帳號層級鎖定影響。
    """
    from app.services import device_service
    device = device_service.current()
    if device is not None and not device.is_locked:
        return lock_remaining([keys['device']])
    return lock_remaining(keys.values())


def record_staff_failure(keys):
    record_failure(keys['device'], STAFF_DEVICE_MAX_FAILS)
    record_failure(keys['account'], STAFF_ACCOUNT_MAX_FAILS)
    db.session.commit()


# 優惠代碼猜測：同一來源 IP / 同一會員輸入不存在的代碼 (或未持有的會員專屬券) 太多次 → 暫停查詢公開代碼
PROMO_IP_MAX_FAILS = 20
PROMO_MEMBER_MAX_FAILS = 10


def promo_keys(tenant_id, ip, user_id=None):
    keys = [f"promo:ip:{tenant_id}:{ip or '-'}"]
    if user_id:
        keys.append(f"promo:member:{tenant_id}:{user_id}")
    return keys


def record_promo_failure(tenant_id, ip, user_id=None):
    """記錄一次無效代碼 (由呼叫端 commit，結帳時與訂單同一交易)"""
    keys = promo_keys(tenant_id, ip, user_id)
    record_failure(keys[0], PROMO_IP_MAX_FAILS)
    if len(keys) > 1:
        record_failure(keys[1], PROMO_MEMBER_MAX_FAILS)


@lru_cache(maxsize=1)
def _dummy_hash():
    from werkzeug.security import generate_password_hash
    return generate_password_hash('dummy-password-for-timing')


def spend_hash_time(secret):
    """帳號不存在時也做一次同樣成本的雜湊比對，讓回應時間無法用來判斷帳號 / 手機是否存在"""
    from werkzeug.security import check_password_hash
    check_password_hash(_dummy_hash(), str(secret or ''))
