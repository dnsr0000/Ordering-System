====================================================================
智慧自助點餐、POS收銀、人臉辨識與多租戶營運系統 (Kiosk & POS System)
====================================================================

📝 專案簡介 (Project Overview)
本專案為一套專為現代餐飲連鎖與單店打造的全方位智慧門市營運系統。
系統架構全面支援「多租戶 (Multi-Tenant) 實體隔離」，整合了四大營運場景：
1. 顧客自助點餐機 (Kiosk)：支援 YuNet + SFace 近景最大人臉刷臉秒級登入、手機安全 PIN 碼登入、套餐客製化與三種加購配額池化管理。
2. 實體櫃檯 POS 收銀系統 (/pos)：觸控快速入單、暫存掛單 (F4)、會員手機模糊搜尋候選綁定、實收/找零對帳 (F2)、會員現場設 PIN 與 80mm 熱感應收據列印。
3. 廚房即時出單看板 (KDS /kitchen) 與 取餐叫號核銷 (/pickup, /counter)：多端長連線即時事件串流 (SSE) 同步，內建 Web Speech API 語音叫號。
4. 店家安全管理後台 (/admin)：RBAC 四級權限矩陣、AI 營運顧問、菜單與套餐 CRUD、員工帳號管理、Excel 10 工作表匯入匯出與稽核日誌。

🚀 核心技術與架構 (Technologies & Architecture)
- Backend: Flask 3.1+ (Application Factory 搭配 Blueprint 藍圖模組化職責分離設計)
- Multi-Tenancy: 租戶隔離中介層 (TenantScoped Mixin、ORM do_orm_execute 全域自動攔截、跨店 404 防禦)
- Concurrency & Locking: Redis 分散式互斥鎖 (acquire_distributed_lock) + 本地執行緒鎖安全降級，保障庫存扣減/回補與取餐流水號 (1~999) 之原子性
- Real-time Stream: SSE (Server-Sent Events) 搭配狀態簽章 (State Signature) 髒檢查與分級配額防護 (單 IP 10 條 / 公開 25 條 / 員工 60 條)
- Computer Vision: OpenCV (YuNet 最大人臉鎖定 + SFace 深度特徵提取 + NPF32v1 安全二進位特徵儲存 + NumPy 矩陣向量化比對)
- Security & RBAC: 全域 CSRF 雙向驗證、IP 頻率限制 (@rate_limit 429)、PIN 碼連續 5 次錯誤鎖定 15 分鐘、會員照片存取保護 (403)、純 POST 狀態變更防護
- Edge & Cloud AI: Google Gemini 3.6 Flash / 本地端 Qwen2.5 SLM (llama-cpp-python) / 啟發式規則引擎三層無縫降級
- Database: SQLite (SQLAlchemy 2.0，開啟 PRAGMA WAL 模式高並行讀寫分離、安全型別 SafeFeatureType、自動無失真遷移)
- Web Servers: Waitress (Windows 32 執行緒生產伺服器) / Python 內建排程服務

