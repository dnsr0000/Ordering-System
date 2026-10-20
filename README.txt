====================================================================
智慧自助點餐、POS收銀、人臉辨識與多租戶營運系統 (Kiosk & POS System)
====================================================================

📝 專案簡介 (Project Overview)
本專案為一套專為現代餐飲連鎖與單店打造的全方位智慧門市營運系統。
系統架構全面支援「多租戶 (Multi-Tenant) 實體隔離」，整合了四大營運場景：
1. 顧客自助點餐機 (Kiosk)：支援 YuNet + SFace 近景最大人臉刷臉秒級登入、手機安全 PIN 碼登入、套餐客製化與三種加購配額池化管理。
2. 實體櫃檯 POS 收銀系統 (/pos)：觸控快速入單、暫存掛單 (F4)、會員手機模糊搜尋候選綁定、實收/找零對帳 (F2)、會員現場設 PIN 與 80mm 熱感應收據列印。
3. 廚房即時出單看板 (KDS /kitchen) 與 取餐叫號核銷 (/pickup, /counter)：多端長連線即時事件串流 (SSE) 同步，內建 Web Speech API 語音叫號。
4. 店家安全管理後台 (/admin)：RBAC 四級權限矩陣、即時營運分析 (預估出餐依實際平均出餐時間、熱門餐點排行含銷售額)、AI 營運顧問、菜單與套餐 CRUD、員工帳號管理、Excel 10 工作表匯入匯出、稽核日誌，以及每日換日自動結案前一日未處理的訂單。

🚀 核心技術與架構 (Technologies & Architecture)
- Backend: Flask 3.1+ (Application Factory 搭配 Blueprint 藍圖模組化職責分離設計)
- Multi-Tenancy: 租戶隔離中介層 (TenantScoped Mixin、ORM do_orm_execute 全域自動攔截、跨店 404 防禦)
- Concurrency & Locking: Redis 分散式互斥鎖 (acquire_distributed_lock) + 本地執行緒鎖安全降級 (啟動時沒有 Redis、或執行中 Redis 中斷都會自動改用「依鎖名稱區分」的本地鎖，結帳不受影響)，保障結帳時庫存扣減與取餐流水號 (1~999，接續當日最後一張訂單遞增，達 999 後回繞為 1) 之原子性；訂單狀態變更、兌換憑證核銷/退點皆以 SQL 條件式更新 (CAS) 防止併發重複處理；顧客送單另有冪等保護 (連按/重送只建一張單)
- Real-time Stream: SSE (Server-Sent Events) 搭配狀態簽章 (State Signature) 髒檢查與分級配額防護 (單 IP 10 條 / 公開 25 條 / 員工 60 條)；超過配額被淘汰的連線會收到結束訊號並釋放伺服器執行緒，瀏覽器自動重連
- Computer Vision: OpenCV (YuNet 最大人臉鎖定 + SFace 深度特徵提取 + NPF32v1 安全二進位特徵儲存 + NumPy 矩陣向量化比對)
- Security & RBAC: 全域 CSRF 雙向驗證、IP 頻率限制 (@rate_limit 429，來源 IP 預設不信任 X-Forwarded-For)、PIN 碼三層鎖定 (裝置/帳號/IP)、會員照片存取保護 (路徑正規化 + 跨店隔離)、安全標頭與內容安全政策 (CSP)、純 POST 狀態變更防護、訂單狀態 API 權限優先於狀態判斷 (403 先於 409，無權限者無法探測訂單資訊)
- Edge & Cloud AI: Google Gemini 3.6 Flash (API 金鑰只放在請求標頭，不出現在網址) / 本地端 Qwen2.5 SLM (llama-cpp-python，選用) / 啟發式規則引擎三層無縫降級
- Database: SQLite (SQLAlchemy 2.0，開啟 PRAGMA WAL 模式高並行讀寫分離、安全型別 SafeFeatureType、自動無失真遷移)
- Web Servers: Waitress (生產伺服器 serve_windows.py，預設 128 執行緒，可用 WAITRESS_THREADS 調整；每條 SSE 長連線占用 1 條執行緒，必須大於 SSE 連線上限) / Flask 內建伺服器 (開發除錯 run.py)

