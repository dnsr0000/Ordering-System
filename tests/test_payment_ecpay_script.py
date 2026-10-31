"""
線上付款 (綠界 ECPay 測試環境) 測試腳本：可直接執行，也會被 pytest 執行 (模擬模式)。不會動到 database/、uploads/ 與 .env 的資料庫設定，
每次都在暫存資料夾建立獨立的「線上端」資料庫，結束後自動刪除。

用法 (在專案根目錄、啟用 venv 後執行)：

  python tests/test_payment_ecpay_script.py
  python tests/test_payment_ecpay_script.py simulate
      模擬模式 (預設，不需網路)：以本機產生、帶正確檢查碼的「綠界通知」走完所有情境並列出結果：
      付款表單簽章、待付款不送分店、檢查碼錯誤 / 金額不符 / 模擬付款被拒、付款成功後送分店、
      重送通知、瀏覽器回傳不覆蓋登入 Cookie、逾時取消並回補庫存與點數、通知遺失時向綠界查詢補記、取消後才付款標記需退款。

  python tests/test_payment_ecpay_script.py live
      實機模式 (需要網路與 Playwright 的 Chromium)：啟動本機線上端 → 下一張線上訂單 → 開啟瀏覽器到綠界測試付款頁，
      由您輸入測試卡完成付款 → 腳本確認訂單變成「已付款」並可同步給分店。
        測試卡號 4311-9522-2222-2222、有效期限任一未來月/年、安全碼任意三碼、3D 驗證簡訊碼 1234
      付款結果的確認來源 (任一即可)：
        1. 付款後瀏覽器回到本機 (Chrome 可能提示「要傳送的資訊不安全」，請按「仍要傳送」)
        2. 每 15 秒向綠界查詢訂單 (QueryTradeInfo)
        3. 綠界伺服器通知 (只有用 --public-url 提供公開的 https 網址時才收得到，例如 ngrok / Cloudflare Tunnel)
      選項：--port 5055  --timeout 600  --public-url https://xxxx.ngrok-free.app  --keep (保留暫存資料夾)

一律使用綠界公開的「測試特店」，即使 .env 填了正式金鑰也不會用到 (不會真的扣款)。
"""
import argparse
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ['KIOSK_SKIP_DOTENV'] = '1'  # 不讀 .env (不受正式設定影響，也不會在專案內產生 .secret_key)
os.environ.setdefault('SECRET_KEY', 'payment-test-secret')
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:  # noqa: BLE001
    pass

TEST_CARD = '4311-9522-2222-2222'
MEMBER_PHONE, MEMBER_PIN = '0900123456', '2468'
ITEM_NAME, ITEM_PRICE = '付款測試套餐', 120


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------
class Report:
    def __init__(self):
        self.results = []

    def step(self, title):
        print(f'\n== {title}')

    def info(self, msg):
        print(f'   {msg}')

    def check(self, ok, label, detail=''):
        self.results.append((bool(ok), label))
        print(f"   [{'PASS' if ok else 'FAIL'}] {label}" + (f'  ({detail})' if detail and not ok else ''))
        return bool(ok)

    def summary(self):
        failed = [label for ok, label in self.results if not ok]
        print('\n' + '=' * 60)
        print(f'共 {len(self.results)} 項檢查，通過 {len(self.results) - len(failed)} 項，失敗 {len(failed)} 項')
        for label in failed:
            print(f'   失敗：{label}')
        return 0 if not failed else 1


