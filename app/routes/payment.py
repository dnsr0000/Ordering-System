"""
線上付款 (線上端)：導向綠界付款頁、接收付款結果通知、付款後回到本站。
付款流程與測試資料說明見 app/services/payment_ecpay.py (三種綠界付款共用的路由骨架也在那裡)。
"""
from flask import Blueprint, abort, redirect, render_template, request, url_for

from app.extensions import csrf, limiter
from app.models.enums import OrderStatus
from app.models.order import Order
from app.services import payment_ecpay
# LINE Pay 的「測試連線」在模組載入時註冊 (settings_service.TESTERS)：啟動時就載入，否則剛啟動的行程 /
# 其他 worker / flask ops check-integrations 都沒有 LINE Pay 的測試 (其他地方只在函式內延遲 import)
from app.services import linepay  # noqa: F401
from app.services.customer_session import current_member

payment_bp = Blueprint('payment', __name__)


def _require_enabled():
    if not payment_ecpay.is_enabled():
        abort(404)


def _member_order(order_uuid):
    member = current_member()
    order = Order.query.filter_by(uuid=order_uuid).first()
    if member is None or order is None or order.user_id != member.id:
        abort(404)
    return order


@payment_bp.route('/pay/<order_uuid>', endpoint='pay')
def pay(order_uuid):
    """自動送出表單到綠界付款頁 (每次開啟都產生新的特店訂單編號)"""
    _require_enabled()
    order = _member_order(order_uuid)
    # 已取消 (店員取消待付款的訂單時付款狀態仍是 pending)：不可再付款，結果頁顯示訂單已取消
    if order.payment_status != 'pending' or order.status == OrderStatus.CANCELLED:
        return redirect(url_for('payment.payment_return', order_uuid=order.uuid))
    return payment_ecpay.render_checkout_page('pay.html', order=order, fields=payment_ecpay.build_payment_form(order))


@payment_bp.route('/payment/ecpay/notify', methods=['POST'], endpoint='ecpay_notify')
@csrf.exempt
@limiter.exempt
def ecpay_notify():
    """綠界伺服器的付款結果通知 (ReturnURL)：必須回應純文字 1|OK"""
    _require_enabled()
    return payment_ecpay.notify_reply(payment_ecpay.handle_notification)


@payment_bp.route('/payment/ecpay/info', methods=['POST'], endpoint='ecpay_info')
@csrf.exempt
@limiter.exempt
def ecpay_info():
    """綠界 ATM / 超商代碼取號結果 (PaymentInfoURL)：記下繳費期限與帳號 / 代碼，回應純文字 1|OK"""
    _require_enabled()
    return payment_ecpay.notify_reply(payment_ecpay.handle_payment_info)


@payment_bp.route('/payment/ecpay/result', methods=['POST'], endpoint='ecpay_result')
@csrf.exempt
def ecpay_result():
    """顧客付款後由綠界頁面以 POST 帶回 (OrderResultURL)：驗證後導向結果頁 (說明見 payment_ecpay.browser_return)"""
    _require_enabled()
    return payment_ecpay.handle_browser_return(request.form,
                                               lambda order_uuid: url_for('payment.payment_return', order_uuid=order_uuid))


@payment_bp.route('/payment/return/<order_uuid>', endpoint='payment_return')
def payment_return(order_uuid):
    _require_enabled()
    order = _member_order(order_uuid)
    return render_template('payment_return.html', order=order)
