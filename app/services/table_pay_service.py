"""
桌邊線上結帳：顧客在掃碼點餐頁用手機付清這一桌未結帳的訂單 (綠界信用卡)，不用到櫃檯。

流程：
1. 顧客按「線上結帳」→ 建立 TablePayment (記下當時這一桌未結帳的訂單與金額) → 自動送出表單到綠界付款頁。
2. 綠界伺服器通知付款成功 (/payment/ecpay/table_notify) → 驗證檢查碼與金額 → 以「信用卡 (線上)」完成桌位結帳
   (與櫃檯結帳相同：條件式更新訂單、收款紀錄、發票)。
3. 付款期間若這幾張訂單已在櫃檯結帳 / 取消，或金額有異動 (例如作廢品項)：標記 conflict 並通知店家到綠界後台退款。
同一張 TablePayment 重複通知只處理一次。
每次開啟付款頁都換新的特店訂單編號 (綠界不接受重複的 MerchantTradeNo)；CustomField1 帶 T:<這筆結帳的 trade_no>，
通知 / 瀏覽器帶回時以它找回同一筆 TablePayment。同桌另一支手機在付款時限內對同一批訂單按線上結帳時沿用同一筆。
"""
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace

from flask import current_app
from sqlalchemy import update

from app.errors import AppError
from app.extensions import db
from app.models.enums import PaymentMethod
from app.models.order import Order
from app.models.table import DiningTable, TablePayment
from app.services import payment_ecpay, settings_service

logger = logging.getLogger(__name__)


enabled = settings_service.flag('table_pay')


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
    payment_ecpay.require_keys_allowed()
    info = bill(table)
    if not info['orders'] or info['total'] <= 0:
        raise AppError('這一桌目前沒有需要結帳的訂單', 409)
    order_ids = ','.join(str(o['id']) for o in info['orders'])
    limit = TablePayment.__table__.c.order_ids.type.length
    if limit and len(order_ids) > limit:
        # 欄位存不下 (PostgreSQL 會在寫入時出錯)：明確請顧客到櫃檯結帳，不要變成 500
        raise AppError('這一桌未結帳的訂單太多，無法線上結帳，請至櫃檯結帳', 409)
    # 同桌另一支手機剛按過線上結帳 (同一批訂單與金額、仍在付款時限內)：沿用那一筆，避免兩人各付一次
    since = datetime.now() - timedelta(minutes=current_app.config.get('PAYMENT_TIMEOUT_MINUTES', 15))
    existing = TablePayment.query.filter(TablePayment.dining_table_id == table.id, TablePayment.status == 'pending',
                                         TablePayment.order_ids == order_ids, TablePayment.amount == info['total'],
                                         TablePayment.created_at >= since) \
        .order_by(TablePayment.id.desc()).first()
    if existing is not None:
        return existing
    tp = TablePayment(tenant_id=table.tenant_id, dining_table_id=table.id, amount=info['total'],
                      trade_no=payment_ecpay.new_trade_no(), order_ids=order_ids)
    db.session.add(tp)
    db.session.commit()
    return tp


def form_fields(tp, base_url=None):
    """
    送到綠界付款頁的表單欄位 (信用卡)。每次呼叫都換新的特店訂單編號 (重新整理 / 返回付款頁時
    綠界不接受重複的 MerchantTradeNo)；CustomField1 帶這筆結帳的 trade_no，通知回來時以它找回。
    """
    orders = Order.query.filter(Order.id.in_(tp.order_id_list)).all()
    names = '#'.join(f'{i.item_name} x{i.quantity}' for o in orders for i in o.items if (i.quantity or 0) > 0)
    return payment_ecpay.aio_form(payment_ecpay.new_trade_no(), tp.amount, names or '餐點', 'TablePayment',
                                  f'T:{tp.trade_no}', '/payment/ecpay/table_notify', '/payment/ecpay/table_result',
                                  base_url)


def payment_key(params):
    """綠界回傳的參數 → 這筆結帳的 trade_no (CustomField1 為 T:<trade_no>；舊付款頁沒有時以 MerchantTradeNo)"""
    custom = str(params.get('CustomField1') or '')
    if custom.startswith('T:') and len(custom) > 2:
        return custom[2:]
    return params.get('MerchantTradeNo')


def find(trade_no):
    from app.tenant_context import tenant_bypass
    with tenant_bypass():
        return TablePayment.query.filter_by(trade_no=str(trade_no or '')[:20]) \
            .execution_options(skip_tenant_filter=True).first()


