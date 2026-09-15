import os
from datetime import timedelta
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

load_dotenv()
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))

class Config:
    BASE_DIR = BASE_DIR
    _secret = os.getenv("SECRET_KEY")
    if not _secret:
        import sys
        _secret = os.urandom(32).hex()
        print("⚠️ 未在 .env 檢測到 SECRET_KEY，已自動生成臨時 Session 金鑰。建議在 .env 中設定！", file=sys.stderr)
    SECRET_KEY = _secret

    SECRET_KEY = os.getenv("SECRET_KEY") or os.urandom(32).hex()
    PERMANENT_SESSION_LIFETIME = timedelta(days=7)
    MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 上傳上限 50MB

    # Redis 連線設定 (雲端環境透過環境變數注入，本機預設 localhost)
    REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    
    # 資料庫路徑
    DATABASE_DIR = os.path.join(BASE_DIR, 'database')
    SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(DATABASE_DIR, 'menu.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "connect_args": {"timeout": 30, "check_same_thread": False},
        "pool_pre_ping": True,
        "pool_recycle": 1800
    }

    # 靜態檔案路徑
    UPLOAD_FOLDER_MEMBER = os.path.join(BASE_DIR, 'static', 'member')
    UPLOAD_FOLDER_MENU = os.path.join(BASE_DIR, 'static', 'menu')

    # AI 模型路徑
    MODELS_DIR = os.path.join(BASE_DIR, 'models')
    YUNET_MODEL = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
    SFACE_MODEL = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")
    LOCAL_MODEL_PATH = os.path.join(MODELS_DIR, "qwen2.5-1.5b-instruct-q4_k_m.gguf")

    # API Keys 與管理者資訊
    GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "1234")
    ADMIN_PASSWORD_HASH = generate_password_hash(os.getenv("ADMIN_PASSWORD", "1234"))

    # 備份與還原安全限制
    MAX_SINGLE_FILE_SIZE = 15 * 1024 * 1024
    MAX_TOTAL_EXTRACT_SIZE = 150 * 1024 * 1024