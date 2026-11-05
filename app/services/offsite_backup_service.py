"""
異地備份：每日 SQLite 備份完成後上傳到 WebDAV / S3 相容儲存 / 自訂指令 (rclone 等)。
- 設定在「自動備份」區塊 (offsite*)；上傳失敗進監控事件 (key offsite-backup)，不影響本機備份。
- S3 以 SigV4 自行簽章 (不依賴 boto3)；WebDAV 用 PUT / PROPFIND / DELETE；都只保留最近 N 份 (offsite_keep)。
- 設定頁的「測試」會上傳一個小檔案 (kiosk-backup-test.txt) 再刪掉。
"""
import datetime as _dt
import hashlib
import hmac
import logging
import os
import re
import shlex
import subprocess
from typing import Any
from urllib.parse import quote, urlparse

import requests

from app.errors import AppError
from app.services import settings_service

logger = logging.getLogger(__name__)
TIMEOUT = 60
BACKUP_RE = re.compile(r'menu-\d{8}-\d{6}\.zip$')


config = settings_service.reader('backup')


def enabled(tenant_id: int | None = None) -> bool:
    return (config(tenant_id).get('offsite') or 'none') != 'none'


# ---------------------------------------------------------------------------
# WebDAV
# ---------------------------------------------------------------------------
def _public_on_hub(url: str) -> None:
    """線上端 (多店家共用主機)：店家填的上傳網址不可指向內網 / 本機 (與 Webhook / SMTP 相同的 SSRF 防護)"""
    from app.services.webhook_service import check_public_host_on_hub
    check_public_host_on_hub(urlparse(url).hostname or '')


def _dav_base(cfg: dict[str, Any]) -> str:
    url = (cfg.get('offsite_url') or '').strip().rstrip('/')
    if not url.startswith(('http://', 'https://')):
        raise AppError('請填寫 WebDAV 資料夾網址 (https://…)')
    _public_on_hub(url)
    return url


def _dav_auth(cfg: dict[str, Any]) -> tuple[str, str] | None:
    return (cfg.get('offsite_user') or '', cfg.get('offsite_password') or '') if cfg.get('offsite_user') else None


def _dav_put(cfg: dict[str, Any], name: str, data: bytes) -> None:
    r = requests.put(f'{_dav_base(cfg)}/{quote(name)}', data=data, auth=_dav_auth(cfg), timeout=TIMEOUT)
    if r.status_code not in (200, 201, 204):
        raise AppError(f'WebDAV 上傳失敗 (HTTP {r.status_code})')


def _dav_list(cfg: dict[str, Any]) -> list[str]:
    r = requests.request('PROPFIND', _dav_base(cfg) + '/', auth=_dav_auth(cfg), headers={'Depth': '1'}, timeout=TIMEOUT)
    if r.status_code not in (207, 200):
        raise AppError(f'WebDAV 列表失敗 (HTTP {r.status_code})')
    names = []
    for href in re.findall(r'<(?:\w+:)?href>([^<]+)</(?:\w+:)?href>', r.text, flags=re.I):
        base = os.path.basename(href.rstrip('/'))
        if BACKUP_RE.search(base):
            names.append(base)
    return sorted(set(names))


def _dav_delete(cfg: dict[str, Any], name: str) -> None:
    requests.delete(f'{_dav_base(cfg)}/{quote(name)}', auth=_dav_auth(cfg), timeout=TIMEOUT)


