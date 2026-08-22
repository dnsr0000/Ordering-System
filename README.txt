====================================================================
自助點餐與人臉辨識系統 (Kiosk & Face Recognition System)
====================================================================

📝 專案簡介 (Project Overview)
本專案為一套結合網頁前端與電腦視覺技術的實體店面自助點餐機 (Kiosk) 系統。
系統提供完整的顧客點餐介面、購物車功能，並導入人臉辨識技術 (Face Recognition) 
實現會員快速登入[cite: 2]。後端同時具備完善的店家管理儀表板 (Admin Dashboard)，
可即時管理菜單與會員資料[cite: 2]。

🚀 核心技術與模組 (Technologies)
- Backend: Flask (Python)[cite: 2]
- Database: SQLite (SQLAlchemy)[cite: 2]
- Computer Vision: OpenCV (DNN 模組)[cite: 2]
- AI Models: YuNet (Face Detection), SFace (Face Recognition)[cite: 2]
- Frontend: HTML5, Bootstrap 5, Vanilla JavaScript (含 LocalStorage 狀態持久化)[cite: 2]

📂 專案架構圖 (Project Structure)
Project Root/
├── app.py                                  # 核心 Backend 主程式[cite: 2]
├── menu.db                                 # SQLite 資料庫 (向下相容自動升級)[cite: 2]
├── face_detection_yunet_2023mar.onnx       # YuNet 人臉偵測模型[cite: 2]
├── face_recognition_sface_2021dec.onnx     # SFace 人臉識別模型[cite: 2]
├── static/                                 # 靜態資源與上傳檔案[cite: 2]
│   ├── menu/                               # 存放店家上傳的餐點照片[cite: 2]
│   └── member/                             # 存放會員註冊時擷取的人臉照片[cite: 2]
└── templates/                              # 前端 HTML 模板[cite: 2]
    ├── admin.html                          # 店家管理後台 (登入與儀表板)[cite: 2]
    ├── customer.html                       # 顧客點餐首頁 (Kiosk UI)[cite: 2]
    ├── register.html                       # 會員註冊、雙軌登入與相機拍攝介面[cite: 2]
    └── rewards.html                        # M-Points 會員紅利回饋商城

💻 安裝與執行環境 (Environment & Setup)
1. 作業系統：Windows 11 (25H2)[cite: 2]
2. Python 版本：Python 3.8+[cite: 2]
3. 安裝必備套件 (Dependencies):
   打開終端機 (Terminal) 執行以下指令：[cite: 2]
   pip install flask flask-sqlalchemy opencv-python numpy pandas pandas openpyxl werkzeug[cite: 2]

4. 啟動伺服器：
   在專案根目錄下執行：[cite: 2]
   python app.py[cite: 2]
   伺服器啟動後，請在瀏覽器輸入：http://127.0.0.1:5000/[cite: 2]

🔑 系統使用指南 (Usage Guide)

【顧客端 - Kiosk】
- 雙軌登入：首頁提供「加入會員」與「會員快速登入 (人臉辨識 / 手機號碼)」功能。
- 智能註冊：提供相機即時視訊彈窗取景，後端具備強制人臉特徵防呆機制，無人臉自動阻斷。
- 行銷與點數：結帳可輸入折扣碼 (Promo Code) 或使用紅利折抵，點選紅利徽章可進入「M-Points 回饋商城」兌換免費餐點。
- 購物車：支援跨頁面 localStorage 狀態保留、客製化選項 (冰塊/甜度/加料) 及熱門推薦品項一鍵下單。

【店家後台 - Admin Dashboard】
- 進入方式：於客用首頁最下方點擊隱藏連結，或直接在網址列輸入 http://127.0.0.1:5000/admin[cite: 2]
- 預設登入：帳號 1234 / 密碼 1234[cite: 2]
- 營運功能：支援 Excel 批量匯入菜單、單一品項完整編輯、1:1 Cropper.js 圖片裁切、會員紅利名冊檢視。
- 訂單管理：實時訂單狀態追蹤與切換 (製作中/已出餐)，並支援頁籤狀態記憶，重整不再迷路跳頁。

⚠️ 重要注意事項與排錯 (Troubleshooting)
1. 純英文路徑 (ASCII Path Required)：OpenCV 讀取 ONNX 模型時不支援中文路徑，請確保專案路徑全英文[cite: 2]。
2. 資料庫遷移 (Auto Migration)：系統已內建 PRAGMA table_info 檢查，啟動時會自動補齊缺失欄位，若遇極端結構衝突，可直接刪除 menu.db 重新啟動[cite: 2]。
3. 攝影機權限：人臉辨識預設調用本機第一台攝影機 (`cv2.VideoCapture(0)`)，如無法開啟請確認瀏覽器授權[cite: 2]。