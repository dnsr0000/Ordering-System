====================================================================
智慧自助點餐、POS 收銀、人臉辨識與多店家營運系統 (Kiosk & POS)
====================================================================

一套給單店與連鎖餐飲使用的門市系統：顧客自助點餐機、櫃台 POS、廚房出單看板、取餐叫號、數位看板、
店家後台，以及總部的線上點餐網站。門市斷網照常營業，連線後與線上端雙向同步。

相關文件
- README.txt       (本檔) 功能、安裝、設定、指令、後台頁面、測試、開發慣例、安全、升級紀錄
- SYNC.txt         門市 ↔ 線上端同步的設計與合併規則
- DEPLOY.txt       線上端 Docker 部署、門市 Windows 部署、同步設定、線上付款、外部服務、上線前檢查清單
- MANUAL_TEST.txt  人工實測步驟 (全功能，每項含步驟與預期結果)

目錄
  一、概述：系統角色與資料分工
  二、功能總覽 (顧客端 / 門市員工)
  三、安裝與啟動 (3.1 快速開始、3.2 套件清單、3.3 人臉辨識模型、3.4 環境變數、3.5 常用指令)
  四、日常操作與後台頁面 (4.1 版面與首頁、4.2 菜單與套餐、4.3 會員、4.4 其他常用頁面)
  五、進階功能 (A 金流與法規、B 收銀營運、C 廚房出餐與顧客通知、D 菜單與原物料、E 會員行銷、
      F 通路與外送、G 報表、人員與總部、H 系統維運與整合、I AI / 語音 / 多語系)
  六、多店與線上端 (指引)
  七、測試
  八、專案結構與開發慣例
  九、權限與安全
  十、API 摘要
  十一、升級紀錄與從舊版升級


一、概述：系統角色與資料分工
--------------------------------------------------------------------
環境變數 SYNC_ROLE 決定這台主機的角色：
  standalone (預設)  單機門市：SQLite，不同步。
  node               門市分店：SQLite，可完全離線營業；背景與線上端同步。
  hub                線上端：PostgreSQL + Docker；線上點餐網站與總部後台，所有分店資料的匯集點。

    [門市 A (node)]──┐  HTTPS + 分店權杖
    [門市 B (node)]──┼──── [線上端 (hub)] ──── 顧客手機 (線上點餐、綠界付款)
    [門市 C (node)]──┘

  - 菜單、優惠券、紅利設定、員工帳號、會員與點數：同一店家的所有分店共用 (雙向同步)。
  - 訂單：分店自己的訂單上傳到線上端；線上訂單付款後只送到顧客選的取餐分店。
  - 庫存：各分店獨立；線上端用分店回報的庫存快照接受線上訂單。
  同步資料表、合併規則與限制見 SYNC.txt；部署與同步設定步驟見 DEPLOY.txt。


二、功能總覽
--------------------------------------------------------------------
顧客
- 自助點餐 (/)：菜單分類與搜尋、套餐 (配餐 / 任選)、客製化、規格、條件加購、優惠券、紅利折抵、預估等候時間、
  我的訂單與「再點一次」、售完即時更新、飲食篩選、待機輪播、大字 / 高對比、淺色 / 深色、PWA、語音點餐、
  AI 客服對話窗、禮物卡付款 (需開放)。
- 會員：手機 + PIN 登入；門市另可人臉登入 (YuNet + SFace，選填並須另行同意；需安裝 requirements-store.txt 的套件)；
  LINE 登入 / 綁定；紅利商城 (/rewards)：兌換優惠券與餐點、會員動態條碼、我的寄杯、集點卡、推薦碼、我的資料
  (下載 / 刪除帳號)、優惠訊息開關。
- 訪客點餐：門市 (點餐機或顧客用手機連門市網站) 可以；線上點餐只限會員。
- 桌邊掃碼點餐 (門市)：顧客手機連店內 Wi-Fi 掃桌上的 QR code (/t/<代碼>)，以訪客或會員身分點餐；固定內用、
  訂單標記桌號並直接送廚房，用餐後到櫃檯結帳 (或開放「桌邊線上結帳」以綠界付清)；桌邊呼叫服務。
- 線上點餐 (線上端)：選取餐分店 → 綠界付款 → 付款後訂單送到分店廚房；取餐號由分店收單時編號；可預約取餐時間、
  可選外送到府。
- 出餐進度頁 (/track/<訂單 uuid>，收據 QR code)、線上訂位 (/reserve) 與候位 (/waitlist)、團體預購、隱私權告知
  (/privacy：個資法第 8 條告知事項；會員可在此撤回人臉同意並刪除人臉資料、解除 LINE 綁定)。

門市員工
- POS (/pos)：觸控點餐、掛單 (F4)、結帳 (F2)、會員查詢 / 綁定 / 設定 PIN、找零、80mm 熱感應收據、當日訂單與補印、
  桌位結帳、桌號 / 顧客稱呼快捷鍵 (桌位管理的桌號 + 收銀規則設定的稱呼)、常用備註快捷鍵、條碼 / 掃描槍、交班、
  儲值、禮物卡、寄杯、外送單、作廢 / 退款 / 單品作廢、加急、開錢櫃、客顯、呼叫服務、接單控制。
- 廚房 KDS (/kitchen，可分工作站)、取餐核銷 (/pickup)、叫號看板 (/counter，公開、會員姓名遮罩、語音叫號)：SSE 即時更新。
- 數位看板 (/signage，公開)：門市大螢幕的菜單，售完 / 供應時段 / 特價即時同步，分類放不下時自動拆欄換頁；
  底部輪播促銷標語 (後台「數位看板」設定，空白時用進行中的促銷活動) 與取餐 / 候位叫號。
- 員工打卡 (/timeclock)、可排時段、訂位 / 候位看板 (/reservations)、外送派車 (/admin/dispatch)、外送員手機頁 (/driver/<憑證>)。
店家後台 (/admin)：見「四」；進階功能見「五」。


三、安裝與啟動
--------------------------------------------------------------------
3.1 快速開始 (Windows 門市 / 單機)
1. 安裝 Python 3.10 以上 (目前以 3.13 / 3.14 測試)。
2. 建立虛擬環境並安裝套件 (專案資料夾中只保留一個虛擬環境 .venv)：
     python -m venv .venv
     .venv\Scripts\activate                        (PowerShell；Linux / macOS：source .venv/bin/activate)
     pip uninstall -y opencv-python                 (舊版安裝過才需要；本專案改用 opencv-python-headless)
     pip install -r requirements-store.txt          (門市：核心 + 人臉辨識；開發 / 測試改用 requirements-dev.txt)
3. 設定：複製 .env.example 為 .env，至少填 ADMIN_USERNAME / ADMIN_PASSWORD (負責人帳號，只在第一次建立時使用)。
   未填會自動產生一組隨機帳密並只在啟動畫面顯示一次。
4. 啟動：python serve_windows.py (正式營業，Waitress，預設 0.0.0.0:5000，區網內的看板與 POS 可連線)；
   python run.py (開發除錯，FLASK_DEBUG=1 開啟除錯模式)。前台 http://127.0.0.1:5000/、後台 http://127.0.0.1:5000/admin。
5. 啟動時會自動升級資料庫 (AUTO_MIGRATE=1)；舊版資料庫會先整理成基準結構再升級，舊的 static/member、static/menu
   上傳檔會搬到 uploads/。升級前請先備份 database/ 與 uploads/ (或舊的 static/member、static/menu)。
   第一次啟動會在背景下載人臉模型檔 (3.3)。
6. 後台「隱私權告知」填寫營運者名稱與聯絡方式。

門市連到線上端：線上端後台「分店管理」新增分店取得權杖，在門市 .env 設定 SYNC_ROLE=node、SYNC_HUB_URL=https://線上端網址、
  SYNC_NODE_TOKEN=kn_…，重新啟動即可 (DEPLOY.txt 第三節)。線上端部署 (Docker：PostgreSQL + Redis + Caddy 自動 HTTPS)
  見 DEPLOY.txt 第一節。
本機模擬線上端 (同一台電腦同時跑線上端與門市)：
  1. cp deploy/hub.env.example .env.hub，設定 SITE_DOMAIN=localhost、PUBLIC_BASE_URL=https://localhost，填 POSTGRES_PASSWORD
     (DATABASE_URL 裡的密碼要相同) 與 SECRET_KEY → docker compose up -d --build。第一次的負責人帳密在
     docker compose logs app；後台 https://localhost/admin (自簽憑證，瀏覽器警告按繼續)。
  2. 線上端後台「分店管理」新增分店 (或 docker compose exec app flask --app run sync create-node --name 台南店) 取得權杖。
  3. 匯出 Caddy 的本機憑證讓門市信任：
       docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt database\caddy-root.crt
  4. 門市 .env (先備份 database\menu.db)：SYNC_ROLE=node、SYNC_HUB_URL=https://localhost、SYNC_NODE_TOKEN=kn_…、
     REQUESTS_CA_BUNDLE=<專案路徑>\database\caddy-root.crt → python serve_windows.py，後台「同步狀態」確認已連線。
  - 綠界伺服器通知連不到 localhost：付款結果改由付款後瀏覽器回到本站與每分鐘向綠界查詢來確認。
  - REQUESTS_CA_BUNDLE 只用於本機模擬 (會讓門市只信任這張憑證)；正式上線前刪除。

3.2 套件清單 (三層，後者包含前者)
  requirements.txt        核心執行套件 (Flask、SQLAlchemy、waitress / gunicorn、openpyxl、Pillow、segno、cryptography、
                          py-vapid、psycopg、redis…)：線上端 Docker 映像只裝這個檔案，因此線上端沒有人臉辨識、沒有測試工具。
  requirements-store.txt  -r requirements.txt + numpy + opencv-python-headless：門市 (人臉辨識登入)；
                          deploy/windows/update_store.cmd (門市一鍵更新) 也安裝這個檔案。
  requirements-dev.txt    -r requirements-store.txt + pytest、pytest-cov、playwright、axe-playwright-python：開發、測試與 CI。
  Excel 匯出 / 匯入直接用 openpyxl (不再依賴 pandas)，只支援 .xlsx 與 .csv；舊版 .xls 會被拒絕並提示先另存為 .xlsx。
  選用：sentry-sdk[flask] (錯誤回報)、llama-cpp-python (本機 Qwen，見五-I)。

