"""
多 worker 負載測試 (第四輪)：對「正在執行的伺服器」(gunicorn 多 worker + PostgreSQL + Redis) 模擬尖峰時段，
並在結束後檢查資料一致性。不是 pytest 測試 (檔名不以 test_ 開頭)，請直接執行：

  1. 準備 (線上端的測試機，或本機 docker compose 環境；不要對正式資料庫執行)：
       flask --app run demo seed --days 14 --password LoadTest!2026
       gunicorn -c deploy/gunicorn.conf.py run:app        (GUNICORN_WORKERS=4、REDIS_URL、TRUSTED_PROXY_COUNT=1)
  2. 執行：
       python tests/load_gunicorn_pg.py --url http://127.0.0.1:8000 --password LoadTest!2026 \
           --kiosks 60 --cashiers 6 --kitchens 2 --sse 40 --duration 120 \
           --database-url postgresql://kiosk:kiosk@127.0.0.1/kiosk
     (--database-url 選填：提供時會把「限量布朗尼」庫存設為 --hot-stock 並在結束後直接查資料庫做一致性檢查)

模擬的角色 (每位虛擬使用者以不同的 X-Forwarded-For 來源 IP，伺服器需設定 TRUSTED_PROXY_COUNT=1)：
  - 點餐機訪客：開首頁 → 訪客登入 → 反覆「瀏覽菜單 → 結帳」(約每 3 秒一筆，低於每 IP 每分鐘 20 次的限制)；
    部分訂單搶同一個限量餐點 (檢查不超賣)
  - POS 收銀員：登入 → 反覆「查當日訂單 → 結帳」(每 IP 每分鐘 30 次以內)
  - 廚房：每秒輪詢廚房看板，把製作中的訂單改為已出餐 / 已取餐 (與結帳同時進行)
  - SSE：員工登入後保持 /api/orders_stream 長連線，統計收到的通知 (多 worker 時需 Redis 才收得到別的 worker 的訂單)
  - 對外 API：以 API 金鑰用 cursor 追蹤訂單異動，結束後比對是否每一張訂單都有收到
結果：各端點的請求數、每秒請求數、p50 / p95 / p99 / 最大延遲、錯誤 (狀態碼)，以及一致性檢查 (✔ / ✘)。
"""
import argparse
import json
import random
import re
import statistics
import sys
import threading
import time
import uuid
from collections import defaultdict

import requests

CSRF_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
PAGE_DATA_RE = re.compile(r'<script type="application/json" id="page-data">(.*?)</script>', re.S)
HOT_ITEM = '布朗尼'


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.lat = defaultdict(list)
        self.codes = defaultdict(lambda: defaultdict(int))
        self.created = {}            # order uuid -> (來源, 餐點數量 dict)
        self.hot_ok = 0
        self.errors = []

    def record(self, name, started, resp=None, exc=None):
        ms = (time.perf_counter() - started) * 1000
        with self.lock:
            self.lat[name].append(ms)
            code = resp.status_code if resp is not None else f'EXC {type(exc).__name__}'
            self.codes[name][code] += 1
            if (resp is None or resp.status_code >= 500) and len(self.errors) < 20:
                self.errors.append(f'{name}: {code} {(resp.text[:200] if resp is not None else exc)}')


def pct(values, p):
    if not values:
        return 0
    values = sorted(values)
    return values[min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))]


class Client:
    def __init__(self, base, ip):
        self.base = base.rstrip('/')
        self.s = requests.Session()
        self.s.headers['X-Forwarded-For'] = ip
        self.csrf = ''

    def get(self, path, **kw):
        r = self.s.get(self.base + path, timeout=30, **kw)
        m = CSRF_RE.search(r.text) if 'text/html' in r.headers.get('Content-Type', '') else None
        if m:
            self.csrf = m.group(1)
        return r

    def post(self, path, **kw):
        headers = kw.pop('headers', {})
        headers['X-CSRFToken'] = self.csrf
        return self.s.post(self.base + path, timeout=30, headers=headers, **kw)


def page_data(html):
    m = PAGE_DATA_RE.search(html)
    return json.loads(m.group(1)) if m else {}


def timed(stats, name, fn):
    t = time.perf_counter()
    try:
        r = fn()
    except requests.RequestException as exc:
        stats.record(name, t, exc=exc)
        return None
    stats.record(name, t, resp=r)
    return r


