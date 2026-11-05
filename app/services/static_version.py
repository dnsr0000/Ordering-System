"""
靜態資源版本化 (第五輪)：url_for('static', filename=...) 自動加上 ?v=<檔案內容雜湊>，
更新 JS / CSS 後瀏覽器與 PWA 快取 (sw.js 以 URL 為鍵) 立刻改抓新檔，不必手動清快取。
- 雜湊只算一次並快取 (正式環境)；DEBUG / TESTING 時檔案修改時間變了會重算。
- /sw.js 的快取名稱帶整個 static/ 的版本摘要：部署後 Service Worker 更新，舊快取整批清掉。
"""
import hashlib
import os
import threading

from flask import Flask, current_app

_cache: dict[str, tuple[float, str]] = {}        # filename -> (mtime, digest)
_lock = threading.Lock()
_bundle = {'digest': ''}


def _static_root() -> str:
    return current_app.static_folder or ''


def _path(filename: str) -> str:
    return os.path.join(_static_root(), filename.replace('/', os.sep))


def version_of(filename: str) -> str:
    """檔案內容的短雜湊 (8 碼)；檔案不存在時回傳空字串 (不加參數)"""
    path = _path(filename)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return ''
    recheck = current_app.debug or current_app.testing
    with _lock:
        hit = _cache.get(filename)
        if hit is not None and (not recheck or hit[0] == mtime):
            return hit[1]
    try:
        with open(path, 'rb') as fh:
            digest = hashlib.md5(fh.read()).hexdigest()[:8]
    except OSError:
        return ''
    with _lock:
        _cache[filename] = (mtime, digest)
    return digest


def bundle_digest() -> str:
    """整個 static/ 的版本摘要 (檔名 + 大小 + 修改時間)：給 Service Worker 的快取名稱用"""
    if _bundle['digest'] and not (current_app.debug or current_app.testing):
        return _bundle['digest']
    h = hashlib.md5()
    root = _static_root()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            h.update(f'{os.path.relpath(full, root)}:{st.st_size}:{int(st.st_mtime)}'.encode())
    _bundle['digest'] = h.hexdigest()[:10]
    return _bundle['digest']


def install(app: Flask) -> None:
    @app.url_defaults
    def _add_static_version(endpoint, values):
        if endpoint == 'static' and values.get('filename') and 'v' not in values:
            v = version_of(values['filename'])
            if v:
                values['v'] = v
