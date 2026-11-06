"""
對外 HTTP 共用 (LINE、外送平台…)：送出請求並回傳 (狀態碼, JSON dict)；回應不是 JSON 時為 {}。
各服務模組保留自己的 _post / _http 薄包裝，測試以 monkeypatch 取代那些名稱。

Windows 門市改用系統的憑證庫驗證 HTTPS (use_system_certificates)：requests 預設只信任 certifi 內建清單，
防毒軟體 / 公司或校園網路攔截 HTTPS 時，會出現 CERTIFICATE_VERIFY_FAILED (unable to get local issuer certificate)，
連 GitHub (人臉模型、版本更新)、綠界、LINE 都連不上；Windows 憑證庫有這些攔截用的根憑證，瀏覽器能連的這裡就能連。
"""
import logging
import sys
from typing import Any

import requests

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15
_system_certs_done = False


def use_system_certificates() -> bool:
    """Windows 上讓所有 HTTPS 連線 (requests / urllib3 / ssl) 改用系統憑證庫；其他平台不變。回傳是否已啟用"""
    global _system_certs_done
    if _system_certs_done:
        return True
    if sys.platform != 'win32':
        return False
    try:
        import truststore
    except ImportError:
        logger.warning('未安裝 truststore，HTTPS 只信任內建憑證清單 (防毒 / 公司網路攔截 HTTPS 時會連線失敗)；'
                       '請執行 pip install -r requirements-store.txt')
        return False
    truststore.inject_into_ssl()
    _system_certs_done = True
    return True


def request_json(method: str, url: str, **kwargs: Any) -> tuple[int, dict]:
    resp = requests.request(method, url, timeout=kwargs.pop('timeout', DEFAULT_TIMEOUT), **kwargs)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}