def staff_login(base, ip, username, password, stats):
    c = Client(base, ip)
    c.get('/admin')
    r = timed(stats, 'staff_login', lambda: c.post('/admin', data={'csrf_token': c.csrf, 'username': username,
                                                                  'password': password}))
    if r is None or r.status_code >= 400:
        raise SystemExit(f'員工 {username} 登入失敗：{r.status_code if r is not None else "連線失敗"}')
    c.get('/admin')
    return c


def pick_lines(items, rng, hot_id):
    lines = []
    for it in rng.sample(items, k=min(len(items), rng.randint(1, 3))):
        line = {'menu_item_id': it['id'], 'quantity': rng.choice((1, 1, 2))}
        if it.get('sizes'):
            line['size'] = it['sizes'][0]['name']
        lines.append(line)
    if hot_id and rng.random() < 0.25:
        lines.append({'menu_item_id': hot_id, 'quantity': 1})
    return lines


def kiosk_user(args, idx, stats, stop):
    rng = random.Random(idx)
    c = Client(args.url, f'10.10.{idx // 250}.{idx % 250 + 1}')
    c.get(f'/?shop={args.shop}')
    timed(stats, 'guest_login', lambda: c.post('/guest_login', data={'csrf_token': c.csrf}))
    while not stop.is_set():
        r = timed(stats, 'kiosk_menu', lambda: c.get('/'))
        if r is None or r.status_code != 200:
            time.sleep(1)
            continue
        data = page_data(r.text)
        items = [i for i in (data.get('menu') or {}).get('items', []) if i.get('is_available', True)
                 and not i.get('is_reward')]
        hot = next((i['id'] for i in items if i['name'] == HOT_ITEM), None)
        items = [i for i in items if i['name'] != HOT_ITEM]
        if not items:
            time.sleep(1)
            continue
        lines = pick_lines(items, rng, hot)
        body = {'client_order_id': str(uuid.uuid4()), 'items': lines, 'payment_method': 'Cash',
                'order_type': rng.choice(('內用', '外帶'))}
        resp = timed(stats, 'kiosk_checkout', lambda: c.post('/submit_order', json=body))
        if resp is not None and resp.status_code == 200:
            with stats.lock:
                stats.created[body['client_order_id']] = ('kiosk', lines)
                if any(ln['menu_item_id'] == hot for ln in lines):
                    stats.hot_ok += 1
        stop.wait(rng.uniform(2.8, 4.0))


def cashier(args, idx, stats, stop):
    rng = random.Random(1000 + idx)
    c = staff_login(args.url, f'10.20.0.{idx + 1}', f'{args.shop}_pos' if idx % 2 == 0 else f'{args.shop}_pos2',
                    args.password, stats)
    r = c.get('/pos')
    items = [i for i in page_data(r.text).get('items', []) if i.get('name') != HOT_ITEM and not i.get('is_reward')]
    while not stop.is_set():
        timed(stats, 'pos_today_orders', lambda: c.get('/api/pos/today_orders'))
        body = {'client_order_id': str(uuid.uuid4()), 'items': pick_lines(items, rng, None), 'payment_method': 'Cash',
                'cash_received': 5000, 'order_type': '內用'}
        resp = timed(stats, 'pos_checkout', lambda: c.post('/api/pos/checkout', json=body))
        if resp is not None and resp.status_code == 200:
            with stats.lock:
                stats.created[body['client_order_id']] = ('pos', body['items'])
        stop.wait(rng.uniform(2.2, 3.0))


def kitchen(args, idx, stats, stop):
    c = staff_login(args.url, f'10.30.0.{idx + 1}', f'{args.shop}_kitchen' if idx == 0 else f'{args.shop}_manager',
                    args.password, stats)
    c.get('/kitchen')
    while not stop.is_set():
        r = timed(stats, 'kitchen_orders', lambda: c.get('/api/kitchen_orders'))
        if r is not None and r.status_code == 200:
            pending = [o['id'] for o in r.json().get('orders', [])]
            for oid in pending[:5]:
                timed(stats, 'order_status', lambda: c.post(f'/api/orders/{oid}/status', json={'status': 'Completed'}))
        stop.wait(1.0)


def sse_listener(args, idx, stats, stop, counts):
    c = staff_login(args.url, f'10.40.{idx // 250}.{idx % 250 + 1}', f'{args.shop}_manager', args.password, stats)
    while not stop.is_set():
        try:
            with c.s.get(args.url + '/api/orders_stream', stream=True, timeout=(10, 30)) as r:
                for line in r.iter_lines(decode_unicode=True):
                    if stop.is_set():
                        return
                    if line and line.startswith('data:'):
                        counts[idx] += 1
        except requests.RequestException:
            stop.wait(1)