📂 專案架構圖 (Project Structure)
Project Root/
├── app/                                    # 核心應用程式套件 (Flask Package)
│   ├── __init__.py                         # 工廠函式 (create_app)、WAL Pragma 監聽、CSRF 全域攔截、照片 403 保護 (路徑正規化 + 跨店隔離)、安全標頭與 CSP、反向代理信任設定 (TRUSTED_PROXY_COUNT)、每日首個請求觸發的換日自動結案 (before_request)
│   ├── config.py                           # 集中設定檔 (環境變數、模型路徑、上傳目錄、資料庫 URI、SECRET_KEY 自動產生並持久化、Cookie/CSP/執行緒數設定)
│   ├── extensions.py                       # 單例擴充元件 (db、Redis Client、分散式互斥鎖管理器：Redis 中斷自動降級為依名稱區分的本地鎖、LLM_LOCK)
│   ├── tenant_context.py                   # 多租戶隔離中介層 (TenantScoped 請求解析、上下文注入與防跨店修改)
│   ├── utils.py                            # RBAC 角色裝飾器 (@role_required、@platform_admin_required)、@rate_limit 限流器、safe_int/safe_float/safe_round 安全轉換、get_json_dict、姓名遮罩 (mask_name)、js_alert_redirect (已跳脫 </script>)
│   ├── models/                             # 資料庫 ORM 模型層 (具備 TenantScoped 隔離)
│   │   ├── __init__.py                     # 模型匯出模組
│   │   ├── tenant.py                       # 店家租戶 (Tenant)、員工帳號 (StaffAccount)
│   │   ├── user.py                         # 會員 (User，特徵向量固定 128 維並驗證)、管理員日誌 (AdminLog)、員工操作稽核 (StaffAuditLog)、顧客日誌 (CustomerLog)、紅利設定 (RewardSetting，每店唯一)、紅利憑證 (RewardRedemption)
│   │   ├── menu.py                         # 菜單品項 (MenuItem)、套餐組合 (ComboOption)、客製化選項 (ModifierOption)
│   │   ├── order.py                        # 訂單總覽 (Order，記錄實際套用的優惠碼與核銷的會員券 ID)、訂單明細 (OrderItem)
│   │   └── coupon.py                       # 優惠券模板 (Coupon)、會員持有券 (UserCoupon)
│   ├── services/                           # 商業邏輯與演算法服務層
│   │   ├── __init__.py                     # 服務層匯出模組
│   │   ├── cv_service.py                   # 近景最大人臉偵測、特徵提取與矩陣向量化餘弦相似度比對 (略過維度不符的壞資料)、上傳圖片像素/格式限制
│   │   ├── ai_service.py                   # Gemini 3.6 Flash、Qwen2.5 SLM 推論 (選用) 與三層降級兜底演算法
│   │   ├── order_service.py                # 統一結帳 process_checkout (Kiosk/POS 共用，擋售完餐點、金額四捨五入一致)、促銷碼驗證、訂單狀態機 change_order_status (流轉表 + 角色矩陣 + CAS + 全量回滾)、紅利上下架、營運分析 build_order_analytics (預估出餐、平均出餐時間排除極端值 mean_without_outliers、熱門排行含銷售額)、換日自動結案 auto_close_stale_orders / ensure_daily_auto_close
│   │   ├── voucher_service.py              # 紅利兌換憑證的原子狀態轉換 (核銷/退點皆為條件式 UPDATE，杜絕「餐點拿了、點數也退了」)
│   │   ├── idempotency.py                  # 送單冪等快取 (連按/網路重送只建立一張訂單)
│   │   ├── backup_service.py               # 完整系統備份包/還原 (SQLite 一致性快照、還原前驗證與自動另存、還原後即時遷移、Windows 檔案占用時原地還原)、Excel 報表匯出 (防公式注入)、菜單匯入共用解析 (upsert_menu_items_from_df) 與欄位對映
│   │   ├── migration_service.py            # 資料庫遷移引擎 (舊 Pickle 特徵轉 NPF32v1 且無法解析者保留原始資料、重複電話帳號自動合併、舊單補值、紅利設定去重與唯一索引)
│   │   └── event_bus.py                    # 即時事件總線 (OrderEventBus，支援配額淘汰 (淘汰時送結束訊號) 與狀態特徵簽章)
│   └── routes/                             # 模組化路由控制器藍圖 (Blueprints)
│       ├── __init__.py                     # 路由藍圖模組
│       ├── customer.py                     # 前台自助點餐、優惠券驗證 (/api/verify_promo，含限流)、送單 (/submit_order，委派 order_service.process_checkout，含冪等保護)、我的訂單 (訪客以進場紀錄歸戶)
│       ├── auth.py                         # 會員註冊 (路徑穿越檔名清洗、擋重複人臉、手機重複競態處理)、人臉登入 (/face_login)、手機 PIN 登入 (防列舉、裝置/帳號/IP 三層鎖定)
│       ├── pos.py                          # 櫃檯收銀 POS 專用介面、結帳 (委派 process_checkout，附實收/找零/收銀員)、會員候選清單、設 PIN、優惠碼驗證
│       ├── rewards.py                      # 紅利商城、商品兌換憑證發放、單券限領驗證與防洗點全額退點
│       ├── kitchen.py                      # 廚房 KDS 出單看板、配餐明細合併、一鍵全部出餐 API (含稽核；單筆出餐改走 /api/orders/<id>/status)
│       ├── pickup.py                       # 取餐核銷介面、大螢幕即時叫號看板 (/counter，公開，會員姓名遮罩) 與 SSE 串流推播 (核銷改走 /api/orders/<id>/status)
│       ├── orders.py                       # 訂單狀態統一端點 POST /api/orders/<id>/status (廚房/櫃檯/POS/後台共用，角色矩陣 + CAS 併發防護)
│       ├── admin.py                        # 店家管理後台、員工帳號 CRUD、菜單/套餐/紅利設定 (編輯庫存/點數以差額套用)、Excel 報表匯出、智慧匯入 (/admin/import_smart)、系統備份 (/admin/backup_system，僅平台管理者) 與還原
│       └── common.py                       # 顧客工作階段追蹤、登入/登出自動退點 (原子)、StaffAuditLog 員工操作日誌記錄器、紅利設定 get_or_create
├── database/                               # 資料庫專屬資料夾 (menu.db / WAL / SHM / 還原前自動另存的 menu.db.bak-* / 自動產生的 .secret_key)
├── models/                                 # AI 模型專屬資料夾
│   ├── face_detection_yunet_2023mar.onnx   # 人臉偵測模型 (YuNet)
│   ├── face_recognition_sface_2021dec.onnx # 人臉特徵比對模型 (SFace)
│   └── qwen2.5-1.5b-instruct-q4_k_m.gguf   # 本地邊緣語言模型 (選配)
├── static/                                 # 靜態資源 (上傳之餐點圖；member/ 目錄受權限保護禁止未授權讀取)
├── templates/                              # 前端 HTML 模板 (admin, pos, customer, register, rewards, kitchen, counter, pickup, staff_accounts, tenant_register)
├── .env                                    # 環境變數設定檔 (REDIS_URL, API Key, 初始帳密、反向代理層數、Cookie/CSP/執行緒設定等，詳見下方「環境變數配置」)
├── .gitignore                              # 版控排除清單 (.env、資料庫與 WAL、database/.secret_key、上傳相片、備份/匯出檔、模型權重)
├── requirements.txt                        # 依賴套件清單 (版本鎖定；llama-cpp-python 為選用，預設註解)
├── serve_windows.py                        # Windows 生產環境啟動入口 (Waitress 高併發伺服器，執行緒數由 WAITRESS_THREADS 決定，預設 128)
├── run.py                                  # 開發除錯啟動腳本 (Flask Debug Server)
└── test_all_features.py                    # 涵蓋 16 大模組、116 項功能細節、資安修補回歸與並發安全自動化驗證測試腳本

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
   (全部約 20~100 秒，視機器而定；測試使用暫存資料庫與暫存相片目錄，不會動到實際的 database/ 與 static/)
   測試涵蓋 16 大模組、共 116 項：
     模組 1  系統初始化、資料庫遷移與多租戶隔離 (6)    模組 8  櫃檯 POS 收銀 (5)
     模組 2  資安防線、CSRF、RBAC、相片保護 (6)         模組 9  KDS、核銷、狀態機、角色矩陣、併發取消 (11)
     模組 3  菜單 CRUD、紅利上下架、加購規則 (3)        模組 10 營運分析、AI 降級、Excel 匯出與智慧匯入 (7)
     模組 4  套餐組合 (2)                               模組 11 頻率限制與 SSE 連線配額 (2)
     模組 5  會員註冊、人臉比對、PIN 鎖定 (5)           模組 12 高併發搶購與 HTTP/SSE 壓力測試 (5)
     模組 6  顧客端結帳、白名單、取餐號流水 (4)         模組 13 補強覆蓋：未測路由、越權、跨店隔離、CSRF 掃描 (18)
     模組 7  優惠券、紅利兌換、重複退點、複合折抵 (4)   模組 14 高風險修補回歸：特徵維度、IP 偽造與帳號鎖定、備份隔離與還原安全、XSS、憑證原子性 (9)
     模組 15 中低風險修補回歸：結帳取消、冪等、後台表單、隱私、註冊上傳、安全標頭、鎖與設定、稽核精確記錄、營運分析口徑、換日自動結案、預估出餐排除極端值、熱門排行銷售額 (27)
     模組 16 舊版備份還原後即時升級 (含 Windows 檔案占用備援) (2)  ※ 會以舊結構資料庫取代目前資料庫，因此固定最後執行

