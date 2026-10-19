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
- Concurrency & Locking: Redis 分散式互斥鎖 (acquire_distributed_lock) + 本地執行緒鎖安全降級，保障結帳時庫存扣減與取餐流水號 (1~999，接續當日最後一張訂單遞增，達 999 後回繞為 1) 之原子性；訂單狀態變更另以 SQL 條件式更新 (CAS) 防止併發重複處理
- Real-time Stream: SSE (Server-Sent Events) 搭配狀態簽章 (State Signature) 髒檢查與分級配額防護 (單 IP 10 條 / 公開 25 條 / 員工 60 條)
- Computer Vision: OpenCV (YuNet 最大人臉鎖定 + SFace 深度特徵提取 + NPF32v1 安全二進位特徵儲存 + NumPy 矩陣向量化比對)
- Security & RBAC: 全域 CSRF 雙向驗證、IP 頻率限制 (@rate_limit 429)、PIN 碼連續 5 次錯誤鎖定 15 分鐘、會員照片存取保護 (403)、純 POST 狀態變更防護、訂單狀態 API 權限優先於狀態判斷 (403 先於 409，無權限者無法探測訂單資訊)
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
│   │   ├── order_service.py                # 統一結帳 process_checkout (Kiosk/POS 共用)、促銷碼驗證、訂單狀態機 change_order_status (流轉表 + 角色矩陣 + CAS + 全量回滾)、紅利上下架、營運分析
│   │   ├── backup_service.py               # 完整系統備份包/還原、Excel 報表匯出、菜單匯入共用解析 (upsert_menu_items_from_df) 與欄位對映
│   │   ├── migration_service.py            # 資料庫遷移引擎 (舊 Pickle 特徵轉 NPF32v1、重複電話帳號自動合併、舊單補值)
│   │   └── event_bus.py                    # 即時事件總線 (OrderEventBus，支援配額淘汰與狀態特徵簽章)
│   └── routes/                             # 模組化路由控制器藍圖 (Blueprints)
│       ├── __init__.py                     # 路由藍圖模組
│       ├── customer.py                     # 前台自助點餐、優惠券驗證 (/api/verify_promo)、送單 (/submit_order，委派 order_service.process_checkout)
│       ├── auth.py                         # 會員註冊 (路徑穿越檔名清洗)、人臉登入 (/face_login)、手機 PIN 登入 (防列舉與防暴力破解)
│       ├── pos.py                          # 櫃檯收銀 POS 專用介面、結帳 (委派 process_checkout，附實收/找零/收銀員)、會員候選清單、設 PIN、優惠碼驗證
│       ├── rewards.py                      # 紅利商城、商品兌換憑證發放、單券限領驗證與防洗點全額退點
│       ├── kitchen.py                      # 廚房 KDS 出單看板、配餐明細合併、一鍵全部出餐 API (單筆出餐改走 /api/orders/<id>/status)
│       ├── pickup.py                       # 取餐核銷介面、大螢幕即時叫號看板 (/counter) 與 SSE 串流推播 (核銷改走 /api/orders/<id>/status)
│       ├── orders.py                       # 訂單狀態統一端點 POST /api/orders/<id>/status (廚房/櫃檯/POS/後台共用，角色矩陣 + CAS 併發防護)
│       ├── admin.py                        # 店家管理後台、員工帳號 CRUD、菜單/套餐/紅利設定、Excel 報表匯出、智慧匯入 (/admin/import_smart)、系統備份 (/admin/backup_system) 與還原
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
└── test_all_features.py                    # 涵蓋 13 大模組、77 項功能細節與並發安全自動化驗證測試腳本

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
   測試涵蓋 13 大模組、共 77 項：
     模組 1  系統初始化、資料庫遷移與多租戶隔離 (5)    模組 8  櫃檯 POS 收銀 (5)
     模組 2  資安防線、CSRF、RBAC、相片保護 (6)         模組 9  KDS、核銷、狀態機、角色矩陣、併發取消 (11)
     模組 3  菜單 CRUD、紅利上下架、加購規則 (3)        模組 10 營運分析、AI 降級、Excel 匯出與智慧匯入 (7)
     模組 4  套餐組合 (2)                               模組 11 頻率限制與 SSE 連線配額 (2)
     模組 5  會員註冊、人臉比對、PIN 鎖定 (5)           模組 12 高併發搶購與 HTTP/SSE 壓力測試 (5)
     模組 6  顧客端結帳、白名單、取餐號流水 (4)         模組 13 補強覆蓋：未測路由、越權、跨店隔離、CSRF 掃描 (18)
     模組 7  優惠券、紅利兌換、重複退點、複合折抵 (4)