# ---------------------------------------------------------------------------
# 測試用的線上端
# ---------------------------------------------------------------------------
class HubEnv:
    """暫存資料夾中的線上端 (SYNC_ROLE=hub)：一家連線中的分店、一位會員、一個有庫存的餐點"""

    def __init__(self, csrf=False, public_url=''):
        from werkzeug.security import generate_password_hash

        from app import create_app
        from app.config import Config

        self.root = tempfile.mkdtemp(prefix='kiosk_payment_')
        dirs = {k: os.path.join(self.root, k) for k in ('database', 'member', 'menu')}
        for d in dirs.values():
            os.makedirs(d)
        cfg = type('PaymentTestConfig', (Config,), dict(
            TESTING=True, SQLALCHEMY_DATABASE_URI='sqlite:///' + os.path.join(dirs['database'], 'hub.db'),
            SQLALCHEMY_ENGINE_OPTIONS={'connect_args': {'timeout': 30, 'check_same_thread': False}},
            DATABASE_DIR=dirs['database'], UPLOAD_FOLDER_MEMBER=dirs['member'], UPLOAD_FOLDER_MENU=dirs['menu'],
            AUTO_MIGRATE=True, ADMIN_USERNAME='payment_owner', ADMIN_PASSWORD='PaymentOwner!2026',
            ADMIN_PASSWORD_HASH=generate_password_hash('PaymentOwner!2026'), ADMIN_CREDENTIALS_GENERATED=False,
            SECRET_KEY='payment-test-secret', CSP_MODE='enforce',
            RESET_ADMIN_PASSWORD_ON_START=False, REDIS_URL='', RATELIMIT_STORAGE_URI='memory://',
            RATELIMIT_ENABLED=False, GEMINI_API_KEY='', LOCAL_MODEL_PATH='', WTF_CSRF_ENABLED=csrf,
            TRUSTED_PROXY_COUNT=0, SESSION_COOKIE_SECURE=False,
            SYNC_ROLE='hub', SYNC_AUTOSTART=False, ONLINE_PAYMENT=True, PAYMENT_TIMEOUT_MINUTES=15,
            # 一律使用綠界公開的測試特店 (公開金鑰需明確允許，見 payment_ecpay.test_keys_blocked)
            ECPAY_STAGE=True, ECPAY_MERCHANT_ID='', ECPAY_HASH_KEY='', ECPAY_HASH_IV='', ECPAY_ALLOW_TEST_KEYS=True,
            PUBLIC_BASE_URL=public_url.rstrip('/'),
        ))
        self.app = create_app(cfg)
        self._seed()

    @contextmanager
    def tenant(self):
        from app.models.tenant import Tenant
        from app.tenant_context import _use_tenant
        with self.app.test_request_context('/'):
            tenant = Tenant.query.filter_by(slug='default').first()
            _use_tenant(tenant, remember=False)
            yield tenant

    def _seed(self):
        from app.extensions import db
        from app.models.menu import MenuItem
        from app.models.sync import NodeStock
        from app.models.user import User
        from app.services import privacy_service
        from app.sync.nodes import create_node
        with self.tenant() as tenant:
            node, self.node_token = create_node(tenant.id, '付款測試分店')
            self.node_uuid, self.node_id = node.uuid, node.id
            item = MenuItem(name=ITEM_NAME, category='主餐', price=ITEM_PRICE, stock=50, total_stock=50)
            user = User(name='付款測試會員', phone=MEMBER_PHONE, photo_path='', points=100)
            user.set_pin(MEMBER_PIN)
            privacy_service.record_consent(user)
            db.session.add_all([item, user])
            db.session.commit()
            self.item_id, self.user_id = item.id, user.id
            db.session.add(NodeStock(node_id=node.id, menu_item_id=item.id, stock=30, is_sold_out=False))
            db.session.commit()
        self.keep_node_online()

    def keep_node_online(self):
        """模擬分店仍在連線 (線上點餐只開放給連線中的分店)"""
        from app.extensions import db
        from app.models.sync import SyncNode
        with self.app.app_context():
            node = db.session.get(SyncNode, self.node_id)
            node.last_seen_at = datetime.now()
            db.session.commit()

    def checkout(self, quantity=1):
        from app.services.order_service import process_checkout
        self.keep_node_online()
        with self.tenant() as tenant:
            order, _ = process_checkout(tenant_id=tenant.id, items=[{'menu_item_id': self.item_id, 'quantity': quantity}],
                                        user_id=self.user_id, node_uuid=self.node_uuid, order_type='外帶')
            return {'uuid': order.uuid, 'id': order.id, 'total': order.total_price}

    def order(self, uuid):
        from app.models.order import Order
        with self.app.app_context():
            o = Order.query.filter_by(uuid=uuid).first()
            return None if o is None else dict(status=o.status, payment_status=o.payment_status, ref=o.payment_ref,
                                               trade_no=o.payment_trade_no, total=o.total_price, paid_at=o.paid_at)

    def node_stock(self):
        from app.extensions import db
        from app.models.sync import NodeStock
        with self.app.app_context():
            return db.session.get(NodeStock, (self.node_id, self.item_id)).stock

    def member_points(self):
        from app.extensions import db
        from app.models.user import User
        with self.app.app_context():
            return db.session.get(User, self.user_id).points

    def node_receives(self, order_uuid):
        """以分店權杖呼叫同步 API：這張訂單 (含明細) 是否會下發給分店"""
        client = self.app.test_client()
        body = client.get('/api/sync/v1/pull', query_string={'cursor': -1, 'limit': 1000},
                          headers={'Authorization': f'Bearer {self.node_token}'}).get_json() or {}
        changes = body.get('changes') or {}
        orders = [o for o in changes.get('order', []) if o['uuid'] == order_uuid]
        items = [i for i in changes.get('order_item', []) if i.get('order_id') == order_uuid]
        return bool(orders), len(items)

    def signed_notification(self, order, **overrides):
        """產生一份帶正確檢查碼的綠界付款結果通知 (與綠界伺服器送來的格式相同)"""
        from app.services import payment_ecpay
        params = {'MerchantID': '3002607', 'MerchantTradeNo': (self.order(order['uuid'])['ref'] or 'KSIMULATED0000000001'),
                  'RtnCode': '1', 'RtnMsg': '交易成功', 'TradeNo': '24' + datetime.now().strftime('%m%d%H%M%S') + '0001',
                  'TradeAmt': str(order['total']), 'PaymentDate': datetime.now().strftime('%Y/%m/%d %H:%M:%S'),
                  'PaymentType': 'Credit_CreditCard', 'PaymentTypeChargeFee': '3',
                  'TradeDate': datetime.now().strftime('%Y/%m/%d %H:%M:%S'), 'SimulatePaid': '0',
                  'CustomField1': order['uuid'], 'CustomField2': '', 'CustomField3': '', 'CustomField4': '', 'StoreID': ''}
        params.update(overrides)
        with self.app.app_context():
            _mid, key, iv, _base = payment_ecpay._cfg()
            params['CheckMacValue'] = payment_ecpay.check_mac_value(params, key, iv)
        return params

    def make_overdue(self, order_uuid, minutes=30):
        from app.extensions import db
        from app.models.order import Order
        with self.app.app_context():
            o = Order.query.filter_by(uuid=order_uuid).first()
            o.created_at = datetime.now() - timedelta(minutes=minutes)
            o.payment_ref = o.payment_ref or ('KOVERDUE' + order_uuid.replace('-', '')[:12].upper())
            db.session.commit()

    def expire(self, query):
        from app.services.payment_ecpay import expire_unpaid_orders
        with self.app.app_context():
            return expire_unpaid_orders(force=True, query=query)

    def close(self, keep=False):
        from app.extensions import db
        with self.app.app_context():
            db.engine.dispose()
        if keep:
            print(f'\n暫存資料夾保留在：{self.root}')
        else:
            shutil.rmtree(self.root, ignore_errors=True)


