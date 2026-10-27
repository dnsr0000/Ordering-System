"""
桌邊線上結帳：顧客在掃碼點餐頁用手機付清這一桌未結帳的訂單 (綠界信用卡)，不用到櫃檯。

流程：
1. 顧客按「線上結帳」→ 建立 TablePayment (記下當時這一桌未結帳的訂單與金額) → 自動送出表單到綠界付款頁。
2. 綠界伺服器通知付款成功 (/payment/ecpay/table_notify) → 驗證檢查碼與金額 → 以「信用卡 (線上)」完成桌位結帳
   (與櫃檯結帳相同：條件式更新訂單、收款紀錄、發票)。
3. 付款期間若這幾張訂單已在櫃檯結帳 / 取消，或金額有異動 (例如作廢品項)：標記 conflict 並通知店家到綠界後台退款。
同一張 TablePayment 重複通知只處理一次。
"""
import logging
from datetime import datetime
from types import SimpleNamespace

from app.errors import AppError
from app.extensions import db
from app.models.enums import PaymentMethod
from app.models.order import Order
from app.models.table import DiningTable, TablePayment
from app.services import payment_ecpay, settings_service

logger = logging.getLogger(__name__)


def enabled(tenant_id=None):
    return bool(settings_service.get('table_pay', tenant_id).get('enabled'))


def bill(table):
    """這一桌未結帳的訂單與合計"""
    from app.services import table_service
    orders = table_service.unpaid_orders_query().filter(Order.dining_table_id == table.id).order_by(Order.id).all()
    return {
        'table': table.name,
        'total': sum(o.total_price or 0 for o in orders),
        'orders': [{'id': o.id, 'pickup_number': o.display_pickup_number, 'total_price': o.total_price,
                    'items': [{'name': i.display_name, 'quantity': i.quantity, 'price': i.price}
                              for i in o.items if (i.quantity or 0) > 0]} for o in orders],
    }


def start(table):
    """建立一筆線上結帳 (記下這一桌目前未結帳的訂單與金額)。由這裡 commit。"""
    if not enabled(table.tenant_id):
        raise AppError('本店未開放桌邊線上結帳，請至櫃檯結帳', 404)
    info = bill(table)
    if not info['orders'] or info['total'] <= 0:
        raise AppError('這一桌目前沒有需要結帳的訂單', 409)
    tp = TablePayment(tenant_id=table.tenant_id, dining_table_id=table.id, amount=info['total'],
                      trade_no=payment_ecpay._new_trade_no(), order_ids=','.join(str(o['id']) for o in info['orders']))
    db.session.add(tp)
    db.session.commit()
    return tp


def form_fields(tp, base_url):
    """送到綠界付款頁的表單欄位 (信用卡)"""
    merchant_id, hash_key, hash_iv, _base = payment_ecpay._cfg()
    orders = Order.query.filter(Order.id.in_(tp.order_id_list)).all()
    names = '#'.join(f'{i.item_name} x{i.quantity}' for o in orders for i in o.items if (i.quantity or 0) > 0)
    base_url = base_url.rstrip('/')
    params = {
        'MerchantID': merchant_id,
        'MerchantTradeNo': tp.trade_no,
        'MerchantTradeDate': datetime.now().strftime('%Y/%m/%d %H:%M:%S'),
        'PaymentType': 'aio',
        'TotalAmount': str(int(tp.amount)),
        'TradeDesc': 'TablePayment',
        'ItemName': names[:180] or '餐點',
        'ReturnURL': f'{base_url}/payment/ecpay/table_notify',
        'OrderResultURL': f'{base_url}/payment/ecpay/table_result',
        'ChoosePayment': 'Credit',
        'EncryptType': '1',
        'CustomField1': f'T:{tp.trade_no}',
        'NeedExtraPaidInfo': 'N',
    }
    params['CheckMacValue'] = payment_ecpay.check_mac_value(params, hash_key, hash_iv)
    return params


def find(trade_no):
    from app.tenant_context import tenant_bypass
    with tenant_bypass():
        return TablePayment.query.filter_by(trade_no=str(trade_no or '')[:20]) \
            .execution_options(skip_tenant_filter=True).first()


def mark_paid(trade_no, provider_trade_no, amount, payment_type=None):
    """
    付款成功 → 完成桌位結帳。回傳 'paid' / 'already' / 'conflict' / 'missing'。
    在呼叫前不需要店家情境 (綠界伺服器的通知沒有登入狀態)；這裡切換到該筆付款的店家。
    """
    from app.models.tenant import Tenant
    from app.services import monitor_service, table_service
    from app.tenant_context import _use_tenant
    tp = find(trade_no)
    if tp is None:
        return 'missing'
    if tp.status != 'pending':
        return 'already'
    tenant = db.session.get(Tenant, tp.tenant_id)
    _use_tenant(tenant, remember=False)
    try:
        paid = int(float(amount))
    except (TypeError, ValueError):
        paid = -1
    table = db.session.get(DiningTable, tp.dining_table_id)
    reason = ''
    if paid != tp.amount:
        reason = f'付款金額 ${paid} 與結帳金額 ${tp.amount} 不符'
    else:
        method = payment_ecpay.method_for(payment_type) if payment_type else PaymentMethod.CREDIT_CARD
        try:
            table_service.settle_table(tp.tenant_id, tp.dining_table_id, method, 0, SimpleNamespace(username='ecpay'),
                                       order_ids=tp.order_id_list,
                                       online={'method': method, 'amount': tp.amount, 'reference': provider_trade_no})
        except AppError as exc:
            db.session.rollback()
            reason = exc.message
    tp = find(trade_no)
    tp.provider_trade_no = str(provider_trade_no or '')[:30]
    tp.paid_at = datetime.now()
    if reason:
        tp.status, tp.note = 'conflict', reason[:200]
        monitor_service.alert(f"{table.label if table else '桌位'} 線上結帳 ${paid} 已付款，但無法完成結帳：{reason}；"
                              f"請到綠界後台退款 (交易 {provider_trade_no})", key=f'table-pay-{tp.trade_no}',
                              tenant_id=tp.tenant_id, level='warning', source='payment')
    else:
        tp.status = 'paid'
    db.session.commit()
    return 'conflict' if reason else 'paid'


def handle_notification(form):
    """綠界 ReturnURL：回傳要回應綠界的字串"""
    params = {k: v for k, v in form.items()}
    if not payment_ecpay.verify(params):
        logger.warning('綠界桌邊結帳通知檢查碼錯誤：%s', params.get('MerchantTradeNo'))
        return '0|CheckMacValue Error'
    if str(params.get('RtnCode')) != '1':
        tp = find(params.get('MerchantTradeNo'))
        if tp is not None and tp.status == 'pending':
            tp.status, tp.note = 'failed', str(params.get('RtnMsg') or '付款失敗')[:200]
            db.session.commit()
        return '1|OK'
    if str(params.get('SimulatePaid', '0')) == '1':
        return '1|OK'
    result = mark_paid(params.get('MerchantTradeNo'), params.get('TradeNo'), params.get('TradeAmt'),
                       params.get('PaymentType'))
    return '0|NotFound' if result == 'missing' else '1|OK'
