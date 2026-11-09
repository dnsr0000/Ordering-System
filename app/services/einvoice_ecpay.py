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
import re
import time
import urllib.parse
from datetime import datetime, timedelta

import requests
from sqlalchemy import case, exists, func, or_, select, update

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
ISSUING = 'issuing'             # 開立中 (已搶到、正在呼叫綠界)
STUCK_ISSUING_MINUTES = 10      # 停在開立中超過這個時間 (開立途中行程重啟等) 視為中斷，由背景重送復原


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
    from app.services.payment_ecpay import ecpay_unique_no
    return ecpay_unique_no('%y%m%d%H%M%S', 6)


def normalize_invoice_info(info, cfg):
    """
    整理顧客選擇的發票資訊 {type, carrier, love_code, buyer_id, buyer_name, email}，格式錯誤拋 AppError。
    type：print / mobile / citizen / donate / company；未選擇時依設定 (紙本或捐贈預設愛心碼)。
    """
    info = info if isinstance(info, dict) else {}
    chosen = str(info.get('type') or '').strip()
    kind = chosen or cfg.get('default_carrier') or 'print'
    love_code = str(info.get('love_code') or cfg.get('default_love_code') or '').strip()
    if not chosen and kind == 'donate' and not _is_love_code(love_code):
        # 顧客沒有選擇、只是套用「預設捐贈」但後台沒填 (或填錯) 愛心碼：改開紙本，不讓結帳因設定錯誤而失敗
        logger.warning('電子發票預設捐贈但預設愛心碼未設定或格式錯誤，改開紙本證明聯')
        kind = 'print'
    out = {'carrier_type': kind, 'carrier_num': '', 'love_code': '', 'buyer_identifier': '', 'buyer_name': '',
           'buyer_email': str(info.get('email') or '').strip()[:100]}
    if kind == 'mobile':
        code = str(info.get('carrier') or '').strip().upper()
        if not (len(code) == 8 and code.startswith('/') and all(c.isalnum() or c in '+-.' for c in code[1:])):
            raise AppError('手機條碼格式錯誤：應為 / 開頭共 8 碼 (例如 /ABC+123)')
        out['carrier_num'] = code
    elif kind == 'citizen':
        code = str(info.get('carrier') or '').strip().upper()
        if not re.fullmatch(r'[A-Z]{2}[0-9]{14}', code):
            raise AppError('自然人憑證條碼格式錯誤：應為 2 碼英文 + 14 碼數字')
        out['carrier_num'] = code
    elif kind == 'donate':
        if not _is_love_code(love_code):
            raise AppError('愛心碼格式錯誤：應為 3~7 碼數字')
        out['love_code'] = love_code
    elif kind == 'company':
        ban = str(info.get('buyer_id') or '').strip()
        if not is_valid_ban(ban):
            raise AppError('統一編號格式錯誤 (8 碼數字且需通過檢查碼)')
        out['buyer_identifier'] = ban
        out['buyer_name'] = str(info.get('buyer_name') or '').strip()[:60]
    elif kind != 'print':
        raise AppError(f'不支援的發票類型：{kind}')
    return out


def _is_love_code(code):
    """愛心碼：3~7 碼 ASCII 數字 (isdigit() 會接受全形 / 上標等 Unicode 數字，不能用)"""
    return bool(re.fullmatch(r'[0-9]{3,7}', code or ''))


