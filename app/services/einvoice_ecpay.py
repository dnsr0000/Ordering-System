"""
綠界 B2C 電子發票 (API v3)。後台「系統設定 → 電子發票」填寫特店資料；未填時使用綠界公開的測試特店。

流程：
- 結帳成功後建立 invoice (pending)，交易提交後開立；斷網等失敗保持 pending，背景每 5 分鐘重送，後台也可手動重送。
- 取消訂單：同一期別 (每兩個月一期) 作廢，跨期改開全額折讓；部分退款開折讓。
- 列印：向綠界取得發票證明聯列印網址。

傳輸格式：{"MerchantID", "RqHeader": {"Timestamp"}, "Data": AES-128-CBC(URL encode(JSON)) 的 Base64}
回應的 Data 以相同方式解密。文件：https://developers.ecpay.com.tw/?p=7809
測試特店：MerchantID 2000132、HashKey ejCk326UnaZWKisg、HashIV q9jcZX8Ib9LM8wYk
"""
import base64
import json
import logging
import secrets
import string
import time
import urllib.parse
from datetime import datetime, timedelta

import requests
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.payment import Invoice, InvoiceStatus
from app.services import settings_service

logger = logging.getLogger(__name__)

STAGE_URL = 'https://einvoice-stage.ecpay.com.tw'
PROD_URL = 'https://einvoice.ecpay.com.tw'
TEST_MERCHANT = ('2000132', 'ejCk326UnaZWKisg', 'q9jcZX8Ib9LM8wYk')
MAX_ATTEMPTS = 12
CARRIER_CODES = {'member': '1', 'citizen': '2', 'mobile': '3'}


# ---------------------------------------------------------------------------
# 加解密
# ---------------------------------------------------------------------------
def _cipher(key, iv):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(key.encode('utf-8')[:16].ljust(16, b'\0')), modes.CBC(iv.encode('utf-8')[:16].ljust(16, b'\0')))


def encrypt_data(data, hash_key, hash_iv):
    from cryptography.hazmat.primitives import padding
    raw = urllib.parse.quote_plus(json.dumps(data, ensure_ascii=False, separators=(',', ':'))).encode('utf-8')
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    enc = _cipher(hash_key, hash_iv).encryptor()
    return base64.b64encode(enc.update(padded) + enc.finalize()).decode('ascii')


def decrypt_data(text, hash_key, hash_iv):
    from cryptography.hazmat.primitives import padding
    dec = _cipher(hash_key, hash_iv).decryptor()
    padded = dec.update(base64.b64decode(text)) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    raw = unpadder.update(padded) + unpadder.finalize()
    return json.loads(urllib.parse.unquote_plus(raw.decode('utf-8')))


# ---------------------------------------------------------------------------
# 呼叫 API
# ---------------------------------------------------------------------------
def credentials(cfg):
    if cfg.get('stage', True):
        return (cfg.get('merchant_id') or TEST_MERCHANT[0], cfg.get('hash_key') or TEST_MERCHANT[1],
                cfg.get('hash_iv') or TEST_MERCHANT[2], STAGE_URL)
    if not all(cfg.get(k) for k in ('merchant_id', 'hash_key', 'hash_iv')):
        raise AppError('電子發票正式環境需填寫特店編號、HashKey 與 HashIV')
    return cfg['merchant_id'], cfg['hash_key'], cfg['hash_iv'], PROD_URL


