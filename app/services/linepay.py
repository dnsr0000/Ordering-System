"""
LINE Pay 線下付款 (Offline API v2)：店員以條碼掃描器掃顧客 LINE Pay App 的「付款碼」(oneTimeKey) 完成扣款。

- 扣款：POST /v2/payments/oneTimeKeys/pay {productName, amount, currency, orderId, oneTimeKey}
- 查詢：GET  /v2/payments/orders/{orderId}/check (扣款逾時時確認是否已成功，避免重複扣款或漏收)
- 退款：POST /v2/payments/orders/{orderId}/refund {refundAmount}
驗證：標頭 X-LINE-ChannelId、X-LINE-ChannelSecret (後台「系統設定 → LINE Pay」填寫)。
"""
import logging

import requests

from app.errors import AppError
from app.services import settings_service

logger = logging.getLogger(__name__)

SANDBOX_URL = 'https://sandbox-api-pay.line.me'
PROD_URL = 'https://api-pay.line.me'


class LinePayError(Exception):
    def __init__(self, message, code=''):
        super().__init__(message)
        self.message = message
        self.code = code


def is_enabled(tenant_id=None):
    cfg = settings_service.get('linepay', tenant_id)
    return bool(cfg.get('enabled') and cfg.get('channel_id') and cfg.get('channel_secret'))


def _headers(cfg):
    headers = {'Content-Type': 'application/json', 'X-LINE-ChannelId': str(cfg.get('channel_id') or ''),
               'X-LINE-ChannelSecret': str(cfg.get('channel_secret') or '')}
    if cfg.get('device_profile_id'):
        headers['X-LINE-MerchantDeviceProfileId'] = cfg['device_profile_id']
    return headers


def _request(method, url, headers, json_body=None, timeout=25):
    """實際送出 (測試時以 monkeypatch 取代)；回傳 JSON dict"""
    resp = requests.request(method, url, headers=headers, json=json_body, timeout=timeout)
    return resp.json()


def _call(cfg, method, path, body=None, timeout=25):
    base = SANDBOX_URL if cfg.get('sandbox', True) else PROD_URL
    try:
        return _request(method, base + path, _headers(cfg), body, timeout)
    except requests.Timeout as exc:
        raise TimeoutError(str(exc)) from exc
    except (requests.RequestException, ValueError) as exc:
        raise LinePayError(f'無法連線到 LINE Pay：{exc}') from exc


def normalize_one_time_key(value):
    key = ''.join(str(value or '').split())
    if not (key.isdigit() and 12 <= len(key) <= 20):
        raise AppError('LINE Pay 付款碼格式錯誤：請掃描顧客 LINE Pay App 上的付款條碼 (純數字)')
    return key


def pay(tenant_id, order_id, amount, one_time_key):
    """扣款，成功回傳 transactionId；失敗拋 LinePayError。逾時時以查詢確認結果。"""
    cfg = settings_service.get('linepay', tenant_id)
    body = {'productName': (cfg.get('product_name') or '餐點')[:100], 'amount': int(amount), 'currency': 'TWD',
            'orderId': str(order_id), 'oneTimeKey': one_time_key}
    try:
        result = _call(cfg, 'POST', '/v2/payments/oneTimeKeys/pay', body, timeout=40)
    except TimeoutError:
        status = check(tenant_id, order_id)
        if status.get('paid'):
            return status.get('transaction_id', '')
        raise LinePayError('LINE Pay 回應逾時且查無付款紀錄，請請顧客確認 App 是否已扣款後重試')
    code = str(result.get('returnCode'))
    if code == '0000':
        info = result.get('info') or {}
        return str(info.get('transactionId') or '')
    raise LinePayError(f"LINE Pay 付款失敗 ({code})：{result.get('returnMessage') or ''}", code)


def check(tenant_id, order_id):
    cfg = settings_service.get('linepay', tenant_id)
    try:
        result = _call(cfg, 'GET', f'/v2/payments/orders/{order_id}/check', timeout=15)
    except (TimeoutError, LinePayError) as exc:
        logger.warning('LINE Pay 查詢失敗：%s', exc)
        return {'paid': False, 'error': str(exc)}
    info = result.get('info') or {}
    paid = str(result.get('returnCode')) == '0000' and str(info.get('status', 'COMPLETE')).upper() in ('COMPLETE', 'SUCCESS')
    return {'paid': paid, 'transaction_id': str(info.get('transactionId') or ''), 'code': str(result.get('returnCode'))}


def refund(tenant_id, order_id, amount):
    """退款 (可部分)；成功回傳退款交易序號，失敗拋 LinePayError"""
    cfg = settings_service.get('linepay', tenant_id)
    try:
        result = _call(cfg, 'POST', f'/v2/payments/orders/{order_id}/refund', {'refundAmount': int(amount)}, timeout=30)
    except TimeoutError as exc:
        raise LinePayError(f'LINE Pay 退款逾時：{exc}') from exc
    code = str(result.get('returnCode'))
    if code == '0000':
        info = result.get('info') or {}
        return str(info.get('refundTransactionId') or info.get('transactionId') or '')
    raise LinePayError(f"LINE Pay 退款失敗 ({code})：{result.get('returnMessage') or ''}", code)


@settings_service.register_tester('linepay')
def _test_connection(cfg):
    if not (cfg.get('channel_id') and cfg.get('channel_secret')):
        raise AppError('請先填寫 Channel ID 與 Channel Secret Key')
    try:
        result = _call(cfg, 'GET', '/v2/payments/orders/connection-test-0000/check', timeout=15)
    except (TimeoutError, LinePayError) as exc:
        raise AppError(str(exc))
    code = str(result.get('returnCode'))
    if code in ('1104', '1106', '1105'):   # 商店驗證失敗
        raise AppError(f"LINE Pay 拒絕：{result.get('returnMessage')} (請確認 Channel ID / Secret)")
    return f"LINE Pay {'Sandbox' if cfg.get('sandbox', True) else '正式環境'} 已回應 (代碼 {code}，查無測試訂單屬正常)"