# ---------------------------------------------------------------------------
# S3 相容 (SigV4)
# ---------------------------------------------------------------------------
def _s3_parts(cfg: dict[str, Any]) -> tuple[str, str, str, str]:
    """(endpoint, bucket, prefix, region)"""
    endpoint = (cfg.get('offsite_url') or '').strip().rstrip('/')
    if not endpoint.startswith(('http://', 'https://')):
        raise AppError('請填寫 S3 端點網址 (https://…)')
    _public_on_hub(endpoint)
    bucket, _, prefix = (cfg.get('offsite_bucket') or '').strip().strip('/').partition('/')
    if not bucket:
        raise AppError('請填寫 S3 Bucket')
    region = (cfg.get('offsite_region') or 'auto').strip() or 'auto'
    return endpoint, bucket, prefix, region


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def _s3_request(cfg: dict[str, Any], method: str, key: str, data: bytes = b'', query: str = '') -> requests.Response:
    endpoint, bucket, prefix, region = _s3_parts(cfg)
    access, secret = cfg.get('offsite_user') or '', cfg.get('offsite_password') or ''
    if not access or not secret:
        raise AppError('請填寫 S3 Access Key / Secret Key')
    host = urlparse(endpoint).netloc
    path = '/' + bucket + ('/' + quote(f'{prefix}/{key}' if prefix else key, safe='/') if key else '')
    now = _dt.datetime.now(_dt.timezone.utc)
    amz_date, date_stamp = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    payload_hash = hashlib.sha256(data).hexdigest()
    headers = {'host': host, 'x-amz-content-sha256': payload_hash, 'x-amz-date': amz_date}
    signed = ';'.join(sorted(headers))
    canonical = '\n'.join([method, path, query, ''.join(f'{k}:{headers[k]}\n' for k in sorted(headers)), signed, payload_hash])
    scope = f'{date_stamp}/{region}/s3/aws4_request'
    to_sign = '\n'.join(['AWS4-HMAC-SHA256', amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    k = _sign(_sign(_sign(_sign(('AWS4' + secret).encode(), date_stamp), region), 's3'), 'aws4_request')
    signature = hmac.new(k, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = f'AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={signature}'
    url = f'{endpoint}{path}' + (f'?{query}' if query else '')
    return requests.request(method, url, data=data, timeout=TIMEOUT,
                            headers={**{k: v for k, v in headers.items() if k != 'host'}, 'Authorization': auth})


def _s3_put(cfg: dict[str, Any], name: str, data: bytes) -> None:
    r = _s3_request(cfg, 'PUT', name, data)
    if r.status_code not in (200, 201):
        raise AppError(f'S3 上傳失敗 (HTTP {r.status_code})：{r.text[:200]}')


def _s3_list(cfg: dict[str, Any]) -> list[str]:
    _e, _b, prefix, _r = _s3_parts(cfg)
    q = 'list-type=2' + (f'&prefix={quote(prefix + "/", safe="")}' if prefix else '')
    r = _s3_request(cfg, 'GET', '', query=q)
    if r.status_code != 200:
        raise AppError(f'S3 列表失敗 (HTTP {r.status_code})')
    return sorted({os.path.basename(k) for k in re.findall(r'<Key>([^<]+)</Key>', r.text) if BACKUP_RE.search(k)})


def _s3_delete(cfg: dict[str, Any], name: str) -> None:
    _s3_request(cfg, 'DELETE', name)


# ---------------------------------------------------------------------------
# 自訂指令
# ---------------------------------------------------------------------------
def _run_command(cfg: dict[str, Any], path: str) -> None:
    """門市主機 (負責人 = 主機管理者) 才可用；線上端多店家共用主機，不讓任何店家在伺服器上執行指令"""
    from app.sync import runtime as sync_runtime
    if sync_runtime.is_hub():
        raise AppError('線上端不提供自訂指令上傳，請改用 WebDAV 或 S3')
    template = (cfg.get('offsite_command') or '').strip()
    if '{file}' not in template:
        raise AppError('自訂指令必須包含 {file}')
    argv = [a.replace('{file}', path) for a in shlex.split(template, posix=os.name != 'nt')]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AppError(f'自訂上傳指令無法執行：{exc}')
    if proc.returncode != 0:
        raise AppError(f'自訂上傳指令失敗 (exit {proc.returncode})：{(proc.stderr or proc.stdout)[-300:]}')


# ---------------------------------------------------------------------------
# 對外
# ---------------------------------------------------------------------------
def upload(path: str, tenant_id: int | None = None, cfg: dict[str, Any] | None = None) -> str:
    """上傳備份檔並修剪異地舊檔；回傳說明文字。失敗拋 AppError。"""
    cfg = cfg or config(tenant_id)
    mode = cfg.get('offsite') or 'none'
    if mode == 'none':
        return '未設定異地備份'
    name = os.path.basename(path)
    if mode == 'command':
        _run_command(cfg, path)
        return f'已執行自訂指令上傳 {name}'
    with open(path, 'rb') as fh:
        data = fh.read()
    put, lister, delete = (_dav_put, _dav_list, _dav_delete) if mode == 'webdav' else (_s3_put, _s3_list, _s3_delete)
    put(cfg, name, data)
    keep = max(1, int(cfg.get('offsite_keep') or 30))
    removed = 0
    try:
        names = lister(cfg)
        for old in names[:-keep] if len(names) > keep else []:
            delete(cfg, old)
            removed += 1
    except AppError as exc:   # 列表 / 刪除失敗不算上傳失敗
        logger.warning('異地備份修剪舊檔失敗：%s', exc.message)
    return f'已上傳 {name} 到 {"WebDAV" if mode == "webdav" else "S3"}' + (f'，刪除 {removed} 份舊備份' if removed else '')


def upload_after_backup(path: str | None, tenant_id: int | None) -> None:
    """每日備份後呼叫：失敗只記錄並通知"""
    if not path or not enabled(tenant_id):
        return
    try:
        logger.info('異地備份：%s', upload(path, tenant_id))
    except AppError as exc:
        from app.services import monitor_service
        monitor_service.alert(f'異地備份上傳失敗：{exc.message}', key='offsite-backup', tenant_id=tenant_id, source='backup')


@settings_service.register_tester('backup')
def _test(cfg: dict[str, Any]) -> str:
    mode = cfg.get('offsite') or 'none'
    if mode == 'none':
        return '未設定異地備份 (本機每日備份照常執行)'
    import tempfile
    stamp = _dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'kiosk-backup-test.txt')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(f'點餐系統異地備份測試 {stamp}\n')
        if mode == 'command':
            _run_command(cfg, path)
            return '自訂指令執行成功 (已上傳 kiosk-backup-test.txt)'
        with open(path, 'rb') as fh:
            data = fh.read()
        if mode == 'webdav':
            _dav_put(cfg, 'kiosk-backup-test.txt', data)
            _dav_delete(cfg, 'kiosk-backup-test.txt')
            return 'WebDAV 上傳 / 刪除測試成功'
        _s3_put(cfg, 'kiosk-backup-test.txt', data)
        _s3_delete(cfg, 'kiosk-backup-test.txt')
        return 'S3 上傳 / 刪除測試成功'