6. 換行格式：專案文字檔統一使用 Windows 換行 (CRLF)。
   python convert_to_crlf.py --check    # 僅檢查，不修改任何檔案
   python convert_to_crlf.py            # 一鍵轉換 (可重複執行，自動略過 venv/.git 與二進位檔)

🛡️ 安全防護與權限控制架構 (Security & Access Control)
1. RBAC 四級權限矩陣：
   - owner (負責人)：全門市完整權限，專屬存取員工帳號建立、停用切換與密碼重設 (/admin/staff)，以及完整系統備份下載 (/admin/backup_system) 與還原 (/admin/restore_backup)。
   - manager (店長)：後台管理主介面、菜單/套餐/優惠券維護、紅利設定、Excel 報表匯出與智慧匯入、POS 收銀、出餐、取餐核銷與訂單取消 (含已取餐訂單之授權取消)。不可下載或還原完整系統備份。
   - staff (櫃檯收銀員)：專屬 POS 收銀點餐 (/pos)、協助會員設定 PIN、門市取餐核銷 (/pickup)、查閱當日即時訂單，可出餐與取消尚未取餐的訂單，但不可取消已取餐訂單。禁止進入後台更改菜單與價格。
   - kitchen (廚房人員)：專屬 KDS 廚房製作看板 (/kitchen)，僅可執行出餐 (Pending → Completed)，不可取餐核銷或取消訂單。禁止存取 POS 收銀與後台設定。
