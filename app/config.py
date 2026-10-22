import os
import secrets
import sys
from datetime import timedelta

from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
# .env 優先於系統環境變數 (門市電腦只需維護這一個檔案)。
# 測試與 tests/test_payment_ecpay_script.py 設定 KIOSK_SKIP_DOTENV=1，避免開發者自己的 .env 影響測試結果。
if os.getenv("KIOSK_SKIP_DOTENV") != "1":
    load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)


def _env(name, default=""):
    return (os.getenv(name) or default).strip()


def _env_flag(name, default=False):
    raw = _env(name).lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def _env_int(name, default):
    try:
        return int(_env(name) or default)
    except ValueError:
        return default


def _load_or_create_secret_key():
    """
    Session 簽章金鑰：.env 的 SECRET_KEY 優先；未設定時第一次啟動自動產生並存到
    database/.secret_key (權限 600，已列入 .gitignore)，之後重啟沿用，登入狀態不會因重啟失效。
    """
    env_value = _env("SECRET_KEY")
    if env_value:
        return env_value
    key_path = os.path.join(BASE_DIR, "database", ".secret_key")
    for _ in range(5):
        try:
            if os.path.exists(key_path):
                with open(key_path, encoding="utf-8") as fh:
                    stored = fh.read().strip()
                if len(stored) >= 32:
                    return stored
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            new_key = secrets.token_hex(32)
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_key)
            print("ℹ️ 未在 .env 設定 SECRET_KEY，已自動產生並存入 database/.secret_key。", file=sys.stderr)
            return new_key
        except FileExistsError:
            continue  # 另一個行程剛建立，重新讀取
        except OSError:
            break
    print("⚠️ 無法讀寫 database/.secret_key，本次使用臨時金鑰 (重啟後需重新登入)。請於 .env 設定 SECRET_KEY。", file=sys.stderr)
    return secrets.token_hex(32)


def normalize_database_url(url):
    """postgres:// 與 postgresql:// 一律改用 psycopg 3 驅動"""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def _database_url():
    """DATABASE_URL 未設定時使用本機 SQLite (database/menu.db)；線上部署設為 postgresql://user:pass@host/db"""
    url = _env("DATABASE_URL")
    return normalize_database_url(url) if url else "sqlite:///" + os.path.join(BASE_DIR, "database", "menu.db")


def engine_options_for(url):
    if url.startswith("sqlite"):
        return {"connect_args": {"timeout": 30, "check_same_thread": False}}
    return {"pool_pre_ping": True, "pool_recycle": 1800}


