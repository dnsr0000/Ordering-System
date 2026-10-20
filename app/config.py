import os
import sys
import secrets
from datetime import timedelta
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)


def _load_or_create_secret_key():
    """
    取得 Session 簽章金鑰：
    1. .env 的 SECRET_KEY 優先。
    2. 未設定時，不使用寫死在原始碼的公開金鑰 (任何人都能偽造登入 Cookie)，
       改為首次啟動自動產生隨機金鑰並存入 database/.secret_key (權限 600，已列入 .gitignore)，
       之後重啟沿用同一把，登入狀態不會因重啟而失效。
    """
    env_value = (os.getenv("SECRET_KEY") or "").strip().strip('"').strip("'")
    if env_value:
        return env_value
    key_path = os.path.join(BASE_DIR, "database", ".secret_key")
    for _ in range(5):
        try:
            if os.path.exists(key_path):
                with open(key_path, "r", encoding="utf-8") as fh:
                    stored = fh.read().strip()
                if len(stored) >= 32:
                    return stored
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            new_key = secrets.token_hex(32)
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_key)
            print("ℹ️ 未在 .env 設定 SECRET_KEY，已自動產生並存入 database/.secret_key (建議仍於 .env 明確設定)。", file=sys.stderr)
            return new_key
        except FileExistsError:
            continue  # 另一個行程剛建立，重新讀取
        except OSError:
            break
    print("⚠️ 無法讀寫 database/.secret_key，本次啟動使用臨時隨機金鑰 (重啟後所有人需重新登入)。請於 .env 設定 SECRET_KEY。", file=sys.stderr)
    return secrets.token_hex(32)


def _env_flag(name, default=False):
    raw = (os.getenv(name) or "").strip().strip('"').strip("'").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class Config:
    BASE_DIR = BASE_DIR
    SECRET_KEY = _load_or_create_secret_key()

    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 上傳上限 50MB
    REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")

    DATABASE_DIR = os.path.join(BASE_DIR, "database")
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + os.path.join(DATABASE_DIR, "menu.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"timeout": 30, "check_same_thread": False},
        "pool_pre_ping": True,
        "pool_recycle": 1800,
    }

    UPLOAD_FOLDER_MEMBER = os.path.join(BASE_DIR, "static", "member")
    UPLOAD_FOLDER_MENU = os.path.join(BASE_DIR, "static", "menu")

    MODELS_DIR = os.path.join(BASE_DIR, "models")
    YUNET_MODEL = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
    SFACE_MODEL = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")
    LOCAL_MODEL_PATH = os.path.join(MODELS_DIR, "qwen2.5-1.5b-instruct-q4_k_m.gguf")

    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    ADMIN_USERNAME = (os.getenv("ADMIN_USERNAME") or "admin").strip().strip('"').strip("'")
    ADMIN_PASSWORD = (os.getenv("ADMIN_PASSWORD") or "").strip().strip('"').strip("'")

    # 未設定 ADMIN_PASSWORD 時產生隨機密碼；僅在「首次建立」預設負責人帳號時才會使用並顯示 (見 migration_service)
    ADMIN_PASSWORD_GENERATED = False
    if not ADMIN_PASSWORD:
        ADMIN_PASSWORD = secrets.token_urlsafe(12)
        ADMIN_PASSWORD_GENERATED = True

    ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)

    # 設為 1 時，每次啟動都把預設負責人帳號的密碼重設為 .env 的 ADMIN_PASSWORD 並重新啟用 (忘記密碼時的救援手段)；
    # 預設 0：帳號建立後，負責人自行修改的密碼與停用狀態不會被重啟洗掉。
    RESET_ADMIN_PASSWORD_ON_START = _env_flag("RESET_ADMIN_PASSWORD_ON_START", False)

    MAX_SINGLE_FILE_SIZE = 15 * 1024 * 1024
    MAX_TOTAL_EXTRACT_SIZE = 150 * 1024 * 1024
    MAX_RESTORE_DB_SIZE = 512 * 1024 * 1024  # 還原備份包內 menu.db 的大小上限

    # 反向代理層數：0 = 不信任任何 X-Forwarded-* 標頭 (直接對外連線時務必維持 0，
    # 否則任何人都能偽造來源 IP，繞過登入/註冊/PIN 的所有頻率限制與鎖定)。
    # 部署於 Nginx / Cloudflare 等「單層」反向代理之後，請於 .env 設定 TRUSTED_PROXY_COUNT=1。
    try:
        TRUSTED_PROXY_COUNT = max(0, int((os.getenv("TRUSTED_PROXY_COUNT") or "0").strip()))
    except ValueError:
        TRUSTED_PROXY_COUNT = 0

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    # 全站使用 HTTPS (例如經 Cloudflare/Nginx 終結 TLS) 時請於 .env 設為 1，Cookie 才會限制只經 HTTPS 傳送。
    # 預設 0，因為門市內網常以 http://區網IP 存取，強制 Secure 會導致瀏覽器不儲存 Cookie 而無法登入。
    SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", False)

    # 內容安全政策 (CSP) 模式：enforce (預設，強制) / report-only (只回報不攔截，排查用) / off (關閉)
    CSP_MODE = (os.getenv("CSP_MODE") or "enforce").strip().strip('"').strip("'").lower()

    # Waitress 工作執行緒數：每條 SSE 長連線會占用一條執行緒 (公開 25 + 員工 60 = 85)，
    # 必須明顯大於 SSE 上限，才不會讓一般請求排不到執行緒而整站卡住。
    WAITRESS_THREADS = max(16, int((os.getenv("WAITRESS_THREADS") or "128").strip() or 128))
    PERMANENT_SESSION_LIFETIME = timedelta(seconds=1800)