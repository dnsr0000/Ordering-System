"""
綠界 ECPay 全方位金流 (AIO, AioCheckOut V5)：線上點餐的信用卡付款。

流程：
1. 線上端結帳建立訂單 (payment_status='pending'，此時不會同步給分店) → 導到 /pay/<訂單> 自動送出表單到綠界付款頁
2. 綠界以伺服器對伺服器 POST 通知 ReturnURL (/payment/ecpay/notify)：驗證檢查碼、金額後標記已付款，
   訂單這時才同步到取餐分店；回應純文字 1|OK (否則綠界會在 5~15 分鐘後重送，一天最多 4 次)
3. 顧客瀏覽器由 OrderResultURL 回到本站顯示結果 (僅顯示用，以 ReturnURL 的通知為準)
4. 超過 PAYMENT_TIMEOUT_MINUTES 仍未收到通知：先向綠界查詢訂單，確定未付款才取消並回補庫存與點數

測試環境使用綠界公開的測試特店 (免註冊)：
    MerchantID 3002607、HashKey pwFHCqoQZGmho4w6、HashIV EkRm7iFT261dpevs
    測試卡號 4311-9522-2222-2222，有效期限填未來的月/年，安全碼任意三碼，3D 驗證簡訊碼 1234
文件：https://developers.ecpay.com.tw/?p=2856 (測試介接資訊)、?p=2902 (檢查碼)、?p=2878 (付款結果通知)
"""
import hashlib
import logging
import secrets
import string
import time
import urllib.parse
from datetime import datetime, timedelta

import requests
from flask import current_app
from sqlalchemy import select, update

from app.extensions import db
from app.models.enums import OrderStatus, PaymentMethod
from app.models.order import Order, OrderItem
from app.sync import tracking

logger = logging.getLogger(__name__)

STAGE_URL = 'https://payment-stage.ecpay.com.tw'
PROD_URL = 'https://payment.ecpay.com.tw'
TEST_MERCHANT = ('3002607', 'pwFHCqoQZGmho4w6', 'EkRm7iFT261dpevs')

# 綠界的 URL 編碼與 .NET HttpUtility.UrlEncode 相同：以下字元不編碼
_DOTNET_SAFE = {'%2d': '-', '%5f': '_', '%2e': '.', '%21': '!', '%2a': '*', '%28': '(', '%29': ')'}


def _cfg():
    cfg = current_app.config
    merchant_id = cfg.get('ECPAY_MERCHANT_ID') or TEST_MERCHANT[0]
    hash_key = cfg.get('ECPAY_HASH_KEY') or TEST_MERCHANT[1]
    hash_iv = cfg.get('ECPAY_HASH_IV') or TEST_MERCHANT[2]
    base = STAGE_URL if cfg.get('ECPAY_STAGE', True) else PROD_URL
    return merchant_id, hash_key, hash_iv, base


def is_enabled():
    """線上端才收線上付款"""
    return current_app.config.get('SYNC_ROLE') == 'hub' and current_app.config.get('ONLINE_PAYMENT', True)


def checkout_url():
    return _cfg()[3] + '/Cashier/AioCheckOut/V5'


def check_mac_value(params, hash_key, hash_iv):
    """檢查碼：參數依名稱排序 → 前後加 HashKey/HashIV → URL 編碼 (.NET 規則) → 轉小寫 → SHA256 → 轉大寫"""
    items = sorted(((k, v) for k, v in params.items() if k != 'CheckMacValue'), key=lambda kv: kv[0].lower())
    raw = f'HashKey={hash_key}&' + '&'.join(f'{k}={v}' for k, v in items) + f'&HashIV={hash_iv}'
    # Python 不編碼 '~'，.NET 會編成 %7e
    encoded = urllib.parse.quote_plus(raw).replace('~', '%7E').lower()
    for src, dst in _DOTNET_SAFE.items():
        encoded = encoded.replace(src, dst)
    return hashlib.sha256(encoded.encode('utf-8')).hexdigest().upper()


def verify(params):
    _merchant, hash_key, hash_iv, _base = _cfg()
    given = str(params.get('CheckMacValue', ''))
    return bool(given) and secrets.compare_digest(given.upper(), check_mac_value(params, hash_key, hash_iv))