2. 全域 CSRF 防護：所有狀態變更 (POST/PUT/DELETE) 強制校驗 Session Token，無 Token 或偽造 Token 一律回傳 HTTP 403。
3. 嚴格純 POST 狀態變更：刪除品項 (/admin/delete)、刪除套餐 (/admin/delete_combo)、下架紅利 (/admin/remove_reward)、刪除優惠券 (/admin/delete_coupon)、刪除會員 (/admin/delete_user) 僅允許 POST 呼叫，GET 呼叫一律回傳 405 Method Not Allowed。
4. 隱私與敏感檔案保護：/static/member/* 會員註冊人臉照片受中介層保護，未登入員工帳號之匿名請求一律回傳 403 攔截。
5. 頻率限制 (Rate Limit) 與防暴力破解：
   - 會員 PIN 碼連續錯誤 5 次自動鎖定該門市裝置 15 分鐘 (HTTP 429)。
   - 會員登入 (/phone_login, /face_login) 與註冊路由內建 IP 速率限制。
6. 分散式原子保護與防雙花 (Anti-Double-Spending)：
   - 結帳臨界區 (庫存扣減、取餐號發放) 使用 acquire_distributed_lock 互斥保護，杜絕庫存超賣與負數庫存。
   - 訂單狀態變更使用 SQL 條件式更新 (CAS：UPDATE ... WHERE status=當前狀態)：同一張訂單被同時操作時只有一次成功，其餘回 409，庫存與點數不會重複回補。
   - 紅利商品兌換憑證 (RewardRedemption) 採用單次核銷制，嚴格攔截空手套白狼與重複退點。
7. 訂單狀態變更權限與錯誤碼：詳見下方「訂單狀態 API 與權限矩陣」。

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
- 狀態機防禦與全量回滾：Pending 禁止越級取餐、Cancelled 禁止復活、任何狀態不可倒退回 Pending；取消訂單時在同一交易內回補主餐庫存與各配餐庫存、退還折抵點數與兌換商品點數、還原優惠券並扣回贈點，且只會回補一次 (CAS 防重複)。

【店家後台管理 - Admin Dashboard (/admin)】
- 營運分析儀表板：即時統計待製作單數、預估等待時間 (ETA)、客單價、營業額與區間熱銷排行榜。
- AI 智慧文案與三層韌性降級：菜單新增/編輯支援 Gemini 3.6 Flash 一鍵生成繁中商品描述；遇配額限制時自動降級為本地 Qwen2.5 SLM 推論，若無權重則走美食辭庫規則引擎。
- 報表匯出 (/admin/export_excel)：涵蓋 10 張工作表 (含訂單對帳、區間營運、熱銷排行與員工稽核日誌)，支援自適應中文字元欄寬；本端點只負責報表，不含備份。
- 智慧匯入 (/admin/import_smart，唯一匯入入口)：可上傳單一 CSV 菜單，或含會員/菜單/優惠券的多工作表 Excel，自動辨識工作表與中英文欄位並 Upsert；採單一交易，任一工作表解析失敗即整批 rollback；既有品項在檔案未提供庫存欄位時不覆蓋庫存與售完狀態，庫存恆不大於總量；紅利兌換點數高於單價、點數為 0 或特惠點數不合法時自動修正。
- 系統備份與還原 (僅 owner)：/admin/backup_system 下載含 SQLite 資料庫與會員/餐點照片的完整備份包，/admin/restore_backup 可選擇還原資料庫與相片。
- 租戶註冊入口 (/tenant/register)：提供新分店自主申請專屬空間與代號 (Slug)，並自動初始化預設加料品項與紅利設定。

【訂單狀態 API 與權限矩陣】
- 唯一端點：POST /api/orders/<order_id>/status    (需登入員工帳號與 CSRF Token)
  請求內容 (JSON)：{"status": "Completed" | "PickedUp" | "Cancelled"}
  廚房 KDS、櫃檯取餐、POS 與後台的狀態操作都使用此端點；另有 POST /api/kitchen_complete_all 供廚房一鍵全部出餐。
- 狀態流轉：Pending → Completed → PickedUp；Pending / Completed / PickedUp 皆可轉為 Cancelled；
  Cancelled 為終態，不可倒退，且沒有任何流程可以轉回 Pending。
- 角色轉換權限 (O 允許 / X 禁止)：

  狀態轉換                    owner   manager   staff   kitchen 
  Pending → Completed           O        O        O        O    
  Pending → Cancelled           O        O        O        X    
  Completed → PickedUp          O        O        O        X    
  Completed → Cancelled         O        O        O        X    
  PickedUp → Cancelled          O        O        X        X    

- 回應碼：
    200  成功
    400  不支援的狀態值
    401  尚未登入
    403  權限不足：該角色無權執行此轉換。訊息不帶出訂單目前狀態；無權限者不論訂單是否存在、處於何種狀態，回應皆相同
    404  查無此訂單 (含他店訂單，跨店隔離)
    409  非法轉換 (重複操作、Cancelled 復活、無任何流程可轉入的狀態)，或訂單已被其他操作者變更 (CAS 併發保護)
- 判定順序：400 (狀態值) → 409 (目標不可達) → 403 (角色) → 404 (訂單) → 409 (重複/非法轉換) → 403 (細部轉換權限) → 409 (CAS)。

【重構與 API 遷移備註 (供維護與串接參考)】
- 共用邏輯：Kiosk (/submit_order) 與 POS (/api/pos/checkout) 共用 order_service.process_checkout；促銷碼驗證共用 verify_promo_code；
  PIN 規則集中於 app.utils.validate_pin 與 User.set_pin；紅利上下架 (新增、下架、編輯) 集中於 set_item_reward_status。
- 已移除的舊端點與替代方式：
    /api/kitchen_update_status/<id>、/api/mark_picked_up/<id>、/api/pos/order_status/<id>、/admin/update_order_status/<id>
        → POST /api/orders/<id>/status
    /admin/import_excel                                  → /admin/import_smart
    /admin/export_excel (export_type=backup_zip)         → /admin/backup_system (僅 owner)
- 取餐號：接續當日最後一張訂單遞增 ((最後號碼 % 999) + 1)。舊版以當日最大值計算，達 999 後會永遠卡在 1。

⚙️ 環境變數配置 (.env)
在專案根目錄建立 `.env` 檔案並填入以下內容：
REDIS_URL="redis://127.0.0.1:6379/0"      # Redis 伺服器位址 (未啟動時系統自動降級使用本地 Process 執行緒鎖)
GEMINI_API_KEY="你的_GEMINI_API_金鑰"      # Google AI Studio API Key (留空則自動降級為本地 SLM 或規則引擎)
SECRET_KEY="請使用_secrets_token_hex_產生長字串"
ADMIN_USERNAME="admin_test"               # 預設負責人帳號
ADMIN_PASSWORD="AdminPass!2026"           # 預設負責人密碼 (至少 10 碼)
DISABLE_RATE_LIMIT="0"                    # 設為 1 可於測試時停用 IP 限流 (預設 0 開啟保護)