def member_client(env):
    client = env.app.test_client()
    client.get('/?shop=default')
    r = client.post('/phone_login', json={'phone': MEMBER_PHONE, 'pin': MEMBER_PIN})
    assert r.status_code == 200, r.get_data(as_text=True)
    return client


# ---------------------------------------------------------------------------
# 模擬模式
# ---------------------------------------------------------------------------
def run_simulate(args):
    from app.services import payment_ecpay
    rep = Report()
    env = HubEnv()
    try:
        rep.step('1. 檢查碼演算法 (綠界文件範例)')
        sample = {'TradeDesc': '促銷方案', 'PaymentType': 'aio', 'MerchantTradeDate': '2023/03/12 15:30:23',
                  'MerchantTradeNo': 'ecpay20230312153023', 'MerchantID': '3002607',
                  'ReturnURL': 'https://www.ecpay.com.tw/receive.php', 'ItemName': 'Apple iphone 15',
                  'TotalAmount': '30000', 'ChoosePayment': 'ALL', 'EncryptType': '1'}
        mac = payment_ecpay.check_mac_value(sample, 'pwFHCqoQZGmho4w6', 'EkRm7iFT261dpevs')
        rep.check(mac == '6C51C9E6888DE861FD62FB1DD17029FC742634498FD813DC43D4243B5685B840', '與文件範例的檢查碼相同', mac)

        rep.step('2. 下單與付款頁')
        stock0, points0 = env.node_stock(), env.member_points()
        order = env.checkout()
        rep.info(f"訂單 {order['uuid']}，金額 ${order['total']}")
        rep.check(env.order(order['uuid'])['payment_status'] == 'pending', '新訂單為「待付款」')
        rep.check(env.node_stock() == stock0 - 1, '已預扣分店庫存快照')
        rep.check(env.node_receives(order['uuid'])[0] is False, '待付款的訂單不會送到分店')
        client = member_client(env)
        r = client.get(f"/pay/{order['uuid']}")
        page = r.get_data(as_text=True)
        fields = {k: v.replace('&amp;', '&') for k, v in re.findall(r'name="([^"]+)" value="([^"]*)"', page)}
        rep.check(r.status_code == 200 and 'payment-stage.ecpay.com.tw/Cashier/AioCheckOut/V5' in page,
                  '付款頁送往綠界測試環境', r.status_code)
        with env.app.app_context():
            rep.check(payment_ecpay.verify(fields), '付款表單的檢查碼正確')
        rep.check(fields.get('TotalAmount') == str(order['total']) and fields.get('CustomField1') == order['uuid'],
                  '表單金額與訂單編號正確')
        rep.check('payment-stage.ecpay.com.tw' in r.headers.get('Content-Security-Policy', ''), '付款頁的 CSP 允許送出到綠界')

        rep.step('3. 綠界付款結果通知 (ReturnURL)')
        hub = env.app.test_client()

        def notify(params):
            return hub.post('/payment/ecpay/notify', data=params).get_data(as_text=True)

        bad = env.signed_notification(order)
        bad['CheckMacValue'] = '0' * 64
        rep.check(notify(bad) == '0|CheckMacValue Error', '檢查碼錯誤的通知被拒絕')
        rep.check(notify(env.signed_notification(order, TradeAmt='1')) == '0|AmountMismatch', '金額不符的通知被拒絕')
        rep.check(notify(env.signed_notification(order, SimulatePaid='1')) == '1|OK'
                  and env.order(order['uuid'])['payment_status'] == 'pending', '後台「模擬付款」不算付款')
        rep.check(notify(env.signed_notification(order, RtnCode='10100248', RtnMsg='授權失敗')) == '1|OK'
                  and env.order(order['uuid'])['payment_status'] == 'pending', '付款失敗的通知不改變訂單')
        good = env.signed_notification(order)
        rep.check(notify(good) == '1|OK', '付款成功通知回應 1|OK')
        o = env.order(order['uuid'])
        rep.check(o['payment_status'] == 'paid' and o['trade_no'] == good['TradeNo'], '訂單變成「已付款」並記錄綠界交易編號')
        rep.check(notify(good) == '1|OK' and env.order(order['uuid'])['payment_status'] == 'paid', '綠界重送通知不影響')
        got_order, item_count = env.node_receives(order['uuid'])
        rep.check(got_order and item_count == 1, '付款後訂單 (含明細) 會送到分店', f'order={got_order} items={item_count}')

        rep.step('4. 付款後瀏覽器回到本站 (OrderResultURL)')
        order2 = env.checkout()
        r = env.app.test_client().post('/payment/ecpay/result', data=env.signed_notification(order2))
        rep.check(r.status_code == 303 and r.headers['Location'].endswith(f"/payment/return/{order2['uuid']}"),
                  '驗證後導向付款結果頁')
        rep.check('Set-Cookie' not in r.headers, '跨站 POST 回來時不會覆蓋顧客的登入 Cookie')
        rep.check(env.order(order2['uuid'])['payment_status'] == 'paid', '瀏覽器回傳也能記錄付款')
        unsigned = env.app.test_client().post('/payment/ecpay/result', data={'CustomField1': order2['uuid']})
        rep.check(unsigned.status_code == 400, '沒有檢查碼的回傳被拒絕')

        rep.step('5. 逾時未付款')
        stock1, points1 = env.node_stock(), env.member_points()
        order3 = env.checkout()
        rep.check(env.member_points() > points1, '下單時已贈點', f'{points1} -> {env.member_points()}')
        rep.check(env.expire(lambda ref: {'TradeStatus': '0'}) == 0, '未超過付款期限不會取消')
        env.make_overdue(order3['uuid'])
        rep.check(env.expire(lambda ref: None) == 0 and env.order(order3['uuid'])['payment_status'] == 'pending',
                  '查不到綠界結果 (例如斷網) 時不冒險取消')
        rep.check(env.expire(lambda ref: {'TradeStatus': '0'}) == 1, '綠界確認未付款 → 取消')
        o3 = env.order(order3['uuid'])
        rep.check(o3['status'] == 'Cancelled' and o3['payment_status'] == 'expired', '訂單為「已取消 / 付款逾時」')
        rep.check(env.node_stock() == stock1, '回補分店庫存快照', f'{stock1} vs {env.node_stock()}')
        rep.check(env.member_points() == points1, '追回贈點', f'{points1} vs {env.member_points()}')
        rep.check(notify(env.signed_notification(order3)) == '1|OK'
                  and env.order(order3['uuid'])['payment_status'] == 'refund_required', '取消後才收到付款 → 標記「需退款」')

        rep.step('6. 綠界通知遺失')
        order4 = env.checkout()
        env.make_overdue(order4['uuid'])
        env.expire(lambda ref: {'TradeStatus': '1', 'TradeNo': 'QUERY0001', 'TradeAmt': str(order4['total'])})
        o4 = env.order(order4['uuid'])
        rep.check(o4['payment_status'] == 'paid' and o4['status'] == 'Pending', '逾時前向綠界查詢到已付款 → 補記為已付款，不取消')

        rep.check(stock0 - env.node_stock() == 3 and points0 <= env.member_points(), '整體庫存與點數一致',
                  f'庫存 {stock0}->{env.node_stock()}')
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rep.check(False, '執行過程發生例外')
    finally:
        env.close(keep=args.keep)
    return rep.summary()


