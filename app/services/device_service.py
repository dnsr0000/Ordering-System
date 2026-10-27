"""
裝置管理 (第三輪 #15)：點餐機 / POS / 廚房看板等裝置的註冊 (配對碼)、心跳、版本、遠端鎖定與重新整理。

- 後台新增裝置 → 顯示 6 位數配對碼 (15 分鐘內有效) → 在該裝置的瀏覽器開啟 /device/pair 輸入 → 瀏覽器記住裝置權杖
  (HttpOnly Cookie，資料庫只存雜湊)。
- 已註冊的裝置頁面每分鐘回報心跳 (程式版本、目前頁面、IP)；後台顯示是否在線 (3 分鐘內有心跳) 與版本是否為最新。
- 遠端鎖定：畫面蓋上鎖定訊息，且該裝置送出的操作 (POST) 一律拒絕 (423)，員工登入除外；解除後自動恢復。
- 要求重新整理：下次心跳時重新載入頁面 (更新程式後套用新版前端)。
未註冊的裝置照常使用 (不強制)。
"""
import hashlib
import os
import secrets
from datetime import datetime, timedelta

from flask import current_app, g, has_request_context, request
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.integration import DEVICE_KINDS, Device, hash_secret

COOKIE = 'kiosk_device'
PAIR_MINUTES = 15
START_PAGES = {'kiosk': '/', 'pos': '/pos', 'kitchen': '/kitchen', 'display': '/counter', 'other': '/'}
_VERSION = {}


def app_version():
    """前端程式版本：static/js、static/css 檔案大小與修改時間的雜湊 (更新程式後改變)"""
    root = current_app.static_folder
    if root not in _VERSION:
        h = hashlib.sha1()
        for sub in ('js', 'css'):
            base = os.path.join(root, sub)
            for name in sorted(os.listdir(base)) if os.path.isdir(base) else ():
                st = os.stat(os.path.join(base, name))
                h.update(f'{sub}/{name}:{st.st_size}:{int(st.st_mtime)}'.encode())
        _VERSION[root] = h.hexdigest()[:8]
    return _VERSION[root]


def new_pair_code(device, now=None):
    now = now or datetime.now()
    device.pair_code = f'{secrets.randbelow(10 ** 6):06d}'
    device.pair_expires_at = now + timedelta(minutes=PAIR_MINUTES)
    return device.pair_code


def create(tenant_id, name, kind, note=''):
    name = str(name or '').strip()[:60]
    if not name:
        raise AppError('請輸入裝置名稱')
    if kind not in DEVICE_KINDS:
        raise AppError('請選擇裝置類型')
    d = Device(tenant_id=tenant_id, name=name, kind=kind, note=str(note or '').strip()[:120])
    new_pair_code(d)
    db.session.add(d)
    return d


def pair(tenant_id, code, now=None):
    """配對碼 → (裝置, 原始權杖)。舊權杖 (同一台重新配對) 立即失效。由呼叫端 commit。"""
    now = now or datetime.now()
    code = str(code or '').strip()
    d = Device.query.filter(Device.tenant_id == tenant_id, Device.pair_code == code).first() if len(code) == 6 else None
    if d is None or d.pair_expires_at is None or d.pair_expires_at < now:
        raise AppError('配對碼錯誤或已過期，請在後台「裝置管理」重新產生')
    raw = 'kd_' + secrets.token_urlsafe(32)
    d.token_hash, d.pair_code, d.pair_expires_at, d.paired_at = hash_secret(raw), None, None, now
    d.last_seen_at = now
    return d, raw


def current():
    """本次請求的裝置 (依 Cookie)；未註冊時為 None"""
    if not has_request_context():
        return None
    if '_device' not in g:
        raw = request.cookies.get(COOKIE)
        d = None
        if raw:
            d = Device.query.filter_by(token_hash=hash_secret(raw)).execution_options(skip_tenant_filter=True).first()
        g._device = d
    return g._device


def heartbeat(d, version='', path='', now=None):
    now = now or datetime.now()
    d.last_seen_at = now
    d.app_version = str(version or '')[:20]
    d.last_path = str(path or '')[:120]
    d.last_ip = (request.remote_addr or '')[:50] if has_request_context() else ''
    d.user_agent = (request.headers.get('User-Agent') or '')[:200] if has_request_context() else ''
    reload_now = False
    if d.reload_requested_at is not None:
        reload_now = bool(db.session.execute(
            update(Device).where(Device.id == d.id, Device.reload_requested_at.is_not(None))
            .values(reload_requested_at=None).execution_options(synchronize_session=False)).rowcount)
    return {'success': True, 'registered': True, 'name': d.name, 'locked': bool(d.is_locked),
            'message': d.lock_message or '此裝置暫停使用，請洽櫃檯', 'reload': reload_now,
            'version_ok': d.app_version == app_version(), 'server_version': app_version()}


ALLOWED_WHEN_LOCKED = ('devices.heartbeat', 'devices.pair', 'admin.admin_dashboard', 'static')   # 員工仍可登入後台


def enforce_lock():
    """before_request：鎖定的裝置不可送出操作"""
    if request.method in ('GET', 'HEAD', 'OPTIONS') or not request.cookies.get(COOKIE):
        return None
    if request.endpoint in ALLOWED_WHEN_LOCKED or (request.endpoint or '').startswith('auth.'):
        return None
    d = current()
    if d is not None and d.is_locked:
        raise AppError(f'此裝置已被遠端鎖定：{d.lock_message or "暫停使用"}', 423)
    return None


def status_rows(tenant_id, now=None):
    now = now or datetime.now()
    version = app_version()
    return [{'d': d, 'online': d.is_online(now), 'version_ok': bool(d.app_version) and d.app_version == version,
             'pairing': bool(d.pair_code and d.pair_expires_at and d.pair_expires_at >= now)}
            for d in Device.query.filter_by(tenant_id=tenant_id).order_by(Device.kind, Device.name).all()]