6. 換行格式：專案文字檔統一使用 Windows 換行 (CRLF)。

🛡️ 安全防護與權限控制架構 (Security & Access Control)
1. RBAC 四級權限矩陣：
   - owner (負責人)：全門市完整權限，專屬存取員工帳號建立、停用切換與密碼重設 (/admin/staff)；預設店家 (default) 的 owner 另為「平台管理者」，才可使用完整系統備份下載 (/admin/backup_system) 與還原 (/admin/restore_backup)。
   - manager (店長)：後台管理主介面、菜單/套餐/優惠券維護、紅利設定、Excel 報表匯出與智慧匯入、POS 收銀、出餐、取餐核銷與訂單取消 (含已取餐訂單之授權取消)。不可下載或還原完整系統備份。
   - staff (櫃檯收銀員)：專屬 POS 收銀點餐 (/pos)、協助會員設定 PIN、門市取餐核銷 (/pickup)、查閱當日即時訂單，可出餐與取消尚未取餐的訂單，但不可取消已取餐訂單。禁止進入後台更改菜單與價格。
   - kitchen (廚房人員)：專屬 KDS 廚房製作看板 (/kitchen)，僅可執行出餐 (Pending → Completed)，不可取餐核銷或取消訂單。禁止存取 POS 收銀與後台設定。
