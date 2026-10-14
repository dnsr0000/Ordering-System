import os
import re
import time
import statistics
import threading
import argparse
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

BASE_URL = os.getenv("TEST_BASE_URL", "http://127.0.0.1:5000")
CSRF_META_REGEX = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')


def extract_csrf_token(html_text):
    m = CSRF_META_REGEX.search(html_text or "")
    return m.group(1) if m else ""


def setup_limited_stock_item(target_stock=10):
    """透過 Flask App Context 直接在資料庫準備一項限量測試商品（庫存 = target_stock），供高併發防超賣壓測使用。"""
    try:
        from app import create_app
        from app.extensions import db
        from app.models.menu import MenuItem
        from app.models.tenant import Tenant

        app = create_app()
        with app.app_context():
            tenant = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug='default').first()
            tid = tenant.id if tenant else 1
            item = MenuItem.query.execution_options(skip_tenant_filter=True).filter_by(
                tenant_id=tid, name="【壓測專用】限量和牛漢堡"
            ).first()
            if not item:
                item = MenuItem(
                    tenant_id=tid,
                    name="【壓測專用】限量和牛漢堡",
                    category="主餐",
                    price=150,
                    stock=target_stock,
                    total_stock=target_stock,
                    is_sold_out=False,
                    modifiers="none"
                )
                db.session.add(item)
            else:
                item.stock = target_stock
                item.total_stock = target_stock
                item.is_sold_out = False
            db.session.commit()
            return item.id, item.name, target_stock
    except Exception as e:
        print(f"⚠️ 無法直接初始化壓測商品 ({e})，將略過自動建品。")
        return None, None, target_stock


def simulate_browsing_user(user_idx):
    """模擬單一在線使用者：訪客登入 -> 瀏覽點餐首頁 -> 查詢公開叫號看板"""
    s = requests.Session()
    t0 = time.perf_counter()
    try:
        r1 = s.get(f"{BASE_URL}/guest_login", timeout=10)
        r2 = s.get(f"{BASE_URL}/api/counter_orders", timeout=10)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        ok = (r1.status_code == 200 and r2.status_code in (200, 429))
        return {"ok": ok, "status": r1.status_code, "ms": elapsed_ms}
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {"ok": False, "status": str(e), "ms": elapsed_ms}


def simulate_checkout_user(user_idx, item_id, item_name):
    """
    模擬單一顧客完整結帳流程：
    1. 進入 /guest_login 取得 Session Cookie 與 CSRF Token
    2. 帶著 X-CSRFToken 標頭發送 POST /submit_order 搶購限量商品
    """
    s = requests.Session()
    t0 = time.perf_counter()
    try:
        r_login = s.get(f"{BASE_URL}/guest_login", timeout=10)
        csrf_token = extract_csrf_token(r_login.text)
        headers = {
            "Content-Type": "application/json",
            "X-CSRFToken": csrf_token,
            "X-Forwarded-For": f"10.0.{user_idx // 250}.{(user_idx % 250) + 1}"
        }
        payload = {
            "items": [{
                "id": item_id,
                "name": item_name,
                "price": 150,
                "quantity": 1,
                "customization": "",
                "is_add_on": False
            }],
            "payment_method": "Cash",
            "order_type": "內用",
            "need_cutlery": False,
            "note": f"壓測併發顧客 #{user_idx}",
            "promo_code": "",
            "use_points": 0
        }
        r_order = s.post(f"{BASE_URL}/submit_order", json=payload, headers=headers, timeout=15)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        data = r_order.json() if r_order.headers.get("content-type", "").startswith("application/json") else {}
        return {
            "status_code": r_order.status_code,
            "ms": elapsed_ms,
            "order_id": data.get("order_id"),
            "pickup_number": data.get("pickup_number"),
            "insufficient_stock": data.get("insufficient_stock", False),
            "msg": data.get("message") or data.get("error") or ""
        }
    except Exception as e:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return {
            "status_code": 0,
            "ms": elapsed_ms,
            "order_id": None,
            "pickup_number": None,
            "insufficient_stock": False,
            "msg": str(e)
        }


def run_sse_background_listeners(num_streams, stop_event):
    """在背景同時維持多條 SSE 長連線，測試 SSE 是否會阻塞一般 HTTP 請求"""
    active_count = 0
    lock = threading.Lock()

    def _listen_worker(idx):
        nonlocal active_count
        try:
            with requests.get(f"{BASE_URL}/api/orders_stream", stream=True, timeout=30) as resp:
                if resp.status_code == 200:
                    with lock:
                        active_count += 1
                    for _ in resp.iter_lines():
                        if stop_event.is_set():
                            break
        except Exception:
            pass

    threads = []
    for i in range(num_streams):
        t = threading.Thread(target=_listen_worker, args=(i,), daemon=True)
        t.start()
        threads.append(t)
    time.sleep(0.8)
    return active_count, threads


def print_latency_stats(title, latencies_ms):
    if not latencies_ms:
        return
    lat_sorted = sorted(latencies_ms)
    p50 = lat_sorted[int(len(lat_sorted) * 0.50)]
    p95 = lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * 0.95))]
    p99 = lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * 0.99))]
    avg = statistics.mean(lat_sorted)
    print(f"📊 [{title}] 回應時間統計：平均 {avg:.1f} ms | P50: {p50:.1f} ms | P95: {p95:.1f} ms | P99: {p99:.1f} ms")


