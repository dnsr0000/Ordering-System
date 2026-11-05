"""
錯誤回報與指標 (第六輪)，都是選用：
- Sentry：.env 設 SENTRY_DSN 並 pip install sentry-sdk；未預期的例外送到 Sentry，事件帶 request_id (對照 app.log) 與店家。
- Prometheus 指標：.env 設 METRICS_TOKEN 後 GET /metrics?token=… (或 Authorization: Bearer) 回文字格式：
  請求數 (依狀態碼類別)、請求耗時直方圖、今日訂單數 / 營業額、待製作訂單、同步延遲 (分店)、列印 / 發票失敗數。
  指標在行程內計數 (gunicorn 多 worker 時每個 worker 各自一份；門市 Waitress 單行程)。
"""
import logging
import threading
import time
from typing import Any

from flask import Response, current_app, g, request

logger = logging.getLogger(__name__)

BUCKETS = (0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_lock = threading.Lock()
_requests: dict[str, int] = {'1xx': 0, '2xx': 0, '3xx': 0, '4xx': 0, '5xx': 0}
_latency: dict[str, Any] = {'buckets': [0] * len(BUCKETS), 'count': 0, 'sum': 0.0}
_started = time.time()


def reset() -> None:
    with _lock:
        for k in _requests:
            _requests[k] = 0
        _latency.update(buckets=[0] * len(BUCKETS), count=0, sum=0.0)


def observe(status: int, seconds: float) -> None:
    cls = f'{status // 100}xx' if 100 <= status < 600 else '5xx'
    with _lock:
        _requests[cls] = _requests.get(cls, 0) + 1
        _latency['count'] += 1
        _latency['sum'] += seconds
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                _latency['buckets'][i] += 1


def _business_metrics() -> list[tuple[str, str, float]]:
    """(名稱, 說明, 值)；查詢失敗回空 (不讓 /metrics 變 500)"""
    out: list[tuple[str, str, float]] = []
    try:
        from sqlalchemy import func
        from app.extensions import db
        from app.models.enums import OrderStatus
        from app.models.order import Order
        from app.models.payment import Invoice, InvoiceStatus
        from app.models.printing import PrintJob
        from app.services.order_service import todays_orders_condition, todays_range
        from app.sync import runtime as sync_runtime
        from app.tenant_context import tenant_bypass
        start, end = todays_range()
        with tenant_bypass():
            today = Order.query.filter(todays_orders_condition(start, end, include_future=False),
                                       Order.status != OrderStatus.CANCELLED)
            out.append(('kiosk_orders_today', '今日訂單數 (不含取消)', float(today.count())))
            out.append(('kiosk_revenue_today', '今日營業額', float(today.with_entities(func.coalesce(func.sum(Order.total_price), 0)).scalar() or 0)))
            out.append(('kiosk_orders_pending', '待製作訂單', float(Order.query.filter(Order.status == OrderStatus.PENDING).count())))
            out.append(('kiosk_print_jobs_failed', '列印失敗', float(PrintJob.query.filter(PrintJob.status == 'failed').count())))
            out.append(('kiosk_invoices_failed', '發票失敗', float(Invoice.query.filter(Invoice.status == InvoiceStatus.FAILED).count())))
        if sync_runtime.is_node():
            last = sync_runtime.get_state('last_success_at', '')
            lag = -1.0
            if last:
                from datetime import datetime
                try:
                    lag = (datetime.now() - datetime.fromisoformat(str(last))).total_seconds()
                except ValueError:
                    lag = -1.0
            out.append(('kiosk_sync_lag_seconds', '距上次同步成功的秒數 (-1 = 從未成功)', lag))
        db.session.rollback()
    except Exception as exc:  # noqa: BLE001
        logger.warning('指標查詢失敗：%s', exc)
    return out


def render_metrics() -> str:
    lines = ['# HELP kiosk_http_requests_total 請求數 (依狀態碼類別)', '# TYPE kiosk_http_requests_total counter']
    with _lock:
        for cls, n in sorted(_requests.items()):
            lines.append(f'kiosk_http_requests_total{{class="{cls}"}} {n}')
        lines += ['# HELP kiosk_http_request_seconds 請求耗時', '# TYPE kiosk_http_request_seconds histogram']
        for b, n in zip(BUCKETS, _latency['buckets']):
            lines.append(f'kiosk_http_request_seconds_bucket{{le="{b}"}} {n}')
        lines.append(f'kiosk_http_request_seconds_bucket{{le="+Inf"}} {_latency["count"]}')
        lines.append(f'kiosk_http_request_seconds_sum {_latency["sum"]:.6f}')
        lines.append(f'kiosk_http_request_seconds_count {_latency["count"]}')
    lines += ['# HELP kiosk_uptime_seconds 行程啟動後的秒數', '# TYPE kiosk_uptime_seconds gauge',
              f'kiosk_uptime_seconds {time.time() - _started:.0f}']
    for name, help_text, value in _business_metrics():
        lines += [f'# HELP {name} {help_text}', f'# TYPE {name} gauge', f'{name} {value:g}']
    return '\n'.join(lines) + '\n'


def _authorized() -> bool:
    token = current_app.config.get('METRICS_TOKEN') or ''
    if not token:
        return False
    given = request.args.get('token') or request.headers.get('Authorization', '').removeprefix('Bearer ').strip()
    import hmac
    return hmac.compare_digest(given, token)


def install(app: Any) -> None:
    """create_app 呼叫：請求計時 + /metrics；SENTRY_DSN 有設時初始化 Sentry"""
    @app.before_request
    def _start_timer():
        g._metrics_started = time.perf_counter()

    @app.after_request
    def _record(resp):
        started = getattr(g, '_metrics_started', None)
        if started is not None and request.path != '/metrics':
            observe(resp.status_code, time.perf_counter() - started)
        return resp

    @app.route('/metrics', endpoint='metrics')
    def _metrics():
        if not _authorized():
            from flask import abort
            abort(404)
        return Response(render_metrics(), mimetype='text/plain; version=0.0.4; charset=utf-8', headers={'Cache-Control': 'no-store'})

    from app.extensions import csrf, limiter
    csrf.exempt(_metrics)
    limiter.exempt(_metrics)
    _init_sentry(app)


def _init_sentry(app: Any) -> bool:
    dsn = (app.config.get('SENTRY_DSN') or '').strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
    except ImportError:
        logger.warning('已設定 SENTRY_DSN 但未安裝 sentry-sdk (pip install sentry-sdk[flask])，略過錯誤回報')
        return False
    from app.services.log_service import current_request_id
    sentry_sdk.init(dsn=dsn, integrations=[FlaskIntegration()], send_default_pii=False,
                    traces_sample_rate=float(app.config.get('SENTRY_TRACES_SAMPLE_RATE') or 0),
                    release=_release(), environment='hub' if app.config.get('SYNC_ROLE') == 'hub' else 'store')

    @app.before_request
    def _sentry_tags():
        with sentry_sdk.configure_scope() as scope:
            scope.set_tag('request_id', current_request_id() or '')
            scope.set_tag('tenant_id', str(getattr(g, 'tenant_id', '') or ''))
    logger.info('Sentry 錯誤回報已啟用')
    return True


def _release() -> str:
    try:
        from app.services.update_service import current_version
        v = current_version()
        return f'kiosk@{v["version"]}' + (f'+{v["commit"]}' if v['commit'] else '')
    except Exception:  # noqa: BLE001
        return 'kiosk'
