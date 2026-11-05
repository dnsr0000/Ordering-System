# 資安審查第二輪 (2026-10-30) — 發現的漏洞與修正

審查範圍：main 分支 55bfe36 (PR #16 合併後)。以程式碼審查 + pytest PoC 驗證。
**全部已於同日修正** (見文末「修正內容」)，正式測試在 tests/test_security_audit2.py (17 項) 與 tests/test_e2e_pos.py 的
`test_pos_points_require_member_pin_modal`；原本的 PoC 檔 poc_security_audit2.py 已不再適用 (修正後會失敗)，可刪除。
上一輪 (claude/security-audit-2026-10-22.md) 已修的項目這次都沒有回歸；前端 html`` 樣板 + CSP、ORM 租戶過濾、綠界檢查碼、
同步信任邊界、備份還原的 zip 檢查都沒有找到新問題。以下依嚴重度排列。

## 高

### 1. 任何店家負責人都能下載整個 SQLite 資料庫 (含其他店家的資料與所有金鑰)
- 位置：`app/routes/system.py` `backup_now` (POST /admin/system/backup)、`download_backup` (GET /admin/system/backup/<name>)、
  `status` 頁列出備份檔。三者只有 `@role_required('owner')`，沒有 `platform_admin_required`。
- 對照：`app/routes/admin.py` 的 `/admin/backup_system` 同樣是整庫備份，卻正確限制為平台管理者 (預設店家負責人)。
- 攻擊鏈 (PoC `test_unauthenticated_chain_register_tenant_then_dump_db`)：未登入 → `/tenant/register` 公開註冊一家新店
  (無審核、10 次/小時) → 成為該店 owner → POST /admin/system/backup → 從 /admin/system 取得檔名 → 下載 zip → `menu.db`
  含全部店家的會員 (手機、PIN 雜湊、人臉特徵、照片路徑)、員工帳號與密碼雜湊、API 金鑰雜湊、分店權杖雜湊，
  以及 `store_setting` 內**明文**儲存的 secret 欄位 (綠界 HashKey/IV、LINE Channel Secret / Access Token、SMTP 密碼、
  簡訊帳密、Uber Client Secret、foodpanda 權杖、監控 Webhook)。
- 影響：門市主機 (standalone / node，SQLite)。線上端 PostgreSQL 的 `run_sqlite_backup` 回 None 所以不受影響；
  但門市主機只要在區網可達 (顧客掃桌邊 QR code 就是連到它)，任何人都能把整個資料庫拿走。
- 建議：
  1. `backup_now` / `download_backup` / 系統監控頁的備份清單一律加 `platform_admin_required` (與 backup_system 一致)，
     或改成只備份本店資料。
  2. `/tenant/register` 在 standalone / node 預設關閉 (加 `ALLOW_TENANT_REGISTER` 設定，預設 0；線上端才開)，
     或至少需要平台管理者登入後才能建立新店。
  3. 補測試：其他店負責人 POST /admin/system/backup → 403；GET /admin/system/backup/<name> → 403。

## 中

### 2. 綠界預設使用公開測試金鑰 (ECPAY_STAGE=1)，付款通知可被任何人偽造 → 免費成立訂單
- 位置：`app/services/payment_ecpay.py` `_cfg()` 在未設定金鑰時用 `TEST_MERCHANT` (3002607 / pwFHCqoQZGmho4w6 / EkRm7iFT261dpevs，
  綠界公開文件就有)；`app/__init__.py` 只在 ECPAY_STAGE=0 時檢查金鑰，ECPAY_STAGE=1 沒有任何執行期防護，只靠 DEPLOY.txt 提醒。
- PoC `test_forged_ecpay_notify_with_public_stage_keys`：只知道訂單 uuid (收據 / 追蹤頁網址) 與金額，用公開金鑰算出
  CheckMacValue，POST /payment/ecpay/notify → 回 `1|OK`，訂單變 paid、同步到分店出餐。
- 同樣根因的入口：`/payment/ecpay/result` (瀏覽器 POST，直接 mark_paid)、桌邊結帳 `/payment/ecpay/table_result` 與
  `table_notify` (`app/routes/table_pay.py`，顧客在桌邊就能把自己這一桌標成已付款，而且這兩條路由沒有檢查
  `payment_ecpay.is_enabled()`)、訂位訂金 `reservation_result` / `reservation_notify`。
- 影響：門市主機 `.env` 沒改 ECPAY_STAGE (預設 1) 卻開了桌邊線上結帳，或線上端忘了設 0，就是免費吃飯。
- 建議：
  1. 非測試環境且實際使用的是 TEST_MERCHANT 時，`handle_notification` / `mark_paid` 只接受 `SimulatePaid` 以外的通知
     需明確設定 `ECPAY_ALLOW_TEST_KEYS=1`；否則記錄警告並回 `0|TestKeys`。啟動時若 hub/node 開了線上付款或桌邊結帳
     而仍用測試金鑰，印出醒目警告 (或直接拒絕啟動)。
  2. `table_pay` 與 `reservations` 的 notify/result 路由加上 `_require_enabled()`。
  3. `mark_paid` 順便檢查 `MerchantTradeNo == order.payment_ref` (多一層綁定)。

### 3. LINE「待綁定」資料不會過期也不隨登出清除 → 共用點餐機上的會員帳號接管
- 位置：`app/routes/member_auth.py` `line_callback` 把 `session['line_pending']` 留在瀏覽器 (點餐機的 Cookie)；
  `bind_pending_line` 在 `phone_login` / `register` 成功後自動綁定；`customer_session.close_customer_session` 的
  `CUSTOMER_SESSION_KEYS` 不含 `line_pending`，`at` 欄位也從未檢查。
- PoC `test_line_pending_binding_survives_logout`：攻擊者在點餐機用自己的 LINE 授權後離開 (或按登出 / 閒置登出)，
  下一位用手機 + PIN 登入的顧客就被綁上攻擊者的 LINE；之後攻擊者在自己手機用 LINE 登入即進入受害者帳號
  (點數、儲值金、優惠券、個資下載 /privacy/my_data 不需 PIN)。登入頁只有一行小字提示「登入後會自動綁定剛才授權的 LINE」。
- 建議：登出 / 閒置登出時清除 `line_pending`；pending 超過 10 分鐘失效；綁定前顯示 LINE 名稱並要求顧客確認 (或輸入 PIN)。

### 4. /timeclock 未登入即可看到員工帳號，配合帳號層級鎖定可把員工帳號持續鎖死 (登入 DoS)
- 位置：`templates/timeclock.html` 列出今日班表與打卡紀錄的 `username`，路由 `/timeclock` 不需登入；
  `auth_throttle.STAFF_ACCOUNT_MAX_FAILS = 10` 不分 IP 累計，鎖 15 分鐘，可無限重複。
- PoC `test_timeclock_page_leaks_usernames_and_enables_lockout`：10 個 IP 各猜錯一次 → 負責人用正確密碼登入也回 429。
  營業中 POS / 廚房 / 後台都無法新登入 (已登入的工作階段不受影響)。
- 建議：打卡看板的名單改只顯示遮罩姓名或需登入 / 已註冊裝置才顯示；帳號層級鎖定改為區網 / 已註冊裝置豁免，
  或改成遞增延遲 + CAPTCHA 而不是硬鎖。

### 5. POS 只憑手機號碼就能動用會員儲值金 / 點數 / 寄杯
- 位置：`app/routes/pos.py` `api_pos_member_lookup` 以手機或姓名模糊查詢即回傳會員；`api_pos_checkout` 帶 `user_id` 與
  `payment_method='儲值金'` / `use_points` 就扣款，`order_checkout` 沒有要求會員 PIN 或動態條碼 (`via_qr`)。
- PoC `test_pos_can_spend_member_wallet_by_phone_only`：一般 staff 查到會員後直接以儲值金結帳，餘額 500 → 380。
- 風險：知道別人手機號碼的人冒用 (社交工程)、店員內部盜用。動態條碼機制已經存在但不是必要條件。
- 建議：儲值金 / 點數折抵 / 寄杯兌換在 POS 需「會員動態條碼」或「會員當面輸入 PIN」，否則走主管核准 (require_approval)。
  後台可加設定開關讓小店選擇寬鬆模式。

## 低

### 6. 前端可自訂 `client_order_id`，而它直接成為 `order.uuid`
- `order_checkout.process_checkout` 把 `client_order_id[:36]` 寫進 `order.uuid`，沒有格式驗證；`uuid` 是 `/track/<uuid>`、
  `/pay/<uuid>` 的唯一憑證 (`public_extras._order_by_uuid` 註解假設它是 uuid4)。門市用 http://區網IP 時瀏覽器不是
  secure context，`Kiosk.newOrderId` 退回 `Math.random()` 產生。
- 建議：`client_order_id` 改存獨立欄位 (tenant_id + client_order_id 唯一)，`uuid` 一律由伺服器產生。

### 7. 店家可設定的對外連線沒有內網過濾 (線上端上任何店家負責人可做 blind SSRF / 掃內網)
- `monitor` 的 `webhook_url` (`monitor_service.send_alert_now` 直接 `requests.post`，含跟隨轉址)、`clock_server` (UDP 123)、
  SMTP host/port (`mail_service`，錯誤訊息會回到畫面，可判斷埠是否開啟)、網路印表機 host/port (escpos TCP)。
  對照 `webhook_service.check_public` 已經有完整的公開位址檢查，但只用在 API Webhook 端點。
- 建議：monitor webhook 重用 `check_public`；線上端 (hub) 把 SMTP / 印表機 / NTP 設成 LOCAL_ONLY 或同樣過濾內網位址。

### 8. 簡訊驗證碼沒有店家層級總量上限
- `sms_service.send_otp` 每支手機每小時 5 次、每 IP 每分鐘 5 次；換 IP + 換號碼即可無上限消耗店家簡訊點數 (SMS pumping)。
- 建議：每店每小時 / 每日上限 + 超過時通知。

### 9. 其他觀察 (不一定要改)
- `/privacy/my_data` 與 `/api/member_qr` 在人臉登入 (非秘密因子) 後即可使用；刪除帳號有要求 PIN，下載個資沒有。
- 桌邊結帳 `table_pay_service.mark_paid` 兩個通知同時到達時沒有鎖，第二個會把已付款的 TablePayment 改成 conflict 並發警報 (誤報)。
- 上一輪保留的低優先項目仍在：docker-compose 的 postgres/caddy 讀整份 .env.hub、首次產生的負責人密碼留在 docker logs、公開註冊店家無審核 (本輪第 1 項讓它升級為高風險)。

## 建議修正順序
1 (權限) → 2 (綠界測試金鑰防護) → 3 (LINE pending) → 4 (打卡頁 / 鎖定) → 5 (POS 會員驗證) → 6~8。
修正後把 `poc_security_audit2.py` 的斷言反過來改成正式測試放進 tests/ (檔名依主題，README 七、測試一併更新)。

## 修正內容 (2026-10-30，同日完成；pytest 819 passed、e2e 99 passed)
1. `app/routes/system.py`：`backup_now` / `download_backup` 加 `platform_admin_required`，系統監控頁只對平台管理者顯示備份清單。
   新增設定 `ALLOW_TENANT_REGISTER` (線上端預設 1，門市主機預設 0)：關閉時 /tenant/register 只有平台管理者登入後可用
   (建立後仍維持平台管理者的登入)，登入頁不顯示「建立新店家帳號」。
2. `payment_ecpay`：`using_test_keys()` / `test_keys_blocked()` / `require_keys_allowed()`；新增設定 `ECPAY_ALLOW_TEST_KEYS` (預設 0)。
   未允許時 `verify()` 一律 False (notify / result / 桌邊 / 訂位都擋)、`query_trade` 回 None、付款表單與線上待付款訂單 /
   桌邊付款紀錄不建立 (503 並說明)、hub 啟動時警告。TestConfig 設 1。
3. `member_auth`：`line_pending` 10 分鐘失效 (`pending_line()`)、登出 / 閒置登出時一併清除 (`CUSTOMER_SESSION_KEYS`)、
   登入頁顯示待綁定的 LINE 名稱並可「取消綁定」(POST /auth/line/pending/discard)。
4. `timeclock`：未登入 (且非已註冊裝置) 的打卡頁帳號以 `mask_name` 遮罩。`auth_throttle.staff_lock_remaining()`：
   後台「裝置管理」註冊過的裝置只受裝置層級 (同 IP 同帳號 5 次) 鎖定，不受帳號層級鎖定 (登入、2FA、主管核准、打卡都用這個)。
5. 新模組 `app/services/member_verify_service.py`：POS 結帳動用儲值金 (含混合付款) / 點數折抵 / 寄杯時，需同一收銀工作階段
   15 分鐘內掃過會員動態條碼、或請求帶 `member_pin` (與點餐機相同的鎖定規則)、或主管核准 (新權限 `member_verify`，
   店長以上預設有；稽核 MEMBER_VERIFY_OVERRIDE)；否則回 403 `need_member_pin`。POS 新增「會員身分驗證」視窗
   (`_pos_pro_modals.html`、`pos-pro.js` `withMemberVerify`、`checkout.js`)。設定「會員經營 → POS 動用儲值金 / 點數 / 寄杯
   需驗證會員身分」預設開啟；測試環境 conftest 對預設店家關閉 (只有 test_security_audit2 開啟)。
6. 遷移 0024：`order.client_order_id` 獨立欄位 (同店唯一索引，舊資料以 uuid 補值)，`uuid` 一律伺服器產生；
   `find_by_client_order_id()` 供結帳重送 / 斷線暫存 / 團購 / 外送平台去重；registry 設為 local_only (不同步)。
7. `webhook_service.check_public_host()` / `check_public_host_on_hub()`：監控 Webhook 一律檢查 (WEBHOOK_ALLOW_PRIVATE=1 放寬)；
   線上端的寄信伺服器、校時伺服器、網路印表機主機不可指向內網 (門市主機不限制)。
8. `sms_service._check_tenant_quota()`：全店每小時 / 每天驗證碼上限 (設定「簡訊驗證碼」可調，預設 60 / 300)，達到時 429 並通知。
文件：README (環境變數、遷移清單、測試清單)、DEPLOY (上線檢查清單)、MANUAL_TEST (AS-01 ~ AS-08)、.env.example、deploy/hub.env.example。