def api_feed(args, key, stats, stop, seen, state):
    """以 cursor 持續追蹤訂單異動 (像外部系統一樣輪詢)；state['cursor'] 給結束後補抓剩下的異動"""
    s = requests.Session()
    s.headers.update({'Authorization': f'Bearer {key}', 'X-Forwarded-For': '10.50.0.1'})
    cursor = state.get('cursor', 0)
    while True:
        r = timed(stats, 'api_v1_feed', lambda: s.get(f'{args.url}/api/v1/orders', params={'cursor': cursor, 'limit': 200},
                                                     timeout=30))
        if r is not None and r.status_code == 200:
            data = r.json()
            for o in data['orders']:
                seen.add(o['uuid'])
            cursor = state['cursor'] = data['next_cursor']
            if data['has_more']:
                continue
        if stop.is_set():
            return cursor
        stop.wait(2)


def create_api_key(args, stats):
    c = staff_login(args.url, '10.60.0.1', f'{args.shop}_owner', args.password, stats)
    c.get('/admin/integrations')
    r = c.post('/admin/integrations', data={'csrf_token': c.csrf, 'action': 'create_key', 'name': f'負載測試 {time.time():.0f}',
                                            'scopes': 'orders:read'})
    m = re.search(r'(kk_[A-Za-z0-9_-]{20,})', r.text)
    if not m:
        raise SystemExit('建立 API 金鑰失敗 (需要負責人帳號)')
    return m.group(1)