def is_valid_ban(ban):
    """統一編號檢查碼 (財政部 2023 年起的新規則：加權和可被 5 整除；第 7 碼為 7 時另一種算法)"""
    if not re.fullmatch(r'[0-9]{8}', ban or ''):   # 只收 ASCII 數字 (「²」的 isdigit() 為真但 int() 會拋錯)
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
    from app.models.enums import OrderStatus
    from app.models.order import Order
    if inv.settlement_id:
        # 桌位合併結帳：開立前被取消的訂單 (發票金額已調降) 不列入明細
        orders = Order.query.filter(Order.settlement_id == inv.settlement_id, Order.status != OrderStatus.CANCELLED) \
            .execution_options(skip_tenant_filter=True).all()
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
    if not is_local_invoice(inv):
        return inv.status   # 線上端：分店上傳的發票由分店自己開立
    cfg = settings_service.get('einvoice', inv.tenant_id)
    if not cfg.get('enabled'):
        return inv.status
    if _order_cancelled(inv):
        # 關閉「取消訂單時自動作廢」時，已取消訂單的發票會留在待開立：不替已取消的訂單開立，由後台作廢
        if inv.last_error != ORDER_CANCELLED_MESSAGE:
            inv.last_error = ORDER_CANCELLED_MESSAGE
            db.session.commit()
        return inv.status
    claimed = db.session.execute(update(Invoice).where(Invoice.id == inv.id, Invoice.status == InvoiceStatus.PENDING)
                                 .values(status=ISSUING, attempts=Invoice.attempts + 1)
                                 .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
    db.session.commit()
    if not claimed:
        return None
    db.session.refresh(inv)
    if _order_cancelled(inv):
        # 檢查後、搶到發票前訂單剛好被取消：不送出開立 (送出次數改回未含這次)
        inv.status, inv.attempts = InvoiceStatus.PENDING, max((inv.attempts or 1) - 1, 0)
        if not cfg.get('void_on_cancel', True):
            inv.last_error = ORDER_CANCELLED_MESSAGE   # 與上面相同：留在待開立，由後台作廢
        elif not inv.attempts:
            inv.status, inv.last_error = InvoiceStatus.VOID, '開立前訂單已取消'
        db.session.commit()
        if inv.status == InvoiceStatus.PENDING and cfg.get('void_on_cancel', True):
            return void_or_allowance(inv.id, '訂單取消')   # 先前送出過 (可能逾時但已開立)：先向綠界查詢再作廢
        return inv.status
    try:
        result = call(cfg, '/B2CInvoice/Issue', issue_payload(inv, cfg))
    except (ConnectionError, AppError) as exc:
        return _retry_later(inv, str(getattr(exc, 'message', exc)))
    except Exception as exc:  # noqa: BLE001  非預期錯誤 (解密失敗等)：不能讓發票永遠停在開立中
        db.session.rollback()
        logger.exception('電子發票開立發生非預期錯誤 (發票 id %s)', invoice_id)
        inv = db.session.get(Invoice, invoice_id)
        return _retry_later(inv, f'開立時發生非預期錯誤：{exc}')
    if str(result.get('RtnCode')) == '1':
        _set_issued(inv, result.get('InvoiceNo'), result.get('RandomNumber'), result.get('InvoiceDate'))
    else:
        error = f"{result.get('RtnCode')} {result.get('RtnMsg')}"[:300]
        # 先前送出時逾時但綠界其實已開立：重送會因 RelateNumber 重複被拒，先以 RelateNumber 查回開立結果
        found = _lookup_quietly(cfg, inv)
        if found is not None:
            _apply_lookup(inv, found)
        else:
            inv.status = InvoiceStatus.FAILED   # 資料錯誤 (例如載具不存在)：重送也不會成功，需人工修正
            inv.last_error = error
            _alert_failure(inv)
    db.session.commit()
    if inv.status == InvoiceStatus.ISSUED and _order_cancelled(inv):
        # 開立期間訂單被取消 (取消當下發票在開立中，無法作廢)：開立完成後補作廢 / 折讓
        if cfg.get('void_on_cancel', True):
            return void_or_allowance(inv.id, '訂單取消')
        inv.last_error = '開立期間訂單已取消 (請作廢此發票)'
        db.session.commit()
    return inv.status


ORDER_CANCELLED_MESSAGE = '訂單已取消，不開立 (請作廢此發票)'


def _retry_later(inv, message):
    """開立未完成 (連線失敗 / 非預期錯誤)：改回待開立讓背景重送，超過次數改為失敗"""
    inv.status = InvoiceStatus.FAILED if (inv.attempts or 0) >= MAX_ATTEMPTS else InvoiceStatus.PENDING
    inv.last_error = str(message)[:300]
    db.session.commit()
    _alert_failure(inv)
    return inv.status


def _set_issued(inv, invoice_no, random_number, invoice_date):
    inv.status = InvoiceStatus.ISSUED
    inv.invoice_no = str(invoice_no or '')[:12]
    inv.random_number = str(random_number or '')[:4]
    try:
        inv.invoice_date = datetime.strptime(str(invoice_date or '').replace('T', ' ')[:19], '%Y-%m-%d %H:%M:%S')
    except ValueError:
        inv.invoice_date = datetime.now()
    inv.last_error = ''


def lookup_issued(cfg, inv):
    """
    以 RelateNumber 向綠界查詢發票 (/B2CInvoice/GetIssue)：已開立回傳綠界的資料 (IIS_Number 等)，查無回傳 None。
    連線失敗拋 ConnectionError。用於開立逾時但綠界其實已開立 (之後重送會因 RelateNumber 重複被拒) 時補回發票號碼。
    """
    result = call(cfg, '/B2CInvoice/GetIssue', {'RelateNumber': inv.relate_number})
    if str(result.get('RtnCode')) != '1' or not str(result.get('IIS_Number') or '').strip():
        return None
    return result


def _lookup_quietly(cfg, inv):
    try:
        return lookup_issued(cfg, inv)
    except (ConnectionError, AppError) as exc:
        logger.warning('查詢綠界發票 %s 失敗：%s', inv.relate_number, getattr(exc, 'message', exc))
        return None


def _apply_lookup(inv, found):
    """查回的開立結果寫回本機：補上發票號碼；綠界上已作廢則標記作廢"""
    _set_issued(inv, found.get('IIS_Number'), found.get('IIS_Random_Number'), found.get('IIS_Create_Date'))
    if str(found.get('IIS_Invalid_Status') or '0') == '1':
        inv.status = InvoiceStatus.VOID
    inv.last_error = '已向綠界查回開立結果'


def is_local_invoice(inv):
    """
    這張發票是不是本機建立的 (只有本機建立的才由本機開立 / 重送)。發票只由分店上傳給線上端 (direction up)，
    線上端收到的分店發票不可用線上端的設定開立。線上端自己建立的：線上點餐 (order_source = Online)、
    線上端本機訂單 (node_uuid 為空；分店上傳的訂單一律帶分店的 node_uuid)、桌位合併結帳 (settlement_id 不同步)。
    """
    from app.sync import runtime as sync_runtime
    if not sync_runtime.is_hub():
        return True
    if inv.settlement_id:
        return True
    if inv.order_id is None:
        return False
    from app.models.enums import OrderSource
    from app.models.order import Order
    row = db.session.execute(select(Order.node_uuid, Order.order_source).where(Order.id == inv.order_id)
                             .execution_options(skip_tenant_filter=True)).first()
    return row is not None and (row.node_uuid is None or row.order_source == OrderSource.ONLINE)


def _local_invoice_clause():
    """is_local_invoice 的 SQL 條件 (非線上端回傳 None)"""
    from app.sync import runtime as sync_runtime
    if not sync_runtime.is_hub():
        return None
    from app.models.enums import OrderSource
    from app.models.order import Order
    own = exists().where(Order.id == Invoice.order_id,
                         or_(Order.node_uuid.is_(None), Order.order_source == OrderSource.ONLINE))
    return or_(Invoice.settlement_id.isnot(None), own)


def _order_cancelled(inv):
    from app.models.enums import OrderStatus
    from app.models.order import Order
    if inv.order_id is None:
        return False
    status = db.session.execute(select(Order.status).where(Order.id == inv.order_id)
                                .execution_options(skip_tenant_filter=True)).scalar()
    return status == OrderStatus.CANCELLED


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
    if inv.status in (InvoiceStatus.PENDING, InvoiceStatus.FAILED) and (inv.attempts or 0) > 0:
        # 送出過 (例如逾時)：綠界可能其實已開立，先以 RelateNumber 查詢，不能只在本機標記作廢
        try:
            found = lookup_issued(settings_service.get('einvoice', inv.tenant_id), inv)
        except (ConnectionError, AppError) as exc:
            inv.last_error = f'作廢前查詢綠界失敗 (請稍後重試)：{getattr(exc, "message", exc)}'[:300]
            db.session.commit()
            return inv.status
        if found is not None:
            _apply_lookup(inv, found)
            db.session.commit()
    if inv.status in (InvoiceStatus.PENDING, InvoiceStatus.WAITING, InvoiceStatus.FAILED):
        inv.status, inv.last_error = InvoiceStatus.VOID, '開立前已取消'
        db.session.commit()
        return inv.status
    if inv.status not in (InvoiceStatus.ISSUED, InvoiceStatus.ALLOWANCE):
        return inv.status
    # 已開過折讓 (先部分退款) 的發票不能作廢，改為剩餘金額開折讓；沒有折讓過的同期作廢、跨期全額折讓
    if inv.status == InvoiceStatus.ISSUED and inv.invoice_date and same_period(inv.invoice_date, datetime.now()):
        return void_invoice(inv.id, reason)
    remaining = (inv.amount or 0) - (inv.allowance_amount or 0)
    return allowance(inv.id, remaining, reason) if remaining > 0 else inv.status


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
    if amount > (inv.amount or 0) - (inv.allowance_amount or 0):
        inv.last_error = '折讓金額超過發票剩餘金額'   # 綠界也會拒絕：不送出
        db.session.commit()
        return inv.status
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
        # 以資料庫端累加 (同一張發票的兩筆折讓可能在不同的背景執行緒同時完成，Python 端累加會互相覆蓋)
        values = {'status': InvoiceStatus.ALLOWANCE, 'last_error': '',
                  'allowance_amount': func.coalesce(Invoice.allowance_amount, 0) + amount}
        number = str(result.get('IA_Allow_No') or '')
        if number:
            old = func.coalesce(Invoice.allowance_numbers, '')
            values['allowance_numbers'] = func.substr(case((old == '', number), else_=old + ',' + number), 1, 300)
        db.session.execute(update(Invoice).where(Invoice.id == inv.id).values(**values)
                           .execution_options(synchronize_session=False, skip_tenant_filter=True))
        db.session.commit()
        return InvoiceStatus.ALLOWANCE
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


def retry_pending(tenant_id=None, older_than_seconds=60, limit=20):
    """
    背景重送：待開立 (pending) 且建立超過一分鐘的發票。不送出的：
    - 關閉「結帳後自動開立」的店家還沒開立過 (attempts == 0) 的發票：等後台手動開立 (手動開立失敗後才由這裡重送)
    - 訂單已取消的發票 (關閉「取消訂單時自動作廢」時會留在待開立，由後台作廢)
    - 線上端收到的分店發票 (由分店自己開立)
    另外先復原停在「開立中」的發票 (開立途中行程重啟等)。
    """
    from app.models.enums import OrderStatus
    from app.models.order import Order
    recover_stuck_issuing(tenant_id)
    deadline = datetime.now() - timedelta(seconds=older_than_seconds)
    conds = [Invoice.status == InvoiceStatus.PENDING, Invoice.created_at <= deadline,
             ~exists().where(Order.id == Invoice.order_id, Order.status == OrderStatus.CANCELLED)]
    if tenant_id is not None:
        conds.append(Invoice.tenant_id == tenant_id)
    local = _local_invoice_clause()
    if local is not None:
        conds.append(local)
    tenants = db.session.execute(select(Invoice.tenant_id).where(*conds).distinct()
                                 .execution_options(skip_tenant_filter=True)).scalars().all()
    disabled, manual = [], []
    for tid in tenants:
        cfg = settings_service.get('einvoice', tid)
        if not cfg.get('enabled'):
            disabled.append(tid)
        elif not cfg.get('auto_issue', True):
            manual.append(tid)
    if disabled:
        conds.append(Invoice.tenant_id.notin_(disabled))
    if manual:
        conds.append(or_(Invoice.attempts > 0, Invoice.tenant_id.notin_(manual)))
    q = Invoice.query.filter(*conds)
    ids = [i.id for i in q.order_by(Invoice.id).limit(limit).execution_options(skip_tenant_filter=True).all()]
    done = 0
    for iid in ids:
        if issue_invoice(iid) == InvoiceStatus.ISSUED:
            done += 1
    return done


def recover_stuck_issuing(tenant_id=None, minutes=STUCK_ISSUING_MINUTES, limit=20):
    """
    停在「開立中」超過 minutes 分鐘的發票 (開立途中行程重啟、延後工作的執行緒中斷等)：
    先以 RelateNumber 向綠界查詢，已開立就補回發票號碼，查無才改回待開立 (之後由重送開立)；查詢失敗下次再試。
    回傳復原的張數。
    """
    cutoff = datetime.now() - timedelta(minutes=minutes)
    conds = [Invoice.status == ISSUING, or_(Invoice.updated_at.is_(None), Invoice.updated_at <= cutoff)]
    if tenant_id is not None:
        conds.append(Invoice.tenant_id == tenant_id)
    local = _local_invoice_clause()
    if local is not None:
        conds.append(local)
    stuck = Invoice.query.filter(*conds).order_by(Invoice.id).limit(limit).execution_options(skip_tenant_filter=True).all()
    recovered = 0
    for inv in stuck:
        try:
            found = lookup_issued(settings_service.get('einvoice', inv.tenant_id), inv)
        except (ConnectionError, AppError) as exc:
            logger.warning('復原開立中的發票 %s：查詢綠界失敗 (下次再試)：%s', inv.relate_number, getattr(exc, 'message', exc))
            continue
        if found is not None:
            _apply_lookup(inv, found)
        else:
            inv.status = InvoiceStatus.FAILED if (inv.attempts or 0) >= MAX_ATTEMPTS else InvoiceStatus.PENDING
            inv.last_error = '開立中斷 (伺服器重新啟動等)，已改回待開立'
            _alert_failure(inv)
        db.session.commit()
        recovered += 1
    return recovered


@settings_service.register_tester('einvoice')
def _test_connection(cfg):
    result = call(cfg, '/B2CInvoice/CheckLoveCode', {'LoveCode': '168001'})
    if str(result.get('RtnCode')) != '1':
        raise AppError(f"綠界回應：{result.get('RtnMsg') or result}")
    env = '測試環境' if cfg.get('stage', True) else '正式環境'
    return f'已連線綠界電子發票{env} (特店 {credentials(cfg)[0]})'
