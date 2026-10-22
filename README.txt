====================================================================
智慧自助點餐、POS 收銀、人臉辨識與多店家營運系統 (Kiosk & POS)
====================================================================

一套給單店與連鎖餐飲使用的門市系統：顧客自助點餐機、櫃台 POS、廚房出單看板、取餐叫號、
店家後台，以及總部的線上點餐網站。門市斷網照常營業，連線後與線上端雙向同步。

相關文件
- README.txt   (本檔) 功能、安裝、設定、指令、開發慣例
- SYNC.txt     門市 ↔ 線上端同步的設計與合併規則
- DEPLOY.txt   線上端 Docker 部署、門市 Windows 部署、同步設定、線上付款、上線前檢查清單
- MANUAL_TEST.txt  人工實測步驟 (全功能，每項含步驟與預期結果)


一、系統角色 (環境變數 SYNC_ROLE)
--------------------------------------------------------------------
  standalone (預設)  單機門市：SQLite，不同步。
  node               門市分店：SQLite，可完全離線營業；背景與線上端同步。
  hub                線上端：PostgreSQL + Docker；線上點餐網站與總部後台，所有分店資料的匯集點。

    [門市 A (node)]──┐  HTTPS + 分店權杖
    [門市 B (node)]──┼──── [線上端 (hub)] ──── 顧客手機 (線上點餐、綠界付款)
    [門市 C (node)]──┘

  - 菜單、優惠券、紅利設定、員工帳號、會員與點數：同一店家的所有分店共用 (雙向同步)。
  - 訂單：分店自己的訂單上傳到線上端；線上訂單付款後只送到顧客選的取餐分店。
  - 庫存：各分店獨立；線上端用分店回報的庫存快照接受線上訂單。


二、功能
--------------------------------------------------------------------
顧客
- 自助點餐 (/)：菜單分類與搜尋、套餐配餐、客製化 (冰塊/甜度/辣度/加料)、條件加購、優惠券、紅利折抵、我的訂單。
- 會員：手機 + PIN 登入；門市另可人臉登入 (YuNet + SFace，選填並須另行同意)；紅利商城 (/rewards) 兌換優惠券與商品。
- 訪客點餐：門市 (點餐機或顧客用手機連門市網站) 可以；線上點餐只限會員。
- 桌邊掃碼點餐 (門市)：顧客手機連店內 Wi-Fi 掃桌上的 QR code (/t/<代碼>)，以訪客或會員身分點餐；
  固定內用、訂單標記桌號並直接送廚房，用餐後到櫃檯結帳。
- 線上點餐 (線上端)：選取餐分店 → 綠界信用卡付款 → 付款後訂單送到分店廚房；取餐號由分店收單時編號。
- 隱私權告知 (/privacy)：個資法第 8 條告知事項；會員可在此撤回人臉同意並刪除人臉資料。

門市員工
- POS (/pos)：觸控點餐、掛單 (F4)、結帳 (F2)、會員查詢與設定 PIN、找零、80mm 熱感應收據、當日訂單、
  桌位結帳 (一次結清某桌所有掃碼點餐的訂單)。
- 廚房 KDS (/kitchen)、取餐核銷 (/pickup)、叫號看板 (/counter，公開、會員姓名遮罩、語音叫號)：SSE 即時更新。

店家後台 (/admin)
- 營運分析 (今日營收、預估出餐時間、熱門排行)、AI 商品文案與營運建議 (Gemini → 本地 Qwen → 規則引擎)。
- 菜單 / 套餐 / 加料 / 優惠券 / 紅利設定 / 會員管理、稽核日誌、Excel 匯出與智慧匯入。
- 員工帳號 (/admin/staff)、隱私權告知設定 (/admin/privacy)。
- 桌位 QR (/admin/tables，門市)：新增桌位 (可用 A1-A10 範圍)、改名、停用、重新產生 QR code (舊的立即失效)、
  設定 QR code 使用的門市網址、列印桌卡 (A4 每頁 6 張)。
- 整庫備份 / 還原 (只限平台管理者，只支援 SQLite)。
- 線上端：分店管理與分店權杖 (/admin/nodes)；門市：同步狀態與立即同步 (/admin/sync)。
- 換日自動結案：前一天仍在製作中的訂單於隔天自動改為已出餐。