📂 專案架構圖 (Project Structure)
Project Root/
├── app/                                    # 核心應用程式套件 (Flask Package)
│   ├── __init__.py                         # 工廠函式 (create_app)、WAL Pragma 監聽、CSRF 全域攔截、照片 403 保護
│   ├── config.py                           # 集中設定檔 (環境變數、模型路徑、上傳目錄、資料庫 URI)
│   ├── extensions.py                       # 單例擴充元件 (db、Redis Client、分散式互斥鎖管理器、LLM_LOCK)
│   ├── tenant_context.py                   # 多租戶隔離中介層 (TenantScoped 請求解析、上下文注入與防跨店修改)
│   ├── utils.py                            # RBAC 角色裝飾器 (@role_required)、@rate_limit 限流器、safe_int 安全轉換
│   ├── models/                             # 資料庫 ORM 模型層 (具備 TenantScoped 隔離)
│   │   ├── __init__.py                     # 模型匯出模組
│   │   ├── tenant.py                       # 店家租戶 (Tenant)、員工帳號 (StaffAccount)
│   │   ├── user.py                         # 會員 (User)、管理員日誌 (AdminLog)、員工操作稽核 (StaffAuditLog)、顧客日誌 (CustomerLog)、紅利設定 (RewardSetting)、紅利憑證 (RewardRedemption)
│   │   ├── menu.py                         # 菜單品項 (MenuItem)、套餐組合 (ComboOption)、客製化選項 (ModifierOption)
│   │   ├── order.py                        # 訂單總覽 (Order)、訂單明細 (OrderItem)
│   │   └── coupon.py                       # 優惠券模板 (Coupon)、會員持有券 (UserCoupon)
│   ├── services/                           # 商業邏輯與演算法服務層
│   │   ├── __init__.py                     # 服務層匯出模組
│   │   ├── cv_service.py                   # 近景最大人臉偵測、特徵提取與矩陣向量化餘弦相似度比對
│   │   ├── ai_service.py                   # Gemini 3.6 Flash、Qwen2.5 SLM 推論與三層降級兜底演算法
│   │   ├── order_service.py                # 結帳防竄改驗證、原子扣庫存、全量回滾 (含主餐/配餐庫存與點數返還)、營運分析
│   │   ├── migration_service.py            # 資料庫遷移引擎 (舊 Pickle 特徵轉 NPF32v1、重複電話帳號自動合併、舊單補值)
│   │   └── event_bus.py                    # 即時事件總線 (OrderEventBus，支援配額淘汰與狀態特徵簽章)
│   └── routes/                             # 模組化路由控制器藍圖 (Blueprints)
│       ├── __init__.py                     # 路由藍圖模組
│       ├── customer.py                     # 前台自助點餐、優惠券驗證、原子送單、取餐號互斥發放 (/submit_order)
│       ├── auth.py                         # 會員註冊 (路徑穿越檔名清洗)、人臉登入 (/face_login)、手機 PIN 登入 (防列舉與防暴力破解)
│       ├── pos.py                          # 櫃檯收銀 POS 專用介面與原子結帳 API (實收/找零/會員候選清單/設PIN)
│       ├── rewards.py                      # 紅利商城、商品兌換憑證發放、單券限領驗證與防洗點全額退點
│       ├── kitchen.py                      # 廚房 KDS 出單看板、配餐明細合併、單筆出餐與一鍵出餐 API
│       ├── pickup.py                       # 取餐核銷介面、大螢幕即時叫號看板 (/counter) 與 SSE 串流推播
│       ├── admin.py                        # 店家管理後台、員工帳號 CRUD、菜單/套餐/紅利設定、Excel 10 表智慧匯入匯出
│       └── common.py                       # 顧客工作階段追蹤與 StaffAuditLog 員工操作日誌記錄器
├── database/                               # 資料庫專屬資料夾 (menu.db / WAL / SHM)
├── models/                                 # AI 模型專屬資料夾
│   ├── face_detection_yunet_2023mar.onnx   # 人臉偵測模型 (YuNet)
│   ├── face_recognition_sface_2021dec.onnx # 人臉特徵比對模型 (SFace)
│   └── qwen2.5-1.5b-instruct-q4_k_m.gguf   # 本地邊緣語言模型 (選配)
├── static/                                 # 靜態資源 (上傳之餐點圖；member/ 目錄受權限保護禁止未授權讀取)
├── templates/                              # 前端 HTML 模板 (admin, pos, customer, kitchen, counter, pickup, rewards, staff_accounts, tenant_register)
├── .env                                    # 環境變數設定檔 (REDIS_URL, API Key, 初始帳密等)
├── requirements.txt                        # 依賴套件清單 (版本鎖定)
├── serve_windows.py                        # Windows 生產環境啟動入口 (Waitress 32-thread 高併發伺服器)
├── run.py                                  # 開發除錯啟動腳本 (Flask Debug Server)
└── test_all_features.py                    # 涵蓋 13 大模組、65 項功能細節與並發安全自動化驗證測試腳本

💻 安裝與執行環境 (Environment & Setup)
1. 作業系統：Windows 11 / 10 / Linux (Ubuntu 22.04+) / macOS
2. Python 版本：Python 3.10+
3. 安裝相依套件：
   打開終端機執行：
   pip install -r requirements.txt

4. 啟動本機伺服器：
   - 【生產環境模式 (推薦)】：
     python serve_windows.py
   - 【日常開發除錯模式】：
     python run.py
   伺服器啟動後開啟瀏覽器訪問：http://127.0.0.1:5000/

5. 執行全功能與高併發安全自動化測試：
   python test_all_features.py
   (支援自訂壓測參數：python test_all_features.py --browse-users 60 --sse-streams 8 --checkout-users 30 --stock 10)

🛡️ 安全防護與權限控制架構 (Security & Access Control)
1. RBAC 四級權限矩陣：
   - owner (負責人)：全門市完整權限，專屬存取員工帳號建立、停用切換與密碼重設 (/admin/staff)。
   - manager (店長)：後台管理主介面、菜單/套餐/優惠券維護、紅利設定、Excel 匯入匯出、POS 收銀、KDS 廚房出餐與訂單取消。
   - staff (櫃檯收銀員)：專屬 POS 收銀點餐 (/pos)、協助會員設定 PIN、門市取餐核銷 (/pickup)、查閱當日即時訂單。禁止進入後台更改菜單與價格。
   - kitchen (廚房人員)：專屬 KDS 廚房製作看板 (/kitchen)，可執行出餐與取消。禁止存取 POS 收銀與後台設定。