def main():
    parser = argparse.ArgumentParser(description="自助點餐與 POS 系統 - 在線人數與防超賣壓力測試工具")
    parser.add_argument("--browse-users", type=int, default=60, help="同時在線瀏覽人數 (預設: 60)")
    parser.add_argument("--sse-streams", type=int, default=8, help="背景同時開啟的 SSE 看板連線數 (預設: 8)")
    parser.add_argument("--checkout-users", type=int, default=30, help="同時搶購結帳人數 (預設: 30)")
    parser.add_argument("--stock", type=int, default=10, help="限量測試商品初始庫存 (預設: 10)")
    args = parser.parse_args()

    print("\n" + "=" * 68)
    print("🚀 啟動【在線人數、SSE 看板與高併發防超賣】綜合壓力測試")
    print(f"目標伺服器: {BASE_URL}")
    print("=" * 68)

    # ------------------------------------------------------------------
    # 階段 1：背景掛載 SSE 長連線 + 高併發在線瀏覽測試
    # ------------------------------------------------------------------
    stop_sse = threading.Event()
    print(f"\n[階段 1] 開啟 {args.sse_streams} 條背景 SSE 即時串流，並模擬 {args.browse_users} 位使用者同時進入點餐首頁...")
    active_sse, _ = run_sse_background_listeners(args.sse_streams, stop_sse)
    print(f"  ➤ 目前活躍 SSE 串流連線數：{active_sse} 條")

    t_start = time.perf_counter()
    browse_results = []
    with ThreadPoolExecutor(max_workers=min(args.browse_users, 64)) as ex:
        futures = [ex.submit(simulate_browsing_user, i) for i in range(args.browse_users)]
        for fut in as_completed(futures):
            browse_results.append(fut.result())
    t_browse_total = time.perf_counter() - t_start
    stop_sse.set()

    ok_browse = [r for r in browse_results if r["ok"]]
    rps = len(browse_results) * 2 / max(0.001, t_browse_total)
    print(f"  ➤ 在線瀏覽成功率：{len(ok_browse)} / {len(browse_results)} ({len(ok_browse)/len(browse_results)*100:.1f}%)")
    print(f"  ➤ 總耗時：{t_browse_total:.2f} 秒 (吞吐量約 {rps:.1f} req/s)")
    print_latency_stats("階段 1 雙請求工作階段", [r["ms"] for r in browse_results])

    # ------------------------------------------------------------------
    # 階段 2：高併發結帳搶購、防超賣與取餐號唯一性驗證
    # ------------------------------------------------------------------
    print(f"\n[階段 2] 準備限量庫存商品 (庫存 = {args.stock})，由 {args.checkout_users} 位併發顧客同時結帳搶購...")
    print("  (提示：若要測試單機超過 20 人同時結帳而不觸發單 IP 防刷限流，請以 DISABLE_RATE_LIMIT=1 啟動伺服器)")
    item_id, item_name, init_stock = setup_limited_stock_item(target_stock=args.stock)
    if not item_id:
        print("  ⚠️ 無法初始化測試商品，跳過階段 2。")
        return

    checkout_results = []
    with ThreadPoolExecutor(max_workers=min(args.checkout_users, 50)) as ex:
        futures = [ex.submit(simulate_checkout_user, i, item_id, item_name) for i in range(args.checkout_users)]
        for fut in as_completed(futures):
            checkout_results.append(fut.result())

    success_orders = [r for r in checkout_results if r["status_code"] == 200 and r["order_id"]]
    sold_out_blocked = [r for r in checkout_results if r["status_code"] == 400 and r["insufficient_stock"]]
    rate_limited = [r for r in checkout_results if r["status_code"] == 429]
    other_errors = [r for r in checkout_results if r not in success_orders and r not in sold_out_blocked and r not in rate_limited]

    pickup_numbers = [r["pickup_number"] for r in success_orders if r["pickup_number"] is not None]
    unique_pickups = set(pickup_numbers)

    print(f"  ➤ 初始設定庫存：{init_stock} 份")
    print(f"  ➤ 搶購成功筆數：{len(success_orders)} 筆 (單號: {[r['order_id'] for r in success_orders]})")
    print(f"  ➤ 庫存不足攔截：{len(sold_out_blocked)} 筆 (防超賣保護觸發)")
    print(f"  ➤ 單IP限流攔截：{len(rate_limited)} 筆 (HTTP 429 防刷保護)")
    if other_errors:
        print(f"  ➤ 其他狀態筆數：{len(other_errors)} 筆 ({other_errors[0]['msg']})")

    print_latency_stats("階段 2 原子結帳", [r["ms"] for r in checkout_results])

    # 嚴格斷言檢查
    print("\n" + "-" * 68)
    if len(success_orders) <= init_stock:
        print(f"✅ [防超賣驗證通過] 成功下單數 ({len(success_orders)}) <= 初始庫存 ({init_stock})，絕無超賣！")
    else:
        print(f"❌ [防超賣驗證失敗] 成功下單數 ({len(success_orders)}) 超過初始庫存 ({init_stock})！")

    if len(pickup_numbers) == len(unique_pickups):
        print(f"✅ [取餐號互斥驗證通過] 發出之取餐號碼 {sorted(pickup_numbers)} 全數唯一無重複！")
    else:
        print(f"❌ [取餐號互斥驗證失敗] 偵測到重複的取餐號碼：{pickup_numbers}")
    print("-" * 68 + "\n")


if __name__ == "__main__":
    main()
