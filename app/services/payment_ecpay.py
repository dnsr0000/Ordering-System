"""
綠界 ECPay 全方位金流 (AIO, AioCheckOut V5)：線上點餐的線上付款 (信用卡 / Apple Pay / ATM 虛擬帳號 / 超商代碼，
可用的方式在後台「系統設定 → 付款方式」勾選)。

流程：
1. 線上端結帳建立訂單 (payment_status='pending'，此時不會同步給分店) → 導到 /pay/<訂單> 自動送出表單到綠界付款頁
2. 綠界以伺服器對伺服器 POST 通知 ReturnURL (/payment/ecpay/notify)：驗證檢查碼、金額後標記已付款，
   訂單這時才同步到取餐分店；回應純文字 1|OK (否則綠界會在 5~15 分鐘後重送，一天最多 4 次)
3. 顧客瀏覽器由 OrderResultURL 回到本站顯示結果 (僅顯示用，以 ReturnURL 的通知為準)
4. 超過 PAYMENT_TIMEOUT_MINUTES 仍未收到通知：先向綠界查詢訂單，確定未付款才取消並回補庫存與點數
   (ATM / 超商代碼：綠界取號後以 PaymentInfoURL 通知繳費期限，期限過後才取消)
5. 取消已用信用卡 / Apple Pay 付款的訂單：可設定自動向綠界退刷 (信用卡請退款 CreditDetail/DoAction)，
   失敗或未開啟時在後台「作廢與退款紀錄」手動處理 / 重試

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

from app.errors import AppError
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


TEST_KEYS_MESSAGE = ('綠界目前使用公開的測試金鑰，任何人都能偽造付款通知；正式收款請設定 ECPAY_STAGE=0 與正式特店資料，'
                     '測試環境要收測試付款請在 .env 設定 ECPAY_ALLOW_TEST_KEYS=1')


class TestKeysNotAllowed(AppError):
    def __init__(self):
        super().__init__(TEST_KEYS_MESSAGE, 503)


def using_test_keys():
    """是否以綠界公開測試特店 (3002607) 收款：特店編號 / HashKey / HashIV 任一未設定"""
    cfg = current_app.config
    return not all(cfg.get(k) for k in ('ECPAY_MERCHANT_ID', 'ECPAY_HASH_KEY', 'ECPAY_HASH_IV'))


def test_keys_blocked():
    """
    測試金鑰是公開的 (寫在綠界文件與本檔最上方)：任何人只要知道訂單編號與金額就能算出正確的檢查碼，
    把訂單 / 桌邊結帳 / 訂位訂金標記為已付款。因此未明確設定 ECPAY_ALLOW_TEST_KEYS=1 時，
    付款表單不產生、付款通知一律視為檢查碼錯誤。
    """
    return using_test_keys() and not current_app.config.get('ECPAY_ALLOW_TEST_KEYS')


def require_keys_allowed():
    """開始一筆付款前呼叫：測試金鑰未被允許時拋出 AppError (503)，不建立付款紀錄"""
    _cfg()


def _cfg():
    if test_keys_blocked():
        logger.warning('綠界付款功能已停用：%s', TEST_KEYS_MESSAGE)
        raise TestKeysNotAllowed()
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
    base = STAGE_URL if current_app.config.get('ECPAY_STAGE', True) else PROD_URL
    return base + '/Cashier/AioCheckOut/V5'


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
    try:
        _merchant, hash_key, hash_iv, _base = _cfg()
    except TestKeysNotAllowed:
        logger.warning('拒絕綠界通知 %s：使用公開測試金鑰且未設定 ECPAY_ALLOW_TEST_KEYS=1', params.get('MerchantTradeNo'))
        return False
    given = str(params.get('CheckMacValue', ''))
    return bool(given) and secrets.compare_digest(given.upper(), check_mac_value(params, hash_key, hash_iv))


def _new_trade_no():
    """特店訂單編號：20 碼英數字，每次付款嘗試都不同 (綠界不允許重複使用)"""
    alphabet = string.ascii_uppercase + string.digits
    return 'K' + datetime.now().strftime('%y%m%d') + ''.join(secrets.choice(alphabet) for _ in range(13))


def _item_name(order):
    return '#'.join(f'{i.item_name} x{i.quantity}' for i in order.items) or '餐點'


# 綠界 AIO 的付款方式代碼 (ChoosePayment=ALL 時以 IgnorePayment 排除未啟用的)
ECPAY_METHODS = ('Credit', 'WebATM', 'ATM', 'CVS', 'BARCODE', 'ApplePay', 'TWQR', 'BNPL')


def payment_choice(tenant_id):
    """依後台設定回傳 (ChoosePayment, IgnorePayment)"""
    from app.services import settings_service
    cfg = settings_service.get('payments', tenant_id)
    methods = [m for m in (cfg.get('online_methods') or ['Credit']) if m in ECPAY_METHODS] or ['Credit']
    if len(methods) == 1:
        return methods[0], ''
    return 'ALL', '#'.join(m for m in ECPAY_METHODS if m not in methods)


def public_base_url():
    """綠界回傳 / LINE 回呼用的公開網址：PUBLIC_BASE_URL 優先，否則目前請求的網址"""
    from flask import request
    return (current_app.config.get('PUBLIC_BASE_URL') or request.url_root).rstrip('/')


def aio_form(trade_no, amount, item_name, desc, custom, return_path, result_path, base_url=None, choose='Credit', extra=None):
    """
    AioCheckOut V5 的表單欄位 (含檢查碼)。線上點餐、桌邊結帳、訂位訂金三種付款都用這一份，差別只在
    TradeDesc / CustomField1 (通知回來時用來找回訂單) 與通知路徑。
    """
    merchant_id, hash_key, hash_iv, _base = _cfg()
    base_url = (base_url or public_base_url()).rstrip('/')
    params = {
        'MerchantID': merchant_id,
        'MerchantTradeNo': trade_no,
        'MerchantTradeDate': datetime.now().strftime('%Y/%m/%d %H:%M:%S'),
        'PaymentType': 'aio',
        'TotalAmount': str(int(amount)),
        'TradeDesc': desc,
        'ItemName': str(item_name)[:180],   # 上限 400 字元，超過會被截斷並造成檢查碼錯誤；中文保守截短
        'ReturnURL': f'{base_url}{return_path}',
        'OrderResultURL': f'{base_url}{result_path}',
        'ChoosePayment': choose,
        'EncryptType': '1',
        'CustomField1': custom,
        'NeedExtraPaidInfo': 'N',
    }
    params.update(extra or {})
    params['CheckMacValue'] = check_mac_value(params, hash_key, hash_iv)
    return params


def build_payment_form(order, base_url=None):
    """線上點餐：產生送到綠界付款頁的表單欄位 (每次呼叫都換新的特店訂單編號)"""
    from app.services import settings_service
    trade_no = _new_trade_no()
    order.payment_ref = trade_no
    db.session.commit()
    choose, ignore = payment_choice(order.tenant_id)
    extra = {'IgnorePayment': ignore} if ignore else {}
    if choose in ('ALL', 'ATM', 'CVS'):
        cfg = settings_service.get('payments', order.tenant_id)
        extra.update(PaymentInfoURL=f"{(base_url or public_base_url()).rstrip('/')}/payment/ecpay/info",
                     ExpireDate=str(int(cfg.get('atm_expire_days') or 1)),
                     StoreExpireDate=str(int(cfg.get('cvs_expire_minutes') or 120)))
    return aio_form(trade_no, order.total_price, _item_name(order), 'OnlineOrder', order.uuid,
                    '/payment/ecpay/notify', '/payment/ecpay/result', base_url, choose=choose, extra=extra)


# 通知 / 瀏覽器帶回的結果：parse_result 的狀態 → 要回應綠界的字串 (paid 由各自的 mark_paid 決定)
RESULT_REPLY = {'bad_mac': '0|CheckMacValue Error', 'failed': '1|OK', 'simulated': '1|OK'}


def parse_result(form, label='付款'):
    """
    驗證綠界的伺服器通知 (ReturnURL) 或瀏覽器帶回的結果 (OrderResultURL)。
    回傳 (params, status)：status 為 'bad_mac' (檢查碼錯誤或測試金鑰未允許) / 'failed' (RtnCode != 1) /
    'simulated' (綠界後台的模擬付款，不出貨) / 'paid'。
    """
    params = {k: v for k, v in form.items()}
    if not verify(params):
        logger.warning('綠界%s通知檢查碼錯誤：%s', label, params.get('MerchantTradeNo'))
        return params, 'bad_mac'
    if str(params.get('RtnCode')) != '1':
        return params, 'failed'
    if str(params.get('SimulatePaid', '0')) == '1':
        logger.info('綠界模擬%s通知 (不出貨)：%s', label, params.get('MerchantTradeNo'))
        return params, 'simulated'
    return params, 'paid'


def _touch_order_rows(order):
    """付款完成：讓訂單與明細重新出現在同步中 (先前待付款時分店不會收到這些列)"""
    marker = tracking.mark_rows(db.session, OrderItem, OrderItem.__table__.c.order_id == order.id)
    return marker


def method_for(payment_type):
    """綠界通知的 PaymentType (例：Credit_CreditCard、ATM_LAND、CVS_CVS、ApplePay) → 本系統付款方式"""
    kind = str(payment_type or '')
    if kind.startswith('ATM_') or kind.startswith('WebATM'):
        return PaymentMethod.ATM
    if kind.startswith('CVS_') or kind.startswith('BARCODE'):
        return PaymentMethod.CVS
    if kind.startswith('ApplePay'):
        return PaymentMethod.APPLE_PAY
    return PaymentMethod.CREDIT_CARD


def mark_paid(order_uuid, trade_no, amount, paid_at=None, payment_type=None):
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
        method = method_for(payment_type)
        swapped = db.session.execute(
            update(Order).where(Order.id == order.id, Order.payment_status == 'pending',
                                Order.status != OrderStatus.CANCELLED)
            .values(payment_status='paid', payment_trade_no=trade_no, paid_at=paid_at,
                    payment_method=method)
            .execution_options(synchronize_session=False)).rowcount
        if not swapped:
            refund = db.session.execute(
                update(Order).where(Order.id == order.id, Order.payment_status.in_(('pending', 'expired')))
                .values(payment_status='refund_required', payment_trade_no=trade_no, paid_at=paid_at,
                        payment_method=method)
                .execution_options(synchronize_session=False)).rowcount
            db.session.commit()
            if refund:
                logger.warning('訂單 %s 已取消後才收到付款，需人工退款 (綠界交易 %s)', order_uuid, trade_no)
                return 'refund_required'
            return 'already'
        _touch_order_rows(order)
        tenant_id = order.tenant_id
        _after_online_paid(order, trade_no, method)
        db.session.commit()
    order_event_bus.notify(tenant_id)
    return 'paid'


def _after_online_paid(order, trade_no, method=PaymentMethod.CREDIT_CARD):
    """線上付款完成：收款紀錄、會員累積消費 / 等級、電子發票 (等待付款 → 待開立)"""
    from app.models.payment import Invoice, InvoiceStatus, PaymentKind
    from app.services import deferred, einvoice_ecpay, payment_service, settings_service, wallet_service
    db.session.refresh(order)
    payment_service.record_payment(order, PaymentKind.SALE, method, order.total_price,
                                   reference=trade_no or '', staff='ecpay')
    if order.user is not None:
        order.user.total_spent = (order.user.total_spent or 0) + (order.total_price or 0)
        wallet_service.refresh_tier(order.user)
    cfg = settings_service.get('einvoice', order.tenant_id)
    for inv in Invoice.query.filter_by(order_id=order.id, status=InvoiceStatus.WAITING) \
            .execution_options(skip_tenant_filter=True).all():
        inv.status = InvoiceStatus.PENDING
        db.session.flush()
        if cfg.get('auto_issue', True):
            deferred.defer(einvoice_ecpay.issue_invoice, inv.id)


def handle_notification(form):
    """ReturnURL：回傳要回應綠界的字串 (付款失敗：訂單維持待付款，逾時後取消)"""
    params, status = parse_result(form, '線上付款')
    if status != 'paid':
        return RESULT_REPLY[status]
    paid_at = None
    try:
        paid_at = datetime.strptime(params.get('PaymentDate', ''), '%Y/%m/%d %H:%M:%S')
    except ValueError:
        pass
    result = mark_paid(params.get('CustomField1'), params.get('TradeNo'), params.get('TradeAmt'), paid_at,
                       params.get('PaymentType'))
    return '0|AmountMismatch' if result == 'amount_mismatch' else '1|OK'


# ATM 取號成功 / 超商代碼取號成功的 RtnCode
INFO_OK_CODES = {'2', '10100073'}


def handle_payment_info(form):
    """
    PaymentInfoURL：ATM / 超商代碼取號結果。記下繳費期限與繳費資訊 (帳號 / 代碼)，
    期限前不會因為一般的付款逾時而取消訂單。回傳要回應綠界的字串。
    """
    from app.tenant_context import tenant_bypass
    params = {k: v for k, v in form.items()}
    if not verify(params):
        logger.warning('綠界取號通知檢查碼錯誤：%s', params.get('MerchantTradeNo'))
        return '0|CheckMacValue Error'
    if str(params.get('RtnCode')) not in INFO_OK_CODES:
        return '1|OK'
    due = None
    raw = str(params.get('ExpireDate') or '').strip()
    for fmt, end_of_day in (('%Y/%m/%d %H:%M:%S', False), ('%Y/%m/%d %H:%M', False), ('%Y/%m/%d', True)):
        try:
            due = datetime.strptime(raw, fmt)
            if end_of_day:
                due = due.replace(hour=23, minute=59, second=59)
            break
        except ValueError:
            continue
    if params.get('vAccount'):
        note = f"ATM 轉帳：銀行代碼 {params.get('BankCode', '')} 帳號 {params.get('vAccount')}"
    elif params.get('PaymentNo'):
        note = f"超商代碼：{params.get('PaymentNo')}"
    else:
        note = '超商條碼繳費'
    with tenant_bypass():
        order = Order.query.filter_by(uuid=params.get('CustomField1')).first()
        if order is None or order.payment_status != 'pending' or order.payment_ref != params.get('MerchantTradeNo'):
            return '1|OK'
        if due is not None:
            order.payment_due_at = due
        order.payment_note = note[:120]
        db.session.commit()
    return '1|OK'


def query_trade(trade_no):
    """向綠界查詢訂單 (QueryTradeInfo V5)；回傳 dict，查詢失敗回傳 None"""
    try:
        merchant_id, hash_key, hash_iv, base = _cfg()
    except TestKeysNotAllowed:
        return None
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
DUE_GRACE_MINUTES = 30   # 繳費期限過後再等 30 分鐘 (銀行 / 超商入帳通知可能延遲) 才取消


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
        stale = db.session.execute(select(Order.id, Order.uuid, Order.tenant_id, Order.payment_ref, Order.total_price,
                                          Order.payment_due_at)
                                   .where(Order.payment_status == 'pending', Order.created_at < deadline)).all()
    now = datetime.now()
    for order_id, uuid, tenant_id, ref, total, due_at in stale:
        if due_at is not None and now < due_at + timedelta(minutes=DUE_GRACE_MINUTES):
            continue   # ATM / 超商代碼：繳費期限內 (另加入帳緩衝) 不取消
        info = query(ref) if ref else {'TradeStatus': '10200095'}
        if info is None:
            continue  # 查不到 (網路問題)：下次再試，不冒險取消
        if str(info.get('TradeStatus')) == '1':
            mark_paid(uuid, info.get('TradeNo'), info.get('TradeAmt') or total, payment_type=info.get('PaymentType'))
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


class EcpayRefundError(Exception):
    def __init__(self, message):
        super().__init__(message)
        self.message = message


def _post(url, data):
    resp = requests.post(url, data=data, timeout=15)
    resp.raise_for_status()
    return dict(urllib.parse.parse_qsl(resp.text, keep_blank_values=True))


def refund(order, amount, post=None):
    """
    信用卡 / Apple Pay 退刷 (CreditDetail/DoAction，Action=R)：已請款的交易退回指定金額。
    若交易尚未關帳 (當日授權)，全額退款時改用 Action=N (放棄授權)。成功回傳說明文字，失敗拋出 EcpayRefundError。
    ATM / 超商代碼付款無法由系統退款 (需向顧客取得帳戶後人工匯款)。
    """
    post = post or _post
    if order.payment_method not in (PaymentMethod.CREDIT_CARD, PaymentMethod.APPLE_PAY):
        raise EcpayRefundError('ATM / 超商代碼付款無法自動退款，請聯絡顧客提供帳戶後匯款')
    if not order.payment_ref or not order.payment_trade_no:
        raise EcpayRefundError('找不到綠界交易編號，請到綠界廠商後台退款')
    merchant_id, hash_key, hash_iv, base = _cfg()
    url = base + '/CreditDetail/DoAction'
    actions = ['R'] + (['N'] if int(amount) >= int(order.total_price or 0) else [])
    last = ''
    for action in actions:
        params = {'MerchantID': merchant_id, 'MerchantTradeNo': order.payment_ref, 'TradeNo': order.payment_trade_no,
                  'Action': action, 'TotalAmount': str(int(amount))}
        params['CheckMacValue'] = check_mac_value(params, hash_key, hash_iv)
        try:
            data = post(url, params)
        except requests.RequestException as exc:
            raise EcpayRefundError(f'無法連線綠界：{exc}') from exc
        if str(data.get('RtnCode')) == '1':
            label = '退刷' if action == 'R' else '取消授權'
            return f"綠界{label}成功 {data.get('TradeNo') or order.payment_trade_no} ${int(amount)}"
        last = str(data.get('RtnMsg') or data.get('RtnCode') or '未知錯誤')
    raise EcpayRefundError(f'綠界退款失敗：{last}')


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