2. 全域 CSRF 防護：所有狀態變更 (POST/PUT/DELETE) 強制校驗 Session Token，無 Token 或偽造 Token 一律回傳 HTTP 403。
3. 嚴格純 POST 狀態變更：刪除品項 (/admin/delete)、刪除套餐 (/admin/delete_combo)、下架紅利 (/admin/remove_reward)、刪除優惠券 (/admin/delete_coupon)、刪除會員 (/admin/delete_user) 僅允許 POST 呼叫，GET 呼叫一律回傳 405 Method Not Allowed。
4. 隱私與敏感檔案保護：/static/member/* 會員註冊人臉照片受中介層保護，未登入員工帳號之匿名請求一律回傳 403 攔截。
5. 頻率限制 (Rate Limit) 與防暴力破解：
   - 會員 PIN 碼連續錯誤 5 次自動鎖定該門市裝置 15 分鐘 (HTTP 429)。
   - 會員登入 (/phone_login, /face_login) 與註冊路由內建 IP 速率限制。
6. 分散式原子保護與防雙花 (Anti-Double-Spending)：
   - 結帳臨界區與訂單取消回滾使用 acquire_distributed_lock 互斥保護，杜絕庫存超賣與負數庫存。
   - 紅利商品兌換憑證 (RewardRedemption) 採用單次核銷制，嚴格攔截空手套白狼與重複退點。

🔑 核心功能指南 (Features Guide)

【顧客自助點餐端 - Kiosk & CV】
- 矩陣向量秒級登入：SFace 提取特徵向量以自訂 NPF32v1 安全二進位封裝存庫，登入時透過 NumPy 矩陣點積一次性計算餘弦相似度，避免迴圈比對延遲。
- 雙軌登入：支援人臉辨識登入、手機號碼 + 4~6 碼 PIN 碼登入，亦支援訪客快速免登點餐。
- 深度客製化與套餐：支援飲料冰塊甜度、辣度與加料，並能升級雙配餐套餐；後端具備防竄改驗證，自動清洗偽造之加價標籤。
- 條件加購配額池化：支援任意品項 (any)、指定分類 (category) 與指定餐點 (item) 三種解鎖規則，並嚴格依主餐數量計算加購上限。

【櫃檯 POS 收銀系統 - POS Terminal (/pos)】
- 會員快速模糊搜尋：支援手機號碼或姓名片段查詢，符合多筆時彈出候選清單供店員點選綁定；提供店員現場核對身分並為會員設定/重設安全 PIN 碼。
- 交易找零與快速鍵：支援實收現金防呆 (實收不足禁止送單)、自動找零試算 ($100/$500/$1000/+100 快捷鍵)、80mm 熱感應收據列印。
- 暫存掛單夾 (Hold/Recall)：可於尖峰時期暫存目前點餐內容 (快捷鍵 F4)，並可隨時取回接續結帳 (快捷鍵 F2)。
- 完整帳務追責：持久化記錄 order_source='POS'、收銀員帳號 (cashier_username)、實收金額與找零，並同步寫入 StaffAuditLog 稽核日誌。

【廚房出單 KDS、取餐叫號與跨端即時通訊】
- KDS 製作看板 (/kitchen)：依訂單建立時間依序排單，自動聚合相同配餐客製規格，支援單筆出餐與「一鍵全部出餐」。
- 叫號即時看板 (/counter)：提供大字體號碼牌與製作中清單，新單出餐時由瀏覽器 Web Speech API 自動播放中文叫號語音。
- 狀態機防禦與全量回滾：Pending 訂單禁止越級取餐；已取消 (Cancelled) 訂單禁止非法復活；取消訂單時透過分散式鎖原子回補主餐庫存、各配餐庫存、退還折抵點數/商品點數並扣回贈點。

【店家後台管理 - Admin Dashboard (/admin)】
- 營運分析儀表板：即時統計待製作單數、預估等待時間 (ETA)、客單價、營業額與區間熱銷排行榜。
- AI 智慧文案與三層韌性降級：菜單新增/編輯支援 Gemini 3.6 Flash 一鍵生成繁中商品描述；遇配額限制時自動降級為本地 Qwen2.5 SLM 推論，若無權重則走美食辭庫規則引擎。
- 報表與資料同步：Excel 匯出涵蓋 10 張工作表 (含訂單對帳、區間營運、熱銷排行與員工稽核日誌)，支援自適應中文字元欄寬；支援 Excel 菜單匯入與智慧欄位對齊 Upsert。
- 租戶註冊入口 (/tenant/register)：提供新分店自主申請專屬空間與代號 (Slug)，並自動初始化預設加料品項與紅利設定。

⚙️ 環境變數配置 (.env)
在專案根目錄建立 `.env` 檔案並填入以下內容：
REDIS_URL="redis://127.0.0.1:6379/0"      # Redis 伺服器位址 (未啟動時系統自動降級使用本地 Process 執行緒鎖)
GEMINI_API_KEY="你的_GEMINI_API_金鑰"      # Google AI Studio API Key (留空則自動降級為本地 SLM 或規則引擎)
SECRET_KEY="請使用_secrets_token_hex_產生長字串"
ADMIN_USERNAME="admin_test"               # 預設負責人帳號
ADMIN_PASSWORD="AdminPass!2026"           # 預設負責人密碼 (至少 10 碼)
DISABLE_RATE_LIMIT="0"                    # 設為 1 可於測試時停用 IP 限流 (預設 0 開啟保護)