def _post(url, payload, timeout):
    """實際送出 (測試時以 monkeypatch 取代)"""
    resp = requests.post(url, json=payload, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def call(cfg, path, data, timeout=8):
    """呼叫綠界發票 API，回傳解密後的 Data；連線失敗拋 ConnectionError，業務錯誤由呼叫端判斷 RtnCode"""
    merchant_id, key, iv, base = credentials(cfg)
    payload = {'MerchantID': merchant_id, 'RqHeader': {'Timestamp': int(time.time())},
               'Data': encrypt_data(dict(data, MerchantID=merchant_id), key, iv)}
    try:
        body = _post(base + path, payload, timeout)
    except requests.RequestException as exc:
        raise ConnectionError(f'無法連線到綠界電子發票：{exc}') from exc
    if str(body.get('TransCode')) != '1':
        raise ConnectionError(f"綠界回應錯誤：{body.get('TransMsg') or body}")
    return decrypt_data(body.get('Data') or '', key, iv) if body.get('Data') else {}


# ---------------------------------------------------------------------------
# 建立 / 開立
# ---------------------------------------------------------------------------
def new_relate_number():
    """綠界的唯一編號 (最長 30 碼英數字)：K + 年月日時分秒 + 6 碼隨機"""
    alphabet = string.ascii_uppercase + string.digits
    return 'K' + datetime.now().strftime('%y%m%d%H%M%S') + ''.join(secrets.choice(alphabet) for _ in range(6))


def normalize_invoice_info(info, cfg):
    """
    整理顧客選擇的發票資訊 {type, carrier, love_code, buyer_id, buyer_name, email}，格式錯誤拋 AppError。
    type：print / mobile / citizen / donate / company；未選擇時依設定 (紙本或捐贈預設愛心碼)。
    """
    info = info if isinstance(info, dict) else {}
    kind = str(info.get('type') or '').strip() or cfg.get('default_carrier') or 'print'
    out = {'carrier_type': kind, 'carrier_num': '', 'love_code': '', 'buyer_identifier': '', 'buyer_name': '',
           'buyer_email': str(info.get('email') or '').strip()[:100]}
    if kind == 'mobile':
        code = str(info.get('carrier') or '').strip().upper()
        if not (len(code) == 8 and code.startswith('/') and all(c.isalnum() or c in '+-.' for c in code[1:])):
            raise AppError('手機條碼格式錯誤：應為 / 開頭共 8 碼 (例如 /ABC+123)')
        out['carrier_num'] = code
    elif kind == 'citizen':
        code = str(info.get('carrier') or '').strip().upper()
        if not (len(code) == 16 and code[:2].isalpha() and code[2:].isdigit()):
            raise AppError('自然人憑證條碼格式錯誤：應為 2 碼英文 + 14 碼數字')
        out['carrier_num'] = code
    elif kind == 'donate':
        code = str(info.get('love_code') or cfg.get('default_love_code') or '').strip()
        if not (code.isdigit() and 3 <= len(code) <= 7):
            raise AppError('愛心碼格式錯誤：應為 3~7 碼數字')
        out['love_code'] = code
    elif kind == 'company':
        ban = str(info.get('buyer_id') or '').strip()
        if not is_valid_ban(ban):
            raise AppError('統一編號格式錯誤 (8 碼數字且需通過檢查碼)')
        out['buyer_identifier'] = ban
        out['buyer_name'] = str(info.get('buyer_name') or '').strip()[:60]
    elif kind != 'print':
        raise AppError(f'不支援的發票類型：{kind}')
    return out


def is_valid_ban(ban):
    """統一編號檢查碼 (財政部 2023 年起的新規則：加權和可被 5 整除；第 7 碼為 7 時另一種算法)"""
    if not (len(ban) == 8 and ban.isdigit()):
        return False
    weights = (1, 2, 1, 2, 1, 2, 4, 1)
    total = 0
    for d, w in zip(ban, weights):
        p = int(d) * w
        total += p // 10 + p % 10
    if total % 5 == 0:
        return True
    return ban[6] == '7' and (total + 1) % 5 == 0


def create_invoice(order, info, status=InvoiceStatus.PENDING, settlement=None, amount=None):
    """建立發票資料列 (與訂單同一交易)；啟用自動開立時由呼叫端在提交後開立"""
    cfg = settings_service.get('einvoice', order.tenant_id)
    fields = normalize_invoice_info(info, cfg)
    inv = Invoice(tenant_id=order.tenant_id, order_id=order.id if settlement is None else None,
                  settlement_id=settlement.id if settlement is not None else None,
                  relate_number=new_relate_number(), status=status,
                  amount=int(amount if amount is not None else order.total_price or 0), **fields)
    db.session.add(inv)
    return inv


def _invoice_lines(inv):
    """發票品項：明細合計需等於發票金額 (折扣、服務費以獨立品項呈現)"""
    from app.models.order import Order
    if inv.settlement_id:
        orders = Order.query.filter_by(settlement_id=inv.settlement_id).execution_options(skip_tenant_filter=True).all()
    else:
        orders = [inv.order] if inv.order is not None else []
    lines = []
    for o in orders:
        for it in o.items:
            qty = it.quantity if it.quantity is not None else 1   # 單品作廢後可能為 0
            if ((it.price or 0) <= 0 and not it.is_reward) or qty <= 0:
                continue
            lines.append((it.display_name[:90], qty, it.price or 0))
        if o.service_charge:
            lines.append(('服務費', 1, o.service_charge))
        if o.delivery_fee:
            lines.append(('外送費', 1, o.delivery_fee))
    total = sum(q * p for _n, q, p in lines)
    if total != inv.amount:
        diff = inv.amount - total
        lines.append(('折扣' if diff < 0 else '調整', 1, diff))
    if not lines:
        lines = [('餐點', 1, inv.amount)]
    return lines


def _items_payload(lines, tax_type):
    return [{'ItemSeq': i, 'ItemName': name, 'ItemCount': qty, 'ItemWord': '份', 'ItemPrice': price,
             'ItemTaxType': tax_type, 'ItemAmount': qty * price} for i, (name, qty, price) in enumerate(lines, 1)]


def issue_payload(inv, cfg):
    has_carrier = inv.carrier_type in CARRIER_CODES
    donate = inv.carrier_type == 'donate'
    company = inv.carrier_type == 'company'
    must_print = company or not (has_carrier or donate)
    tax = cfg.get('item_tax_type') or '1'
    return {
        'RelateNumber': inv.relate_number,
        'CustomerID': '',
        'CustomerIdentifier': inv.buyer_identifier if company else '',
        'CustomerName': (inv.buyer_name or ('公司' if company else '消費者')) if must_print else (inv.buyer_name or ''),
        'CustomerAddr': (cfg.get('customer_addr') or '門市現場消費') if must_print else '',
        'CustomerPhone': '',
        'CustomerEmail': inv.buyer_email or cfg.get('fallback_email') or '',
        'ClearanceMark': '',
        'Print': '1' if must_print else '0',
        'Donation': '1' if donate else '0',
        'LoveCode': inv.love_code if donate else '',
        'CarrierType': CARRIER_CODES.get(inv.carrier_type, ''),
        'CarrierNum': inv.carrier_num if has_carrier else '',
        'TaxType': tax,
        'SalesAmount': int(inv.amount),
        'InvoiceRemark': '',
        'InvType': '07',
        'vat': '1',
        'Items': _items_payload(_invoice_lines(inv), tax),
    }


def issue_invoice(invoice_id):
    """
    開立 (pending → issued)。先以條件式更新把狀態改為 issuing 搶到這張發票，多個背景執行緒同時重送也只會開立一次。
    回傳最新狀態。
    """
    inv = db.session.get(Invoice, invoice_id)
    if inv is None or inv.status != InvoiceStatus.PENDING:
        return inv.status if inv else None
    cfg = settings_service.get('einvoice', inv.tenant_id)
    if not cfg.get('enabled'):
        return inv.status
    claimed = db.session.execute(update(Invoice).where(Invoice.id == inv.id, Invoice.status == InvoiceStatus.PENDING)
                                 .values(status='issuing', attempts=Invoice.attempts + 1)
                                 .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
    db.session.commit()
    if not claimed:
        return None
    db.session.refresh(inv)
    try:
        result = call(cfg, '/B2CInvoice/Issue', issue_payload(inv, cfg))
    except (ConnectionError, AppError) as exc:
        inv.status = InvoiceStatus.FAILED if inv.attempts >= MAX_ATTEMPTS else InvoiceStatus.PENDING
        inv.last_error = str(getattr(exc, 'message', exc))[:300]
        db.session.commit()
        _alert_failure(inv)
        return inv.status
    if str(result.get('RtnCode')) == '1':
        inv.status = InvoiceStatus.ISSUED
        inv.invoice_no = result.get('InvoiceNo') or ''
        inv.random_number = result.get('RandomNumber') or ''
        try:
            inv.invoice_date = datetime.strptime(result.get('InvoiceDate', '')[:19], '%Y-%m-%d %H:%M:%S')
        except ValueError:
            inv.invoice_date = datetime.now()
        inv.last_error = ''
    else:
        inv.status = InvoiceStatus.FAILED   # 資料錯誤 (例如載具不存在)：重送也不會成功，需人工修正
        inv.last_error = f"{result.get('RtnCode')} {result.get('RtnMsg')}"[:300]
        _alert_failure(inv)
    db.session.commit()
    return inv.status


def _alert_failure(inv):
    if inv.status == InvoiceStatus.FAILED:
        from app.services import monitor_service
        monitor_service.alert(f'電子發票開立失敗 (編號 {inv.relate_number})：{inv.last_error}', key=f'invoice-{inv.id}',
                              tenant_id=inv.tenant_id)


def same_period(d1, d2):
    """同一個發票期別 (每兩個月一期：1-2、3-4…)"""
    return d1.year == d2.year and (d1.month - 1) // 2 == (d2.month - 1) // 2


def void_or_allowance(invoice_id, reason='訂單取消'):
    """整張發票取消：同期作廢，跨期開全額折讓"""
    inv = db.session.get(Invoice, invoice_id)
    if inv is None:
        return None
    if inv.status in (InvoiceStatus.PENDING, InvoiceStatus.WAITING, InvoiceStatus.FAILED):
        inv.status, inv.last_error = InvoiceStatus.VOID, '開立前已取消'
        db.session.commit()
        return inv.status
    if inv.status != InvoiceStatus.ISSUED:
        return inv.status
    if inv.invoice_date and same_period(inv.invoice_date, datetime.now()):
        return void_invoice(inv.id, reason)
    return allowance(inv.id, inv.amount - (inv.allowance_amount or 0), reason)


def void_invoice(invoice_id, reason='訂單取消'):
    inv = db.session.get(Invoice, invoice_id)
    cfg = settings_service.get('einvoice', inv.tenant_id)
    try:
        result = call(cfg, '/B2CInvoice/Invalid', {'InvoiceNo': inv.invoice_no,
                                                   'InvoiceDate': (inv.invoice_date or datetime.now()).strftime('%Y-%m-%d'),
                                                   'Reason': reason[:20] or '訂單取消'})
    except (ConnectionError, AppError) as exc:
        inv.last_error = f'作廢失敗 (稍後重試)：{getattr(exc, "message", exc)}'[:300]
        db.session.commit()
        return inv.status
    if str(result.get('RtnCode')) == '1':
        inv.status, inv.last_error = InvoiceStatus.VOID, ''
    else:
        inv.last_error = f"作廢失敗：{result.get('RtnCode')} {result.get('RtnMsg')}"[:300]
    db.session.commit()
    return inv.status


def allowance(invoice_id, amount, reason='退款'):
    """開立折讓 (amount 元)"""
    inv = db.session.get(Invoice, invoice_id)
    amount = int(amount or 0)
    if inv is None or inv.status not in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE) or amount <= 0:
        return inv.status if inv else None
    cfg = settings_service.get('einvoice', inv.tenant_id)
    data = {'InvoiceNo': inv.invoice_no, 'InvoiceDate': (inv.invoice_date or datetime.now()).strftime('%Y-%m-%d'),
            'AllowanceNotify': 'E' if inv.buyer_email else 'N', 'CustomerName': inv.buyer_name or '',
            'NotifyMail': inv.buyer_email or '', 'NotifyPhone': '', 'AllowanceAmount': amount,
            'Items': _items_payload([(reason[:30] or '退款', 1, amount)], cfg.get('item_tax_type') or '1')}
    try:
        result = call(cfg, '/B2CInvoice/Allowance', data)
    except (ConnectionError, AppError) as exc:
        inv.last_error = f'折讓失敗 (請於後台重試)：{getattr(exc, "message", exc)}'[:300]
        db.session.commit()
        return inv.status
    if str(result.get('RtnCode')) == '1':
        inv.status = InvoiceStatus.ALLOWANCE
        inv.allowance_amount = (inv.allowance_amount or 0) + amount
        inv.allowance_numbers = ','.join(filter(None, [inv.allowance_numbers, result.get('IA_Allow_No') or '']))[:300]
        inv.last_error = ''
    else:
        inv.last_error = f"折讓失敗：{result.get('RtnCode')} {result.get('RtnMsg')}"[:300]
    db.session.commit()
    return inv.status


def print_url(invoice_id):
    """綠界的發票證明聯列印網址"""
    inv = db.session.get(Invoice, invoice_id)
    if inv is None or not inv.invoice_no:
        raise AppError('發票尚未開立')
    cfg = settings_service.get('einvoice', inv.tenant_id)
    try:
        result = call(cfg, '/B2CInvoice/InvoicePrint', {
            'InvoiceNo': inv.invoice_no, 'InvoiceDate': (inv.invoice_date or datetime.now()).strftime('%Y-%m-%d'),
            'PrintStyle': 1, 'IsShowingDetail': 1})
    except ConnectionError as exc:
        raise AppError(str(exc), 502)
    if str(result.get('RtnCode')) != '1' or not result.get('InvoiceHtml'):
        raise AppError(f"取得列印網址失敗：{result.get('RtnMsg') or result}", 502)
    return result['InvoiceHtml']


def check_barcode(code, tenant_id=None):
    """手機條碼是否存在 (財政部)"""
    cfg = settings_service.get('einvoice', tenant_id)
    result = call(cfg, '/B2CInvoice/CheckBarcode', {'BarCode': code})
    return str(result.get('RtnCode')) == '1' and result.get('IsExist') == 'Y'


def retry_pending(tenant_id=None, older_than_seconds=60, limit=20):
    """背景重送：待開立 (pending) 且建立超過一分鐘的發票"""
    deadline = datetime.now() - timedelta(seconds=older_than_seconds)
    q = Invoice.query.filter(Invoice.status == InvoiceStatus.PENDING, Invoice.created_at <= deadline)
    if tenant_id is not None:
        q = q.filter(Invoice.tenant_id == tenant_id)
    ids = [i.id for i in q.order_by(Invoice.id).limit(limit).execution_options(skip_tenant_filter=True).all()]
    done = 0
    for iid in ids:
        if issue_invoice(iid) == InvoiceStatus.ISSUED:
            done += 1
    return done


@settings_service.register_tester('einvoice')
def _test_connection(cfg):
    result = call(cfg, '/B2CInvoice/CheckLoveCode', {'LoveCode': '168001'})
    if str(result.get('RtnCode')) != '1':
        raise AppError(f"綠界回應：{result.get('RtnMsg') or result}")
    env = '測試環境' if cfg.get('stage', True) else '正式環境'
    return f'已連線綠界電子發票{env} (特店 {credentials(cfg)[0]})'
