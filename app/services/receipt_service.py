"""
電子收據：純文字明細寄到 Email (mail_service) 或推到 LINE (line_service)。
- 顧客在收據視窗自己操作 (/api/orders/<id>/receipt/email、/receipt/line)，會員可記住 Email。
- 設定 receipt.auto_line：結帳時 (交易提交後) 自動傳給 LINE 綁定會員。
寄送都在交易外 (deferred)，失敗只記錄不影響訂單。
"""
import logging
import re
from typing import Any

from app.errors import AppError
from app.extensions import db
from app.models.enums import PAYMENT_METHOD_LABELS
from app.models.tenant import Tenant
from app.services import deferred, line_service, mail_service, settings_service

logger = logging.getLogger(__name__)
EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


config = settings_service.reader('receipt')


def email_enabled(tenant_id: int | None = None) -> bool:
    return bool(config(tenant_id).get('email_enabled')) and mail_service.is_configured(tenant_id)


def line_enabled(tenant_id: int | None = None) -> bool:
    return bool(config(tenant_id).get('line_enabled')) and line_service.messaging_enabled(tenant_id)


def normalize_email(value: Any) -> str:
    email = str(value or '').strip()[:120]
    if not EMAIL_RE.match(email):
        raise AppError('Email 格式不正確')
    return email


def receipt_rows(order: Any, shop: Any = None, detailed: bool = False, title: str = '交易明細') -> list:
    """
    收據內容 (熱感機收據與純文字電子收據共用)，回傳由上到下的列：
    - ('text', 文字, {bold, size, align})       一行文字
    - ('row', 標籤, 值, {bold, text_label})      左右對齊的一行 (text_label：純文字格式改用的標籤)
    - ('item', 品名 x數量, 金額)                 餐點明細
    - ('rule', 字元)                             分隔線
    - ('track', 網址)                            出餐進度頁 (熱感機印 QR code，純文字印網址)
    detailed=True (熱感機) 另有顧客/桌號、收銀、外送地址、實收找零、授權碼 / 禮物卡、載具。
    頁尾由呼叫端自行加上 (POS 與電子收據各自讀自己的設定)。
    """
    from app.services import table_service
    shop = shop or db.session.get(Tenant, order.tenant_id)
    rows = [('text', shop.name if shop else '', {'bold': True, 'size': 2, 'align': 'center'}),
            ('text', title, {'align': 'center'}), ('rule', '='),
            ('text', f'取餐號碼 #{order.display_pickup_number:03d}', {'bold': True, 'size': 2, 'align': 'center'}),
            ('row', '單號', f'#{order.id}', {}),
            ('row', '時間', order.created_at.strftime('%Y-%m-%d %H:%M') if order.created_at else '', {})]
    if detailed:
        rows.append(('row', '顧客/桌號', order.table_number or '', {}))
    rows.append(('row', '用餐方式', order.order_type or '', {}))
    if detailed and order.cashier_username:
        rows.append(('row', '收銀', order.cashier_username, {}))
    rows.append(('rule', '-'))
    for it in order.items:
        rows.append(('item', f'{it.display_name} x{it.quantity}', f'${it.price * it.quantity}'))
        if it.customization:
            rows.append(('text', f'  {it.customization}', {}))
    rows += [('rule', '-'), ('row', '小計', f'${order.subtotal}', {})]
    for label, value in (('促銷', order.promotion_discount), ('會員折扣', order.member_discount), ('整單折扣', order.manual_discount)):
        if value:
            rows.append(('row', label, f'-${value}', {}))
    other = (order.discount_amount or 0) - (order.promotion_discount or 0) - (order.member_discount or 0) - (order.manual_discount or 0)
    if other > 0:
        rows.append(('row', '優惠券/點數', f'-${other}', {'text_label': '優惠券 / 點數'}))
    if order.service_charge:
        rows.append(('row', '服務費', f'+${order.service_charge}', {}))
    if order.delivery_fee:
        rows.append(('row', '外送費', f'+${order.delivery_fee}', {}))
    if detailed and order.delivery_address:
        rows += [('text', f'外送：{order.delivery_address}', {'bold': True}),
                 ('text', f'電話：{order.delivery_phone}　區域：{order.delivery_zone}', {})]
    rows += [('row', '應付總額', f'${order.total_price}', {'bold': True}),
             ('row', '付款方式', PAYMENT_METHOD_LABELS.get(order.payment_method, order.payment_method), {})]
    if detailed and order.payment_method == 'Cash':
        rows += [('row', '實收', f'${order.cash_received or order.total_price}', {}), ('row', '找零', f'${order.cash_change or 0}', {})]
    if detailed and order.payment_reference:
        rows.append(('row', '禮物卡' if order.payment_method == '禮物卡' else '授權碼/交易序號', order.masked_payment_reference[:20], {}))
    inv = order.invoice_summary()
    if inv and inv.get('invoice_no'):
        if detailed:
            rows.append(('rule', '-'))
        rows += [('row', '電子發票', inv['invoice_no'], {}), ('row', '隨機碼', inv.get('random_number') or '', {})]
        if detailed:
            rows.append(('row', '載具', inv.get('carrier') or '', {}))
    if order.user and order.points_earned:
        rows.append(('row', '本次贈點', f'+{order.points_earned}', {}))
    if detailed:
        rows.append(('rule', '-'))
    url = table_service.track_url(order, shop, fallback=False)
    if url:
        rows.append(('track', url))
    return rows