def mark_paid(trade_no, provider_trade_no, amount, payment_type=None, attempt=None):
    """
    付款成功 → 完成桌位結帳。回傳 'paid' / 'already' / 'duplicate' / 'conflict' / 'missing'。
    trade_no 為這筆結帳的編號 (payment_key)；attempt 為這次送綠界的特店訂單編號 (只用於通知店家退款)。
    在呼叫前不需要店家情境 (綠界伺服器的通知沒有登入狀態)；這裡切換到該筆付款的店家。
    """
    from app.models.tenant import Tenant
    from app.services import monitor_service, table_service
    from app.tenant_context import _use_tenant
    tp = find(trade_no)
    if tp is None:
        # 綠界已確認扣款卻找不到這筆結帳 (例如紀錄已被刪除)：通知店家人工退款 (沒有店家情境，不帶 tenant_id)
        monitor_service.alert(f'桌邊線上結帳付款通知找不到紀錄 (特店訂單 {trade_no}，綠界交易 {provider_trade_no}，'
                              f'金額 {amount})，若已扣款請到綠界後台退款',
                              key=f'table-pay-missing-{trade_no}', level='warning', source='payment')
        return 'missing'
    # failed：先前某次付款失敗，之後顧客重開付款頁 (新的特店訂單編號) 付款成功，仍要結帳
    if tp.status not in ('pending', 'failed'):
        provider = str(provider_trade_no or '')[:30]
        if tp.status in ('paid', 'conflict') and provider and tp.provider_trade_no and provider != tp.provider_trade_no:
            # 同一筆結帳付了兩次 (兩個付款頁各付一次)：第二筆要退
            monitor_service.alert(f'桌邊線上結帳 {tp.trade_no} 重複付款 ${payment_ecpay.parse_amount(amount)}，'
                                  f'請到綠界後台退款 (交易 {provider_trade_no}，特店訂單編號 {attempt or tp.trade_no})',
                                  key=f'table-pay-dup-{provider}', tenant_id=tp.tenant_id, level='warning',
                                  source='payment')
            return 'duplicate'
        return 'already'
    # 綠界的伺服器通知與瀏覽器帶回幾乎同時到：只讓搶到的那一個去結帳，另一個回 'already'
    # (原本兩個都會結帳，後到的結帳失敗後把已付款的這筆改成「衝突」並通知店員退款)
    # 同時先記下收到付款的時間與綠界交易編號：結帳中途行程中斷而停在 processing 時，
    # 定期工作 (maintenance_service) 以 paid_at 判斷逾時、改為 conflict 並在警示帶出綠界交易編號供退款
    claimed = db.session.execute(update(TablePayment)
                                 .where(TablePayment.id == tp.id, TablePayment.status.in_(('pending', 'failed')))
                                 .values(status='processing', paid_at=datetime.now(),
                                         provider_trade_no=str(provider_trade_no or '')[:30])
                                 .execution_options(synchronize_session=False, skip_tenant_filter=True)).rowcount
    db.session.commit()
    if not claimed:
        return 'already'
    tenant = db.session.get(Tenant, tp.tenant_id)
    _use_tenant(tenant, remember=False)
    paid = payment_ecpay.parse_amount(amount)
    # 桌位已刪除時 dining_table_id 為 NULL (ON DELETE SET NULL)：不要以 NULL 主鍵查詢 (SQLAlchemy 會發出警告)
    table = db.session.get(DiningTable, tp.dining_table_id) if tp.dining_table_id else None
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
        except Exception as exc:  # noqa: BLE001  (不可永久停在 processing：改為 conflict 並通知店家)
            db.session.rollback()
            logger.exception('桌邊線上結帳 %s 完成結帳時發生錯誤', trade_no)
            reason = f'結帳時發生系統錯誤 ({type(exc).__name__})，請確認這一桌的訂單是否已結帳'
    tp = find(trade_no)
    tp.provider_trade_no = str(provider_trade_no or '')[:30]
    tp.paid_at = datetime.now()
    if reason:
        tp.status, tp.note = 'conflict', reason[:200]
        monitor_service.alert(f"{table.label if table else '桌位'} 線上結帳 ${paid} 已付款，但無法完成結帳：{reason}；"
                              f"請到綠界後台退款 (交易 {provider_trade_no}，特店訂單編號 {attempt or tp.trade_no})",
                              key=f'table-pay-{tp.trade_no}', tenant_id=tp.tenant_id, level='warning', source='payment')
    else:
        tp.status = 'paid'
    db.session.commit()
    return 'conflict' if reason else 'paid'


def mark_paid_from(params):
    """綠界通知 / 瀏覽器帶回的參數 → mark_paid"""
    return mark_paid(payment_key(params), params.get('TradeNo'), params.get('TradeAmt'), params.get('PaymentType'),
                     attempt=params.get('MerchantTradeNo'))


def _mark_failed(params):
    """
    付款失敗 (RtnCode != 1)：待付款的那筆標記為 failed (顧客可再按一次線上結帳)。
    綠界通知沒有店家情境：以條件式 UPDATE 寫入 (改 ORM 物件會被跨店防護擋下)。
    """
    tp = find(payment_key(params))
    if tp is not None and tp.status == 'pending':
        db.session.execute(update(TablePayment).where(TablePayment.id == tp.id, TablePayment.status == 'pending')
                           .values(status='failed', note=str(params.get('RtnMsg') or '付款失敗')[:200])
                           .execution_options(synchronize_session=False, skip_tenant_filter=True))
        db.session.commit()


def handle_notification(form):
    """綠界 ReturnURL：回傳要回應綠界的字串"""
    return payment_ecpay.notification_response(form, '桌邊結帳', mark_paid_from, replies={'missing': '0|NotFound'},
                                               on_failed=_mark_failed)
