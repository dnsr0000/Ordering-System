"""
門市一鍵更新 (第六輪)：
- 目前版本：專案根目錄 VERSION 檔 (+ git 短雜湊，若有 .git)。
- 最新版本：GitHub Releases (UPDATE_REPO，預設 dnsr0000/Ordering-System)，沒有 release 時退回最新 tag；快取 1 小時。
- 下載：release 的 zipball 存到 database/updates/<tag>.zip (只下載，不套用)。
- 套用：Windows 門市主機執行 deploy\\windows\\update_store.cmd <zip> <pid>：備份 → 停伺服器 → 覆蓋程式檔 →
  pip install → alembic upgrade → 重啟 (失敗自動還原)。後台「套用並重啟」會以獨立行程啟動該腳本。
線上端 (Docker) 不走這條：DEPLOY.txt 的「更新版本」。
"""
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlparse

import requests
from flask import current_app

from app.errors import AppError

logger = logging.getLogger(__name__)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
DEFAULT_REPO = 'dnsr0000/Ordering-System'
CACHE_SECONDS = 3600
_cache: dict[str, Any] = {'at': 0.0, 'data': None}
TAG_RE = re.compile(r'^[\w.\-]{1,40}$')


def repo() -> str:
    return (current_app.config.get('UPDATE_REPO') or os.environ.get('UPDATE_REPO') or DEFAULT_REPO).strip('/ ')


def updates_dir() -> str:
    from app.services.maintenance_service import backup_dir
    return os.path.join(os.path.dirname(backup_dir()), 'updates')


def current_version() -> dict[str, str]:
    version = ''
    try:
        with open(os.path.join(ROOT, 'VERSION'), encoding='utf-8') as fh:
            version = fh.read().strip()
    except OSError:
        pass
    commit = ''
    if os.path.isdir(os.path.join(ROOT, '.git')):
        try:
            commit = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'], cwd=ROOT, capture_output=True, text=True,
                                    timeout=5, check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            commit = ''
    return {'version': version or '未知', 'commit': commit}


def _version_key(text: str) -> tuple:
    parts = re.findall(r'\d+|[A-Za-z]+', (text or '').lstrip('vV'))
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in parts)


def is_newer(latest: str, current: str) -> bool:
    if not latest or not current or current == '未知':
        return bool(latest) and current != latest
    return _version_key(latest) > _version_key(current)