# ---------------------------------------------------------------------------
# 實機模式 (綠界測試環境)
# ---------------------------------------------------------------------------
def run_live(args):
    from werkzeug.serving import make_server

    from app.services import payment_ecpay
    rep = Report()
    base = f'http://127.0.0.1:{args.port}'
    env = HubEnv(csrf=True, public_url=args.public_url or base)
    server = make_server('127.0.0.1', args.port, env.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        rep.step('1. 本機線上端已啟動')
        rep.info(f'網址 {base}  (暫存資料夾 {env.root})')
        if args.public_url:
            rep.info(f'綠界伺服器通知會送到 {args.public_url}/payment/ecpay/notify (請確認通道已指向 {base})')
        else:
            rep.info('未提供 --public-url：收不到綠界伺服器通知，改以瀏覽器回傳與向綠界查詢確認付款')

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            rep.check(False, '需要 Playwright：pip install playwright 後執行 playwright install chromium')
            return rep.summary()

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=False)
            except Exception as exc:  # noqa: BLE001
                rep.check(False, '無法啟動 Chromium，請先執行 playwright install chromium', str(exc))
                return rep.summary()
            page = browser.new_context(locale='zh-TW').new_page()

            rep.step('2. 會員登入並下單')
            page.goto(base + '/?shop=default')
            token = page.locator('meta[name="csrf-token"]').get_attribute('content')
            r = page.request.post(base + '/phone_login', data={'phone': MEMBER_PHONE, 'pin': MEMBER_PIN},
                                  headers={'X-CSRFToken': token})
            rep.check(r.ok, '會員登入')
            env.keep_node_online()
            r = page.request.post(base + '/pickup_node', form={'node_uuid': env.node_uuid, 'csrf_token': token})
            r = page.request.post(base + '/submit_order', headers={'X-CSRFToken': token}, data={
                'items': [{'menu_item_id': env.item_id, 'quantity': 1}], 'payment_method': 'Credit Card',
                'order_type': '外帶', 'client_order_id': str(uuid.uuid4())})
            data = r.json() if r.ok else {}
            if not rep.check(r.ok and data.get('payment_url'), '線上訂單建立並取得付款網址', r.text()[:200]):
                return rep.summary()
            order_uuid = data['order_uuid']
            rep.info(f"訂單 {order_uuid}，金額 ${data['total_price']}")

            rep.step('3. 前往綠界測試付款頁')
            print(f'''
   ┌──────────────────────────────────────────────────────────┐
     請在開啟的瀏覽器中完成付款 (測試環境，不會真的扣款)：
       付款方式：信用卡
       卡號：{TEST_CARD}
       有效期限：任一未來的月/年      安全碼：任意三碼
       3D 驗證簡訊碼：1234
     付款後若出現「要傳送的資訊不安全」，請按「仍要傳送」。
   └──────────────────────────────────────────────────────────┘''')
            page.goto(base + data['payment_url'])

            rep.step(f'4. 等待付款結果 (最多 {args.timeout} 秒)')
            deadline, last_query, source = time.time() + args.timeout, 0.0, ''
            while time.time() < deadline:
                order = env.order(order_uuid)
                if order['payment_status'] == 'paid':
                    source = source or '瀏覽器回傳或綠界伺服器通知'
                    break
                if time.time() - last_query >= 15 and order['ref']:
                    last_query = time.time()
                    with env.app.app_context():
                        info = payment_ecpay.query_trade(order['ref'])
                    if info is None:
                        rep.info('向綠界查詢失敗 (網路或檢查碼問題)，稍後再試')
                    else:
                        rep.info(f"綠界查詢：TradeStatus={info.get('TradeStatus')} (1 = 已付款，0 = 尚未付款)")
                        if str(info.get('TradeStatus')) == '1':
                            with env.app.app_context():
                                payment_ecpay.mark_paid(order_uuid, info.get('TradeNo'), info.get('TradeAmt'))
                            source = '向綠界查詢 (QueryTradeInfo)'
                            continue
                try:
                    page.wait_for_timeout(2000)
                except Exception:  # noqa: BLE001  (瀏覽器被關閉)
                    time.sleep(2)

            rep.step('5. 結果')
            order = env.order(order_uuid)
            if rep.check(order['payment_status'] == 'paid', '訂單已付款', f"目前狀態：{order['payment_status']}"):
                rep.info(f"確認來源：{source}")
                rep.info(f"綠界交易編號：{order['trade_no']}，特店訂單編號：{order['ref']}，付款時間：{order['paid_at']}")
                with env.app.app_context():
                    info = payment_ecpay.query_trade(order['ref'])
                rep.check(info is not None and str(info.get('TradeStatus')) == '1'
                          and int(info.get('TradeAmt') or 0) == order['total'], '綠界端查詢結果一致 (已付款、金額相同)',
                          str(info)[:200])
                got_order, item_count = env.node_receives(order_uuid)
                rep.check(got_order and item_count >= 1, '訂單 (含明細) 已可同步給分店')
                rep.info("可到綠界測試後台查看：https://vendor-stage.ecpay.com.tw (stagetest3 / test1234)")
            try:
                page.wait_for_timeout(1500)
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        rep.check(False, '執行過程發生例外')
    finally:
        server.shutdown()
        env.close(keep=args.keep)
    return rep.summary()


