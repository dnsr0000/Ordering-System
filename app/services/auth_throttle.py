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

from sqlalchemy import delete, func, or_, update
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


def record_failure(key, max_fails, escalate=False, only_unlocked=False):
    """
    失敗次數 +1，達到門檻即鎖定 (由呼叫端 commit)。計數以 UPDATE ... SET fails = fails + 1 原子遞增：
    同時送出的請求不會讀到同一個值再互相覆蓋 (遞增後該列已被本交易鎖住，之後的鎖定判斷不會被併發改動)。
    only_unlocked=True：該鍵正在鎖定中時不計數並回傳 False (預扣額度用)；其餘回傳 True。
    """
    now = datetime.now()
    if db.session.get(AuthThrottle, key) is None:
        try:
            with db.session.begin_nested():
                db.session.add(AuthThrottle(key=key, fails=0, strikes=0, window_start=now))
        except IntegrityError:  # 另一個請求剛建立同一個鍵
            pass
    unlocked = or_(AuthThrottle.locked_until.is_(None), AuthThrottle.locked_until <= now)
    # 未鎖定且計數視窗已過：重新計算
    db.session.execute(update(AuthThrottle)
                       .where(AuthThrottle.key == key, unlocked,
                              or_(AuthThrottle.window_start.is_(None), AuthThrottle.window_start < now - FAIL_WINDOW))
                       .values(fails=0, window_start=now).execution_options(synchronize_session=False))
    conds = [AuthThrottle.key == key] + ([unlocked] if only_unlocked else [])
    counted = db.session.execute(update(AuthThrottle).where(*conds).values(fails=AuthThrottle.fails + 1)
                                 .execution_options(synchronize_session=False)).rowcount
    row = db.session.get(AuthThrottle, key, populate_existing=True)
    if counted and row is not None and row.fails >= max_fails:
        row.strikes = (row.strikes or 0) + 1
        duration = LOCK_DURATION * (2 ** (row.strikes - 1)) if escalate else LOCK_DURATION
        row.locked_until = now + min(duration, MAX_LOCK_DURATION)
        row.fails, row.window_start = 0, now
    return bool(counted)


def record_pin_failure(keys, ip_only=False):
    record_failure(keys['ip'], IP_MAX_FAILS)
    if not ip_only:
        record_failure(keys['device'], DEVICE_MAX_FAILS)
        record_failure(keys['account'], ACCOUNT_MAX_FAILS, escalate=True)
    db.session.commit()


def reserve_pin_attempt(keys):
    """
    驗證 PIN 之前先預扣一次裝置與帳號層級的額度 (原子遞增並 commit)：先檢查、驗證完才計數時，
    同時送出的一批請求都會通過「未鎖定」的檢查，鎖定形同虛設。回傳 None 表示可以驗證；
    已鎖定 (含被同時送出的其他請求用完額度) 回傳剩餘時間。
    驗證成功請 clear_keys(device, account)；失敗再以 record_pin_failure(keys, ip_only=True) 計入 IP 層級。
    """
    ok = (record_failure(keys['device'], DEVICE_MAX_FAILS, only_unlocked=True)
          and record_failure(keys['account'], ACCOUNT_MAX_FAILS, escalate=True, only_unlocked=True))
    db.session.commit()
    if ok:
        return None
    return lock_remaining(keys.values()) or timedelta(minutes=1)


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
    只有裝置與被登入的帳號屬於同一家店才豁免：裝置是否屬於「本店」看的是 g.tenant_id (未登入時由 session /
    /shop/<代號> 決定，攻擊者可控)，別家店的裝置 (或帳號不存在) 一律套用帳號層級鎖定，否則任何店家都能拿
    自己店的裝置 Cookie 換 IP 猜別家店 (含平台管理者) 的密碼。
    """
    from app.services import device_service
    device = device_service.current()
    if device is not None and not device.is_locked and _same_tenant_account(keys, device.tenant_id):
        return lock_remaining([keys['device']])
    return lock_remaining([keys['device'], keys['account']])


def _same_tenant_account(keys, tenant_id):
    """帳號層級鍵對應的員工帳號 (名稱不分大小寫) 存在，且全部屬於 tenant_id"""
    from app.models.tenant import StaffAccount
    name = keys['account'].split(':', 2)[2]
    if not name:
        return False
    owners = [tid for (tid,) in db.session.query(StaffAccount.tenant_id)
              .filter(func.lower(func.substr(StaffAccount.username, 1, 80)) == name).all()]
    return bool(owners) and all(tid == tenant_id for tid in owners)


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
