====================================================================
智慧自助點餐、人臉辨識與邊緣AI營運系統 (Kiosk & Edge-AI System)
====================================================================

📝 專案簡介 (Project Overview)
本專案為一套整合網頁前端、電腦視覺 (Computer Vision) 與邊緣語言模型 (Edge SLM) 的實體店面自助點餐機 (Kiosk) 整合系統。
系統提供顧客端流暢點餐、購物車狀態維持、套餐升級與深度客製化選項，並導入 YuNet 與 SFace 實現會員刷臉秒級登入。
後端整合店家管理儀表板 (Admin Dashboard)、廚房出單看板 (KDS)、櫃檯取餐與叫號核銷系統，更具備「雲端 Gemini API → 本地端 Qwen2.5 SLM → 本地動態規則」三層無縫降級機制，支援全天候零中斷的 AI 智慧營運決策與菜單文案自動生成。

🚀 核心技術與模組 (Technologies)
- Backend: Flask (Python)
- Database: SQLite (SQLAlchemy 關聯式架構，含 PRAGMA 自動遷移與快取清理)
- Computer Vision: OpenCV (YuNet 人臉偵測 + SFace 人臉識別)
- Edge AI / SLM: llama-cpp-python (本機直接推論 GGUF 模型，零外部軟體相依)
- Cloud AI: Google Gemini API (gemini-3.6-flash / Flash 系列)
- Real-time Stream: SSE (Server-Sent Events 即時推送訂單特徵特徵簽章)
- Frontend: HTML5, Bootstrap 5, Vanilla JavaScript (含 LocalStorage 狀態持久化)

📂 專案架構圖 (Project Structure)
Project Root/
├── app.py                                  # 核心 Backend 主程式 (含三層備援降級、排程與防呆驗證)
├── menu.db                                 # SQLite 資料庫 (自動檢測欄位並遷移)
├── face_detection_yunet_2023mar.onnx       # YuNet 人臉偵測模型
├── face_recognition_sface_2021dec.onnx     # SFace 人臉特徵向量比對模型
├── qwen2.5-1.5b-instruct-q4_k_m.gguf       # 本地邊緣語言模型 (Q4_K_M 量化權重，選配/備援)
├── static/                                 # 靜態資源與上傳檔案
│   ├── menu/                               # 店家上傳之餐點圖檔
│   └── member/                             # 會員註冊時擷取之人臉相片
└── templates/                              # 前端 HTML 模板
    ├── admin.html                          # 店家管理後台 (儀表板、菜單、套餐、會員與 AI 文案生成)
    ├── customer.html                       # 顧客點餐首頁 (Kiosk UI、套餐彈窗與多重客製化)
    ├── register.html                       # 會員註冊、雙軌登入與鏡頭擷取介面
    ├── kitchen.html                        # KDS 廚房即時出單與配料看板
    ├── counter.html                        # 櫃檯顧客取餐叫號螢幕
    ├── pickup.html                         # 櫃檯人員取餐核銷操作介面
    └── rewards.html                        # 會員 Points 紅利回饋兌換商城

💻 安裝與執行環境 (Environment & Setup)
1. 作業系統：Windows 11 (25H2) / Linux / macOS
2. Python 版本：Python 3.10+
3. 安裝必備套件 (Dependencies):
   打開終端機 (Terminal) 執行以下指令：
   pip install flask flask-sqlalchemy opencv-python numpy pandas openpyxl werkzeug google-generativeai python-dotenv llama-cpp-python

   * 註：Windows 環境安裝 llama-cpp-python 若無編譯器，建議使用預編譯 Wheel：
     pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

4. 本地模型配置 (選配，確保離線 AI 運作)：
   至 Hugging Face 下載 `qwen2.5-1.5b-instruct-q4_k_m.gguf`，直接放置於專案根目錄。
   https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/tree/main

5. 啟動伺服器：
   在專案根目錄下執行：
   python app.py
   伺服器啟動後，請在瀏覽器輸入：http://127.0.0.1:5000/

🤖 三層無縫降級 AI 機制 (Tri-tier Resilient AI Architecture)
系統在「智慧營運建議 (AI Insight)」與「菜單商品文案生成 (AI Copywriter)」模組中建構了三層自動降級防護網：
1. 第一層 (主要 - 雲端)：優先請求 Google Gemini 3.6 Flash，獲取高語意品質輸出。
2. 第二層 (備援 - 本地邊緣 AI)：當遇 API Key 未配置、無網路連線、403 或 429 配額超額時，啟動 60 秒冷卻，自動切換至本機 `Qwen2.5-1.5B (GGUF)` 進行純本機推論（內建 threading.Lock 執行緒鎖，防並發衝突與記憶體覆寫）。
3. 第三層 (兜底 - 本地動態規則)：若本機未加載 GGUF 權重，則退回本地動態營運規則與美食辭庫演算法，0 毫秒極速組裝回傳，全流程不報錯、無錯誤彈窗、不卡死主執行緒。

🔑 系統功能指南 (Features Guide)

【顧客端 - Kiosk】
- 雙軌登入：首頁支援「刷臉自動辨識」與「手機號碼」雙通道登入，亦支援隨機訪客快速點餐。
- 智能註冊：提供鏡頭視訊彈窗取景，後端 YuNet 演算法強制過濾無人臉相片。
- 套餐與客製化：點選主餐可選擇單點或升級套餐（支援至多三款配餐），支援同介面多品項客製化（甜度/冰塊/加料）。
- 會員紅利：結帳實付金額自動累積點數，點數可用於折抵消費或至「M-Points 回饋商城」兌換專屬優惠券與指定餐點。

【店家後台 - Admin Dashboard】
- 進入方式：客用首頁點擊管理員入口，或造訪 http://127.0.0.1:5000/admin (預設帳密：1234 / 1234)。
- AI 智慧文案：編輯或新增餐點時，點擊「AI 生成文案」即可根據餐點名稱與分類秒級撰寫誘人描述。
- 營運分析看板：即時統計待製作單數、出餐等待時間 (ETA)、客單價，並由 AI 產出具體經營建議。
- 資料匯入匯出：支援多工作表 (Sheet) Excel 智慧雙向匯入與匯出（包含進場留存耗時、取餐流水號與營運報表）。

【廚房與櫃檯協作 - KDS & Counter】
- 廚房看板 (kitchen.html)：透過 SSE 串流實現零延遲更新，同單同品項客製化自動歸納合併，支援單筆出餐與一鍵批次出餐。
- 櫃檯叫號 (counter.html) 與核銷 (pickup.html)：支援 1~999 每日循環取餐流水號，狀態即時同步。

⚠️ 重要注意事項與排錯 (Troubleshooting)
1. 純英文路徑：OpenCV 與 GGUF 模型載入不支援包含中文字元的目錄路徑，請確保專案根目錄全為英數字。
2. Windows Console 寫入保護：伺服器啟動已加入 `NO_COLOR=1` 與 `use_reloader=False` 設定，避免 Windows 控制台彩色控制碼崩潰與多程序重複載入模型問題。
3. 攝影機授權：使用人臉辨識登入時，請確保瀏覽器已允許本機攝影機存取權限。

⚙️ 環境變數設定 (.env)
在專案根目錄建立 `.env` 檔案並填入以下內容：
GEMINI_API_KEY="你的_GEMINI_API_金鑰" # 若留空則系統自動全程走本地 Qwen2.5 SLM 推論
ADMIN_USERNAME="1234"
ADMIN_PASSWORD="1234"