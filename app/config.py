import os
import sys
import secrets
from datetime import timedelta
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
load_dotenv(os.path.join(BASE_DIR, ".env"), override=True)


class Config:
    BASE_DIR = BASE_DIR
    _secret = (os.getenv("SECRET_KEY") or "").strip().strip('"').strip("'")
    if not _secret:
        _secret = "kiosk-fallback-secret-key-please-set-in-env-2026"
        print("⚠️ 未在 .env 檢測到 SECRET_KEY，已使用預設固定金鑰。建議在 .env 中設定！", file=sys.stderr)
    SECRET_KEY = _secret

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

    if not ADMIN_PASSWORD:
        ADMIN_PASSWORD = secrets.token_urlsafe(12)
        print(f"\n⚠️  [安全警告] 未在 .env 設定 ADMIN_PASSWORD，已自動生成初始密碼: {ADMIN_PASSWORD}\n")

    ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)

    MAX_SINGLE_FILE_SIZE = 15 * 1024 * 1024
    MAX_TOTAL_EXTRACT_SIZE = 150 * 1024 * 1024

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = timedelta(seconds=1800)
