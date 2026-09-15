====================================================================
智慧自助點餐、人臉辨識與邊緣AI營運系統 (Kiosk & Edge-AI System)
====================================================================

📝 專案簡介 (Project Overview)
本專案為一套整合網頁前端、電腦視覺 (Computer Vision)、邊緣語言模型 (Edge SLM) 與雲端高併發分散式架構的實體店面自助點餐機 (Kiosk) 系統。
系統提供顧客端流暢點餐、購物車狀態維持、套餐升級、條件式加購與深度客製化選項，並導入 YuNet 與 SFace 實現會員秒級刷臉登入。
後端採用 Flask 藍圖工廠架構，結合 Redis 分散式互斥鎖與 Pub/Sub 跨行程即時廣播，打通店家管理後台、廚房出單看板 (KDS) 與櫃檯取餐叫號螢幕；更具備「雲端 Gemini API → 本地端 Qwen2.5 SLM → 本地動態規則」三層無縫降級機制，支援全天候零中斷的 AI 營運決策與菜單文案自動生成。

🚀 核心技術與架構 (Technologies & Architecture)
- Backend: Flask (Application Factory Pattern 搭配 Blueprint 模組化職責分離設計)
- Concurrency & Locking: Redis 分散式互斥鎖 (Distributed Lock，防超賣與號碼重複) + 本地執行緒鎖自動降級
- Real-time Stream: SSE (Server-Sent Events) 搭配 Redis Pub/Sub 跨行程訂單簽章廣播
- Computer Vision: OpenCV (YuNet 近景最大人臉鎖定 + SFace 特徵向量 + NumPy 矩陣批次 Cosine Similarity 比對)
- Edge AI / SLM: llama-cpp-python (本地推論 Qwen2.5-1.5B GGUF 權重，內建 Threading Lock 避免並發衝突)
- Cloud AI: Google Gemini API (gemini-3.6-flash 高效語意理解)
- Database: SQLite (SQLAlchemy 關聯式架構，含 PRAGMA WAL 高併發讀寫分離、超時鎖定與動態結構自動遷移)
- Web Servers: Waitress (Windows 32 執行緒生產伺服器) / Gunicorn (Linux 雲端 gthread 非同步模式)
- Frontend: HTML5, Bootstrap 5, Vanilla JavaScript (含 LocalStorage 狀態持久化與條件加購配額池化管理)

📂 專案架構圖 (Project Structure)
Project Root/
├── app/                                    # 核心應用程式套件 (Flask Package)
│   ├── __init__.py                         # 應用程式工廠 (create_app)、PRAGMA 監聽、自動遷移與 413 攔截
│   ├── config.py                           # 集中設定檔 (環境變數、REDIS_URL、資料庫 URI、模型路徑)
│   ├── extensions.py                       # 單例擴充元件 (db、Redis Client、分散式互斥鎖管理器、LLM_LOCK)
│   ├── models/                             # 資料庫 ORM 模型層
│   │   ├── __init__.py                     # 模型匯出模組
│   │   ├── user.py                         # 會員 (User)、管理員日誌 (AdminLog)、顧客日誌 (CustomerLog)
│   │   ├── menu.py                         # 菜單品項 (MenuItem)、套餐組合 (ComboOption)
│   │   ├── order.py                        # 訂單總覽 (Order)、訂單明細 (OrderItem)
│   │   └── coupon.py                       # 優惠券模板 (Coupon)、會員持有券 (UserCoupon)
│   ├── services/                           # 商業邏輯與演算法服務層
│   │   ├── __init__.py                     # 服務層匯出模組
│   │   ├── cv_service.py                   # 近景最大人臉鎖定、SFace 特徵提取與多特徵矩陣向量化比對
│   │   ├── ai_service.py                   # Gemini 3.6 Flash、Qwen2.5 SLM 推論與三層降級兜底演算法
│   │   ├── order_service.py                # 結帳驗證、庫存扣減、訂單全量回滾與營運分析
│   │   ├── migration_service.py            # 資料庫結構動態檢查、每日取餐流水號校正與舊資料清洗
│   │   └── event_bus.py                    # SSE 廣播器 (支援本地 Queue 與 Redis Pub/Sub 跨行程廣播)
│   └── routes/                             # 功能路由控制器藍圖
│       ├── __init__.py                     # 路由藍圖模組
│       ├── customer.py                     # 前台點餐、優惠券驗證、分散式鎖原子結帳、取餐號互斥發放
│       ├── auth.py                         # 會員註冊、向量化人臉秒級登入、手機號碼登入、訪客模式
│       ├── rewards.py                      # 紅利商城頁面、點數兌換優惠券/餐點、購物車移除動態退點
│       ├── admin.py                        # 後台儀表板、菜單/套餐/會員 CRUD、安全備份還原與 Excel 智慧匯入匯出
│       ├── kitchen.py                      # KDS 廚房出單看板、訂單合併與批次出餐
│       ├── pickup.py                       # 櫃檯叫號螢幕、取餐核銷與 PEP 3333 標準 SSE 串流推播
│       └── common.py                       # 顧客進出工作階段 (CustomerLog Tracking) 輔助工具
├── database/                               # 資料庫專屬資料夾 (menu.db / WAL / SHM)
├── models/                                 # AI 模型專屬資料夾
│   ├── face_detection_yunet_2023mar.onnx   # 人臉偵測模型
│   ├── face_recognition_sface_2021dec.onnx # 人臉特徵比對模型
│   └── qwen2.5-1.5b-instruct-q4_k_m.gguf   # 本地邊緣語言模型 (Qwen2.5 SLM)
├── static/                                 # 靜態資源與上傳檔案 (餐點圖檔與會員註冊照片)
├── templates/                              # 前端 HTML 模板 (admin, customer, kitchen, counter, pickup, rewards, register)
├── .env                                    # 環境變數設定檔 (REDIS_URL, API Key, Secret Key, 帳密)
├── requirements.txt                        # 雲端與生產環境依賴清單 (版本鎖定與相容性規範)
├── serve_windows.py                        # Windows 11 生產環境啟動入口 (Waitress 32-thread 高併發伺服器)
├── run.py                                  # 本機開發除錯啟動腳本 (Flask Debug Server)
├── test_lock.py                            # 高併發分散式互斥鎖與防超賣自動化壓測腳本