def _new_trade_no():
    """特店訂單編號：20 碼英數字，每次付款嘗試都不同 (綠界不允許重複使用)"""
    alphabet = string.ascii_uppercase + string.digits
    return 'K' + datetime.now().strftime('%y%m%d') + ''.join(secrets.choice(alphabet) for _ in range(13))


def _item_name(order):
    names = '#'.join(f'{i.item_name} x{i.quantity}' for i in order.items) or '餐點'
    return names[:180]  # 上限 400 字元，超過會被截斷並造成檢查碼錯誤；中文保守截短


def build_payment_form(order, base_url):
    """產生送到綠界付款頁的表單欄位 (每次呼叫都換新的特店訂單編號)"""
    merchant_id, hash_key, hash_iv, _base = _cfg()
    trade_no = _new_trade_no()
    order.payment_ref = trade_no
    db.session.commit()
    base_url = base_url.rstrip('/')
    params = {
        'MerchantID': merchant_id,
        'MerchantTradeNo': trade_no,
        'MerchantTradeDate': datetime.now().strftime('%Y/%m/%d %H:%M:%S'),
        'PaymentType': 'aio',
        'TotalAmount': str(int(order.total_price)),
        'TradeDesc': 'OnlineOrder',
        'ItemName': _item_name(order),
        'ReturnURL': f'{base_url}/payment/ecpay/notify',
        'OrderResultURL': f'{base_url}/payment/ecpay/result',
        'ChoosePayment': 'Credit',
        'EncryptType': '1',
        'CustomField1': order.uuid,
        'NeedExtraPaidInfo': 'N',
    }
    params['CheckMacValue'] = check_mac_value(params, hash_key, hash_iv)
    return params


def _touch_order_rows(order):
    """付款完成：讓訂單與明細重新出現在同步中 (先前待付款時分店不會收到這些列)"""
    marker = tracking.mark_rows(db.session, OrderItem, OrderItem.__table__.c.order_id == order.id)
    return marker


def mark_paid(order_uuid, trade_no, amount, paid_at=None):
    """
    標記已付款 (只有「待付款且未取消」→ paid)。回傳 'paid' / 'already' / 'refund_required' / 'amount_mismatch' / 'not_found'。
    訂單已逾時或已取消後才收到付款：標記 refund_required，讓後台人工退款。
    判斷與更新在同一個條件式 UPDATE 中完成，與逾時取消、店員取消同時發生時也不會漏標退款。
    """
    from app.services.event_bus import order_event_bus
    from app.tenant_context import tenant_bypass
    with tenant_bypass():
        order = Order.query.filter_by(uuid=order_uuid).first()
        if order is None:
            return 'not_found'
        if int(amount or 0) != int(order.total_price or 0):
            logger.error('綠界付款金額不符：訂單 %s 應付 %s，通知金額 %s', order_uuid, order.total_price, amount)
            return 'amount_mismatch'
        paid_at = paid_at or datetime.now()
        swapped = db.session.execute(
            update(Order).where(Order.id == order.id, Order.payment_status == 'pending',
                                Order.status != OrderStatus.CANCELLED)
            .values(payment_status='paid', payment_trade_no=trade_no, paid_at=paid_at,
                    payment_method=PaymentMethod.CREDIT_CARD)
            .execution_options(synchronize_session=False)).rowcount
        if not swapped:
            refund = db.session.execute(
                update(Order).where(Order.id == order.id, Order.payment_status.in_(('pending', 'expired')))
                .values(payment_status='refund_required', payment_trade_no=trade_no, paid_at=paid_at)
                .execution_options(synchronize_session=False)).rowcount
            db.session.commit()
            if refund:
                logger.warning('訂單 %s 已取消後才收到付款，需人工退款 (綠界交易 %s)', order_uuid, trade_no)
                return 'refund_required'
            return 'already'
        _touch_order_rows(order)
        tenant_id = order.tenant_id
        db.session.commit()
    order_event_bus.notify(tenant_id)
    return 'paid'