def db_checks(args, stats, t0, hot_start, feed_seen):
    import psycopg
    url = args.database_url.replace('postgresql+psycopg://', 'postgresql://')
    out = []
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute('SELECT id FROM tenant WHERE slug = %s', (args.shop,))
        tid = cur.fetchone()[0]
        cur.execute('SELECT uuid FROM "order" WHERE tenant_id = %s AND created_at >= %s', (tid, t0))
        uuids = {r[0] for r in cur.fetchall()}
        ok = set(stats.created)
        out.append(('成功回應的訂單都在資料庫 (沒有遺失)', ok <= uuids, f'回應 {len(ok)} / 資料庫 {len(uuids)}'))
        out.append(('資料庫沒有多出未回應成功的訂單 (沒有重複建單)', len(uuids - ok) == 0, f'多出 {len(uuids - ok)}'))
        # 取餐號 1~999 循環：同一天每個號碼出現的次數不可超過「當天張數 ÷ 999」(無條件進位)
        cur.execute('SELECT created_at::date, pickup_number, count(*) FROM "order" WHERE tenant_id = %s '
                    'AND created_at >= %s::date GROUP BY 1, 2', (tid, t0))
        per_day = defaultdict(dict)
        for day, num, n in cur.fetchall():
            per_day[day][num] = n
        worst = max((max(v.values()) - -(-sum(v.values()) // 999) for v in per_day.values()), default=0)
        total_today = sum(sum(v.values()) for v in per_day.values())
        out.append(('取餐號不重複 (每 999 號循環一次)', worst <= 0, f'當天 {total_today} 張、超出 {max(worst, 0)}'))
        cur.execute('SELECT stock FROM menu_item WHERE tenant_id = %s AND name = %s', (tid, HOT_ITEM))
        hot_now = cur.fetchone()[0]
        out.append(('限量餐點不超賣 (庫存 ≥ 0，賣出 = 成功訂單)', hot_now >= 0 and hot_start - hot_now == stats.hot_ok,
                    f'開始 {hot_start}、剩 {hot_now}、成功訂單 {stats.hot_ok}'))
        cur.execute("""SELECT count(*) FROM "order" o WHERE o.tenant_id = %s AND o.created_at >= %s AND NOT EXISTS (
                         SELECT 1 FROM payment_record p WHERE p.order_id = o.id AND p.kind = 'sale')""", (tid, t0))
        missing = cur.fetchone()[0]
        out.append(('每張訂單都有收款紀錄', missing == 0, f'缺 {missing}'))
        cur.execute("SELECT value FROM sync_state WHERE key = 'scheduler_leader'")
        leader = cur.fetchone()
        out.append(('排程主控只有一個 (多 worker 不重複執行背景工作)', leader is not None, (leader[0] if leader else '無')[:80]))
    out.append(('對外 API cursor 異動追蹤收到每一張訂單', set(stats.created) <= feed_seen,
                f'漏 {len(set(stats.created) - feed_seen)}'))
    return out


def set_hot_stock(args):
    import psycopg
    url = args.database_url.replace('postgresql+psycopg://', 'postgresql://')
    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute("""UPDATE menu_item SET stock = %s, total_stock = %s, is_sold_out = false
                       WHERE name = %s AND tenant_id = (SELECT id FROM tenant WHERE slug = %s)""",
                    (args.hot_stock, args.hot_stock, HOT_ITEM, args.shop))
        conn.commit()
        cur.execute('SELECT now()::timestamp')
        return cur.fetchone()[0]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--url', default='http://127.0.0.1:8000')
    ap.add_argument('--shop', default='demo')
    ap.add_argument('--password', required=True, help='flask demo seed --password 設定的示範帳號密碼')
    ap.add_argument('--kiosks', type=int, default=60)
    ap.add_argument('--cashiers', type=int, default=6)
    ap.add_argument('--kitchens', type=int, default=2)
    ap.add_argument('--sse', type=int, default=40)
    ap.add_argument('--duration', type=int, default=120)
    ap.add_argument('--database-url', default='')
    ap.add_argument('--hot-stock', type=int, default=150)
    args = ap.parse_args()

    stats = Stats()
    t0 = set_hot_stock(args) if args.database_url else None
    key = create_api_key(args, stats)
    stop = threading.Event()
    sse_counts = defaultdict(int)
    feed_seen, feed_state = set(), {'cursor': 0}
    threads = [threading.Thread(target=sse_listener, args=(args, i, stats, stop, sse_counts), daemon=True)
               for i in range(args.sse)]
    threads += [threading.Thread(target=kitchen, args=(args, i, stats, stop), daemon=True) for i in range(args.kitchens)]
    threads += [threading.Thread(target=cashier, args=(args, i, stats, stop), daemon=True) for i in range(args.cashiers)]
    threads += [threading.Thread(target=kiosk_user, args=(args, i, stats, stop), daemon=True) for i in range(args.kiosks)]
    feed = threading.Thread(target=api_feed, args=(args, key, stats, stop, feed_seen, feed_state), daemon=True)
    started = time.time()
    for t in threads:
        t.start()
        time.sleep(0.02)
    feed.start()
    time.sleep(args.duration)
    stop.set()
    elapsed = time.time() - started
    for t in threads:
        t.join(timeout=35)
    feed.join(timeout=60)
    # 所有虛擬使用者都結束後，從最後的 cursor 繼續抓完 (結束前一刻送出的訂單)
    api_feed(args, key, stats, stop, feed_seen, feed_state)

    total = sum(len(v) for v in stats.lat.values())
    print(f'\n=== 負載測試結果：{elapsed:.0f} 秒，共 {total} 個請求 ({total / elapsed:.1f} req/s)，'
          f'成功訂單 {len(stats.created)} 張 ({len(stats.created) / elapsed:.2f} 張/秒) ===')
    print(f"{'端點':<18}{'次數':>7}{'req/s':>8}{'p50':>8}{'p95':>8}{'p99':>8}{'最大':>8}  狀態碼")
    for name in sorted(stats.lat):
        v = stats.lat[name]
        codes = ' '.join(f'{k}×{n}' for k, n in sorted(stats.codes[name].items(), key=lambda kv: str(kv[0])))
        print(f'{name:<18}{len(v):>7}{len(v) / elapsed:>8.1f}{pct(v, 50):>8.0f}{pct(v, 95):>8.0f}{pct(v, 99):>8.0f}'
              f'{max(v):>8.0f}  {codes}')
    print(f'SSE 長連線 {args.sse} 條：每條收到的通知數 最少 {min(sse_counts.values(), default=0)}、'
          f'中位數 {statistics.median(sse_counts.values()) if sse_counts else 0}、最多 {max(sse_counts.values(), default=0)}')
    errors = sum(n for codes in stats.codes.values() for c, n in codes.items() if not isinstance(c, int) or c >= 500)
    checks = [('沒有 5xx 或連線錯誤', errors == 0, f'{errors} 次'),
              ('每條 SSE 長連線都收到其他 worker 的訂單通知', bool(sse_counts) and min(sse_counts.values()) > 1,
               f'最少 {min(sse_counts.values(), default=0)}')]
    if args.database_url:
        checks += db_checks(args, stats, t0, args.hot_stock, feed_seen)
    print('\n一致性檢查：')
    for title, ok, detail in checks:
        print(f"  {'✔' if ok else '✘'} {title} ({detail})")
    for e in stats.errors:
        print('  錯誤樣本：', e)
    sys.exit(0 if all(ok for _t, ok, _d in checks) else 1)


if __name__ == '__main__':
    main()