def _fetch_latest() -> dict[str, Any] | None:
    base = f'https://api.github.com/repos/{repo()}'
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': 'kiosk-updater'}
    try:
        r = requests.get(f'{base}/releases/latest', headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return {'tag': d.get('tag_name') or '', 'name': d.get('name') or d.get('tag_name') or '',
                    'published_at': (d.get('published_at') or '')[:10], 'notes': (d.get('body') or '')[:3000],
                    'zip_url': d.get('zipball_url') or '', 'url': d.get('html_url') or ''}
        r = requests.get(f'{base}/tags?per_page=1', headers=headers, timeout=10)
        if r.status_code == 200 and r.json():
            d = r.json()[0]
            return {'tag': d.get('name') or '', 'name': d.get('name') or '', 'published_at': '', 'notes': '',
                    'zip_url': d.get('zipball_url') or '', 'url': f'https://github.com/{repo()}/releases'}
        logger.warning('查詢新版本失敗 (HTTP %s)', r.status_code)
    except (requests.RequestException, ValueError) as exc:
        logger.warning('查詢新版本失敗：%s', exc)
    return None


def latest_release(force: bool = False) -> dict[str, Any] | None:
    if not force and _cache['data'] is not None and time.time() - _cache['at'] < CACHE_SECONDS:
        return _cache['data']
    data = _fetch_latest()
    if data is not None or force:
        _cache.update(at=time.time(), data=data)
    return data


def check(force: bool = False) -> dict[str, Any]:
    cur = current_version()
    latest = latest_release(force)
    tag = (latest or {}).get('tag') or ''
    downloaded = os.path.join(updates_dir(), f'{_safe_tag(tag)}.zip') if tag else ''
    return {**cur, 'latest': latest, 'repo': repo(), 'update_available': is_newer(tag, cur['version']),
            'downloaded': downloaded if downloaded and os.path.isfile(downloaded) else '',
            'can_apply': os.name == 'nt', 'checked_at': _cache['at']}


MAX_DOWNLOAD_BYTES = 300 * 1024 * 1024
ALLOWED_HOSTS = ('github.com', 'api.github.com', 'codeload.github.com', 'objects.githubusercontent.com')


def _safe_tag(tag: str) -> str:
    tag = (tag or '').strip()
    if not TAG_RE.match(tag) or tag.startswith('.') or '..' in tag:
        raise AppError('版本代號格式不正確')
    return tag


def _github_url(url: str) -> str:
    """只從 GitHub 下載 (release 資料來自 GitHub API，仍再檢查一次，避免被導到別處)"""
    host = (urlparse(url).hostname or '').lower()
    if urlparse(url).scheme != 'https' or host not in ALLOWED_HOSTS:
        raise AppError('更新檔網址不是 GitHub，拒絕下載')
    return url


def download(tag: str | None = None) -> str:
    """下載 release 壓縮檔到 database/updates/<tag>.zip；回傳路徑"""
    latest = latest_release()
    if latest is None or not latest.get('zip_url'):
        raise AppError('目前查不到新版本 (請確認主機可連到 github.com)')
    tag = _safe_tag(tag or latest['tag'])
    if tag != latest['tag']:
        raise AppError('只能下載最新版本')
    os.makedirs(updates_dir(), exist_ok=True)
    target = os.path.join(updates_dir(), f'{tag}.zip')
    try:
        with requests.get(_github_url(latest['zip_url']), headers={'User-Agent': 'kiosk-updater'}, timeout=120, stream=True) as r:
            if r.status_code != 200:
                raise AppError(f'下載失敗 (HTTP {r.status_code})')
            tmp = target + '.part'
            size = 0
            with open(tmp, 'wb') as fh:
                for chunk in r.iter_content(1024 * 256):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise AppError('更新檔超過 300 MB，拒絕下載')
                    fh.write(chunk)
        os.replace(tmp, target)
    except requests.RequestException as exc:
        raise AppError(f'下載失敗：{exc}')
    import zipfile
    if not zipfile.is_zipfile(target):
        os.remove(target)
        raise AppError('下載的檔案不是有效的壓縮檔')
    return target


def apply(zip_path: str) -> str:
    """Windows 門市：以獨立行程啟動更新腳本 (會停掉目前伺服器、覆蓋、升級、重啟)。回傳腳本路徑。"""
    if os.name != 'nt':
        raise AppError('一鍵套用只支援 Windows 門市主機；線上端請依 DEPLOY.txt「更新版本」操作')
    if not os.path.isfile(zip_path) or os.path.dirname(os.path.abspath(zip_path)) != os.path.abspath(updates_dir()):
        raise AppError('請先下載更新檔')
    script = os.path.join(ROOT, 'deploy', 'windows', 'update_store.cmd')
    if not os.path.isfile(script):
        raise AppError('找不到 deploy\\windows\\update_store.cmd')
    # cmd.exe 逐行讀批次檔：更新會覆蓋 deploy\windows\update_store.cmd 本身，所以先複製到 database\updates 再從複本執行
    import shutil
    running = os.path.join(updates_dir(), 'update_store.run.cmd')
    shutil.copyfile(script, running)
    creation = getattr(subprocess, 'DETACHED_PROCESS', 0) | getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0)
    subprocess.Popen(['cmd.exe', '/c', running, zip_path, str(os.getpid()), sys.executable], cwd=ROOT,
                     creationflags=creation, close_fds=True)
    return running
