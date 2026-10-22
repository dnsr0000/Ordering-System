"""
線上付款 (線上端)：導向綠界付款頁、接收付款結果通知、付款後回到本站。
付款流程與測試資料說明見 app/services/payment_ecpay.py。
"""
from flask import Blueprint, Response, abort, current_app, redirect, render_template, request, url_for

from app.extensions import csrf, limiter
from app.models.order import Order
from app.services import payment_ecpay
from app.services.customer_session import current_member

payment_bp = Blueprint('payment', __name__)

ECPAY_FORM_ACTION = "form-action 'self' https://payment-stage.ecpay.com.tw https://payment.ecpay.com.tw"


def _require_enabled():
    if not payment_ecpay.is_enabled():
        abort(404)


def _member_order(order_uuid):
    member = current_member()
    order = Order.query.filter_by(uuid=order_uuid).first()
    if member is None or order is None or order.user_id != member.id:
        abort(404)
    return order


def _base_url():
    return current_app.config.get('PUBLIC_BASE_URL') or request.url_root.rstrip('/')


@payment_bp.route('/pay/<order_uuid>', endpoint='pay')
def pay(order_uuid):
    """自動送出表單到綠界付款頁 (每次開啟都產生新的特店訂單編號)"""
    _require_enabled()
    order = _member_order(order_uuid)
    if order.payment_status != 'pending':
        return redirect(url_for('payment.payment_return', order_uuid=order.uuid))
    fields = payment_ecpay.build_payment_form(order, _base_url())
    resp = Response(render_template('pay.html', order=order, fields=fields, action=payment_ecpay.checkout_url(),
                                    stage=current_app.config.get('ECPAY_STAGE', True)))
    # 付款表單要送到綠界：放寬本頁的 form-action
    from app import CSP_POLICY
    resp.headers['Content-Security-Policy'] = CSP_POLICY.replace("form-action 'self'", ECPAY_FORM_ACTION)
    return resp


@payment_bp.route('/payment/ecpay/notify', methods=['POST'], endpoint='ecpay_notify')
@csrf.exempt
@limiter.exempt
def ecpay_notify():
    """綠界伺服器的付款結果通知 (ReturnURL)：必須回應純文字 1|OK"""
    _require_enabled()
    return Response(payment_ecpay.handle_notification(request.form), mimetype='text/plain')


@payment_bp.route('/payment/ecpay/result', methods=['POST'], endpoint='ecpay_result')
@csrf.exempt
def ecpay_result():
    """
    顧客付款後由綠界頁面以 POST 帶回 (OrderResultURL)。跨站 POST 不會帶上本站 Cookie，
    因此這裡不碰 session，只驗證後導向 GET 頁面 (那時瀏覽器才會帶 Cookie)；實際付款狀態以 ReturnURL 通知為準。
    """
    _require_enabled()
    params = dict(request.form.items())
    order_uuid = params.get('CustomField1') if payment_ecpay.verify(params) else None
    if not order_uuid:
        abort(400)
    if str(params.get('RtnCode')) == '1' and str(params.get('SimulatePaid', '0')) != '1':
        payment_ecpay.mark_paid(order_uuid, params.get('TradeNo'), params.get('TradeAmt'))
    return redirect(url_for('payment.payment_return', order_uuid=order_uuid), code=303)


@payment_bp.route('/payment/return/<order_uuid>', endpoint='payment_return')
def payment_return(order_uuid):
    _require_enabled()
    order = _member_order(order_uuid)
    return render_template('payment_return.html', order=order)