def _quiet_logs(verbose):
    import logging
    if not verbose:  # 只顯示檢查結果；--verbose 顯示資料庫遷移與程式的紀錄
        logging.basicConfig(level=logging.CRITICAL, force=True)
        for name in ('alembic', 'app', 'werkzeug'):
            logging.getLogger(name).setLevel(logging.CRITICAL)


def main(argv=None):
    parser = argparse.ArgumentParser(description='線上付款 (綠界測試環境) 測試腳本')
    parser.add_argument('mode', nargs='?', choices=('simulate', 'live'), default='simulate',
                        help='simulate：模擬綠界通知 (預設，不需網路)；live：實際到綠界測試環境刷測試卡')
    parser.add_argument('--port', type=int, default=5055, help='live 模式本機伺服器的埠號')
    parser.add_argument('--timeout', type=int, default=600, help='live 模式等待付款的秒數')
    parser.add_argument('--public-url', default='', help='live 模式：指向本機的公開 https 網址 (接收綠界伺服器通知)')
    parser.add_argument('--keep', action='store_true', help='保留暫存資料夾 (除錯用)')
    parser.add_argument('--verbose', action='store_true', help='顯示程式紀錄 (除錯用)')
    args = parser.parse_args(argv)
    _quiet_logs(args.verbose)
    return run_simulate(args) if args.mode == 'simulate' else run_live(args)


def test_simulate_mode_all_checks_pass(capsys):
    """pytest：模擬模式的所有情境都要通過 (避免腳本跟著程式修改而壞掉)"""
    assert main(['simulate', '--verbose']) == 0, capsys.readouterr().out[-3000:]
    out = capsys.readouterr().out
    assert '失敗 0 項' in out and '[FAIL]' not in out


if __name__ == '__main__':
    sys.exit(main())
