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


def text_receipt(order: Any) -> str:
    """純文字收據 (Email 內文 / LINE 訊息)"""
    shop = db.session.get(Tenant, order.tenant_id)
    cfg = config(order.tenant_id)
    lines = [shop.name if shop else '', '交易明細', '=' * 24,
             f'取餐號碼 #{order.display_pickup_number:03d}', f'單號 #{order.id}',
             f'時間 {order.created_at:%Y-%m-%d %H:%M}' if order.created_at else '',
             f'用餐方式 {order.order_type or ""}', '-' * 24]
    for it in order.items:
        lines.append(f'{it.display_name} x{it.quantity}  ${it.price * it.quantity}')
        if it.customization:
            lines.append(f'  {it.customization}')
    lines += ['-' * 24, f'小計 ${order.subtotal}']
    for label, value in (('促銷', order.promotion_discount), ('會員折扣', order.member_discount), ('整單折扣', order.manual_discount)):
        if value:
            lines.append(f'{label} -${value}')
    other = (order.discount_amount or 0) - (order.promotion_discount or 0) - (order.member_discount or 0) - (order.manual_discount or 0)
    if other > 0:
        lines.append(f'優惠券 / 點數 -${other}')
    if order.service_charge:
        lines.append(f'服務費 +${order.service_charge}')
    if order.delivery_fee:
        lines.append(f'外送費 +${order.delivery_fee}')
    lines.append(f'應付總額 ${order.total_price}')
    lines.append(f'付款方式 {PAYMENT_METHOD_LABELS.get(order.payment_method, order.payment_method)}')
    inv = order.invoice_summary()
    if inv and inv.get('invoice_no'):
        lines += [f'電子發票 {inv["invoice_no"]}', f'隨機碼 {inv.get("random_number") or ""}']
    if order.user and order.points_earned:
        lines.append(f'本次贈點 +{order.points_earned}')
    base = (shop.table_qr_base_url if shop else '') or ''
    if base:
        lines.append(f'出餐進度 {base.rstrip("/")}/track/{order.uuid}')
    lines += ['', cfg.get('footer') or '感謝您的惠顧']
    return '\n'.join(ln for ln in lines if ln is not None)


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