def handle_notification(form):
    """ReturnURL：回傳要回應綠界的字串"""
    params = {k: v for k, v in form.items()}
    if not verify(params):
        logger.warning('綠界通知檢查碼錯誤：%s', params.get('MerchantTradeNo'))
        return '0|CheckMacValue Error'
    if str(params.get('RtnCode')) != '1':
        return '1|OK'  # 付款失敗：訂單維持待付款，逾時後取消
    if str(params.get('SimulatePaid', '0')) == '1':
        logger.info('綠界模擬付款通知 (不出貨)：%s', params.get('MerchantTradeNo'))
        return '1|OK'
    paid_at = None
    try:
        paid_at = datetime.strptime(params.get('PaymentDate', ''), '%Y/%m/%d %H:%M:%S')
    except ValueError:
        pass
    result = mark_paid(params.get('CustomField1'), params.get('TradeNo'), params.get('TradeAmt'), paid_at)
    return '0|AmountMismatch' if result == 'amount_mismatch' else '1|OK'


def query_trade(trade_no):
    """向綠界查詢訂單 (QueryTradeInfo V5)；回傳 dict，查詢失敗回傳 None"""
    merchant_id, hash_key, hash_iv, base = _cfg()
    params = {'MerchantID': merchant_id, 'MerchantTradeNo': trade_no, 'TimeStamp': str(int(time.time()))}
    params['CheckMacValue'] = check_mac_value(params, hash_key, hash_iv)
    try:
        resp = requests.post(base + '/Cashier/QueryTradeInfo/V5', data=params, timeout=10)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning('綠界查詢訂單失敗：%s', exc)
        return None
    data = dict(urllib.parse.parse_qsl(resp.text, keep_blank_values=True))
    if data.get('CheckMacValue') and not verify(data):
        logger.warning('綠界查詢結果檢查碼錯誤：%s', trade_no)
        return None
    return data


_last_sweep = {'at': 0.0}


def expire_unpaid_orders(force=False, query=query_trade):
    """
    逾時未付款的線上訂單：先向綠界查詢 (避免通知遺失而誤取消)，確定未付款才取消並回補庫存與點數。
    每個行程最多每分鐘執行一次。回傳取消的張數。
    """
    if not force and time.monotonic() - _last_sweep['at'] < 60:
        return 0
    _last_sweep['at'] = time.monotonic()
    from app.models.tenant import StaffAccount
    from app.services.order_service import change_order_status
    from app.tenant_context import tenant_bypass
    deadline = datetime.now() - timedelta(minutes=current_app.config.get('PAYMENT_TIMEOUT_MINUTES', 15))
    cancelled = 0
    with tenant_bypass():
        stale = db.session.execute(select(Order.id, Order.uuid, Order.tenant_id, Order.payment_ref, Order.total_price)
                                   .where(Order.payment_status == 'pending', Order.created_at < deadline)).all()
    for order_id, uuid, tenant_id, ref, total in stale:
        info = query(ref) if ref else {'TradeStatus': '10200095'}
        if info is None:
            continue  # 查不到 (網路問題)：下次再試，不冒險取消
        if str(info.get('TradeStatus')) == '1':
            mark_paid(uuid, info.get('TradeNo'), info.get('TradeAmt') or total)
            continue
        with tenant_bypass():
            # 先把付款狀態改為逾時 (條件式，與付款通知只有一方成功)；之後才到的付款會被標記為需退款
            expired = db.session.execute(update(Order).where(Order.id == order_id, Order.payment_status == 'pending')
                                         .values(payment_status='expired')
                                         .execution_options(synchronize_session=False)).rowcount
            db.session.commit()
            if not expired:
                continue  # 剛好付款完成
            system = StaffAccount(tenant_id=tenant_id, username='系統 (付款逾時)', role='owner', password_hash='-')
            try:
                change_order_status(order_id, OrderStatus.CANCELLED, system, tenant_id)
            except Exception:  # noqa: BLE001  (已被其他操作取消等)
                db.session.rollback()
                continue
            cancelled += 1
    return cancelled


def start_expiry_worker(app, interval=60):
    """背景執行緒：定期取消逾時未付款的訂單 (查詢綠界需要網路，不放在顧客的請求中執行)"""
    import threading

    def loop():
        while True:
            time.sleep(interval)
            with app.app_context():
                try:
                    expire_unpaid_orders(force=True)
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    logger.exception('取消逾時未付款訂單失敗 (稍後重試)')

    threading.Thread(target=loop, name='payment-expiry', daemon=True).start()