3.3 人臉辨識模型 (models/*.onnx)
  - 人臉辨識是選用功能：沒有安裝 opencv / numpy 時 (例如線上端 Docker) 程式照常啟動，只是人臉註冊 / 登入不可用；
    系統監控頁 (/admin/system) 的「人臉辨識登入」方塊顯示可用 / 停用與原因。
  - 模型檔 (YuNet、SFace，約 37 MB) 不進版控：伺服器第一次啟動時在背景從 OpenCV Zoo 自動下載到 models/ 並比對 SHA-256
    (不符不保留)，也可手動 flask --app run models download [--force]。離線主機請先在有網路的電腦下載後把 models/ 複製過去。

3.4 環境變數
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
    ALLOW_TENANT_REGISTER             1 = 開放公開的「建立新店家」頁 (/tenant/register)。線上端預設 1；門市主機預設 0
                                      (任何連得到主機的人都能建店並取得負責人權限)，平台管理者登入後仍可建立新店
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
    ECPAY_ALLOW_TEST_KEYS             1 = 允許以綠界「公開測試金鑰」收款 (測試環境用)。測試金鑰是公開的，任何人都能
                                      偽造付款通知，因此未設定時付款頁與通知一律拒絕 (線上付款 / 桌邊結帳 / 訂位訂金)
    PUBLIC_BASE_URL                   綠界付款通知用的公開網址 (留空使用目前網址)；門市使用桌邊線上結帳 / 訂位訂金 /
                                      外送員 GPS 回報時門市主機也要設定 (對外 HTTPS)，訂位提醒簡訊的取消連結也用這個網址
    PAYMENT_TIMEOUT_MINUTES           逾時未付款自動取消 (預設 15 分鐘)
  Webhook (說明見 app/services/webhook_service.py)
    WEBHOOK_ALLOW_PRIVATE             1 = 允許送到內網 / 本機位址 (只限自架內網整合；預設只送公開網際網路位址；
                                      監控通知的 Webhook 也依此檢查)
    WEBHOOK_ALLOW_HTTP                1 = 允許 http:// 網址 (預設只允許 https)
  記錄檔 (app/services/log_service.py)
    LOG_FILE                          輪替記錄檔位置 (預設 database/logs/app.log；設為空字串只輸出主控台，Docker 用)
    LOG_MAX_BYTES / LOG_BACKUP_COUNT  每個記錄檔大小 (預設 10 MB) 與保留個數 (預設 5)
  選用
    GEMINI_API_KEY                    Google Gemini (AI 文案與營運建議、一鍵翻譯菜單、語音點餐 / 客服的 AI 備援)
    GEMINI_MODEL / GEMINI_FALLBACK_MODEL   Gemini 主要模型與備援模型 (主要模型 503 / 逾時重試一次後改用備援)
    SENTRY_DSN                        設定後 (需 pip install sentry-sdk) 未預期錯誤送到 Sentry；
                                      SENTRY_TRACES_SAMPLE_RATE 效能取樣 (預設 0)
    METRICS_TOKEN                     設定後 GET /metrics?token=… 回 Prometheus 指標 (未設定 404)
    UPDATE_REPO                       版本更新來源 GitHub owner/repo (預設 dnsr0000/Ordering-System)

3.5 常用指令
  flask --app run setup                              升級資料庫並建立預設資料 (可重複執行)
  flask --app run db migrate -m "說明"                修改 app/models/ 後產生遷移檔 (檢查後再 setup)
  flask --app run models download [--force]          下載 (或重新下載) 人臉辨識模型檔到 models/ (啟動時也會自動補下載)
  flask --app run sync create-node --name 台南店      (線上端) 新增分店並顯示權杖
  flask --app run sync now / sync status             (分店) 立即同步 / 顯示同步狀態
  flask --app run payments expire                    (線上端) 取消逾時未付款的訂單 (背景也會每分鐘執行)
  flask --app run privacy purge-inactive [--apply]   刪除停止使用超過設定月數的會員 (多分店時在線上端執行)
  flask --app run tables check [--fix]               檢查桌邊點餐的資料衝突 (沒有桌位的未結帳訂單、已取消卻已收款、
                                                     久未結帳)；--fix 把「已取消卻已收款」改為需退款
  flask --app run ops check-integrations            檢查後台填寫的外部服務 (電子發票、LINE Pay、LINE、簡訊、SMTP、外送平台)
  flask --app run ops retry-invoices                 立即重送待開立的電子發票 (背景每 5 分鐘也會執行)
  flask --app run ops backup [--keep-days 14]        立即備份門市資料庫到 database/backups/
  flask --app run ops daily                          立即執行每日工作 (自動備份、生日禮、定期報表；已執行過的當天不重複)
  flask --app run demo seed [--days 60] [--orders-per-day 80] [--password ...] [--reset] [--slug demo]
                                                     建立獨立的「示範店家」(/shop/demo) 與過去 N 天的訂單、會員、成本、費用、
                                                     打卡、團購等資料 (尖峰時段分布、假日較多、多種付款方式、常客 / 久未消費、
                                                     少量作廢、評分、每月費用、進貨成本變動、收單中的團購)，所有報表都有內容；
                                                     帳號 demo_owner / demo_manager / demo_pos / demo_pos2 / demo_kitchen
                                                     (密碼顯示在畫面)、示範會員手機 0900000001 起、PIN 2468；不會動到其他店家，
                                                     同代號的真實店家一律拒絕；只能在單機執行 (分店主機 / 線上端拒絕)
  flask --app run demo remove [--yes]                刪除示範店家與它的所有資料 (只刪由 demo seed 建立的店家)
  python tests/test_payment_ecpay_script.py [live]   線上付款測試 (見「七、測試」)
  docker compose up -d --build                       (線上端) 部署 / 更新


四、日常操作與後台頁面
--------------------------------------------------------------------
4.1 版面與首頁
- 所有後台頁共用左側選單 (手機收合成漢堡選單) 與頂欄 (店名、時鐘、登入者、點餐前台、登出)，分七組；項目依角色 /
  頁面權限 / 主機角色 (單機、總部、分店) 自動隱藏，目前頁面以橘色標示。定義在 app/services/admin_nav.py
  (新增後台頁面時加一行即可)：
    今日營運    總覽、實時訂單、即時看板、櫃檯 POS、廚房 KDS、取餐核銷、叫號看板、數位看板
    菜單        菜單與套餐、菜單工程分析、原物料與配方、供應商與採購、備料預測、分店叫貨 / 調撥
    顧客與行銷  會員、回饋商城與優惠券、促銷活動、集點 / 寄杯、禮物卡、分群與自動行銷、顧客評價
    通路        桌位管理 (門市)、外送平台、外送派車、訂位 / 候位、團體預購
    人員        員工帳號 (負責人)、打卡與排班、可排時段、排班建議、自動排班、交班與日結
    報表        營運報表、損益表、營業費用、成本 / 售價歷史、會計匯出、電子發票、作廢與退款、員工異常偵測、稽核紀錄
    系統        系統設定、分店管理 (線上端)、分店價格 / 上下架 (線上端)、同步狀態 (分店)、印表機與工作站、裝置管理、
                API 與 Webhook、系統監控與備份、舊資料封存、隱私權告知、帳號安全、店家管理 / 建立新店家 (平台管理者)
- 後台首頁 (/admin) 三個分頁：
  - 總覽：今日訂單 / 營收 / 待製作 / 客單價 / 預估等候 / 已出餐、平均每單時間、熱門餐點排行 (可調排行筆數)、
    智慧營運建議 (AI)、待處理事項 (逾時未出餐、未結帳桌、外送進行中、低庫存、發票 / 列印 / 退款異常、今日訂位)、
    常用功能捷徑；每 10 秒與訂單變動時即時更新。
  - 實時訂單看板：今日訂單與狀態操作 (出餐 / 取餐 / 取消)。
  - 回饋商城與優惠券：紅利折抵設定、優惠券 (新增 / 編輯 / 批量發放) 與兌換餐點清單 (上架；「修改點數」開同一個
    對話框並預填)。
  舊網址 /admin?tab=menu | users | audit 分別導向 /admin/menu、/admin/members、/admin/audit (?tab=analytics 顯示總覽)。
- 頂欄「匯出資料 / 匯入資料」：Excel 匯出 (勾選資料表)、智慧匯入 (.xlsx / .csv；範例 test_menu.xlsx)、整庫備份 / 還原
  (只限平台管理者、只支援 SQLite)。
- 系統設定 (/admin/settings)：左右兩欄，左欄是 A～H 群組與區塊清單 (含搜尋，支援同義詞例如「綠界」找到電子發票，
  Enter 直接開第一個符合的區塊)，右欄是卡片或表單；同一群組內再以子群組歸類 (收款、顧客通知、財務…)。外部服務的
  金鑰一律在這裡由負責人填寫，已存的金鑰不回顯、未填寫的功能自動停用；每個設定頁有「測試連線」。匯出 / 匯入 JSON 見五-H。

4.2 菜單與套餐 (/admin/menu，舊網址 /admin/menu_pro 仍可用；templates/admin_menu.html、static/js/admin-menu.js)
- 一份品項清單 (上方篩選框依品名 / 分類過濾)：每列「編輯」(基本資料對話框：名稱、分類、價格、特價、庫存、客製化、
  加購條件、圖片裁切、AI 生成文案)、「進階」(展開該列：供應時段 / 星期 / 販售期間、內用 / 外帶 / 線上 / 外送通路價與
  會員價、過敏原 / 素食 / 辣度、規格「中杯:0, 大杯:15」、英文 / 日文翻譯、外送平台 / 條碼代碼、單位成本、製作時間、
  熱量與營養文字、碳足跡 (依配方或手動)、廚房製作卡)、「刪除」；手動售完。
- 套餐清單 (主餐 + 加價 + 配餐，可客製化；「任選群組」每行「群組: 餐點, 餐點+加價」)、分類翻譯、「AI 一鍵翻譯菜單」、
  「匯入 Excel」/「匯出」按鈕。

4.3 會員 (/admin/members，舊網址 /admin/members_pro 仍可用；templates/admin_members.html、static/js/admin-members.js)
- 名冊：分頁 (每頁 50 位)、搜尋姓名或手機 (?q=)、排序 (最新加入 / 累積消費)；每列「編輯」(姓名、手機、點數、PIN)、
  「進階」(生日、等級、解除 LINE、儲值金調整)、「刪除」、「票券」(持券視窗：未使用 / 近 7 天已使用與過期，開啟時才載入)。
- 同頁另有會員等級設定、LINE 推播 (群發)、生日禮 (立即發送)、最近儲值金異動。

4.4 其他常用頁面
- 員工帳號 (/admin/staff，負責人)：建立 / 停用 / 重設密碼、角色、額外權限 (折扣 / 退款 / 開班 / 報表 / 原物料 / 訂位 /
  儲值 / 外送登錄 / 免驗證會員，後台頁面權限 menu / coupons / members / feedback / settings)、時薪、每週工時上限、
  強制登出 / 重設 2FA。
- 桌位管理 (/admin/tables，門市)：新增桌位 (可用 A1-A10 範圍)、改名、停用、重新產生 QR code (舊的立即失效)、設定
  QR code 使用的門市網址、列印桌卡 (A4 每頁 6 張)、平面圖 (座位數 / 區域 / 形狀、拖拉擺放)。
- 隱私權告知 (/admin/privacy)、帳號安全 (/admin/security)、稽核紀錄 (/admin/audit)、系統監控與備份 (/admin/system：
  健康狀態、AI 引擎與人臉辨識狀態、錯誤事件、每日備份清單 / 立即備份 / 下載、版本更新)。
  線上端：分店管理 (/admin/nodes)；門市：同步狀態 (/admin/sync)。
- 換日自動結案：前一天仍在製作中的訂單於隔天自動改為已出餐 (預約明天以後的訂單除外)。


五、進階功能
--------------------------------------------------------------------
A 金流與法規
- 電子發票 (綠界 B2C)：結帳自動開立 (手機條碼 / 自然人憑證 / 捐贈 / 統編 / 紙本)、失敗自動重送、取消自動作廢
  (跨期改折讓)、部分退款開折讓、後台列表 / 重送 / 修改載具 / 列印證明聯 (/admin/invoices)。外送平台訂單與儲值金
  消費不重複開立。
- 門市電子支付：LINE Pay 掃顧客付款碼 (Offline API，扣款失敗自動取消訂單)；信用卡刷卡機輸入授權碼 / 末四碼記帳。
- 線上付款方式：信用卡 / Apple Pay / ATM 虛擬帳號 / 超商代碼在「系統設定 → 付款方式」勾選；ATM、超商代碼依繳費期限
  保留訂單 (會員訂單頁顯示帳號 / 代碼)；取消已付款的線上信用卡訂單可自動向綠界退刷 (失敗時後台「綠界退刷」重試)。
- 作廢 / 部分退款：沒有「作廢 / 退款」權限的員工需主管輸入帳密核准；LINE Pay 自動退款、其他方式登記人工退款
  (/admin/refunds)；儲值金 / 禮物卡自動退回。單品作廢 (POS 當日訂單「作廢/退款」→ 作廢品項；桌邊未結帳的訂單「改單」)：
  作廢某品項的部分份數，回補庫存與原料，依比例分攤折扣後部分退款 (未結帳則直接降低應付)；廚房看板顯示作廢份數；會同步。
- 混合付款 (POS 付款方式「混合付款」)：一筆訂單分現金 / 刷卡 / 儲值金 / 禮物卡 / 行動支付，每種各記一筆收款
  (交班報表依實際付款方式統計)；作廢時每種方式各自退回，部分退款可指定退回哪一種。
- 錢櫃：收據印表機勾選「接錢櫃」後，櫃檯收 / 退現金 (結帳、桌位結帳、儲值、零用金、退款) 自動開錢櫃；
  POS「開錢櫃」按鈕手動開啟並留稽核紀錄。
- 禮物卡 (系統設定 → 禮物卡；後台 /admin/gift_cards)：POS 售出 16 碼禮物卡 (可列印卡號)、查詢餘額、結帳以「禮物卡」
  扣款、作廢 / 退款退回卡片；各分店通用；售出時記為預收款不開發票，兌換消費時才開立。點餐機也可開放禮物卡付款
  (POST /api/gift_cards/balance 查餘額，只回遮罩卡號、20 次 / 分鐘；餘額不足 409 insufficient_gift，請至櫃檯混合付款)。
- 會計匯出 (系統設定 → 會計匯出科目)：依收款紀錄產生每日傳票 (借：現金 / 信用卡 / 行動支付 / 預收款項…，貸：銷貨收入
  + 銷項稅額；退款、儲值、禮物卡、零用金另有分錄，借貸平衡) 與發票明細，下載 CSV (UTF-8 BOM) / Excel 匯入會計軟體；
  科目代號可設定。

B 收銀營運
- 交班 / 日結 (/admin/shifts)：開班零用金、存入 / 支出、依面額盤點、應有現金與帳差、交班報表列印；可要求必須先開班才能結帳。
- 服務費、整單折扣與改價 (超過門檻需主管核准)、員工個別權限 (/admin/staff)、常用備註與桌號 / 稱呼快捷鍵 (收銀規則)。
- 桌位結帳：分開結帳 / 多種付款、換桌 / 併桌、整桌開一張發票；「平面圖」即時顯示空桌 / 用餐中 (用餐分鐘、未結帳金額) /
  呼叫中 / 即將訂位，點用餐中的桌跳到該桌帳單。
- 桌邊線上結帳 (系統設定 → 桌邊線上結帳)：掃碼點餐的顧客按「線上結帳」查看本桌帳單，以綠界信用卡付清後自動完成桌位
  結帳；付款期間已在櫃檯結帳或訂單有異動 → 標記需退款並通知店家。
- 桌邊呼叫服務：掃碼點餐頁「加水 / 想結帳 / 呼叫服務」，POS「呼叫」按鈕顯示待處理並提示音，按「已處理」。
- 客顯螢幕：POS「客顯」開啟第二個視窗 (/pos/display，拖到面向顧客的螢幕)，即時顯示品項、應付金額、收款與找零。
- 會員動態條碼：會員在紅利商城開啟「會員條碼」(每分鐘更新、截圖無效)，櫃檯以掃碼槍掃描即帶出會員。
- POS 條碼：菜單「進階」的「外送平台 / 條碼代碼」(external_code) 就是商品條碼；POS 搜尋框輸入代碼 + Enter 或掃描槍
  (焦點不在輸入框時連續輸入 + Enter) 直接加入購物車 (有套餐 / 客製化 / 規格的照常跳選擇視窗)；搜尋只剩一個商品時
  Enter 也直接加入。
- POS 動用儲值金 / 點數 / 寄杯預設需驗證會員身分 (掃會員動態條碼或會員輸入 PIN，否則店長核准，稽核 MEMBER_VERIFY_OVERRIDE；
  15 分鐘內同一會員不必再輸入；「系統設定 → 會員等級 / 生日禮 / 儲值金」可關閉)。
- 網路熱感印表機 (ESC/POS TCP 9100)：收據、各工作站廚房單、補印、離線佇列重試 (/admin/printers)。
- 接單控制 (系統設定 → 接單控制；POS 右上角「接單」一鍵切換)：暫停接單 (15 / 30 / 60 分鐘後自動恢復或手動恢復) 後
  點餐機、顧客手機 (桌邊掃碼 / 線上) 與團購送單一律被擋 (409 intake_paused，點餐頁顯示橫幅)；POS 櫃檯、外送平台訂單、
  斷線暫存補送與排程送出的團體預購不受影響。每時段訂單上限：以設定的時段長度為單位，同一時段 (盡快 = 下單時間；
  預約取餐 = 取餐時間) 的點餐機 / 手機 / 線上訂單達上限即拒絕 (409 intake_full)。分店的暫停狀態隨同步上傳到線上端
  (SyncNode.paused_until)，線上點餐選到暫停中的分店時一併擋下；稽核 INTAKE_PAUSE / INTAKE_RESUME。
- 點餐機斷線暫存：點餐機送出訂單時連不到門市主機，現金 (櫃檯付款) 且未用點數 / 優惠券 / 紅利 / 寄杯的訂單先存在點餐機，
  顧客拿到 4 碼暫存單號至櫃檯付款；連線恢復後自動補送 (以訂單編號去重，不會重複建單；一律以訪客建立、金額以補送時為準)，
  左下角「暫存單」按鈕顯示待補送 / 已補送 (取餐號) / 失敗原因；暫存超過 3 小時不自動補送，改由店員確認。

C 廚房出餐與顧客通知
- KDS 工作站分站 (/kitchen?station=)、各站完成、加急、逾時顏色；廚房製作卡 (菜單「進階」的 prep_note，做法 / 擺盤)：
  KDS 有做法或配方的品名變成按鈕，點開顯示做法、製作時間、配方用量 (每份)、圖片 (GET /api/kitchen/prep_card/<id>，
  第一次點才載入)。
- 叫號語音播報設定 (系統設定 → 取餐通知)；顧客手機掃收據 QR code 追蹤出餐 (/track/<訂單>)；完成時 LINE 推播。
- 預估等候時間 (系統設定 → 廚房看板；菜單「進階」可填各餐點的製作時間)：基本等候 + 本單工作量 (最長製作時間 + 其餘每份
  1 分鐘) + 排隊中訂單剩餘工作量 ÷ 廚房同時製作的訂單數；點餐頁購物車與確認視窗顯示「預估等候約 N 分鐘」(/api/eta)，
  結帳時把預計完成時間存進訂單 (order.estimated_ready_at；預約單不估)，收據、我的訂單、出餐進度頁與廚房看板顯示
  「預計 HH:MM」，出餐後不再顯示；線上訂單由取餐分店收到時估算並同步回線上端。
- 手機推播通知 (系統設定 → 手機推播通知 Web Push)：顧客在出餐進度頁按「開啟完成通知」後訂閱 (/api/push/vapid、
  /api/track/<uuid>/push)，餐點完成 / 外送員出發 / 店家回覆評價時推播到手機 (離開頁面、關閉瀏覽器也收得到)；只在 HTTPS
  網址有效，門市以 http://IP 開啟時退回頁面內通知；啟用後第一次訂閱自動產生 VAPID 金鑰；訂閱送出後即刪除，超過 7 天的
  訂閱每日清除；推播伺服器回 404 / 410 時刪除訂閱。
- 電子收據 (系統設定 → 電子收據寄送)：收據視窗「寄到信箱」(需 SMTP；會員可記住 Email，User.email) /「傳到 LINE」(需 LINE
  Messaging、會員已綁定)；可設定結帳時自動傳給 LINE 綁定會員 (點餐機與 POS 都會)。receipt_service.text_receipt 產生純文字
  明細，寄送在交易後背景執行，失敗只記錄；每張訂單每天最多寄 5 次。API：POST /api/my_orders/<id>/receipt/email | line
  (會員自己或本次進場訪客)。
- 餐後評分：出餐追蹤頁 1~5 星與意見，高分邀請到 Google 評論、低分通知店家；後台「顧客評價」(/admin/feedback) 可回覆一句
  (通知顧客：LINE 文字、追蹤頁 Web Push feedback_reply) 並勾「公開」；4 星以上且有留言的公開評價 (姓名遮罩) 顯示在
  未登入頁「顧客好評」，追蹤頁顯示店家回覆；稽核 FEEDBACK_REPLY。評價只上傳不下發：回覆要在看得到該評價的那台主機做。
- 預約取餐時間 (系統設定 → 線上點餐)：顧客結帳選「盡快」或指定時段；廚房看板 / POS / 追蹤頁顯示「預約 HH:MM」，
  預約明天以後的訂單當天才出現在廚房看板、不會被換日自動結案。
- 再點一次：我的訂單每張訂單 (非已取消) 有「再點一次」，伺服器把品項還原成購物車格式 (套餐 / 任選 / 規格 / 加料 /
  客製化文字；/api/reorder/<id>，只能是自己的訂單)，前端依目前菜單重新計價加入購物車；紅利兌換、寄杯、贈品與已下架 /
  售完的品項略過並提示。
- 售完即時更新 (點餐頁)：每 30 秒輪詢 /api/menu/availability (帶 ETag / If-None-Match，沒變化回 304 不重畫)，售完 /
  恢復供應 / 不在供應時段 / 改價不必重新整理就會更新卡片與購物車標示；對外 API /api/v1/menu 也帶 ETag。

D 菜單與原物料
- 供應時段 / 星期 / 販售期間、內用 / 外帶 / 線上 / 外送不同價、會員價 (MenuItem.member_price：登入 / 綁定會員時與通路價
  (含特價) 取低者，MenuItem.price_for(channel, member=True)；點餐頁顯示劃線價 + 會員價徽章，POS 綁定 / 解除會員時購物車
  重算)、過敏原與素食標示、英文 / 日文菜單、規格 (大小杯) 自動加價、熱量與營養標示 (點餐頁顯示 kcal，購物車顯示熱量合計；
  系統設定 → 菜單顯示可關閉)。
- AI 一鍵翻譯菜單：品名 / 說明 / 分類的英文與日文由 ai_service.translate_entries 一批 25 筆送 Gemini (JSON 回應) 填入
  空白欄位，不覆蓋手填 (可勾「連已填的也重新翻譯」)；沒有 GEMINI_API_KEY 或回應壞掉時用規則字典 (常見分類 / 品名整字
  命中才翻)；稽核 MENU_TRANSLATE。
- 套餐任選 (套餐「任選群組」)：每行「群組: 餐點, 餐點+加價」，例如「飲料: 紅茶, 綠茶, 珍珠奶茶+15」；點餐機 / 桌邊 / 線上 /
  POS 點套餐時每組任選一項 (可升級加價、選項售完不能選、選項可客製冰塊甜度)，扣選中的配餐庫存；訂單記錄賣出當下的配餐。
- 碳足跡標示 (參考值)：原物料設定產地與碳排係數 (g CO₂e / 單位)，餐點碳足跡 = 配方用量 × 係數，自動存回餐點並同步；
  菜單「進階」可改手動填寫。點餐頁餐點卡與購物車合計以「參考值」標示 (系統設定 → 菜單顯示可關閉)。
- 原物料 (/admin/inventory)：配方扣料、進貨 / 報廢 / 盤點、低庫存通知、毛利；原料批號效期：進貨 / 收貨建立批號，扣料、
  報廢、盤虧依「先到期先出」；即將到期與已過期的批號在原物料頁顯示並通知 (每天 07:00 一次)，可整批報廢。
- 供應商與採購單 (/admin/purchasing)：原料設定預設供應商、目標庫存、採購單位；低庫存一鍵建立建議採購單 (依供應商分單)，
  編修 → 送出 (可 Email 給供應商) → 收貨入庫 (填有效期限)；採購單可列印。
- 備料預測 (/admin/forecast)：以過去幾週同一個星期幾的銷量平均預估份數、來客時段分布與原料需求 (標示不足)。
- 分店價格 / 上下架 (線上端：系統 → 分店價格 / 上下架)：總部為某家分店設定不同售價 (例如機場店貴 $20) 或下架餐點；
  通路價與特價依同一個價差加減；只同步給該分店，該分店的點餐機 / POS / 桌邊與以該分店取餐的線上點餐都套用，分店頁面為唯讀。
- 分店叫貨 / 中央廚房 / 調撥 (菜單 → 分店叫貨 / 調撥)：分店向總部中央廚房 (線上端的原料庫存) 或其他分店叫貨，出貨方出貨
  (扣自己的原料) 或拒絕、收貨方收貨 (加到自己的原料，可填實收數量，差異會通知)；分店間也可直接調撥；單據經線上端同步給
  雙方，同時按兩次出貨只扣一次。
- 菜單工程分析 (/admin/reports/menu-engineering)：依銷售份數與每份毛利把餐點分成明星 / 金牛 / 潛力 / 瘦狗四象限 (散佈圖與
  建議)，可依期間、分類、分店篩選並匯出 Excel；未設定成本的餐點會提示。
- 原料成本 / 售價歷史 (報表 → 成本 / 售價歷史)：原料單位成本與餐點售價 / 特價 / 通路價 / 單位成本的每一次變動 (後台編輯、
  進貨加權平均、調撥收貨、匯入、同步) 都留下紀錄 (誰、何時、舊值 → 新值、漲跌 %)；並列出與 N 天前相比原料成本上漲最多的
  餐點、每份成本與毛利率的變化。

E 會員行銷
- 會員等級 (依累積消費自動升級、折扣與點數倍率)、生日禮、儲值金 (加值贈送)、LINE 登入 / 綁定與推播、簡訊驗證註冊與
  忘記 PIN (系統設定 → 會員等級 / 生日禮 / 儲值金、LINE 登入與官方帳號、簡訊驗證碼)。
- 優惠券：使用期限 (活動起訖日、領取後 N 天到期)、會員專屬券、批量發放 (全部 / 等級 / 本月壽星 / 已綁 LINE / 手機清單，
  可指定張數與到期日、LINE 通知、整批撤回未使用)；防濫用 (猜錯代碼次數過多暫停查詢、公開代碼限會員 / 每人限用次數、
  最高折抵金額、代碼格式檢查)；紅利商城對已持有的券顯示「剩餘 N 張」並可重複兌換。
- 促銷活動 (/admin/promotions)：買 X 送 Y、第 N 件折扣、滿額折 / 滿額贈，可限時段 / 管道 / 會員；POST /api/promotions/preview 試算。
- 點數有效期限：先得到的點數先到期 (每天由線上端 / 單機執行)，紅利商城顯示最近到期的點數，到期前 LINE 提醒。
- 集點卡 (系統設定 → 集點卡)：買指定分類每份 1 章或每筆滿額 1 章，集滿自動送點數 / 優惠券；取消訂單扣回。
- 推薦好友：會員在紅利商城看到推薦碼 (註冊網址 /register?ref=推薦碼)，新會員第一筆消費達門檻後雙方得點。
- 寄杯 / 多次券 (系統設定 → 集點卡；後台 /admin/passes)：設定「美式咖啡 10 杯 $450」等商品；POS 綁定會員後售出 (建立
  寄杯購買訂單，收款 / 發票 / 交班照常)；會員在點餐機 / 桌邊 / 線上 (購物車「我的寄杯」) 或 POS 兌換一份 (免費，只收加料)，
  同時在多個通路兌換不會扣成負數；兌換訂單取消退回次數；購買訂單未使用才可整筆作廢；剩餘次數跨分店同步。
- 會員分群與自動行銷 (/admin/marketing)：依最近消費 / 次數 / 金額 (RFM) 分成冠軍、忠實、新客、需要關注、快流失、沉睡、
  未消費，可看名單與匯出 Excel；「久未消費自動發券」每天 10:00 對超過 N 天沒來的會員發券並 LINE 推播 (冷卻期內不重發)，
  「排程推播」指定時間對某個分群發送；顯示 30 天回購人數 / 營業額與用券數。會員可在紅利商城關閉優惠訊息 (拒絕行銷的
  會員不發券、不推播，也排除在 LINE 群發外)；自動行銷只在線上端 / 單機執行。
- 會員自助 (紅利商城「我的資料」)：下載自己的個人資料 (JSON)、輸入 PIN 刪除帳號 (訂單保留但去識別化；儲值金有餘額不可刪)；
  人臉登入的會員做敏感操作 (下載、會員條碼、解除 LINE) 須再輸入 PIN。

F 通路、外送與訂位
- 外送平台：Uber Eats / foodpanda 訂單 Webhook 自動匯入 (或 POS 手動登錄外送單) (/admin/delivery)；找不到品項代碼的訂單
  拒單並通知。
- 自有外送 (系統設定 → 自有外送；派車 /admin/dispatch)：線上點餐與 POS 電話訂餐可選「外送到府」(地址、電話、配送區域)，
  依區域收外送費 (最低消費、滿額免運)；派車頁指派外送員 → 出發 → 送達 / 失敗，顧客在追蹤頁與「我的訂單」看到配送狀態。
- 外送員 GPS 追蹤：派車頁指派外送員後出現外送員手機連結 / QR code (/driver/<憑證>)，外送員允許定位後每隔幾秒回報位置
  (需 HTTPS)；顧客出餐進度頁顯示外送員最後位置、地圖連結與距離 (顧客可分享位置)。位置回報到公開網址 (PUBLIC_BASE_URL /
  QR code 網址 / 總部網址)，訂單上的憑證會同步到線上端。
- 線上訂位 (/reserve) 與現場取號候位 (/waitlist)，店員看板 (/reservations)，叫號看板顯示候位號碼。
- 訂位訂金與未到管理 (系統設定 → 訂位 / 候位)：線上訂位達設定人數須以綠界信用卡預付每位訂金，30 分鐘內未付款自動取消；
  入座自動退還 (或結帳折抵)、訂位前 N 小時以前取消自動退刷、之後取消或未到不退；同一支手機未到達設定次數暫停線上訂位；
  訂位前 N 小時以簡訊或 LINE 提醒。訂位看板顯示訂金狀態、未到次數與本月未到 / 不退金額。
- 團體預購 (系統設定 → 團體預購；門市 / 單機)：主揪在點餐首頁「團體預購」(或店員在後台代客建立) 設定取餐時間與截止時間，
  取得分享連結與只顯示一次的管理連結；同事打開分享連結各自填名字、選餐點 / 規格 / 備註 (同一個瀏覽器或個人連結可修改、
  刪除)；截止時自動 (或主揪按「送單」) 合併成一張預約取餐訂單 (現場付款，備註列出每個人點的內容)，廚房在取餐前出現；
  可列印每人一格的分裝清單；送單失敗 (售完等) 通知店家，主揪調整後可再送；同時送單只會建立一張。

G 報表、人員與總部
- 營運報表 (/admin/reports)：毛利、時段熱度、付款方式、管道、分類 / 品項、收銀員、分店比較、促銷成效、人事成本率
  (打卡工時 × 時薪佔營業額比例)、Excel 匯出、定期 Email 寄送。
- 即時看板 (/admin/live)：今天營業額 / 訂單 / 客單價 / 與昨日同時段比較、各分店 (線上端彙整) 連線狀態、每小時營業額、
  熱銷品項、最新訂單，自動更新。
- 營業費用 (報表 → 營業費用，店長以上)：逐筆登錄房租、水電、薪資 (未打卡部分 / 勞健保)、耗材、行銷、修繕、手續費、稅捐等，
  可填廠商與進項發票號碼；勾選「每月固定」的費用下個月一鍵複製 (重複按不會重複複製)。分店登錄的費用上傳到線上端
  (看得到但不能改)，刪除為標記刪除並一起同步。
- 損益表 (報表 → 損益表，需報表權限；費率在系統設定「損益表手續費」)：營業額 − 銷項稅額 = 營業收入淨額，− 銷貨成本
  (配方原料或餐點單位成本) = 營業毛利，− 人事成本 (打卡工時 × 時薪) − 金流 / 外送平台手續費 (依收款方式 × 費率估算)
  − 營業費用 = 營業淨利與淨利率；與上一期比較、近 6 個月趨勢、可選月份或任意區間 (最長 400 天)，線上端可依分店篩選，
  可匯出 Excel。
- 員工打卡 (/timeclock)、班表與工時 (/admin/timeclock)、員工時薪 (員工帳號頁)。
- 排班建議 (系統設定 → 排班建議，店長以上；備料預測頁「依預測排班」)：以過去同一個星期幾各時段的訂單數估算每小時需要的
  人力 (每人每小時可處理張數、最少人數可設定)，與班表比較標示不足 / 過多，產生建議班別 (選員工後一鍵加入班表)，並估算
  人事成本率。
- 自動排班 (/admin/staffing/auto)：員工在「可排時段」頁填每週可上班的時段與每週上限，一鍵生成一週班表預覽後套用。
  貪婪分配：每日 ≤ 8 小時、每週 ≤ 40 小時、最多連續 6 天 (勞基法 §30 / §36，可在排班參數調整)，本週工時少的人優先。
- 員工異常偵測 (報表 → 員工異常偵測)：各收銀員的整單作廢 / 部分退款 / 單品作廢、手動折扣 / 改價、交班短溢與全店平均比較，
  超過門檻時標示並可每天通知；交班帳差超過設定金額時立即通知。
- 稽核紀錄 (/admin/audit，店長以上)：員工操作稽核與後台登入 / 登出日誌伺服器端分頁 (每頁 50)，依員工 / 動作類型 / 日期 /
  關鍵字 (單號、會員 ID、內容、IP) 篩選，匯出 Excel (目前篩選，最多 20000 筆，留下 AUDIT_EXPORT)；動作名稱在
  app/services/audit.py 的 ACTION_LABELS。
- 總部統一設定：不含金鑰的系統設定 (收銀規則、KDS、會員、訂位、報表…) 在線上端儲存後自動下發到所有分店；分店可自訂 (覆寫)
  並隨時「改回總部設定」；含金鑰與本機性質的設定各台主機自己設定。
- 店家管理 (/admin/tenants，只有平台管理者 = 預設店家負責人)：這台主機上所有店家的狀態、建立日期、員工 / 會員數、分店數與
  連線狀態、近 30 天訂單與營業額、最近訂單；停用 / 啟用店家 (停用後員工無法登入、點餐頁與店家入口 404、分店同步被拒；
  預設店家不可停用；稽核 TENANT_TOGGLE)；建立新店家沿用 /tenant/register。
- 一鍵展示資料 (flask demo seed，見 3.5)：建立獨立的「示範店家」與過去 N 天的營運資料，展示或教育訓練時不必動到正式資料。

H 系統維運與整合
- 帳號安全 (/admin/security)：兩步驟驗證 (驗證 App + 一次性備用碼，可要求負責人 / 店長必須啟用)、登入中的裝置列表與個別登出、
  登出所有裝置 (含各分店)、閒置自動登出；負責人可在員工帳號頁強制登出 / 重設員工的 2FA。
- 後台頁面權限：權限矩陣 (員工帳號 → 額外權限) 的 menu / coupons / members / feedback / settings 五個頁面權限 (店長 / 負責人
  預設全有)；櫃檯 / 廚房被授權後可進後台，只看到對應分頁 (實時訂單 = reports、回饋商城 = coupons) 與側欄頁面 (菜單與套餐、
  會員、促銷 / 行銷 / 禮物卡 / 寄杯、顧客評價、系統設定；稽核只有店長以上)；對應路由用 permission_required。
- 系統監控 (/admin/system)：門市資料庫每日自動備份 (系統設定 → 自動備份)、系統健康 / 錯誤紀錄、Webhook / Email / LINE 錯誤
  通知、主機時鐘誤差檢查、AI 引擎與人臉辨識狀態；線上端 PostgreSQL 由 docker compose 的 backup 服務每日備份；
  PWA (可安裝、斷線提示)。
- 異地備份 (自動備份 → 異地備份)：每日 SQLite 備份後上傳到 WebDAV、S3 相容儲存 (自行 SigV4 簽章，不需 boto3；AWS / MinIO /
  R2 / B2) 或自訂指令 (rclone 等，{file} 代入；只在門市主機可用)；只保留最近 N 份；上傳失敗進監控事件 (offsite-backup)；
  「測試」會上傳一個小檔再刪掉；WebDAV / S3 網址在線上端不可指向內網。
- 門市一鍵更新 (系統監控 → 版本更新，平台管理者、門市端)：顯示 VERSION 檔的版本 (+ git 短雜湊) 與 GitHub 最新 release
  (UPDATE_REPO，快取 1 小時；只從 github.com 下載、上限 300 MB)；「下載更新檔」存到 database/updates/；Windows 主機
  「套用並重啟」以獨立行程執行 deploy\windows\update_store.cmd (從 database\updates\update_store.run.cmd 複本執行)：
  備份程式檔與資料庫 → 停伺服器 → robocopy 覆蓋 (不動 database / uploads / .env / .venv) → pip install -r requirements-store.txt
  → flask db upgrade → 重啟 (NSSM 服務 / 工作排程 / start_store.cmd)；任一步失敗自動還原；記錄 database/updates/update.log。
  線上端 (Docker) 仍依 DEPLOY.txt。
- 系統設定匯出 / 匯入 JSON：不含金鑰與本機專屬區塊 (平台管理者 ?secrets=1 可含金鑰)；匯入只認得的區塊 / 欄位、型別與範圍
  檢查，一律不寫金鑰欄位、非負責人匯入時含金鑰的區塊整個略過。
- 記錄檔與請求識別碼：記錄寫到 LOG_FILE (預設 database/logs/app.log，10 MB × 5 輪替；Docker 設 LOG_FILE= 只輸出 stdout)，
  有檔案記錄時主控台只留警告以上 (門市的 database/server.log 只剩啟動訊息與錯誤，超過 5 MB 時由 start_store.cmd 換檔)；
  每個請求有 X-Request-ID (沿用反向代理送來的)，每一行記錄都有 [識別碼]，500 頁面 / JSON 顯示「錯誤代碼」方便對照記錄。
- 錯誤回報與指標 (選用)：SENTRY_DSN + pip install sentry-sdk → 未預期錯誤送 Sentry (tag request_id / tenant_id，release 帶版本)；
  METRICS_TOKEN → GET /metrics?token=… (或 Bearer) 回 Prometheus 文字格式：請求數 (狀態碼類別)、耗時直方圖、今日訂單 /
  營業額、待製作、列印 / 發票失敗、分店同步延遲 (行程內計數，多 worker 各一份)。
- 靜態資源版本化：url_for('static') 自動加 ?v=檔案雜湊，更新 JS / CSS 後瀏覽器與 PWA 立刻用新檔；/sw.js 的快取名稱帶
  static/ 的版本摘要，部署後舊快取整批清除。
- 對外 API 與 Webhook (系統 → API 與 Webhook，負責人)：API 金鑰 (只顯示一次、可設權限範圍、可停用) 讀取訂單 / 菜單 / 會員 /
  每日統計、更新售完與庫存 (/api/v1/...)；Webhook 在訂單成立、付款、狀態改變、取消時以 HMAC 簽章 POST 到指定 https 網址，
  失敗自動重試、不送內網位址，後台可看送出紀錄、測試與重送。API 說明文件 (/developers，公開)：每個端點的參數、權限、
  回應欄位、錯誤碼、Webhook 標頭與簽章驗證範例，可貼上 API 金鑰「線上試打」(只在瀏覽器中使用)；/developers/openapi.json
  為 OpenAPI 3.0 規格 (可匯入 Postman / Swagger)。
- 裝置管理 (系統 → 裝置管理)：登記點餐機 / POS / 廚房看板，在裝置上輸入配對碼註冊；顯示在線、程式版本 (舊版提示重新整理)、
  最後頁面與 IP；可遠端鎖定 (畫面鎖定、操作被拒) 與要求重新整理。
- 舊資料封存 (系統 → 舊資料封存，負責人)：超過 N 個月的已結束訂單 (含明細、收款、退款、發票) 與日誌搬到 database/archive
  的封存檔，主資料庫保持輕量；可下載、可依訂單編號 / 日期 / 會員手機查詢；會員等級累積消費不受影響；可每月自動執行。
- Excel 匯出 / 匯入的安全：只有「菜單」權限的櫃檯匯出時會員 / 訂單工作表被濾掉、匯入含會員 / 優惠券工作表被拒。

I AI、語音與多語系
- AI 引擎：Gemini 主要模型 503 / 逾時重試一次後改用備援模型 (GEMINI_MODEL / GEMINI_FALLBACK_MODEL)，都失敗才依原因冷卻；
  本機 Qwen 需要 llama-cpp-python (pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu，
  預編譯 CPU 版) 與 models/ 下的 GGUF (不進版控)；都沒有時用規則引擎；系統監控頁顯示兩者狀態與最近錯誤。
  用途：商品文案與營運建議、一鍵翻譯菜單、語音點餐與客服的備援。
- 語音點餐：點餐頁搜尋列的麥克風按鈕 (系統設定 → 菜單顯示可關閉)，瀏覽器語音辨識或打字 → /api/voice/parse 先以菜單規則
  解析 (品名、數量、規格、甜度冰塊、辣度、加料；離線可用)，規則解析不到才交給 AI (Gemini → 本機 Qwen)；一律先在確認視窗
  調整數量後才加入購物車，價格由購物車依菜單計算。
- AI 客服機器人 (系統設定 → AI 客服機器人；/api/chat)：點餐頁與出餐進度頁右下角的對話窗。過敏原 / 素食 / 辣度只從菜單標示
  直接查、訂單進度直接讀訂單、營業資訊與常見問答用後台填的文字；其他問題才交給 AI 並標示「請以櫃檯為準」。
- 顧客端多語系 (中 / 英 / 日；前端翻譯、後端維持中文)：所有顧客看得到的頁面 (點餐頁、未登入頁、購物車、結帳確認、收據、
  我的訂單、套餐 / 客製化視窗、桌邊橫幅、註冊 / 登入、忘記 PIN、出餐進度、線上付款與付款結果、桌邊線上結帳、線上訂位 /
  訂金 / 訂位狀態、候位、紅利商城、團體預購、隱私權告知、離線頁、錯誤頁、提示訊息) 與其 JS 的介面文字都由 ui.<鍵> /
  Kiosk.t(鍵, 中文) 提供，字串集中在 app/services/i18n_pages.py (PAGES 元組：鍵、中、英、日；i18n_service.py 只有邏輯，
  UI 字典由 PAGES 產生)；頁面為英 / 日文時 base.html 另嵌 #ui-data 字典給 JS。後端 (路由 / service / AppError / flash / HTTP
  錯誤) 的訊息仍是中文：i18n_pages.MESSAGES (逐句) 與 MESSAGE_PATTERNS (帶品名 / 數量的樣式) 提供對照，顧客頁的 JSON 錯誤
  回應另附 message_i18n (Kiosk.api 優先顯示)、flash 經 tr_msg 過濾器翻譯、錯誤頁顯示翻譯；沒有對照的句子原樣顯示。
  員工頁 (後台 / POS / 廚房 / 取餐 / 看板，STAFF_BLUEPRINTS) 一律中文。第一次進站依瀏覽器 Accept-Language 選語言 (只選本店
  有開的語言；中文一律繁中)，之後以 ?lang= 切換並記在 session。訂單 / 外送 / 付款狀態以代碼對照翻譯 (my_orders JSON 多了
  status_code / delivery_status)。客製化選項的「值」維持中文送廚房，只翻顯示文字；菜單內容 (品名 / 分類 / 說明)、店名、
  支付品牌名稱不翻譯，由店家在菜單「進階」填翻譯。


六、多店與線上端 (指引)
--------------------------------------------------------------------
- 同步的資料表、方向與合併規則、跨店同時使用券 / 點數 / 庫存的處理、安全限制：SYNC.txt。
- 線上端 Docker 部署、門市 Windows 部署與開機自動啟動、同步設定步驟、線上付款 (綠界)、外部服務申請、上線前檢查清單：DEPLOY.txt。
- 本機同時模擬線上端與門市：本檔 3.1。線上端映像只裝 requirements.txt，沒有人臉辨識 (會員人臉特徵仍隨會員資料同步到各分店)。


七、測試
--------------------------------------------------------------------
測試一律使用暫存資料庫與暫存資料夾，不讀 .env，不會動到 database/、uploads/ 與 static/。
  pip install -r requirements-dev.txt  安裝測試工具 (pytest、pytest-cov、playwright、axe-playwright-python)
  pytest -q                           後端測試 (SQLite)；需要 node (前端共用純邏輯的測試 test_shared_js_logic 以 node 執行，
                                      沒有 node 時該測試跳過)
  set TEST_DATABASE_URL=postgresql://kiosk:kiosk@127.0.0.1/kiosk_test → pytest   同一套測試改在 PostgreSQL 上跑 (該資料庫會被清空)
  playwright install chromium (第一次) → pytest -m e2e   瀏覽器端對端測試 (實際操作各頁面；JS 錯誤或違反 CSP 都會失敗；
                                      含 axe-core 無障礙檢查)
  pytest -m stress                    真實 HTTP 壓力測試
  set TEST_SYNC_HUB_DATABASE_URL=postgresql://kiosk:kiosk@127.0.0.1/kiosk_sync_test → pytest tests/test_sync_hub_node.py
                                      同步測試的線上端改用 PostgreSQL
  pytest tests/test_pos_checkout.py   只跑一個主題 (有情境依賴的檔案會自動加入前置檔，開頭會列出)
  python tests/test_payment_ecpay_script.py [live]   線上付款：模擬綠界通知走完 30 項情境 (不需網路)；live 到綠界測試環境用
                                      測試卡實際付款 (卡號 4311-9522-2222-2222、未來的有效期限、任意安全碼、3D 驗證碼 1234)
  ruff check . / mypy                 靜態檢查 (ruff.toml、mypy.ini；pip install ruff mypy)
  pip-audit -r requirements.txt -r requirements-store.txt -r requirements-dev.txt   依賴套件已知漏洞 (pip install pip-audit)
  pytest --cov=app                    涵蓋率 (CI 的 3.13 工作，app/ 低於 85% 失敗)
  python tests/load_gunicorn_pg.py --url ... --password ... [--database-url ...]   多 worker 負載測試：對執行中的 gunicorn +
                                      PostgreSQL + Redis 模擬點餐機 / POS / 廚房 / SSE / 對外 API 的尖峰流量並檢查一致性
                                      (步驟與參考結果見 DEPLOY.txt 一-11)
  GitHub Actions (.github/workflows/tests.yml)：push 到 main 或開 PR 時安裝 requirements-dev.txt 並執行 lint (ruff / mypy；
  pip-audit 只警告)、unit (SQLite，Python 3.13 含涵蓋率門檻 85% / 3.14，另裝 node 22 跑前端純邏輯測試)、postgres (含同步測試
  的線上端)、e2e 四組工作；
  .github/dependabot.yml 每週檢查 pip 與 actions。

測試檔 (tests/，檔名即測試主題；新的測試一律放這裡並照此命名)
  啟動與升級    test_startup_defaults (預設店家 / 負責人 / 加料、SECRET_KEY、預設帳密重設規則)、test_legacy_database_upgrade
                (舊版資料庫升級、舊上傳資料夾搬移)、test_face_models_download (人臉模型缺檔時下載並比對 SHA-256、系統健康頁的
                人臉狀態、flask models download)
  權限與安全    test_staff_login_and_roles (員工登入、帳號管理、角色權限矩陣)、test_web_security (CSRF 全路由掃描、安全標頭、
                訊息跳脫、錯誤輸入不出 500)、test_rate_limits_and_lockout (登入頻率限制、PIN 鎖定、X-Forwarded-For 信任)、
                test_tenants_and_isolation (新店家註冊、店家入口、跨店隔離)、test_security_hardening (整庫備份限平台管理者、
                門市不開放公開建店、綠界測試金鑰需明確允許、LINE 待綁定清除 / 逾時、打卡頁遮罩帳號、已註冊裝置不受帳號鎖定、
                POS 動用儲值金 / 點數需會員驗證、訂單 uuid 由伺服器產生、監控 Webhook 與線上端寄信 / 校時 / 印表機不可連內網、
                簡訊全店上限)、test_backup_restore (整庫備份 / 還原、惡意備份檔、舊版備份還原)
  後台版面      test_admin_layout (側欄七個群組依角色 / 權限 / 主機角色過濾、總覽分頁、菜單 / 會員 / 稽核各自頁面與舊 ?tab=
                導向、系統設定左欄與搜尋、舊按鈕 id 保留)、test_settings_admin (系統設定權限、金鑰遮罩、表單驗證、連線測試)
  菜單與會員    test_menu_admin (菜單、套餐、手動售完)、test_member_accounts (會員註冊、欄位驗證、後台編輯 / 刪除與稽核)、
                test_member_face_and_photos (人臉特徵與登入、重複註冊、上傳圖片限制、照片權限)、test_carbon (碳足跡係數 /
                配方計算 / 手動 / 顯示 / 開關)、test_pricing_promotions (通路價、供應時段、改價 / 整單折扣核准、服務費、會員等級、
                促銷活動)、test_menu_i18n_kiosk_perms (AI 翻譯菜單、飲食篩選、點餐機禮物卡、會員價、後台頁面權限)、
                test_menu_etag_barcode (availability / api/v1/menu 的 ETag、售完 / 時段 / 改價變化、POS 條碼、mypy 通過)
  點餐與訂單    test_kiosk_checkout (計價、加料、冪等、取餐號)、test_coupons_and_rewards (優惠券、紅利商品與兌換憑證、紅利設定)、
                test_pos_checkout、test_pos_today_orders_api、test_pos_quick_tables (桌號 / 稱呼快捷鍵)、
                test_order_status_and_cancel (狀態流轉、取消回補、換日結案)、test_concurrency_stock_points (不超賣、點數不雙花)、
                test_stock_race_all_channels (各通路同時搶同一份庫存、平台訂單重送只建一張、分店回報庫存不吃掉線上預扣)、
                test_coupon_points_race_all_channels (各通路同時用同一張券 / 點數)、test_combo_and_shared_resource_races
                (套餐配餐與單點共用庫存、取消回補賣出當下的配餐、配餐停售、集點卡 / 推薦獎勵只發一次、同時訂最後座位、
                兩台 POS 同時開班、關班時結帳、跨店禮物卡 / 儲值金同時用光)、test_price_gift_and_crossnode_loyalty (改價與
                送出之間的價差、贈品最後一份、跨店重複推薦沖銷、跨店同時集滿集點卡、點數到期與分店同時使用的校正)、
                test_live_updates_sse (SSE、連線配額、看板遮蔽姓名)、test_intake_eta_reorder (接單控制、預估等候、再點一次)、
                test_combo_choices_split_payment_void (套餐任選、混合付款、單品作廢)、test_signage (數位看板)、
                test_voice_order (語音點餐規則解析與 AI 備援)、test_chatbot (AI 客服)、test_ai_service (Gemini 重試 / 備援 /
                冷卻、本機模型診斷)
  優惠券        test_coupon_security (猜碼鎖定、公開代碼限會員 / 每人次數、最高折抵、代碼格式、跨店隔離、商城剩餘張數)、
                test_coupon_issue_expiry (會員專屬券、批量發放、過期與活動期間、後台票券視窗)
  報表與前端    test_reports_and_excel (營運分析、Excel 匯出 / 匯入、儀表板與稽核)、test_deploy_config (/healthz、反向代理標頭、
                部署設定檔)、test_frontend_static_files (CSP、外部網址、XSS、POS / 點餐機 / shared 模組的 import / export 對得上、
                檔案大小)、test_shared_js_logic (以 node 執行 tests/js/shared_spec.mjs：金額順序與進位、加購配額、單價、合併鍵、
                payload、發票、預覽排程；沒有 node 時跳過)、test_shared_utils (app/utils.py 的日期 / 時間 / 分頁 / ETag / Excel /
                門市限定等共用工具)
  同步          test_sync_hub_node (一個線上端 + 兩家分店的完整情境，含分店暫停狀態上傳)、test_sync_admin_pages (分店管理、
                同步狀態、線上點餐選分店)、test_sync_security (分店權杖外流時的限制)、test_sync_invariants (全部跑完後檢查同步
                不變量)、test_sync_pro_tables (促銷 / 等級 / 翻譯下發、兩店離線使用儲值金合併、收款與退款上傳、會員券同步)
  付款與個資    test_payment_ecpay (綠界檢查碼、付款通知、逾時取消)、test_payment_ecpay_script (付款測試腳本)、test_privacy_pdpa
                (隱私權告知、同意紀錄、撤回人臉、清除久未使用會員)、test_online_payments_and_pos_hardware (付款方式 / ATM / 超商
                代碼期限、自動退刷、錢櫃、客顯、會員條碼、規格)
  桌位點餐      test_table_qr_ordering (桌位管理、掃碼點餐、失效 QR code、POS 桌位結帳)、test_table_concurrency (同時操作的衝突；
                輪數 TABLE_CONFLICT_ROUNDS)、test_floor_plan_table_pay_passes (平面圖、桌邊線上結帳、寄杯)、
                test_pickup_calls_feedback_display (預約取餐、桌邊呼叫、待機輪播、大字、熱量營養、餐後評分)
  進階功能      test_payments_refunds_wallet (刷卡授權碼、LINE Pay 扣款 / 退款、部分退款、作廢、儲值金)、test_einvoice_ecpay
                (電子發票加解密、載具 / 統編、開立 / 重送 / 作廢 / 折讓)、test_shifts_and_table_split (交班 / 帳差、必須開班、
                換桌、分開結帳、多種付款、員工權限)、test_kitchen_printing_inventory (工作站 KDS、加急、熱感印表機出單、原物料
                扣料 / 盤點)、test_receipt_kds_feedback (電子收據、廚房製作卡、評價回覆與公開好評)、test_receipt_rows_shared
                (熱感機與純文字收據由同一份 receipt_rows 產生、禮物卡遮罩)、test_members_line_sms (會員
                等級、生日禮、儲值金調整、LINE 登入 / 推播、簡訊驗證)、test_loyalty_giftcards_member_data (點數到期、集點卡、
                推薦好友、禮物卡、會員下載資料與刪除帳號)、test_marketing_deposits_anomalies (RFM 分群與自動行銷、訂位訂金 /
                退刷 / 未到停權 / 提醒、員工異常偵測、交班帳差通知)、test_delivery_and_reservations (外送平台 Webhook、線上訂位
                名額、候位與叫號看板)、test_home_delivery (外送到府、派車、配送狀態同步)、test_delivery_gps (外送員 GPS 憑證 /
                回報 / 追蹤頁 / 同步)、test_purchasing_lots_forecast_labor_live (建議採購、批號效期、備料預測、人事成本率、即時
                看板)、test_branch_menu_engineering_transfers (分店價格 / 下架、菜單工程四象限、叫貨 / 調撥、線上端不收訂位)、
                test_expenses_pnl_cost_history (營業費用、損益表、Excel、成本歷史、分店費用上傳、API 文件與 OpenAPI 涵蓋每個
                /api/v1 路由)、test_accounting_api_devices_archive (傳票借貸平衡、發票明細、API 金鑰、Webhook 簽章 / 重試 /
                內網阻擋 / 多行程只送一次、裝置配對 / 遠端鎖定、封存、排程主控、匯出防公式注入、遷移代號長度)、
                test_group_orders_staffing_offline (團體預購、排班建議、斷線暫存補送)、test_auto_schedule (可排時段、生成一週
                班表、套用不重複)、test_ops_reports_timeclock_pwa (進階報表 / 定期寄送、每日排程、系統監控與備份、打卡、追蹤頁、
                PWA、多語系)、test_hq_settings_and_staff_security (總部設定下發 / 覆寫、會員分頁、2FA、登入裝置、閒置登出)、
                test_audit_tenants_push (稽核頁、店家管理、Web Push)、test_logging_static_version_ci (X-Request-ID、錯誤代碼、
                輪替記錄檔、?v=、Service Worker 快取名稱、CI 設定、ruff 全專案通過)、test_i18n_coverage (鍵名不撞 dict 方法、
                三語齊全、顧客頁無寫死中文、translate_message、message_i18n、員工頁中文、Accept-Language)、
                test_ops_backup_update_metrics (異地備份、一鍵更新、/metrics 與 Sentry、N+1 守門 (tests/support.count_queries)、
                設定匯出匯入、POS 常用備註、淺色模式、Dependabot、百分比券後點數折抵上限四捨五入)、test_order_service_modules
                (拆分後對外名稱不變、模組大小與依賴方向)、test_demo_seed (示範店家各報表有內容、其他店家不變、--reset / remove、
                拒絕覆蓋真實店家 / 分店主機 / 線上端)
  瀏覽器 (e2e)  test_e2e_admin / customer / kitchen_pickup / members / pos / page_smoke / table_ordering / pro_features (各頁面基本
                流程)、test_e2e_price_change (改價後購物車更新)、test_e2e_sizes_display_member_qr (規格、客顯、會員條碼)、
                test_e2e_combo_split_void (套餐任選 / 混合付款 / 作廢品項 / 平面圖 / 桌邊結帳 / 寄杯 / 菜單工程 / 自動行銷 /
                裝置鎖定)、test_e2e_expenses_api_offline_group (費用 → 損益表 / API 文件試打 / 斷線暫存 / 團體預購)、
                test_e2e_intake_eta_barcode_audit (暫停接單 / 預估等候與再點一次 / 售完即時更新 / 掃條碼 / 稽核頁)、
                test_e2e_a11y_filters_receipt_perms (axe-core 無障礙 (未裝 axe-playwright-python 時跳過) / 飲食篩選 / 會員價 /
                點餐機禮物卡 / 製作卡 / 評價回覆 / 頁面權限)
  壓力          test_stress_http (真實 HTTP 壓力測試)、load_gunicorn_pg (不是 pytest，直接執行：gunicorn 多 worker 負載測試與一致性檢查)

測試共用同一個暫存資料庫，有先後依賴 (例如 POS 測試使用菜單測試建立的餐點)：執行順序寫在 tests/conftest.py 的 RUN_ORDER，
前置依賴寫在 NEEDS (新增測試檔時一併維護)。共用的輔助函式在 tests/support.py (login_staff、make_item、make_member、checkout、
run_together、count_queries、get_or_create_tenant、tenant_request、item_stock、seed_demo…)。


八、專案結構與開發慣例
--------------------------------------------------------------------
8.1 專案結構
Project Root/
├── app/
│   ├── __init__.py            create_app：設定、擴充元件、安全標頭 / CSP、錯誤處理、多店家、啟動遷移、背景工作、人臉模型補下載
│   ├── config.py              所有設定 (讀取 .env)
│   ├── cli.py                 flask CLI 指令 (setup、sync、payments、privacy、tables、models、ops、demo)
│   ├── extensions.py、errors.py、tenant_context.py、utils.py
│   │                          db / migrate / csrf / login / limiter；AppError 與統一錯誤處理；多店家隔離 (查詢自動加店家條件、
│   │                          寫入防跨店)；role_required、platform_admin_required、store_only / hub_only、get_own / find_own、
│   │                          輸入驗證、日期區間 / 分頁 / ETag 回應 / Excel 下載等各路由共用的小工具 (見 5.3)
│   ├── models/                ORM：mixins (uuid / updated_at / sync_rev / tenant_id、CreatedAt / LastSeen / KindLabel、公開代碼)、
│   │                          enums (訂單狀態、來源、付款方式、權限)、
│   │                          tenant (店家、員工、可排時段)、table (桌位、平面圖、桌邊線上結帳)、user (會員、紅利設定、兌換憑證、
│   │                          日誌、隱私權設定)、menu、order、coupon、sync (同步序號、刪除紀錄、點數異動帳、分店、庫存快照)、
│   │                          settings (每家店一組 key / JSON)、payment (收款、退款、桌位結帳批次、電子發票)、shift、printing、
│   │                          inventory (原物料 / 供應商 / 批號)、member (等級 / 儲值金 / 集點 / 禮物卡 / 寄杯)、promotion、
│   │                          reservation (訂位含訂金 / 候位)、staff (打卡 / 班表)、branch (分店價格、叫貨 / 調撥)、marketing、
│   │                          integration (API 金鑰、Webhook、裝置)、expense (營業費用、成本歷史)、group_order、system (PIN 鎖定)
│   ├── services/              商業邏輯 (重點)：
│   │   ├── order_service.py   結帳與訂單的對外入口 (只匯出；呼叫端一律 import 這裡)。實作：order_pricing (優惠碼、加購配額、
│   │   │                      購物車計價、通路價 / 服務費 / 折扣)、order_stock (取餐號、條件式扣 / 回補庫存、分店庫存快照)、
│   │   │                      order_checkout (原子結帳 process_checkout，_prepare / _validate / _reserve / _price / _persist /
│   │   │                      _after_commit 階段)、order_lifecycle (狀態機、取消回補、單品作廢、換日結案)
│   │   ├── settings_service.py  系統設定的區塊 / 欄位定義 (後台表單自動產生；每個 Section 帶 subgroup 與 keywords 供設定頁
│   │   │                      左欄與搜尋)、讀寫、連線測試；各服務以 settings_service.reader('key') / flag('key', field) 取得
│   │   │                      config() / enabled()
│   │   ├── i18n_pages.py (PAGES / MESSAGES / MESSAGE_PATTERNS 字串)、i18n_service.py (翻譯邏輯)、admin_nav.py (側欄)、
│   │   │   cv_service.py (人臉；opencv / numpy 選用、模型下載與校驗)、excel_service.py (openpyxl 匯出 / 匯入)、
│   │   │   ai_service.py、voice_order_service.py、chatbot_service.py、analytics_service.py、demo_service.py
│   │   ├── payment_service.py、payment_ecpay.py、einvoice_ecpay.py、linepay.py、table_pay_service.py、deposit_service.py
│   │   │   (收款 / 退款、綠界、發票、LINE Pay、桌邊結帳、訂金)；line_service.py、sms_service.py、mail_service.py、
│   │   │   delivery_service.py、push_service.py、receipt_service.py (外部服務：HTTP 呼叫走 http_client.request_json，各模組
│   │   │   保留 _post / _http 薄包裝讓測試以假 API 取代；Web Push 自行實作 VAPID + aes128gcm)；receipt_service.receipt_rows
│   │   │   為熱感機與純文字收據共用的內容來源；payment_ecpay 提供綠界付款頁 / 通知 / 瀏覽器帶回三種路由的共用骨架
│   │   ├── points_service.py (點數異動帳)、reward_service.py、voucher_service.py、coupon_service.py、wallet_service.py、
│   │   │   loyalty_service.py (點數到期 / 集點 / 推薦)、giftcard_service.py、pass_service.py、member_qr_service.py、
│   │   │   member_verify_service.py、marketing_service.py (RFM)
│   │   ├── inventory_service.py、purchasing_service.py、forecast_service.py、branch_service.py、history_service.py (成本 / 售價
│   │   │   歷史，flush 前比對)、menu_engineering_service.py、menu_service.py、kitchen_service.py、print_service.py、escpos.py、
│   │   │   eta_service.py、reorder_service.py、intake_service.py、pickup_service.py、feedback_service.py、own_delivery_service.py、
│   │   │   group_order_service.py、reservation_service.py
│   │   ├── shift_service.py、approval_service.py、security_service.py (TOTP、備用碼、登入工作階段)、staffing_service.py、
│   │   │   staff_anomaly_service.py、report_service.py、pnl_service.py、accounting_service.py、live_dashboard_service.py
│   │   ├── backup_service.py、offsite_backup_service.py、update_service.py、maintenance_service.py / monitor_service.py (背景排程：
│   │   │   多行程只由「排程主控」執行；自動備份、健康、通知、時鐘)、archive_service.py、observability.py、log_service.py、
│   │   │   static_version.py
│   │   └── webhook_service.py、device_service.py、api_docs.py (端點只在這裡宣告一次)、deferred.py (交易提交後才執行)、
│   │       db_locks.py、event_bus.py (SSE，可選 Redis)、privacy_service.py、table_service.py、upload_paths.py、auth_throttle.py、
│   │       customer_session.py、audit.py、db_migrate.py、bootstrap.py、legacy_upgrade.py、baseline_schema.py
│   ├── sync/                  registry (資料表、方向、本機欄位)、tracking (sync_rev 與刪除紀錄)、engine (合併規則)、
│   │                          hub (/api/sync/v1)、node (分店背景同步)、nodes (分店登記與權杖)、runtime (目前角色)
│   └── routes/                Blueprint：customer (點餐、結帳、我的訂單、語音)、auth、rewards、pos、kitchen、pickup (含 SSE)、
│                              orders、admin (後台首頁、員工帳號、備份還原、匯出匯入、TAB_PAGES 舊分頁導向)、menu_pro (/admin/menu)、
│                              members_pro (/admin/members)、payment、privacy、tables、sync_admin、media、settings_admin、security、
│                              finance (發票、退款、交班)、printing、inventory、promotions、reports (含菜單工程、異常偵測、會計匯出)、
│                              system、member_auth、delivery、reservations、timeclock (打卡、班表、可排時段、排班建議、自動排班)、
│                              public_extras (追蹤頁、PWA、/api/chat)、feedback、gift_cards、purchasing、live_dashboard、dispatch、
│                              driver、passes、table_pay、branches、marketing、integrations (API / Webhook、/developers、裝置、封存)、
│                              pnl、group_orders、api_v1、audit_log、tenants_admin、signage、health (/healthz)
├── migrations/versions/       0001 舊版基準、0002 重構、0003 同步引擎、0004 線上付款與個資同意、0005 桌位與掃碼點餐、0006 進階營運、
│                              0007 優惠券期限 / 批量發放、0008 優惠券防濫用、0009 總部設定 / 2FA / 登入裝置、0010 線上付款方式 /
│                              錢櫃 / 規格 / 會員條碼、0011 預約取餐 / 桌邊呼叫 / 評分 / 熱量、0012 集點卡 / 禮物卡 / 推薦、
│                              0013 採購 / 批號 / 時薪、0014 自有外送、0015 套餐訂單記錄配餐、0016 套餐任選 / 單品作廢、
│                              0017 平面圖 / 桌邊線上結帳 / 寄杯、0018 分店價格 / 叫貨調撥、0019 自動行銷 / 訂金提醒、
│                              0020 API / Webhook / 裝置 / 封存、0021 營業費用 / 成本歷史、0022 團體預購、0023 pickup_at 索引、
│                              0024 client_order_id、0025 接單控制 / 預估等候 / Web Push、0026 會員價 / 製作卡 / 評價回覆 / 會員 Email、
│                              0027 外送員 GPS / 碳足跡、0028 可排時段 / 每週工時上限
├── templates/                 全部繼承 base.html：admin.html (後台首頁三分頁)、_admin_sidebar.html、admin_menu.html、admin_members.html、
│                              對話框子樣板 _menu_modals / _member_modals / _coupon_modals / _data_modals / _pos_pro_modals.html、
│                              其餘 admin_*.html 各後台頁
├── static/css/、js/           theme.css、common.js 共用；admin.js (後台共用：頁籤記憶、實時訂單、總覽即時更新，提供 window.AdminPage)、
│                              admin-menu.js、admin-coupons.js、admin-members.js、admin-pro.js、settings-hub.js；pos.js 為 POS 進入點
│                              (ES module)，功能拆在 js/pos/ (core / menu / cart / member / totals / checkout / live / pro / shift / split)；
│                              customer.js 為點餐機進入點，功能拆在 js/customer/ (core / cart / totals / menu / checkout / live / voice)；
│                              js/shared/ 是點餐機與 POS 共用的純邏輯 (addons 加購配額、pricing 單價、cartline 購物車行、modifiers
│                              客製化視窗、totals 金額計算、orderpayload、promo、invoice、combos)，不碰 DOM；
│                              floor-plan、device、api-docs、kiosk-offline、kiosk-attract、chatbot、signage、driver、dispatch、track 等
│                              各頁腳本；img/ 預設圖片；vendor/ Bootstrap、Bootstrap Icons、Cropper.js (離線使用)
├── uploads/                   會員照片 (member/)、餐點照片 (menu/)；不放在 static/，由 /media/ 控管讀取 [不進版控]
├── database/                  menu.db、.secret_key、menu.db.bak-* (還原前另存)、caddy-root.crt (本機模擬)、backups/ (每日自動備份)、
│                              archive/ (封存檔，需備份)、updates/、logs/、system_events.jsonl [不進版控]
├── models/                    人臉辨識模型 (YuNet、SFace .onnx，自動下載) 與選用的 Qwen2.5 GGUF [都不進版控]
├── tests/                     所有測試 (檔名即測試主題)、support.py 共用輔助、conftest.py (RUN_ORDER / NEEDS)、
│                              js/shared_spec.mjs (node 執行的前端共用純邏輯測試)
├── deploy/                    gunicorn.conf.py、entrypoint.sh、Caddyfile、hub.env.example、pg_backup.sh / pg_restore_check.sh、
│                              windows/ (install_autostart.ps1、start_store.cmd、update_store.cmd)
├── Dockerfile、docker-compose.yml、.dockerignore、.github/workflows/tests.yml、.github/dependabot.yml、ruff.toml、mypy.ini、
│   pytest.ini (預設略過 e2e、stress)
├── .env.example (門市 / 單機範例)、.env、.env.hub [不進版控]、VERSION (YYYY.MM.DD)、README.txt、SYNC.txt、DEPLOY.txt、MANUAL_TEST.txt
├── requirements.txt、requirements-store.txt、requirements-dev.txt (見 3.2)、test_menu.xlsx (菜單匯入範例)
├── serve_windows.py           正式營業啟動 (Waitress)
└── run.py                     開發用啟動

8.2 開發慣例
- 換行格式：專案文字檔統一使用 Windows 換行 (CRLF)。
- 前端 (CSP 強制，tests/test_frontend_static_files.py 自動檢查)：
  - 不寫行內 <script> 或 onclick；事件用 data-action="名稱" 搭配 Kiosk.on(root, 'click', {名稱: fn})。頁面資料以
    <script type="application/json" id="page-data"> 嵌入，用 Kiosk.pageData() 讀取。不引用外部網址，第三方套件放 static/vendor/。
    模組載入完成前顧客就按下的 data-action 按鈕，common.js 會先記下、等 Kiosk.on 綁定後補跑一次 (點擊不會不見)；
    點餐機與 POS 的 <link rel="modulepreload"> 清單要涵蓋入口、頁面模組與 shared/ 的每個檔案 (新增模組時一併加入，有測試檢查)。
  - 動態 HTML 一律用 Kiosk.html`...` (自動跳脫) 或 textContent。API 用 Kiosk.api(url, {json})：自動帶 CSRF Token，失敗時丟出
    含後端訊息的錯誤。刪除確認用 <form data-confirm="…">；圖片備援用 <img data-fallback="預設圖網址">。
  - POS 與點餐機前端全部是 ES module (templates/pos.html、customer.html 以 type="module" 載入 js/pos.js、js/customer.js)：
    新增功能放進 js/pos/、js/customer/ 對應的模組並 export，其他模組以 import 使用；共用狀態只放 core.js 的 state (跨模組不可
    重新指定 let 變數)；每個檔案 500 行以內 (有測試檢查)。POS 進階功能在 pos/pro.js (主管核准 / 會員驗證 / LINE Pay / 補印 /
    開錢櫃 / 接單控制)、pos/shift.js (交班 / 儲值 / 禮物卡)、pos/split.js (作廢退款 / 分桌換桌)；對話框在 templates/_pos_pro_modals.html。
  - 點餐機與 POS 都要遵守的規則 (單價、加購配額、購物車行合併鍵、客製化視窗、金額順序、送後端的品項格式、優惠碼驗證、發票欄位、
    套餐可售判斷) 只寫在 js/shared/ 的純函式，兩邊的模組只負責讀寫 DOM；改規則時兩邊會一起變 (有測試檢查匯入與 500 行限制，
    tests/test_shared_js_logic.py 以 node 跑 tests/js/shared_spec.mjs 固定這些規則；改了規則請一併補案例)。
    各頁共用的小工具在 common.js 的 Kiosk：$ / modal / setText / toggleRow、store (localStorage JSON)、money、pad3 / pickupNo、
    ORDER_STATUS / PAYMENT_STATUS / statusBadge / paymentBadge、stockClass、takeoutBlock、setOrderStatus、
    onOrdersChanged (含備援輪詢)、pollJson (ETag)、beep；不要在各頁再寫一份。庫存徽章的黃色門檻只定義在
    menu_service.LOW_STOCK (樣板的 stock_badge 直接用，base.html 以 <meta name="low-stock"> 交給 Kiosk.stockClass)。
  - 模板共用巨集在 templates/_macros.html：modal_head (title_id 可指定標題 id)、stat_tile (KPI 卡)、screen_header (廚房 / 取餐 /
    櫃台看板頂欄)、pagination、stock_badge (庫存徽章，門檻與 Kiosk.stockClass 相同)。
  - 後台對話框與表單巨集依頁面拆在 templates/_menu_modals.html (菜單 / 套餐)、_member_modals.html (會員)、_coupon_modals.html
    (優惠券 / 兌換餐點)、_data_modals.html (匯出 / 匯入 / 備份)。新增後台頁面：在 app/services/admin_nav.py 的 NAV_GROUPS 加一行
    (條件 perm:xxx / role:xxx / platform、sync=hub|node|store)。
  - 提示訊息 flash(文字, 類別)：圖示由 _flashes.html 依類別 (success / danger / warning / info) 自動加上，文字不要再寫 ✅ ❌。
  - 顧客頁 (點餐機 / 手機看得到的樣板與 JS) 不寫死中文介面文字：樣板用 {{ ui.鍵 }}，JS 用 Kiosk.t('鍵', '中文備援') (點餐頁
    ES module 用 core.js 的 ui.鍵 || '中文')；鍵與中 / 英 / 日文加在 app/services/i18n_pages.py 的 PAGES，鍵名不可與 dict 方法
    同名 (clear、copy、items…)。送給後端的值 (order_type、冰塊 / 甜度選項…) 維持中文，只翻顯示文字。員工頁維持中文。
    tests/test_i18n_coverage.py 的 CUSTOMER_TEMPLATES 列出顧客頁樣板，新增顧客頁時一併加入。顧客頁的顏色：深底上的文字
    對比 4.5:1 以上 (axe-core 會擋嚴重 / 重大違規)；表單控制項要有 label 或 aria-label。
- 後端：
  - 錯誤一律 raise AppError(訊息, 狀態碼)：API 回 {"success": false, "message": …}；表單的 400/409 flash 後導回。訊息一律寫
    中文；顧客會看到的新訊息請在 i18n_pages.py 的 MESSAGES (整句) 或 MESSAGE_PATTERNS (帶品名 / 數量，具名群組) 補英 / 日文。
  - 權限用 @role_required(*MANAGER_ROLES) (角色組合定義在 app/models/enums.py：MANAGER_ROLES / POS_ROLES / KITCHEN_ROLES /
    ALL_STAFF_ROLES，不要在各檔自己寫元組)；整庫備份等用 @platform_admin_required。店長以上的後台頁面用
    @permission_required('menu' | 'coupons' | 'members' | 'feedback' | 'settings') (enums.PERMISSIONS，可個別授權給櫃檯)；
    只有負責人的仍用 role_required('owner')。
  - 查詢自動加上目前店家條件；系統工作需要跨店時用 with tenant_bypass():。路由以主鍵取單筆資料一律用 app/utils.py 的
    get_own(Model, id, '標籤') (不存在或其他店家 → 404) 或 find_own (回 None)，不要自己寫 db.session.get + tenant_id 比對
    (命中 identity map 時不經過租戶過濾)。
  - 結帳一律走 order_service.process_checkout，訂單狀態一律走 order_service.change_order_status (下層不可匯入上層，有測試檢查)；
    加減會員點數一律用 points_service.change_points (寫入點數異動帳)。
  - 系統設定：新區塊加在 settings_service.py (填 subgroup / keywords)，服務模組用 settings_service.reader('key') /
    flag('key', field) 取得 config() / enabled()，不要自己寫包裝函式。
  - 路由常用的輸入與回應都在 app/utils.py：parse_date (選填日期)、parse_date_range (?start / ?end / ?preset)、day_bounds
    (日期 → 半開 datetime 區間)、parse_local_dt、parse_hm / hm_minutes (結束時間可以是 24:00)、paginate、json_etag_response、
    workbook_bytes / send_xlsx、sha256_hex、
    current_staff_name、@store_only / @hub_only (門市 / 線上端限定)。會員頁用 @member_required。營收訂單條件用
    report_service.revenue_orders / is_revenue；點數與儲值金的原子加減共用 points_service.atomic_change。
  - 修改資料表：改 app/models/ → flask --app run db migrate → 檢查遷移檔 → 測試。遷移代號 (revision) 不可超過 32 個字
    (有測試檢查)。新資料表要同步：繼承 TenantScoped 並加入 app/sync/registry.py 的 SPECS (依外鍵順序)；本機欄位放 local_only。
  - 背景工作 / 會被同時觸發的動作先以條件式 UPDATE「搶」再處理 (例如 status pending → sending)，多個行程才不會重複執行。
  - Excel / CSV 匯出含使用者輸入的文字時，用 app/utils.py 的 neutralize_formulas / csv_safe (防公式注入)。
  - 送出前執行 ruff check . 與 mypy (CI 會擋)；新的 service 函式請加型別註記 (ORM 物件用 Any)；模組層級的 dict / tuple 要標
    型別；新模組請加進 mypy.ini 最下方的完整檢查清單 (check_untyped_defs = True)。
  - 記錄用 logging.getLogger(__name__)；每一行會自動帶請求識別碼，不要自己 print。新的稽核動作在 app/services/audit.py 的
    ACTION_LABELS 補顯示名稱。
  - 列表頁 / API 的查詢數要與資料量無關 (tests/test_ops_backup_update_metrics.py 的 N+1 守門)：關聯用 selectinload 一次載入。
  - 人臉辨識相關程式要能在沒有 opencv / numpy 時載入 (cv_service.FACE_AVAILABLE)。
  - 版本：發布新版時改 VERSION 檔 (YYYY.MM.DD) 並在 GitHub 建 release (tag 同版本)，門市才會看到「有新版本」。


九、權限與安全
--------------------------------------------------------------------
1. 角色：owner (負責人) 全部權限，含員工帳號；預設店家 (default) 的 owner 為平台管理者，才可整庫備份 / 還原、店家管理。
   manager (店長)：後台、菜單 / 優惠券 / 紅利、隱私權告知設定、匯出匯入、POS、出餐、核銷、取消訂單。staff (櫃台)：POS、
   會員設定 PIN、取餐核銷、當日訂單、出餐、取消未取餐的訂單；登入後直接進 POS (除非被授權後台頁面)。kitchen (廚房)：KDS，
   只能出餐；登入後直接進 KDS，看不到會員照片。額外權限與後台頁面權限見五-H。
2. CSRF：所有表單與 API 的 POST 都需要 CSRF Token；例外只有以權杖驗證的同步 API、以 API 金鑰驗證的 /api/v1、以檢查碼驗證的
   綠界通知 (線上付款、桌邊線上結帳、訂位訂金) 與以簽章驗證的外送平台 Webhook。登出、訪客登入只接受 POST。
3. 會員照片：只能經 /media/member/ 讀取，需登入同店員工 (廚房除外)；/static/member 一律 404。上傳檔路徑一律檢查格式並限制在
   上傳資料夾內 (app/services/upload_paths.py)，/media/menu 只提供圖片；上傳圖片有像素上限與格式白名單。
4. 頻率限制：登入、註冊、優惠碼、送單、禮物卡查餘額有 IP 頻率限制；員工登入與註冊新店家只以 IP 計數 (切換店家繞不過)。
   反向代理之後請設 TRUSTED_PROXY_COUNT=1。
5. 鎖定：會員 PIN 同裝置對同手機連錯 5 次鎖 15 分鐘、同手機累計錯 10 次鎖定 (每次加倍，上限 24 小時)、同 IP 累計錯 30 次鎖定；
   員工密碼同 IP 對同帳號連錯 5 次、同帳號累計錯 10 次 → 鎖 15 分鐘 (已註冊裝置不受帳號鎖定)；帳號 / 手機不存在時同樣計數且
   回應時間相同；新設定的 PIN 不可太簡單 (0000、1234、1212、手機末幾碼)；人臉登入後的敏感操作需再輸入 PIN。
6. 工作階段：登入時換新的工作階段；員工改密碼或角色後，舊的登入自動失效；兩步驟驗證與登入裝置管理見五-H。
7. 安全標頭：CSP (script-src 'self')、X-Content-Type-Options、X-Frame-Options、Referrer-Policy、Permissions-Policy；HTTPS 時加
   HSTS。付款頁只放寬 form-action 到綠界網域。
8. 個資：註冊須同意隱私權告知並記錄版本與時間；人臉另行同意、可撤回 (各分店的照片經同步一併刪除)；停用會員可定期刪除
   (訂單去識別化保留)。
9. 同步：HTTPS + 分店權杖 (只存雜湊)。分店只能修改自己負責的訂單、不能建立 / 修改 / 刪除負責人帳號、不能讀寫其他店家的圖片
   或資料；單次上傳有筆數上限 (詳見 SYNC.txt)。
10. 線上付款：已付款的訂單被取消一律標記「需退款」，由線上端自動退刷 (需開啟) 或店家到綠界後台退刷；綠界測試金鑰需
    ECPAY_ALLOW_TEST_KEYS=1 才可用；門市主機預設不開放公開建店。
11. 整庫備份 / 還原：只限平台管理者；大檔案上限只對已登入的平台管理者放寬；含觸發程序或檢視表的備份檔拒絕還原。
12. 匯入：Excel 解壓後超過 30MB 或超過 2 萬列拒絕 (防壓縮炸彈)；.xls 與非 zip 檔拒絕；匯出時以 = + - @ 開頭的文字一律當成文字。
13. 開發模式：FLASK_DEBUG=1 只監聽本機 (除錯頁可執行程式碼)。
14. 對外 API：金鑰只顯示一次 (資料庫存雜湊、不寫入 Cookie)，依權限範圍開放、只能讀寫自己店家的資料、每把每分鐘 120 次。
15. Webhook / 外連：只送 https 公開網際網路位址 (解析後每個 IP 都檢查，連線固定用檢查過的 IP，防 DNS 換址與內網存取)，每次
    送出附 HMAC-SHA256 簽章與時間戳；接收端應驗證簽章並以 X-Kiosk-Delivery 去除重複。監控通知、線上端的寄信 / 校時 / 印表機 /
    異地備份位址同樣不可指向內網。
16. 裝置：裝置權杖存在 HttpOnly Cookie (資料庫只存雜湊)；遠端鎖定的裝置送出的操作一律回 423。
17. 更新與模型檔：更新檔只從 github.com 下載且上限 300 MB，版本代號不接受 . / ..；更新腳本從複本執行、只殺伺服器本身；
    人臉模型下載後比對 SHA-256。


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
  {"client_order_id": "<前端產生的去重識別，重送沿用；訂單 uuid 由伺服器另外產生>",
   "items": [{"menu_item_id": 1, "quantity": 2, "modifier_ids": [3], "combo_option_id": null, "combo_choices": [任選配餐 id],
              "pass_id": null, "is_add_on": false, "is_reward": false, "customization": "備註"}],
   "payment_method": "Cash", "order_type": "內用", "promo_code": "", "use_points": 0, "note": ""}
  POS 另有 user_id、cash_received、table_number、payment_info {auth_code, card_last4, one_time_key, tenders (混合付款)}、
  invoice {type, carrier, love_code, buyer_id, buyer_name, email}、order_discount {type: amount|percent, value}、
  items[].override_price、is_rush。
  價格一律由後端計算；replayed: true 表示是重送的同一張訂單；線上端另回傳 payment_url。需主管核准的操作回應 403 +
  need_approval，帶 approver_username / approver_password 重送。被擋：409 intake_paused / intake_full (接單控制)、
  coupon_unavailable / points_changed (券或點數已被其他通路用掉，不會默默改用原價)、insufficient_gift；畫面金額與後端不同時
  要求確認後重送。

POS / 店員
  GET  /api/pos/today_orders (含未結帳桌位)、/api/kitchen_orders、/api/pickup_orders、/api/counter_orders、/api/orders_stream (SSE)
  POST /api/pos/tables/<id>/settle {payment_method, cash_received, order_ids (分開結帳), payments [{method, amount, ...}] (多種付款)}
  POST /api/pos/tables/<id>/move {to_table_id, order_ids} 換桌 / 併桌      GET /api/pos/floor 平面圖狀態
  POST /api/pos/orders/<id>/refund {amount, reason}、/void_item {item_id, quantity, reason, method}、/reprint、/rush
  GET  /api/pos/shift、POST /api/pos/shift/open | movement | close        POST /api/pos/wallet/topup {user_id, amount, payment_method}
  POST /api/pos/delivery_order {items, platform, external_ref}            POST /api/pos/passes/sell {user_id, product_id, payment_method}
  POST /api/kitchen/orders/<id>/station_done {station_id}                 GET /api/kitchen/prep_card/<id>
  POST /api/promotions/preview   促銷試算 (點餐頁 / POS 即時顯示)
顧客
  GET /api/eta、/api/menu/availability (ETag)、POST /api/reorder/<id>、/api/voice/parse、/api/chat、/api/gift_cards/balance、
  /api/member/marketing {opt_in}
  GET /track/<uuid>、/api/track/<uuid>、POST /api/track/<uuid>/push、GET /api/push/vapid     GET /api/table/bill、POST /api/table/pay
  POST /api/my_orders/<id>/receipt/email | line      POST /api/kiosk/offline_order {shop, code, queued_at, order} 斷線暫存補送
  (一律訪客 / 現金；重送回原訂單 replayed: true；店家不符 409、超過 3 小時 410)
  /shop/<店家>/group/new、/shop/<店家>/group/<token> (參加)、…/manage (主揪)、…/print (分裝清單)   團體預購
  /driver/<憑證>、POST /api/driver/<憑證>/position   外送員手機頁與位置回報
付款與外部
  /pay/<訂單>、/payment/ecpay/notify | result | info (線上端)；/payment/ecpay/table_notify | table_result、reservation_notify |
  reservation_result (門市)；/reserve/<代碼>/pay 訂位訂金 (每次開啟換新的交易編號)
  POST /webhooks/ubereats (X-Uber-Signature)、/webhooks/foodpanda/<店家代碼> (Bearer 權杖)   外送平台
  /api/sync/v1/hello、push、pull、media/menu/<路徑> (分店權杖，詳見 SYNC.txt)      GET /healthz、GET /metrics?token=
  /device/pair、POST /api/device/heartbeat {version, path} (回傳 locked / reload)
後台
  /admin/menu、/admin/members?q=&page=、/admin/audit、/admin/tenants、/admin/expenses?month=YYYY-MM (POST action=add | edit |
  delete | copy_fixed)、/admin/pnl?month= | start=&end= [&node=分店 uuid] [&format=xlsx]、/admin/cost-history?days=30&kind=ingredient|menu、
  /admin/group-orders、/admin/staffing?date=YYYY-MM-DD、/admin/staffing/auto
對外 API  /api/v1/... (Authorization: Bearer kk_...)
  GET orders?since=&after_id=&limit= (依建立時間；next 為下一頁參數)、GET orders?cursor=0 (異動追蹤，回傳 next_cursor；建議在
  線上端呼叫)、GET orders/<uuid>、GET menu (ETag)、POST menu/<id>/availability {sold_out, stock}、GET members/<手機>、
  GET reports/daily?date=
  完整說明 (參數、回應欄位、錯誤碼、Webhook 簽章範例、線上試打)：/developers；OpenAPI 3.0：/developers/openapi.json


十一、升級紀錄與從舊版升級
--------------------------------------------------------------------
從舊版升級 (任何版本 → 目前版本)
1. 先備份 database/menu.db、uploads/ (或舊的 static/member、static/menu)。
2. pip uninstall -y opencv-python 後 pip install -r requirements-store.txt (門市) 或 requirements-dev.txt (開發)。舊版曾安裝的
   pandas、opencv-python 已不需要；人臉模型檔第一次啟動自動下載 (或 flask --app run models download)。
3. 第一次啟動自動完成：升級資料庫 (0001 → 0028)、舊上傳檔搬到 uploads/、會員現有點數記為「期初」點數異動。
4. 舊檔案已不使用：app/routes/common.py、app/services/idempotency.py、app/services/migration_service.py、test_all_features.py、
   static/js/pos-pro.js、templates/admin_menu_pro.html、admin_members_pro.html、_admin_modals.html。
5. 環境變數 DISABLE_RATE_LIMIT 改為 RATELIMIT_ENABLED (0 = 停用)；門市不需要 Redis (只有線上端多行程部署使用)。
6. 升級前註冊的會員沒有個資同意紀錄：登入後點餐頁上方會請他閱讀並同意；原有人臉資料仍可使用，可在 /privacy 刪除。
7. 所有進階功能預設關閉或不影響原流程，到後台「系統設定」逐項啟用並填寫金鑰；隱私權告知版本 2026-10-23 (新增 LINE 綁定、
   生日、儲值金、電子發票載具等蒐集項目)，會員下次登入會再請他同意。

升級紀錄
- 2026-11-05：後台版面改版 (共用左側選單、總覽儀表板、/admin/menu「菜單與套餐」、/admin/members「會員」、系統設定兩欄與
  搜尋)；套件清單拆成 requirements / -store / -dev，移除 pandas (Excel 改用 openpyxl，.xls 不再支援)，人臉辨識改為選用且模型檔
  自動下載 (線上端映像不含人臉辨識)；POS 前端全部 ES module (pos-pro.js 併入 pos/pro.js、shift.js、split.js)；顧客端字串集中到
  i18n_pages.py；設定區塊帶 subgroup / keywords、服務改用 settings_service.reader / flag；AI 引擎重試與備援模型、語音點餐、
  AI 客服機器人、自動排班 (遷移 0028)、POS 桌號快捷鍵；外送員 GPS 追蹤與碳足跡 (遷移 0027)；數位看板；測試檔依主題重新命名。
  同功能合併：點餐機與 POS 的計價 / 加購 / 客製化 / 金額 / 優惠碼邏輯合成 js/shared/ (順帶修正點餐機促銷預覽漏送套餐任選與
  寄杯、優惠碼連按時舊回應蓋掉新回應、配餐售完的套餐仍可選、POS 加料順序不同不合併)；各頁小工具集中到 Kiosk；後台巨集
  (KPI 卡、看板頂欄、分頁、庫存徽章、對話框標頭)；路由共用的日期 / 分頁 / ETag / Excel 工具集中到 app/utils.py；綠界三組路由、
  兩種收據、點數 / 儲值金記帳、公開網址、sha256、建立時間欄位等各只留一份；POS 即時連線中斷時改為每 5 秒輪詢。
- 2026-10-31：AI 一鍵翻譯菜單、飲食篩選、點餐機禮物卡付款、會員價、電子收據、廚房製作卡、評價回覆與公開好評、後台頁面權限、
  異地備份、門市一鍵更新、Sentry / Prometheus、N+1 守門與 axe-core、設定匯出匯入、淺色模式、PWA 安裝按鈕、POS 常用備註
  (遷移 0026)；接單控制、預估等候時間、再點一次、售完即時更新、Web Push、稽核紀錄完整頁、店家管理、POS 條碼、記錄檔與
  X-Request-ID、靜態資源版本化、顧客端全面多語系、ruff / mypy / pip-audit / 涵蓋率 CI、Pillow 13.0.0 與 Werkzeug 3.1.9
  (遷移 0025)；修正百分比優惠券後的點數折抵上限 (改為四捨五入後的應付金額，點餐機 / POS / 後端一致)。
- 2026-10：進階營運功能 (電子發票、LINE Pay、交班、工作站 KDS、原物料、會員等級 / 儲值金 / LINE、外送平台、訂位候位、進階
  報表、打卡、總部設定、2FA…，遷移 0006 ～ 0024；需要 cryptography)；依負載測試加上 order.pickup_at 索引、看板每秒最多重抓一次。