三、快速開始 (Windows 門市 / 單機)
--------------------------------------------------------------------
1. 安裝 Python 3.10 以上 (目前以 3.13 / 3.14 測試)。
2. 建立虛擬環境並安裝套件 (專案資料夾中只保留一個虛擬環境 .venv)：
     python -m venv .venv
     .venv\Scripts\activate                        (PowerShell；Linux / macOS：source .venv/bin/activate)
     pip uninstall -y opencv-python                 (舊版安裝過才需要；本專案改用 opencv-python-headless)
     pip install -r requirements.txt
3. 設定：複製 .env.example 為 .env，至少填 ADMIN_USERNAME / ADMIN_PASSWORD (負責人帳號，只在第一次建立時使用)。
   未填會自動產生一組隨機帳密並只在啟動畫面顯示一次。
4. 啟動：
     python serve_windows.py      正式營業 (Waitress，預設 0.0.0.0:5000，區網內的看板與 POS 可連線)
     python run.py                開發除錯 (FLASK_DEBUG=1 開啟除錯模式)
   前台 http://127.0.0.1:5000/   後台 http://127.0.0.1:5000/admin
5. 啟動時會自動升級資料庫 (AUTO_MIGRATE=1)；舊版資料庫會先整理成基準結構再升級，舊的 static/member、
   static/menu 上傳檔會搬到 uploads/。升級前請先備份 database/ 與 uploads/ (或舊的 static/member、static/menu)。
6. 後台「隱私權告知」填寫營運者名稱與聯絡方式。

門市連到線上端：線上端後台「分店管理」新增分店取得權杖，在門市 .env 設定
  SYNC_ROLE=node、SYNC_HUB_URL=https://線上端網址、SYNC_NODE_TOKEN=kn_…，重新啟動即可 (DEPLOY.txt 第三節)。
線上端部署：Docker (PostgreSQL + Redis + Caddy 自動 HTTPS)，步驟見 DEPLOY.txt 第一節。

本機模擬線上端 (同一台電腦同時跑線上端與門市)：
  1. cp deploy/hub.env.example .env.hub，設定 SITE_DOMAIN=localhost、PUBLIC_BASE_URL=https://localhost，
     填 POSTGRES_PASSWORD (DATABASE_URL 裡的密碼要相同) 與 SECRET_KEY → docker compose up -d --build。
     第一次的負責人帳密在 docker compose logs app；後台 https://localhost/admin (自簽憑證，瀏覽器警告按繼續)。
  2. 線上端後台「分店管理」新增分店 (或 docker compose exec app flask --app run sync create-node --name 台南店) 取得權杖。
  3. 匯出 Caddy 的本機憑證讓門市信任：
       docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt database\caddy-root.crt
  4. 門市 .env (先備份 database\menu.db)：SYNC_ROLE=node、SYNC_HUB_URL=https://localhost、SYNC_NODE_TOKEN=kn_…、
     REQUESTS_CA_BUNDLE=<專案路徑>\database\caddy-root.crt → python serve_windows.py，後台「同步狀態」確認已連線。
  - 綠界伺服器通知連不到 localhost：付款結果改由付款後瀏覽器回到本站與每分鐘向綠界查詢來確認。
  - REQUESTS_CA_BUNDLE 只用於本機模擬 (會讓門市只信任這張憑證)；正式上線前刪除。


