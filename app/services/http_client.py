"""
對外 HTTP 共用 (LINE、外送平台…)：送出請求並回傳 (狀態碼, JSON dict)；回應不是 JSON 時為 {}。
各服務模組保留自己的 _post / _http 薄包裝，測試以 monkeypatch 取代那些名稱。
"""
import requests

DEFAULT_TIMEOUT = 15


def request_json(method, url, **kwargs):
    resp = requests.request(method, url, timeout=kwargs.pop('timeout', DEFAULT_TIMEOUT), **kwargs)
    try:
        return resp.status_code, resp.json()
    except ValueError:
        return resp.status_code, {}