class Config:
    BASE_DIR = BASE_DIR
    SECRET_KEY = _load_or_create_secret_key()

    # ---- 資料庫 ----
    DATABASE_DIR = os.path.join(BASE_DIR, "database")
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_ENGINE_OPTIONS = engine_options_for(SQLALCHEMY_DATABASE_URI)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # 啟動時自動套用資料庫遷移 (本機門市預設開啟；線上部署可設 0，改在部署流程執行 flask db upgrade)
    AUTO_MIGRATE = _env_flag("AUTO_MIGRATE", True)

    # ---- 選用的 Redis：線上多行程部署時用於頻率限制計數與即時通知；本機不需要 ----
    REDIS_URL = _env("REDIS_URL")
    RATELIMIT_STORAGE_URI = REDIS_URL or "memory://"
    RATELIMIT_ENABLED = _env_flag("RATELIMIT_ENABLED", True)
    RATELIMIT_HEADERS_ENABLED = True

    # ---- 線上 / 分店同步 (說明見 SYNC.txt) ----
    # standalone：單機 (預設，不同步)；hub：線上端；node：分店 (本機) 端
    SYNC_ROLE = (_env("SYNC_ROLE") or "standalone").lower()
    SYNC_HUB_URL = _env("SYNC_HUB_URL").rstrip("/")          # 分店端：線上端網址，例如 https://shop.example.com
    SYNC_NODE_TOKEN = _env("SYNC_NODE_TOKEN")                 # 分店端：線上端「分店管理」產生的權杖
    SYNC_TENANT_SLUG = _env("SYNC_TENANT_SLUG") or "default"  # 分店端：要同步的本機店家
    SYNC_INTERVAL = max(2, _env_int("SYNC_INTERVAL", 10))     # 分店端：兩次同步的間隔秒數
    SYNC_LONG_POLL = max(0, _env_int("SYNC_LONG_POLL", 20))   # 分店端：等待線上新訂單的長輪詢秒數 (0 = 不等待)
    SYNC_AUTOSTART = _env_flag("SYNC_AUTOSTART", True)        # 分店端：伺服器啟動時自動在背景同步
    # 線上端：分店超過幾秒沒有連線就視為離線，暫停該分店的線上點餐 (避免訂單送到收不到的分店)
    SYNC_NODE_OFFLINE_AFTER = max(30, _env_int("SYNC_NODE_OFFLINE_AFTER", 120))

    # ---- 線上付款 (線上端；綠界全方位金流，說明見 app/services/payment_ecpay.py) ----
    ONLINE_PAYMENT = _env_flag("ONLINE_PAYMENT", True)        # 線上點餐需先線上付款，付款後才送到分店
    ECPAY_STAGE = _env_flag("ECPAY_STAGE", True)              # 1 = 綠界測試環境 (預設)；正式收款改 0 並填入正式特店資料
    ECPAY_MERCHANT_ID = _env("ECPAY_MERCHANT_ID")             # 留空 = 綠界公開的測試特店
    ECPAY_HASH_KEY = _env("ECPAY_HASH_KEY")
    ECPAY_HASH_IV = _env("ECPAY_HASH_IV")
    # 綠界付款結果要通知的公開網址 (例如 https://shop.example.com)；留空則使用目前請求的網址
    PUBLIC_BASE_URL = _env("PUBLIC_BASE_URL").rstrip("/")
    PAYMENT_TIMEOUT_MINUTES = max(5, _env_int("PAYMENT_TIMEOUT_MINUTES", 15))  # 逾時未付款自動取消

    # ---- 上傳與模型 ----
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024
    # 上傳檔案放在 static/ 之外，由 app/routes/media.py 控管讀取權限 (線上部署可改指向持久化磁碟)
    UPLOAD_FOLDER_MEMBER = _env("UPLOAD_FOLDER_MEMBER") or os.path.join(BASE_DIR, "uploads", "member")
    UPLOAD_FOLDER_MENU = _env("UPLOAD_FOLDER_MENU") or os.path.join(BASE_DIR, "uploads", "menu")
    MODELS_DIR = os.path.join(BASE_DIR, "models")
    YUNET_MODEL = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
    SFACE_MODEL = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")
    LOCAL_MODEL_PATH = os.path.join(MODELS_DIR, "qwen2.5-1.5b-instruct-q4_k_m.gguf")
    GEMINI_API_KEY = _env("GEMINI_API_KEY")

    MAX_SINGLE_FILE_SIZE = 15 * 1024 * 1024
    MAX_TOTAL_EXTRACT_SIZE = 150 * 1024 * 1024
    MAX_RESTORE_DB_SIZE = 512 * 1024 * 1024
    # 整庫還原上傳的 zip 上限 (資料庫 + 照片)；其他請求維持 MAX_CONTENT_LENGTH
    MAX_RESTORE_UPLOAD_SIZE = MAX_RESTORE_DB_SIZE + MAX_TOTAL_EXTRACT_SIZE

    # ---- 預設店家負責人 ----
    ADMIN_USERNAME = _env("ADMIN_USERNAME")
    ADMIN_PASSWORD = _env("ADMIN_PASSWORD")
    # 帳號或密碼任一未設定時，產生一組新的隨機憑證 (啟動時印出一次)
    ADMIN_CREDENTIALS_GENERATED = not (ADMIN_USERNAME and ADMIN_PASSWORD)
    if ADMIN_CREDENTIALS_GENERATED:
        ADMIN_USERNAME = f"owner-{secrets.token_urlsafe(9)}"
        ADMIN_PASSWORD = secrets.token_urlsafe(24)
    ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)
    # 設為 1：每次啟動都把預設負責人密碼重設為 .env 的值 (忘記密碼時的救援手段)
    RESET_ADMIN_PASSWORD_ON_START = _env_flag("RESET_ADMIN_PASSWORD_ON_START", False)

    # ---- 安全性 ----
    # 反向代理層數：0 = 不信任 X-Forwarded-*。部署在 Nginx / Cloudflare 之後設為 1。
    TRUSTED_PROXY_COUNT = max(0, _env_int("TRUSTED_PROXY_COUNT", 0))
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # 全站 HTTPS 時設為 1；門市內網常以 http://區網IP 存取，預設關閉
    SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = timedelta(seconds=1800)
    # CSRF Token 在整個工作階段內有效 (kiosk 頁面可能整天開著，不使用 Flask-WTF 預設的 1 小時期限)
    WTF_CSRF_TIME_LIMIT = None
    # 內容安全政策：enforce (預設) / report-only (排查用) / off
    CSP_MODE = _env("CSP_MODE", "enforce").lower()

    # Waitress 執行緒數：每條 SSE 長連線占用一條 (公開 25 + 員工 60 = 85)，必須明顯大於 SSE 上限
    WAITRESS_THREADS = max(16, _env_int("WAITRESS_THREADS", 128))