def text_receipt(order: Any) -> str:
    """純文字收據 (Email 內文 / LINE 訊息)"""
    lines = []
    for row in receipt_rows(order):
        kind = row[0]
        if kind == 'text':
            lines.append(row[1])
        elif kind == 'row':
            lines.append(f'{row[3].get("text_label", row[1])} {row[2]}')
        elif kind == 'item':
            lines.append(f'{row[1]}  {row[2]}')
        elif kind == 'rule':
            lines.append(row[1] * 24)
        elif kind == 'track':
            lines.append(f'出餐進度 {row[1]}')
    lines += ['', config(order.tenant_id).get('footer') or '感謝您的惠顧']
    return '\n'.join(lines)


def send_email(order_id: int, tenant_id: int, email: str) -> bool:
    """寄出 Email 收據 (交易外)；失敗只記錄"""
    from app.models.order import Order
    order = db.session.get(Order, order_id)
    if order is None:
        return False
    try:
        shop = db.session.get(Tenant, tenant_id)
        mail_service.send_mail(tenant_id, [email], f'{shop.name if shop else ""} 電子收據 #{order.id}', text_receipt(order))
        return True
    except AppError as exc:
        logger.warning('電子收據寄信失敗 (訂單 %s)：%s', order_id, exc.message)
        return False


def send_line(order_id: int, tenant_id: int) -> bool:
    from app.models.order import Order
    order = db.session.get(Order, order_id)
    if order is None or order.user is None or not order.user.line_user_id:
        return False
    return line_service.push_text(tenant_id, order.user.line_user_id, text_receipt(order))


def request_email(order: Any, email: str, remember: bool = False) -> None:
    """顧客在收據視窗按「寄到信箱」：檢查設定與格式後排程寄送；會員可記住 Email"""
    if not email_enabled(order.tenant_id):
        raise AppError('本店未開放 Email 電子收據', 404)
    email = normalize_email(email)
    if remember and order.user is not None:
        order.user.email = email
    deferred.defer(send_email, order.id, order.tenant_id, email)


def request_line(order: Any) -> None:
    if not line_enabled(order.tenant_id):
        raise AppError('本店未開放 LINE 電子收據', 404)
    if order.user is None or not order.user.line_user_id:
        raise AppError('請先綁定 LINE 帳號才能收 LINE 電子收據', 409)
    deferred.defer(send_line, order.id, order.tenant_id)


def auto_on_checkout(order: Any) -> None:
    """結帳交易內呼叫：設定 auto_line 且會員綁定 LINE 時，提交後自動傳收據"""
    if order.user is None or not order.user.line_user_id:
        return
    if config(order.tenant_id).get('auto_line') and line_enabled(order.tenant_id):
        deferred.defer(send_line, order.id, order.tenant_id)