四、環境變數
--------------------------------------------------------------------
門市 / 單機寫在專案根目錄的 .env (範例 .env.example)；線上端寫在 .env.hub (範例 deploy/hub.env.example)。
.env 的值優先於系統環境變數。

  一般
    ADMIN_USERNAME / ADMIN_PASSWORD   預設店家負責人 (第一次建立時使用)
    RESET_ADMIN_PASSWORD_ON_START     1 = 重啟時把負責人密碼重設為 ADMIN_PASSWORD (忘記密碼時用，用完改回 0)
    SECRET_KEY                        Session 簽章金鑰；留空自動產生並存於 database/.secret_key (線上端必填)
    DATABASE_URL                      留空 = database/menu.db (SQLite)；線上端為 postgresql://…
    AUTO_MIGRATE                      1 = 啟動時升級資料庫；0 = 需先執行 flask --app run setup
    UPLOAD_FOLDER_MEMBER / _MENU      上傳檔位置 (預設 uploads/member、uploads/menu)
    HOST / PORT                       serve_windows.py、run.py 的監聽位址與埠號 (預設 0.0.0.0:5000)
    WAITRESS_THREADS                  Waitress 執行緒數 (每條 SSE 看板連線占 1 條，預設 128)
  安全
    TRUSTED_PROXY_COUNT               前面有幾層反向代理 (直接對外 0；Nginx / Cloudflare / Caddy 後面 1)
    SESSION_COOKIE_SECURE             全站 HTTPS 時 1 (門市以 http://區網IP 使用時維持 0)
    CSP_MODE                          enforce / report-only / off
    RATELIMIT_ENABLED                 1 = 啟用 IP 頻率限制
    REDIS_URL                         選用：多行程部署時共用頻率限制計數與 SSE 通知
  同步 (說明見 SYNC.txt)
    SYNC_ROLE                         standalone / node / hub
    SYNC_HUB_URL / SYNC_NODE_TOKEN    分店：線上端網址 (https) 與分店權杖
    SYNC_INTERVAL / SYNC_LONG_POLL    分店：上傳檢查間隔、等待線上新訂單的長輪詢秒數
    SYNC_TENANT_SLUG / SYNC_AUTOSTART 分店：要同步的本機店家、啟動時是否自動同步
    SYNC_NODE_OFFLINE_AFTER           線上端：分店超過幾秒沒連線就暫停該分店的線上點餐
  線上付款 (線上端；說明見 DEPLOY.txt 第五節)
    ONLINE_PAYMENT                    1 = 線上點餐需先付款 (付款後才送分店)
    ECPAY_STAGE                       1 = 綠界測試環境 (預設，免註冊、不扣款)；0 = 正式收款 (需填下面三項)
    ECPAY_MERCHANT_ID / _HASH_KEY / _HASH_IV   綠界正式特店資料
    PUBLIC_BASE_URL                   綠界付款通知用的公開網址 (留空使用目前網址)
    PAYMENT_TIMEOUT_MINUTES           逾時未付款自動取消 (預設 15 分鐘)
  選用
    GEMINI_API_KEY                    Google Gemini (AI 文案與營運建議)


五、常用指令
--------------------------------------------------------------------
  flask --app run setup                              升級資料庫並建立預設資料 (可重複執行)
  flask --app run db migrate -m "說明"                修改 app/models/ 後產生遷移檔 (檢查後再 setup)
  flask --app run sync create-node --name 台南店      (線上端) 新增分店並顯示權杖
  flask --app run sync now / sync status             (分店) 立即同步 / 顯示同步狀態
  flask --app run payments expire                    (線上端) 取消逾時未付款的訂單 (背景也會每分鐘執行)
  flask --app run privacy purge-inactive [--apply]   刪除停止使用超過設定月數的會員 (多分店時在線上端執行)
  flask --app run tables check [--fix]               檢查桌邊點餐的資料衝突 (沒有桌位的未結帳訂單、已取消卻已收款、
                                                     久未結帳)；--fix 把「已取消卻已收款」改為需退款
  python tests/test_payment_ecpay_script.py [live]   線上付款測試 (見「七、測試」)
  docker compose up -d --build                       (線上端) 部署 / 更新


六、專案結構
--------------------------------------------------------------------
Project Root/
├── app/
│   ├── __init__.py            create_app：設定、擴充元件、安全標頭 / CSP、錯誤處理、多店家、啟動遷移、背景工作
│   ├── config.py              所有設定 (讀取 .env)
│   ├── cli.py                 flask CLI 指令 (setup、sync、payments、privacy)
│   ├── extensions.py          db、migrate、csrf、login_manager、limiter
│   ├── errors.py              AppError 與統一錯誤處理
│   ├── tenant_context.py      多店家隔離 (查詢自動加店家條件、寫入防跨店)
│   ├── utils.py               role_required、platform_admin_required、輸入驗證
│   ├── models/                ORM 模型
│   │   ├── mixins.py          uuid / updated_at / sync_rev / tenant_id
│   │   ├── enums.py           訂單狀態、來源、付款方式、憑證狀態
│   │   ├── tenant.py          店家、員工帳號
│   │   ├── table.py           門市桌位 (掃碼點餐的 QR code 代碼)
│   │   ├── user.py            會員、紅利設定、兌換憑證、日誌、隱私權告知設定
│   │   ├── menu.py、order.py、coupon.py
│   │   ├── sync.py            同步序號、刪除紀錄、點數異動帳、分店、庫存快照
│   │   └── system.py          PIN 登入鎖定
│   ├── services/              商業邏輯
│   │   ├── order_service.py   結帳 (Kiosk / POS / 線上共用)、訂單狀態機、取餐號、換日結案
│   │   ├── points_service.py  會員點數 (異動帳)
│   │   ├── reward_service.py、voucher_service.py   紅利兌換
│   │   ├── payment_ecpay.py   綠界金流 (檢查碼、付款通知、逾時處理)
│   │   ├── privacy_service.py 隱私權告知、同意紀錄、撤回人臉、刪除停用會員
│   │   ├── table_service.py   桌位管理、QR code、掃碼綁定桌位、桌位結帳
│   │   ├── upload_paths.py    上傳檔路徑檢查與刪除 (限制在上傳資料夾內)
│   │   ├── cv_service.py      人臉偵測與特徵比對
│   │   ├── ai_service.py、analytics_service.py
│   │   ├── backup_service.py、excel_service.py、menu_service.py
│   │   ├── event_bus.py       SSE 訂單通知 (可選 Redis)
│   │   ├── auth_throttle.py、customer_session.py、audit.py
│   │   └── db_migrate.py、bootstrap.py、legacy_upgrade.py、baseline_schema.py   資料庫升級與預設資料
│   ├── sync/                  門市 ↔ 線上同步
│   │   ├── registry.py        同步的資料表、方向、本機欄位、自然鍵
│   │   ├── tracking.py        變更序號 (sync_rev) 與刪除紀錄
│   │   ├── engine.py          編碼、讀取變更、合併規則
│   │   ├── hub.py             線上端同步 API (/api/sync/v1)
│   │   ├── node.py            分店背景同步
│   │   ├── nodes.py           分店登記與權杖 (線上端)
│   │   └── runtime.py         目前的同步角色與相關工具
│   └── routes/                Blueprint
│       ├── customer.py        點餐頁、結帳、我的訂單、取餐分店
│       ├── auth.py            註冊 (含個資同意)、人臉 / 手機登入、訪客、登出
│       ├── rewards.py、pos.py、kitchen.py、pickup.py (含 SSE)、orders.py (訂單狀態 API)
│       ├── admin.py           後台、員工帳號、備份還原、匯出匯入
│       ├── payment.py         綠界付款頁、付款通知、付款結果
│       ├── privacy.py         隱私權告知、同意、撤回人臉、後台設定
│       ├── tables.py          桌位管理、列印桌卡、掃碼入口 /t/<代碼> (門市)
│       ├── sync_admin.py      分店管理 (線上端)、同步狀態 (分店)
│       ├── media.py           /media/menu、/media/member (會員照片需員工權限)
│       └── health.py          /healthz
├── migrations/versions/       0001 舊版基準、0002 重構、0003 同步引擎、0004 線上付款與個資同意、
│                              0005 門市桌位與掃碼點餐
├── templates/                 頁面樣板 (全部繼承 base.html)
├── static/
│   ├── css/、js/              各頁面樣式與程式 (theme.css、common.js 為共用)
│   ├── img/                   預設圖片
│   └── vendor/                Bootstrap、Bootstrap Icons、Cropper.js (離線使用)
├── uploads/                   會員照片 (member/)、餐點照片 (menu/)；不放在 static/，由 /media/ 控管讀取 [不進版控]
├── database/                  menu.db、.secret_key、還原前另存的 menu.db.bak-*、本機模擬用的 caddy-root.crt [不進版控]
├── models/                    人臉辨識模型 (YuNet、SFace)；選用的 Qwen2.5 GGUF [GGUF 不進版控]
├── tests/                     所有測試 (含可直接執行的付款測試腳本)，檔名即測試主題
├── deploy/                    線上端部署：gunicorn.conf.py、entrypoint.sh、Caddyfile、hub.env.example
├── Dockerfile、docker-compose.yml、.dockerignore
├── .env.example               門市 / 單機設定範例 (複製為 .env)
├── .env、.env.hub             門市設定、線上端設定 [不進版控]
├── .gitignore
├── README.txt、SYNC.txt、DEPLOY.txt、MANUAL_TEST.txt
├── pytest.ini                 測試設定 (預設略過 e2e、stress)
├── requirements.txt
├── test_menu.xlsx             菜單匯入範例 (後台「智慧匯入」可直接使用)
├── serve_windows.py           正式營業啟動 (Waitress)
└── run.py                     開發用啟動


七、測試
--------------------------------------------------------------------
測試一律使用暫存資料庫與暫存資料夾，不讀 .env，不會動到 database/、uploads/ 與 static/。

  pytest                              後端測試 (SQLite)
  set TEST_DATABASE_URL=postgresql://kiosk:kiosk@127.0.0.1/kiosk_test
  pytest                              同一套測試改在 PostgreSQL 上跑 (該資料庫會被清空)
  playwright install chromium         (第一次)
  pytest -m e2e                       瀏覽器端對端測試 (實際操作各頁面；JS 錯誤或違反 CSP 都會失敗)
  pytest -m stress                    真實 HTTP 壓力測試
  set TEST_SYNC_HUB_DATABASE_URL=postgresql://kiosk:kiosk@127.0.0.1/kiosk_sync_test
  pytest tests/test_sync_hub_node.py  同步測試的線上端改用 PostgreSQL
  pytest tests/test_pos_checkout.py   只跑一個主題 (有情境依賴的檔案會自動加入前置檔，開頭會列出)
  python tests/test_payment_ecpay_script.py       線上付款：模擬綠界通知走完 30 項情境 (不需網路)
  python tests/test_payment_ecpay_script.py live  線上付款：到綠界測試環境用測試卡實際付款
                                      (卡號 4311-9522-2222-2222、未來的有效期限、任意安全碼、3D 驗證碼 1234)

測試檔 (tests/，檔名即測試主題；新的測試一律放這裡並照此命名)
  啟動與升級    test_startup_defaults           預設店家 / 負責人 / 加料、SECRET_KEY、預設帳密重設規則
                test_legacy_database_upgrade    舊版資料庫升級、舊上傳資料夾搬移
  權限與安全    test_staff_login_and_roles      員工登入、帳號管理、角色權限矩陣、未登入一律擋下
                test_web_security               CSRF (全路由掃描)、安全標頭、訊息跳脫、錯誤輸入不出 500
                test_rate_limits_and_lockout    登入頻率限制、PIN 鎖定、X-Forwarded-For 信任
                test_tenants_and_isolation      新店家註冊、店家入口、跨店資料隔離
                test_backup_restore             整庫備份 / 還原、惡意備份檔、舊版備份還原
  菜單與會員    test_menu_admin                 菜單、套餐、手動售完
                test_member_accounts            會員註冊、欄位驗證、後台編輯 / 刪除與稽核
                test_member_face_and_photos     人臉特徵與登入、重複註冊、上傳圖片限制、照片權限
  點餐與訂單    test_kiosk_checkout             自助點餐結帳 (計價、加料、冪等、取餐號)
                test_coupons_and_rewards        優惠券、紅利商品與兌換憑證、紅利設定
                test_pos_checkout               POS 櫃檯結帳
                test_pos_today_orders_api       POS 當日訂單輕量 API
                test_order_status_and_cancel    訂單狀態流轉、取消回補、換日自動結案
                test_concurrency_stock_points   併發：不超賣、點數不雙花、同時結帳與取消
                test_live_updates_sse           SSE 即時更新、連線配額、看板遮蔽姓名
  報表          test_reports_and_excel          營運分析、Excel 匯出 / 匯入、儀表板與稽核
  部署與前端    test_deploy_config              /healthz、反向代理標頭、部署設定檔
                test_frontend_static_files      前端靜態檢查 (CSP、外部網址、XSS)
  同步          test_sync_hub_node              一個線上端 + 兩家分店的完整同步情境
                test_sync_admin_pages           分店管理、同步狀態、線上點餐選分店
                test_sync_security              分店權杖外流時的限制 (負責人帳號、路徑、跨店、上傳上限)
                test_sync_invariants            全部測試跑完後檢查同步不變量
  付款與個資    test_payment_ecpay              綠界檢查碼、付款通知、逾時取消
                test_payment_ecpay_script       付款測試腳本 (pytest 跑模擬模式，也可直接執行)
                test_privacy_pdpa               隱私權告知、同意紀錄、撤回人臉、清除久未使用會員
  桌位點餐      test_table_qr_ordering          桌位管理、掃碼點餐、失效 QR code、POS 桌位結帳
                test_table_concurrency          同時操作的衝突 (多台 POS 結同一桌、結帳與取消、送單與刪桌、同名桌位、
                                                換 QR code、線上端取消與分店結帳)；輪數 TABLE_CONFLICT_ROUNDS
  瀏覽器        test_e2e_*                      端對端 (後台、顧客點餐、廚房取餐、會員、POS、所有頁面)
  壓力          test_stress_http                真實 HTTP 壓力測試

測試共用同一個暫存資料庫，有先後依賴 (例如 POS 測試使用菜單測試建立的餐點)：
執行順序寫在 tests/conftest.py 的 RUN_ORDER，前置依賴寫在 NEEDS (新增測試檔時一併維護)。


八、開發慣例
--------------------------------------------------------------------
- 換行格式：專案文字檔統一使用 Windows 換行 (CRLF)。
- 前端 (CSP 強制，tests/test_frontend_static_files.py 自動檢查)：
  - 不寫行內 <script> 或 onclick；事件用 data-action="名稱" 搭配 Kiosk.on(root, 'click', {名稱: fn})。
  - 頁面資料以 <script type="application/json" id="page-data"> 嵌入，用 Kiosk.pageData() 讀取。
  - 不引用外部網址，第三方套件放 static/vendor/。
  - 動態 HTML 一律用 Kiosk.html`...` (自動跳脫) 或 textContent。
  - API 用 Kiosk.api(url, {json})：自動帶 CSRF Token，失敗時丟出含後端訊息的錯誤。
  - 刪除確認用 <form data-confirm="…">；圖片備援用 <img data-fallback="預設圖網址">。
- 後端：
  - 錯誤一律 raise AppError(訊息, 狀態碼)：API 回 {"success": false, "message": …}；表單的 400/409 flash 後導回。
  - 權限用 @role_required('owner', 'manager')；整庫備份等用 @platform_admin_required。
  - 查詢自動加上目前店家條件；系統工作需要跨店時用 with tenant_bypass():。
  - 結帳一律走 order_service.process_checkout，訂單狀態一律走 order_service.change_order_status。
  - 加減會員點數一律用 points_service.change_points (寫入點數異動帳)。
  - 修改資料表：改 app/models/ → flask --app run db migrate → 檢查遷移檔 → 測試。
  - 新資料表要同步：繼承 TenantScoped 並加入 app/sync/registry.py 的 SPECS (依外鍵順序)；本機欄位放 local_only。


九、權限與安全
--------------------------------------------------------------------
1. 角色：
   - owner (負責人)：全部權限，含員工帳號。預設店家 (default) 的 owner 為平台管理者，才可整庫備份 / 還原。
   - manager (店長)：後台、菜單 / 優惠券 / 紅利、隱私權告知設定、匯出匯入、POS、出餐、核銷、取消訂單。
   - staff (櫃台)：POS、會員設定 PIN、取餐核銷、當日訂單、出餐、取消未取餐的訂單。登入後直接進 POS (看不到後台)。
   - kitchen (廚房)：KDS，只能出餐。登入後直接進 KDS，看不到會員照片。
2. CSRF：所有表單與 API 的 POST 都需要 CSRF Token；例外只有以權杖驗證的同步 API 與以檢查碼驗證的綠界通知。
   登出、訪客登入只接受 POST (其他網站無法用連結或圖片讓人登出)。
3. 會員照片：只能經 /media/member/ 讀取，需登入同店員工 (廚房除外)；/static/member 一律 404。
   上傳檔路徑一律檢查格式並限制在上傳資料夾內 (app/services/upload_paths.py)，/media/menu 只提供圖片。
4. 頻率限制：登入、註冊、優惠碼、送單有 IP 頻率限制；員工登入與註冊新店家只以 IP 計數 (切換店家繞不過)。
   反向代理之後請設 TRUSTED_PROXY_COUNT=1。
5. 鎖定：
   - 會員 PIN：同裝置對同手機連錯 5 次鎖 15 分鐘；同手機累計錯 10 次鎖定 (每次加倍，上限 24 小時)；同 IP 累計錯 30 次鎖定。
   - 員工密碼：同 IP 對同帳號連錯 5 次、同帳號累計錯 10 次 → 鎖 15 分鐘。
   - 帳號 / 手機不存在時同樣計數且回應時間相同，無法藉此列舉帳號。
   - 新設定的 PIN 不可太簡單 (0000、1234、1212、手機末幾碼)。
6. 工作階段：登入時換新的工作階段；員工改密碼或角色後，舊的登入自動失效。
7. 安全標頭：CSP (script-src 'self')、X-Content-Type-Options、X-Frame-Options、Referrer-Policy、
   Permissions-Policy；HTTPS 時加 HSTS。付款頁只放寬 form-action 到綠界網域。
8. 個資：註冊須同意隱私權告知並記錄版本與時間；人臉另行同意、可撤回 (各分店的照片經同步一併刪除)；
   停用會員可定期刪除 (訂單去識別化保留)。
9. 同步：HTTPS + 分店權杖 (只存雜湊)。分店只能修改自己負責的訂單、不能建立 / 修改 / 刪除負責人帳號、
   不能讀寫其他店家的圖片或資料；單次上傳有筆數上限 (詳見 SYNC.txt)。
10. 線上付款：已付款的訂單被取消 (店員、分店或逾時後才付款) 一律標記「需退款」，由店家到綠界後台退刷。
11. 整庫備份 / 還原：只限平台管理者；大檔案上限只對已登入的平台管理者放寬；含觸發程序或檢視表的備份檔拒絕還原。
12. 匯入：Excel 解壓後超過 30MB 或超過 2 萬列拒絕 (防壓縮炸彈)；匯出時公式一律轉成文字。
13. 開發模式：FLASK_DEBUG=1 只監聽本機 (除錯頁可執行程式碼)。

十、API 摘要
--------------------------------------------------------------------
訂單狀態  POST /api/orders/<id>/status   {"status": "Completed" | "PickedUp" | "Cancelled"}
  流轉：Pending → Completed → PickedUp；非終態都可轉為 Cancelled (終態)。
  權限：             owner  manager  staff  kitchen
    Pending→Completed  O      O       O      O
    Pending→Cancelled  O      O       O      X
    Completed→PickedUp O      O       O      X
    Completed→Cancelled O     O       O      X
    PickedUp→Cancelled O      O       X      X
  回應：200 成功、400 狀態值錯誤、401 未登入、403 權限不足 (先於訂單是否存在判斷)、404 查無訂單、409 非法轉換或已被變更。
  取消會在同一交易內回補庫存、退還點數、還原會員券、扣回贈點，只執行一次。

結帳  POST /submit_order (點餐頁)、POST /api/pos/checkout (POS)
  {"client_order_id": "<UUID，重送沿用>",
   "items": [{"menu_item_id": 1, "quantity": 2, "modifier_ids": [3], "combo_option_id": null,
              "is_add_on": false, "is_reward": false, "customization": "備註"}],
   "payment_method": "Cash", "order_type": "內用", "promo_code": "", "use_points": 0, "note": ""}
  (POS 另有 user_id、cash_received、table_number)
  價格一律由後端計算；replayed: true 表示是重送的同一張訂單；線上端另回傳 payment_url (前往付款)。

其他
  GET  /api/pos/today_orders       POS 當日訂單 (含未結帳桌位)   GET /api/kitchen_orders、/api/pickup_orders、/api/counter_orders
  POST /api/pos/tables/<id>/settle  桌位結帳 {"payment_method": "Cash", "cash_received": 600}
  GET  /api/orders_stream          SSE 訂單通知          GET /healthz   健康檢查 (資料庫連線)
  /pay/<訂單>、/payment/ecpay/notify、/payment/ecpay/result   綠界付款 (線上端)
  /api/sync/v1/hello、push、pull、media/menu/<路徑>          同步 API (分店權杖，詳見 SYNC.txt)


十一、從舊版升級
--------------------------------------------------------------------
1. 先備份 database/menu.db 與舊的 static/member、static/menu。
2. pip uninstall -y opencv-python 後 pip install -r requirements.txt (新增 Flask-Migrate、Flask-WTF、Flask-Login、
   Flask-Limiter、psycopg、opencv-python-headless、segno (桌位 QR code) 等)。
3. 第一次啟動自動完成：升級資料庫 (0001 → 0005)、舊上傳檔搬到 uploads/、會員現有點數記為「期初」點數異動。
4. 舊檔案已不使用：app/routes/common.py、app/services/idempotency.py、app/services/migration_service.py、test_all_features.py。
5. 環境變數 DISABLE_RATE_LIMIT 改為 RATELIMIT_ENABLED (0 = 停用)；門市不需要 Redis (只有線上端多行程部署使用)。
6. 升級前註冊的會員沒有個資同意紀錄：登入後點餐頁上方會請他閱讀並同意；原有人臉資料仍可使用，可在 /privacy 刪除。
