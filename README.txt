====================================================================
自助點餐與人臉辨識系統 (Kiosk & Face Recognition System)
====================================================================

📝 專案簡介 (Project Overview)
本專案為一套結合網頁前端與電腦視覺技術的實體店面自助點餐機 (Kiosk) 系統。
系統提供完整的顧客點餐介面、購物車功能，並導入人臉辨識技術 (Face Recognition) 
實現會員快速登入。後端同時具備完善的店家管理儀表板 (Admin Dashboard)，
可即時管理菜單與會員資料。

🚀 核心技術與模組 (Technologies)
- Backend: Flask (Python)
- Database: SQLite (SQLAlchemy)
- Computer Vision: OpenCV (DNN 模組)
- AI Models: YuNet (Face Detection), SFace (Face Recognition)
- Frontend: HTML5, Bootstrap 5, Vanilla JavaScript

📂 專案架構圖 (Project Structure)
Project Root/
├── app.py                                  # 核心 Backend 主程式
├── menu.db                                 # SQLite 資料庫 (自動生成)
├── face_detection_yunet_2023mar.onnx       # YuNet 人臉偵測模型 (需手動放入)
├── face_recognition_sface_2021dec.onnx     # SFace 人臉識別模型 (需手動放入)
├── static/                                 # 靜態資源與上傳檔案
│   ├── menu/                               # 存放店家上傳的餐點照片
│   └── member/                             # 存放會員註冊時擷取的人臉照片
└── templates/                              # 前端 HTML 模板
    ├── admin.html                          # 店家管理後台 (登入與儀表板)
    ├── customer.html                       # 顧客點餐首頁 (Kiosk UI)
    └── register.html                       # 會員註冊與相機拍攝介面

💻 安裝與執行環境 (Environment & Setup)
1. 作業系統：Windows 11 (已於 25H2 環境測試通過)
2. Python 版本：Python 3.8+ 
3. 安裝必備套件 (Dependencies):
   打開終端機 (Terminal) 執行以下指令：
   pip install flask flask-sqlalchemy opencv-python numpy werkzeug

4. 啟動伺服器：
   在專案根目錄下執行：
   python app.py
   伺服器啟動後，請在瀏覽器輸入：http://127.0.0.1:5000/

🔑 系統使用指南 (Usage Guide)

【顧客端 - Kiosk】
- 首頁提供「加入會員」與「人臉快速登入」功能。
- 註冊時可直接授權開啟 Webcam，系統會自動擷取 (Capture) 臉部特徵。
- 登入後將自動跳轉至點餐畫面，結帳完成後系統會自動清除 Session 並登出。

【店家後台 - Admin Dashboard】
- 進入方式：於客用首頁最下方點擊隱藏連結，或直接在網址列輸入 http://127.0.0.1:5000/admin
- 預設登入帳號 (Username)：1234
- 預設登入密碼 (Password)：1234
- 功能：可新增/刪除菜單 (支援圖片上傳)，以及檢視/銷毀會員資料 (採用 ID Reuse Algorithm 回收會員號碼)。

⚠️ 重要注意事項與排錯 (Troubleshooting & Precautions)
1. 純英文路徑 (ASCII Path Required)：
   OpenCV 的 DNN 模組在讀取 ONNX 模型時不支援中文路徑。請確保專案資料夾所在的路徑 (如 Desktop) 每一層都是純英文，否則會觸發 `Can't read ONNX file` 錯誤。
2. 攝影機權限 (Webcam Access)：
   人臉辨識與註冊功能預設調用本機第一台攝影機 (`cv2.VideoCapture(0)`)，請確認設備有實體鏡頭且未被其他應用程式佔用。
3. 照片清理機制：
   若透過管理員後台刪除菜單或會員，系統會同步物理刪除 `static/menu/` 與 `static/member/` 內的關聯圖檔。請勿手動在資料夾中任意更改檔名，以免 Database 連結失效。

✨ 最新功能更新 (2026/03/18)
- 新增「會員點數機制」：會員買單可獲得點數，且可在下次結帳時折抵現金。
- 加入「點數設定」後台管理：管理員可設定每 N 點折 1 元、點數賺取倍數、整體點數功能開關。
- 提供會員「單一登入」：會員登入後保持 session，可直接快速點餐。
- 前端完善購物車與結帳預覽：含品項、總價、會員折扣、點數折抵、應付金額。
- 新增「訂單狀態管理、訂單內容編輯」：管理員可更新訂單狀態 (Pending/Preparing/Ready/Completed/Cancelled) 並直接編輯訂單內文。
- 加入「熱門推薦」「分類篩選」「價格排序」：顧客可快速篩選/排序菜單。
- 新增「訂單匯出 CSV」與「營收分析報表」功能。

📌 使用小提醒
1. 會員點數折抵輸入必須為數字，且不可超過可用點數（系統會自動檢查）。
2. 若要重置所有點數，請在 admin 單一扣減會員或重新註冊帳號。
3. 點數功能開關請在 admin > 點數設定調整；關閉後前端將不顯示點數折抵欄位。