2. 全域 CSRF 防護：所有狀態變更 (POST/PUT/DELETE) 強制校驗 Session Token，無 Token 或偽造 Token 一律回傳 HTTP 403。
3. 嚴格純 POST 狀態變更：刪除品項 (/admin/delete)、刪除套餐 (/admin/delete_combo)、下架紅利 (/admin/remove_reward)、刪除優惠券 (/admin/delete_coupon)、刪除會員 (/admin/delete_user) 僅允許 POST 呼叫，GET 呼叫一律回傳 405 Method Not Allowed。
4. 隱私與敏感檔案保護：
   - /static/member/* 會員註冊人臉照片受中介層保護：路徑先正規化再比對 (./、大小寫變體都無法繞過)；未登入一律 403，已登入的員工也只能讀取「自己店家」會員的照片。
   - 公開叫號看板 (/counter、/api/counter_orders) 不需登入，會員姓名一律遮罩 (王小明 → 王○明)；訪客的「我的訂單」以本次進場紀錄歸戶，不會因訪客代號撞號看到別人的訂單。
   - 註冊/刷臉等免登入上傳端點限制圖片像素數 (2,500 萬) 與格式 (JPEG/PNG/WEBP)，防止大圖耗盡記憶體。
5. 頻率限制 (Rate Limit) 與防暴力破解：
   - 來源 IP 預設以實際連線位址為準，不信任 X-Forwarded-For (見 .env 的 TRUSTED_PROXY_COUNT)，因此無法偽造 IP 繞過限流。
   - 會員 PIN 碼三層鎖定 (HTTP 429)：同一裝置對同一手機連續錯 5 次鎖 15 分鐘；同一手機「不分來源 IP」累計錯 10 次鎖定，且每次再鎖時間加倍 (上限 24 小時)；同一 IP 對任意手機累計錯 30 次鎖定。空號與已註冊號碼採相同計數，無法藉鎖定行為列舉會員。櫃檯協助設定/重設 PIN 時會一併解除該會員的鎖定。
   - 會員登入 (/phone_login, /face_login)、註冊、優惠碼驗證 (/api/verify_promo、/api/pos/verify_promo) 與送單路由內建 IP 速率限制。
6. 分散式原子保護與防雙花 (Anti-Double-Spending)：
   - 結帳臨界區 (庫存扣減、取餐號發放) 使用 acquire_distributed_lock 互斥保護，杜絕庫存超賣與負數庫存。
   - 訂單狀態變更使用 SQL 條件式更新 (CAS：UPDATE ... WHERE status=當前狀態)：同一張訂單被同時操作時只有一次成功，其餘回 409，庫存與點數不會重複回補。
   - 紅利商品兌換憑證 (RewardRedemption) 採用單次核銷制：結帳核銷、手動退點、登入/登出自動退點全部是「單一條件式 UPDATE」，同一張憑證只有一方能成功，不會出現「餐點拿了、點數也退了」。
   - 顧客送單具備冪等保護：連按兩次「送出」或網路重送 (瀏覽器尚未收到第一次回應) 只會建立一張訂單；成功後刻意再點同樣的餐則是新訂單。
   - 取消訂單只還原「本單實際核銷」的那一張會員券 (訂單記錄 applied_user_coupon_id)；點數以原子 SQL 回沖，會員餘額不足以追回贈點時餘額最低到 0，並把追不回的點數寫入稽核紀錄。
7. 訂單狀態變更權限與錯誤碼：詳見下方「訂單狀態 API 與權限矩陣」。
8. 輸出與瀏覽器端防護：
   - 所有 HTML 回應帶 Content-Security-Policy (只允許本站、jsDelivr、Google Fonts、Unsplash)、X-Content-Type-Options、X-Frame-Options、Referrer-Policy、Permissions-Policy (僅本站可用相機)；HTTPS 連線另帶 HSTS。CSP 模式可由 .env 的 CSP_MODE 調整 (enforce / report-only / off)。
   - 廚房、取餐、叫號、顧客端畫面的動態內容一律 escapeHtml；後台警示頁面 (js_alert_redirect) 會跳脫 </script>。
   - Excel 匯出不會把以 "=" 開頭的姓名/備註當成公式。
9. 帳號與金鑰：
   - SECRET_KEY 未設定時，首次啟動自動產生 64 字元隨機金鑰存入 database/.secret_key (已列入 .gitignore)，重啟沿用同一把；不再使用寫死在原始碼的公開金鑰。
   - 預設負責人帳號只在「首次建立」時使用 .env 的 ADMIN_PASSWORD (未設定則產生隨機密碼並只顯示一次)；之後重啟不會洗掉負責人自行修改的密碼或停用狀態。忘記密碼時可暫時設定 RESET_ADMIN_PASSWORD_ON_START=1 重啟一次重設。
   - 稽核日誌涵蓋：POS 建單、取消訂單、取餐核銷、會員編輯 (只記錄真正變動的欄位：姓名/手機/點數/重設 PIN，沒有異動就不寫)/刪除、設定 PIN、菜單/員工異動，以及優惠券異動、套餐異動、紅利設定異動、會員券核銷切換、廚房一鍵出餐、系統換日自動結案。稽核紀錄一定歸屬明確的店家，缺租戶資訊時直接報錯而不是記到別家。

🔑 核心功能指南 (Features Guide)

【顧客自助點餐端 - Kiosk & CV】
- 矩陣向量秒級登入：SFace 提取特徵向量以自訂 NPF32v1 安全二進位封裝存庫，登入時透過 NumPy 矩陣點積一次性計算餘弦相似度，避免迴圈比對延遲。
- 雙軌登入：支援人臉辨識登入、手機號碼 + 4~6 碼 PIN 碼登入，亦支援訪客快速免登點餐。註冊時會擋下「已註冊過的同一張臉」(相似度 ≥ 0.45)，避免用不同手機重複領註冊禮點數或造成同臉多帳號。
- 人臉特徵資料驗證：特徵向量固定 128 維且須為有限數值；匯入的舊資料庫或損毀的資料只會被略過，不會讓全店人臉比對失效。
- 售完與庫存：店員手動標示「售完」的餐點，即使庫存大於 0 也不可下單 (含套餐配餐)；取消訂單回補庫存時不會自動解除手動售完。
- 金額計算：百分比優惠產生小數時，應付金額四捨五入到整數元，並以「小計 − 應付」反推折扣總額，保證 小計 = 應付 + 折扣；點數折抵只折整數元。
- 深度客製化與套餐：支援飲料冰塊甜度、辣度與加料，並能升級雙配餐套餐；後端具備防竄改驗證，自動清洗偽造之加價標籤。
- 條件加購配額池化：支援任意品項 (any)、指定分類 (category) 與指定餐點 (item) 三種解鎖規則，並嚴格依主餐數量計算加購上限。

【櫃檯 POS 收銀系統 - POS Terminal (/pos)】
- 會員快速模糊搜尋：支援手機號碼或姓名片段查詢，符合多筆時彈出候選清單供店員點選綁定；提供店員現場核對身分並為會員設定/重設安全 PIN 碼。
- 交易找零與快速鍵：支援實收現金防呆 (實收不足禁止送單)、自動找零試算 ($100/$500/$1000/+100 快捷鍵)、80mm 熱感應收據列印。
- 暫存掛單夾 (Hold/Recall)：可於尖峰時期暫存目前點餐內容 (快捷鍵 F4)，並可隨時取回接續結帳 (快捷鍵 F2)。
- 完整帳務追責：持久化記錄 order_source='POS'、收銀員帳號 (cashier_username)、實收金額與找零，並同步寫入 StaffAuditLog 稽核日誌。
- POS 同樣不會讓被標示售完的餐點入單，優惠碼只記錄「實際套用成功」的代碼。

【廚房出單 KDS、取餐叫號與跨端即時通訊】
- KDS 製作看板 (/kitchen)：依訂單建立時間依序排單，自動聚合相同配餐客製規格，支援單筆出餐與「一鍵全部出餐」。
- 叫號即時看板 (/counter)：提供大字體號碼牌與製作中清單，新單出餐時由瀏覽器 Web Speech API 自動播放中文叫號語音。
- 狀態機防禦與全量回滾：Pending 禁止越級取餐、Cancelled 禁止復活、任何狀態不可倒退回 Pending；取消訂單時在同一交易內回補主餐庫存與各配餐庫存、退還折抵點數與兌換商品點數、還原優惠券並扣回贈點，且只會回補一次 (CAS 防重複)。

【店家後台管理 - Admin Dashboard (/admin)】
- 營運分析儀表板：即時統計待製作單數、預估等待時間 (ETA)、客單價、營業額與區間熱銷排行榜。儀表板與即時訂單 API 只載入「今天 + 尚未出餐」的訂單並預載明細，資料量成長後不會越來越慢。待製作數只計入「今天」的待製作訂單 (與廚房看板一致)。預估出餐 = 待製作張數 × 今日實際平均出餐時間 (建單到出餐完成)，平均值會排除極端值：先丟掉超過 90 分鐘或負數的紀錄 (例如忘了按出餐)，樣本數 ≥ 4 時再以四分位距 (IQR) 法排除離群值；今天還沒有可用的出餐紀錄時暫以 3 分鐘/單估算，頁面會註明估算依據。
- 熱門餐點排行：顯示份數與銷售額 (單價 × 份數，點數兌換的免費品項為 $0，已取消的訂單不計入)，筆數可自訂，並隨即時更新刷新，不必按 F5。
- 換日自動結案：每天跨日後的第一個請求 (以及每次啟動) 會把「今天 00:00 以前建立、卻還停在製作中」的訂單一律改為已出餐 (Completed)，涵蓋所有店家。使用條件式更新，不會與店員同時操作互相覆蓋；不填 completed_at，所以不會汙染平均出餐時間；庫存與點數不動；每家有結案的店會寫一筆系統稽核 (換日自動結案，列出訂單編號)。已出餐、已取餐、已取消的訂單不受影響，今天的訂單也不會被動到。「已完成今日訂單」含已出餐與已取餐，「平均每單時間」為建單到出餐完成的平均分鐘數，「平均每單」排除實付 0 元的純點數訂單。
- 編輯保護：編輯餐點庫存、會員點數時，表單會帶開啟視窗當下的數值 (loaded_stock / loaded_points)，後端只套用「差額」，不會用過期的表單值蓋掉編輯期間顧客買走的庫存或賺到/花掉的點數；餐點圖片檔名使用隨機 UUID，圖片損毀時會明確報錯，不會把原本的圖弄丟。
- AI 智慧文案與三層韌性降級：菜單新增/編輯支援 Gemini 3.6 Flash 一鍵生成繁中商品描述；遇配額限制時自動降級為本地 Qwen2.5 SLM 推論，若無權重則走美食辭庫規則引擎。
- 報表匯出 (/admin/export_excel)：涵蓋 10 張工作表 (含訂單對帳、區間營運、熱銷排行與員工稽核日誌)，支援自適應中文字元欄寬；本端點只負責報表，不含備份。
- 智慧匯入 (/admin/import_smart，唯一匯入入口)：可上傳單一 CSV 菜單，或含會員/菜單/優惠券的多工作表 Excel，自動辨識工作表與中英文欄位並 Upsert (會員工作表若有點數欄位則照填，包含 0 點；整份檔案沒有點數欄位才給新會員預設 20 點)；採單一交易，任一工作表解析失敗即整批 rollback；既有品項在檔案未提供庫存欄位時不覆蓋庫存與售完狀態，庫存恆不大於總量；紅利兌換點數高於單價、點數為 0 或特惠點數不合法時自動修正。
- 系統備份與還原 (僅平台管理者 = 預設店家 default 的 owner)：/admin/backup_system 下載含 SQLite 資料庫一致性快照與會員/餐點照片 (保留 static/menu/<店家ID>/ 結構) 的完整備份包；
  備份包含「所有店家」的資料，因此其他店家的 owner 不可使用 (請改用 Excel 匯出自己店家的資料)。
  /admin/restore_backup 還原前會先驗證備份包 (SQLite 完整性、系統資料表、相片副檔名與容量上限)，
  覆蓋前自動把現有資料庫另存為 database/menu.db.bak-<時間>，還原後立即執行資料庫遷移，舊版備份即刻升級。
- 租戶註冊入口 (/tenant/register)：提供新分店自主申請專屬空間與代號 (Slug)，並自動初始化預設加料品項與紅利設定。

【訂單狀態 API 與權限矩陣】
- 唯一端點：POST /api/orders/<order_id>/status    (需登入員工帳號與 CSRF Token)
  請求內容 (JSON)：{"status": "Completed" | "PickedUp" | "Cancelled"}
  廚房 KDS、櫃檯取餐、POS 與後台的狀態操作都使用此端點；另有 POST /api/kitchen_complete_all 供廚房一鍵全部出餐。
- 狀態流轉：Pending → Completed → PickedUp；Pending / Completed / PickedUp 皆可轉為 Cancelled；
  Cancelled 為終態，不可倒退，且沒有任何流程可以轉回 Pending。
- 系統自動流轉 (不經此端點)：每日換日自動結案會把前一日(含更早)還停在 Pending 的訂單改為 Completed (不填出餐時間，並寫入系統稽核)，其餘狀態不受影響。
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
- 營運分析欄位約定：後台模板讀取的 analytics.* 與 top_items[].revenue 必須都由 build_order_analytics 提供 (測試模組 15 會掃描模板驗證)，新增畫面欄位時請同步補上。
- 已移除的無作用程式：order_service.update_popular_items (實際上不做任何事)、Redis Pub/Sub 發佈 (沒有任何訂閱者；多行程部署請自行評估即時通知機制)。

⚙️ 環境變數配置 (.env)
在專案根目錄建立 `.env` 檔案並填入以下內容：
REDIS_URL="redis://127.0.0.1:6379/0"      # Redis 伺服器位址 (未啟動時系統自動降級使用本地 Process 執行緒鎖)
GEMINI_API_KEY="你的_GEMINI_API_金鑰"      # Google AI Studio API Key (留空則自動降級為本地 SLM 或規則引擎)
SECRET_KEY="請使用_secrets_token_hex_產生長字串"   # Session 簽章金鑰；留空則首次啟動自動產生並存到 database/.secret_key (之後沿用)
ADMIN_USERNAME="admin_test"               # 預設負責人帳號
ADMIN_PASSWORD="AdminPass!2026"           # 預設負責人密碼 (至少 10 碼)，只在「首次建立」該帳號時使用；留空則產生隨機密碼並只顯示一次
RESET_ADMIN_PASSWORD_ON_START="0"         # 忘記負責人密碼時，改為 1 重啟一次，把密碼重設成上面的 ADMIN_PASSWORD 並重新啟用帳號；用完請改回 0
DISABLE_RATE_LIMIT="0"                    # 設為 1 可於測試時停用 IP 限流 (預設 0 開啟保護)
TRUSTED_PROXY_COUNT="0"                   # 前面有幾層反向代理 (預設 0 = 不信任 X-Forwarded-For，直接對外連線請維持 0)
                                          # 部署在 Nginx / Cloudflare 等「單層」反向代理之後，請改為 1，
                                          # 否則所有請求都會被視為來自代理本身的 IP (限流、鎖定與稽核 IP 都會失準)。
                                          # 直接對外連線卻設成 1，等於讓任何人都能偽造來源 IP 繞過所有限流與 PIN 鎖定。
SESSION_COOKIE_SECURE="0"                 # 全站使用 HTTPS (例如經 Cloudflare/Nginx 終結 TLS) 時改為 1，Cookie 才限制只經 HTTPS 傳送；
                                          # 門市內網若是 http://區網IP 存取，設 1 會讓瀏覽器不儲存 Cookie 而無法登入，請維持 0。
CSP_MODE="enforce"                        # 內容安全政策：enforce 強制 / report-only 只回報不攔截 (畫面異常時排查用) / off 關閉
WAITRESS_THREADS="128"                    # Waitress 工作執行緒數；每條 SSE 長連線占用 1 條 (上限 25 公開 + 60 員工)，請勿低於 100

⬆️ 升級注意事項 (從舊版升級時請先看)
1. 來源 IP：預設不再信任 X-Forwarded-For。如果站台在 Nginx / Cloudflare 後面，務必在 .env 設定 TRUSTED_PROXY_COUNT=1。
2. 整庫備份/還原僅限平台管理者 (預設店家 default 的 owner)。其他店家的 owner 看不到也無法呼叫，請改用 Excel 匯出自己店家的資料。
3. 預設負責人密碼重啟時不再被重設。若過去習慣「改 .env 再重啟來重設密碼」，請改用 RESET_ADMIN_PASSWORD_ON_START=1。
4. 若 .env 沒有 SECRET_KEY，升級後第一次啟動會換成新產生的金鑰，所有人 (含員工) 需要重新登入一次。
5. 資料庫會在啟動時自動升級：訂單新增 applied_user_coupon_id 欄位、紅利設定去重並建立唯一索引、無法解析的舊人臉特徵保留原始資料並於啟動時列出會員 ID (需重新註冊人臉)。
6. 內容安全政策預設強制。如果自行在頁面加了其他外部資源 (新的 CDN、字型、地圖等)，請把網域加進 app/__init__.py 的 _CSP_POLICY，或先用 CSP_MODE=report-only 觀察。
7. llama-cpp-python (本地 Qwen2.5 SLM) 已改為選用，requirements.txt 預設註解；需要時取消註解後再安裝。
8. 換日自動結案：升級後第一次啟動，會把過去所有還停在「製作中」的舊訂單改為「已出餐」。如果有需要保留為製作中的舊單，請在升級前先處理。
9. 電話 + PIN 登入新增帳號層級鎖定：他人可能故意連續輸錯來鎖住某位會員，被鎖的會員可改用人臉登入，或請櫃檯重設 PIN 立即解除。