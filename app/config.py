import os
import secrets
import sys
from datetime import timedelta
from dotenv import load_dotenv
from werkzeug.security import generate_password_hash

# 載入 .env
load_dotenv()
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


class Config:
  BASE_DIR = BASE_DIR

  # 1. Session 金鑰設定
  _secret = os.getenv('SECRET_KEY')
  if not _secret:
    _secret = os.urandom(32).hex()
    print(
        '⚠️ 未在 .env 檢測到 SECRET_KEY，已自動生成臨時 Session'
        ' 金鑰。建議在 .env 中設定！',
        file=sys.stderr,
    )
  SECRET_KEY = _secret

  MAX_CONTENT_LENGTH = 50 * 1024 * 1024  # 上傳上限 50MB

  # 2. Redis 連線設定
  REDIS_URL = os.getenv('REDIS_URL', 'redis://127.0.0.1:6379/0')

  # 3. 資料庫路徑設定
  DATABASE_DIR = os.path.join(BASE_DIR, 'database')
  SQLALCHEMY_DATABASE_URI = 'sqlite:///' + os.path.join(DATABASE_DIR, 'menu.db')
  SQLALCHEMY_TRACK_MODIFICATIONS = False
  SQLALCHEMY_ENGINE_OPTIONS = {
      'connect_args': {'timeout': 30, 'check_same_thread': False},
      'pool_pre_ping': True,
      'pool_recycle': 1800,
  }

  # 4. 靜態檔案路徑
  UPLOAD_FOLDER_MEMBER = os.path.join(BASE_DIR, 'static', 'member')
  UPLOAD_FOLDER_MENU = os.path.join(BASE_DIR, 'static', 'menu')

  # 5. AI 模型路徑
  MODELS_DIR = os.path.join(BASE_DIR, 'models')
  YUNET_MODEL = os.path.join(MODELS_DIR, 'face_detection_yunet_2023mar.onnx')
  SFACE_MODEL = os.path.join(MODELS_DIR, 'face_recognition_sface_2021dec.onnx')
  LOCAL_MODEL_PATH = os.path.join(
      MODELS_DIR, 'qwen2.5-1.5b-instruct-q4_k_m.gguf'
  )

  # 6. API Keys 與管理者資訊
  GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
  ADMIN_USERNAME = os.getenv('ADMIN_USERNAME', '1234')

  # 管理者密碼處理：若 .env 沒設定，則隨機生成一組安全初始密碼
  _admin_pwd = os.getenv('ADMIN_PASSWORD')
  if not _admin_pwd:
    _admin_pwd = secrets.token_urlsafe(12)
    print(
        '\n'
        + '=' * 60
        + '\n⚠️  [安全警告] 未在 .env 設定 ADMIN_PASSWORD！\n'
        f'預設帳號: {ADMIN_USERNAME}\n'
        f'自動生成的臨時初始密碼: {_admin_pwd}\n'
        '請妥善保管此密碼，登入後建議立即修改！\n'
        + '=' * 60
        + '\n'
    )
  ADMIN_PASSWORD = _admin_pwd
  # 補齊 migration_service.py 所需的雜湊值，徹底解決找不到屬性的 Bug！
  ADMIN_PASSWORD_HASH = generate_password_hash(ADMIN_PASSWORD)

  # 7. 備份與還原安全限制
  MAX_SINGLE_FILE_SIZE = 15 * 1024 * 1024
  MAX_TOTAL_EXTRACT_SIZE = 150 * 1024 * 1024

  # 8. 資安防護 Cookie 與 Session 規則
  SESSION_COOKIE_HTTPONLY = (
      True  # 禁止 JavaScript 讀取 Session Cookie，防禦 XSS
  )
  SESSION_COOKIE_SAMESITE = 'Lax'  # 防禦跨站請求偽造（CSRF）
  PERMANENT_SESSION_LIFETIME = timedelta(minutes=30)  # 閒置 30 分鐘自動逾期