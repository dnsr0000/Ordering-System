"""
真實 HTTP 壓力測試 (預設略過，需明確執行：pytest -m stress)。
在測試行程內啟動多執行緒 HTTP 伺服器，模擬 SSE 長連線、同時瀏覽與同時搶購。
可用環境變數調整：STRESS_SSE_STREAMS、STRESS_BROWSE_USERS、STRESS_CHECKOUT_USERS、STRESS_STOCK。
"""
import os
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
import requests
from werkzeug.serving import WSGIRequestHandler, make_server

from app.extensions import db, limiter
from app.models.menu import MenuItem
from tests.conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from tests.support import make_item

pytestmark = pytest.mark.stress


class _QuietHandler(WSGIRequestHandler):
    def log_request(self, *args, **kwargs):
        pass


@pytest.fixture(scope='module')
def live_url(app):
    limiter.enabled = False
    server = make_server('127.0.0.1', 0, app, threaded=True, request_handler=_QuietHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    limiter.enabled = True


def latency(ms):
    ms = sorted(ms)
    pick = lambda q: ms[min(len(ms) - 1, int(len(ms) * q))]  # noqa: E731
    return f"平均 {statistics.mean(ms):.1f}ms | P50 {pick(.5):.1f} | P95 {pick(.95):.1f} | P99 {pick(.99):.1f}"


def test_browse_with_sse_streams(live_url):
    streams = int(os.environ.get('STRESS_SSE_STREAMS', 8))
    users = int(os.environ.get('STRESS_BROWSE_USERS', 60))
    sess = requests.Session()
    sess.post(f'{live_url}/admin', data={'username': ADMIN_USERNAME, 'password': ADMIN_PASSWORD}, timeout=10)
    active, stop = [], threading.Event()

    def sse(i):
        with requests.get(f'{live_url}/api/orders_stream', cookies=sess.cookies.get_dict(), stream=True, timeout=15,
                          headers={'X-Forwarded-For': f'10.88.0.{i + 1}'}) as resp:
            if resp.status_code == 200:
                active.append(i)
                next(resp.iter_lines(), None)
                stop.wait(10)

    for i in range(streams):
        threading.Thread(target=sse, args=(i,), daemon=True).start()
    time.sleep(0.3)

    def browse(i):
        t0 = time.perf_counter()
        with requests.Session() as s:
            ip = {'X-Forwarded-For': f'10.89.{i // 250}.{i % 250 + 1}'}
            ok = s.post(f'{live_url}/guest_login', headers=ip, timeout=10).ok and \
                s.get(f'{live_url}/api/counter_orders', headers=ip, timeout=10).ok
        return ok, (time.perf_counter() - t0) * 1000

    with ThreadPoolExecutor(max_workers=min(users, 32)) as ex:
        results = list(ex.map(browse, range(users)))
    stop.set()
    assert active, 'SSE 串流未建立'
    assert all(ok for ok, _ in results)
    print(f"SSE {len(active)} 條 | {users} 人瀏覽 | {latency([ms for _, ms in results])}")


def test_concurrent_checkout_over_http(app, live_url):
    users = int(os.environ.get('STRESS_CHECKOUT_USERS', 30))
    stock = int(os.environ.get('STRESS_STOCK', 10))
    item_id = make_item(app, '【壓測專用】限量和牛漢堡', price=150, stock=stock)

    def buy(i):
        t0 = time.perf_counter()
        with requests.Session() as s:
            ip = {'X-Forwarded-For': f'10.90.{i // 250}.{i % 250 + 1}'}
            s.get(f'{live_url}/?shop=default', headers=ip, timeout=10)
            s.post(f'{live_url}/guest_login', headers=ip, timeout=10)
            r = s.post(f'{live_url}/submit_order', headers=ip, timeout=15, json={
                'items': [{'menu_item_id': item_id, 'quantity': 1}], 'payment_method': 'Cash', 'order_type': '內用'})
        return r.status_code, r.json(), (time.perf_counter() - t0) * 1000

    with ThreadPoolExecutor(max_workers=min(users, 30)) as ex:
        results = list(ex.map(buy, range(users)))
    ok = [d for code, d, _ in results if code == 200]
    blocked = [d for code, d, _ in results if code == 400 and d.get('insufficient_stock')]
    assert len(ok) == stock and len(blocked) == users - stock
    assert len({d['pickup_number'] for d in ok}) == stock
    with app.app_context():
        assert db.session.get(MenuItem, item_id).stock == 0
    print(f"{users} 人搶 {stock} 份 | {latency([ms for *_, ms in results])}")