💻 安裝與執行環境 (Environment & Setup)
1. 作業系統：Windows 11 (25H2) / Linux (Ubuntu 22.04+) / macOS
2. Python 版本：Python 3.10+ (支援至 Python 3.14 環境)
3. 安裝相依套件：
   打開終端機執行：
   pip install -r requirements.txt

4. 本地邊緣語言模型配置 (選配)：
   至 Hugging Face 下載 `qwen2.5-1.5b-instruct-q4_k_m.gguf` 置於 `models/` 資料夾下：
   https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/tree/main

5. 啟動本機伺服器：
   - 【Windows 生產模式 (推薦)】：
     python serve_windows.py
   - 【日常開發除錯模式 (含熱重載)】：
     python run.py
   啟動後開啟瀏覽器訪問：http://127.0.0.1:5000/

6. 啟動高併發防超賣驗證 (壓測)：
   在伺服器運行時，另開終端機執行：
   python test_lock.py

🤖 三層無縫降級 AI 機制 (Tri-tier Resilient AI Architecture)
系統在「智慧營運建議 (AI Insight)」與「菜單商品文案生成 (AI Copywriter)」模組中具備三層自動防護網：
1. 第一層 (主要 - 雲端)：優先調用 Google Gemini 3.6 Flash，生成高質量專業文案。
2. 第二層 (備援 - 本地邊緣 AI)：遇網路中斷、API Key 未配置或 429 配額限制時，啟動 60 秒冷卻，自動切換至本機 `Qwen2.5-1.5B (GGUF)` 執行推論 (內建執行緒鎖防止跨執行緒資源衝突)。
3. 第三層 (兜底 - 本地動態規則)：若本機未載入 GGUF 權重，則退回本地動態營運規則與美食辭庫演算法，0 毫秒極速組裝回傳，全流程無錯誤彈窗、不卡死主執行緒。

🔑 核心功能指南 (Features Guide)

【顧客端 - Kiosk & CV】
- 近景最大人臉鎖定：YuNet 偵測多人入鏡時，依 Bounding Box 面積優先鎖定最貼近機台的點餐者，排除背景路人干擾。
- 矩陣向量秒級登入：SFace 提取之 128/512 維特徵向量透過 NumPy 2D 矩陣進行批次 Cosine Similarity 計算，徹底消除迴圈比對延遲。
- 雙軌登入與訪客模式：支援人臉辨識、手機號碼與隨機訪客快速點餐。
- 套餐與深度客製化：支援多款配餐組合、同介面多品項規格客製 (冰塊/甜度/加料)。
- 智慧加購配額池化：嚴格依條件累算可用名額，加購品不可再次作為資格母體，通用加購品限定主餐解鎖。
- 會員紅利與退點機制：實付滿額自動積點，購物車移除兌換餐點時由後端安全全額退還紅利。

【高併發防護與多行程協作 - Concurrency & PubSub】
- Redis 分散式互斥鎖：結帳臨界區透過 `acquire_distributed_lock` 保護，確保多 Worker 併發下庫存絕不超賣、每日 1~999 取餐號碼無衝突 (未連線 Redis 時自動無縫降級為本地鎖)。
- 跨行程 SSE 推播：廚房看板 (kitchen.html)、櫃檯取餐螢幕 (counter.html) 與取餐核銷介面 (pickup.html) 透過 Redis Channel 實現跨行程即時廣播，遵循 PEP 3333 標準。

【店家後台 - Admin Dashboard】
- 進入方式：首頁點擊後台管理或訪問 http://127.0.0.1:5000/admin (預設帳密：1234 / 1234)。
- AI 文案與菜單管理：新增與編輯餐點支援 AI 一鍵撰寫描述，支援套餐配餐防重複與無客製化旗標自動校正。
- 營運分析看板：即時計算待製作單數、出餐等待時間 (ETA)、客單價與區間熱銷排行。
- 報表與資料備份：多工作表 Excel 雙向智慧匯入/匯出 (欄寬自適應)，一鍵備份 SQLite 資料庫與實體相片壓縮檔 (內建 Zip Bomb 防護)。

在專案根目錄建立 `.env` 檔案並填入以下內容：
REDIS_URL="redis://127.0.0.1:6379/0" # 本機預設 6379 埠；若未啟動 Redis 系統會自動安全降級為本地單機鎖
GEMINI_API_KEY="你的_GEMINI_API_金鑰" # 若留空則系統自動全程走本地 Qwen2.5 SLM 推論
SECRET_KEY="請使用_secrets_token_hex_32_產生的長字串"
ADMIN_USERNAME="1234"
ADMIN_PASSWORD="1234"