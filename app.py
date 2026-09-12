import time
import os
import cv2
import json
import io
import re
import sys
import requests
import threading
import unicodedata
import random
import zipfile
import queue
try:
    import google.generativeai as genai
except ModuleNotFoundError:
    genai = None
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from PIL import Image, ImageOps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file 
from flask_sqlalchemy import SQLAlchemy
from openpyxl.styles import Font
from dotenv import load_dotenv
from flask import Response
from werkzeug.security import generate_password_hash, check_password_hash
from llama_cpp import Llama
from werkzeug.utils import secure_filename
from sqlalchemy import event
from sqlalchemy.engine import Engine

# ==============================================================================
# 1. 應用程式基礎設定 (App Configuration)
# ==============================================================================
# 最大單一還原檔案限制 15MB，總解壓縮量限制 150MB
MAX_SINGLE_FILE_SIZE = 15 * 1024 * 1024
MAX_TOTAL_EXTRACT_SIZE = 150 * 1024 * 1024


# 全域鎖定物件，防止多線程同時修改訂單資料
_ORDER_CHECKOUT_LOCK = threading.Lock()

# 自動載入專案根目錄的 .env 檔案
load_dotenv()

# 從環境變數安全讀取金鑰
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# 從 .env 讀取管理員帳密 (若未設定則給予預設值)
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "1234")

# 讀取明文密碼後立即透過 generate_password_hash 轉換
ADMIN_PASSWORD_HASH = generate_password_hash(os.getenv("ADMIN_PASSWORD", "1234"))

if GEMINI_API_KEY and genai is not None:
    genai.configure(api_key=GEMINI_API_KEY)

app = Flask(__name__)

# 安全設定：動態或環境變數 Secret Key
app.secret_key = os.getenv("SECRET_KEY")
if not app.secret_key:
    app.secret_key = os.urandom(32).hex()
    print("⚠️ 未在 .env 檢測到 SECRET_KEY，已自動生成臨時 Session 金鑰。建議在 .env 中設定！")

app.permanent_session_lifetime = timedelta(days=7)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# 1. 資料庫資料夾配置
DATABASE_DIR = os.path.join(BASE_DIR, 'database')
os.makedirs(DATABASE_DIR, exist_ok=True)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(DATABASE_DIR, 'menu.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 設定 SQLite 連線池與鎖定等待逾時
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "connect_args": {
        "timeout": 30,
        "check_same_thread": False
    },
    "pool_pre_ping": True
}

# 監聽底層連線建立事件，強制啟用 WAL 模式與效能 PRAGMA
@event.listens_for(Engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL;")        # 開啟讀寫分離 WAL 模式
    cursor.execute("PRAGMA busy_timeout=30000;")       # 遇鎖等待 30000ms
    cursor.execute("PRAGMA synchronous=NORMAL;")       # 提升寫入速度且兼顧 ACID
    cursor.execute("PRAGMA cache_size=-64000;")        # 配置 64MB 記憶體快取
    cursor.close()

# 建立靜態資源上傳路徑
UPLOAD_FOLDER_MEMBER = os.path.join(BASE_DIR, 'static', 'member')
UPLOAD_FOLDER_MENU = os.path.join(BASE_DIR, 'static', 'menu')
os.makedirs(UPLOAD_FOLDER_MEMBER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER_MENU, exist_ok=True)
app.config['UPLOAD_FOLDER_MEMBER'] = UPLOAD_FOLDER_MEMBER
app.config['UPLOAD_FOLDER_MENU'] = UPLOAD_FOLDER_MENU

# 2. AI 模型資料夾配置
MODELS_DIR = os.path.join(BASE_DIR, 'models')
os.makedirs(MODELS_DIR, exist_ok=True)

# OpenCV 人臉辨識模型路徑
YUNET_MODEL = os.path.join(MODELS_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_MODEL = os.path.join(MODELS_DIR, "face_recognition_sface_2021dec.onnx")

# LLaMA 模型路徑 (GGUF 格式)
LOCAL_MODEL_PATH = os.path.join(MODELS_DIR, "qwen2.5-1.5b-instruct-q4_k_m.gguf")

db = SQLAlchemy(app)

# ==============================================================================
# 2. 資料庫模型定義 (Database Models)
# ==============================================================================
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(50), nullable=False)
    photo_path = db.Column(db.String(200), nullable=False)
    feature = db.Column(db.PickleType, nullable=True)
    points = db.Column(db.Integer, default=0)  # 會員紅利點數
    last_login_at = db.Column(db.DateTime, nullable=True)  # 記錄最後登入時間
    last_logout_at = db.Column(db.DateTime, nullable=True)  # 記錄最後登出時間

class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), default='主餐')
    price = db.Column(db.Integer, nullable=False)
    stock = db.Column(db.Integer, default=50)               # 剩餘數量
    total_stock = db.Column(db.Integer, default=50)         # 總量
    is_sold_out = db.Column(db.Boolean, default=False)
    is_discount = db.Column(db.Boolean, default=False)      # 是否特價
    discount_price = db.Column(db.Integer, default=0)       # 特價金額
    is_recommended = db.Column(db.Boolean, default=False)   # 系統自動前 3 名熱門
    is_manual_popular = db.Column(db.Boolean, default=False)# 後台手動勾選熱門
    modifiers = db.Column(db.String(50), default='none')
    description = db.Column(db.String(200), default='')
    image_path = db.Column(db.String(200), default='')
    is_new = db.Column(db.Boolean, default=False)
    is_reward = db.Column(db.Boolean, default=False)          # 是否開放紅利兌換
    reward_points = db.Column(db.Integer, default=0)          # 兌換所需點數
    reward_discount_points = db.Column(db.Integer, default=0) # 限時優惠點數 (0代表無優惠)
    can_be_add_on = db.Column(db.Boolean, default=False)      # 是否可作為加購品
    add_on_price = db.Column(db.Integer, default=0)           # 加購專屬優惠價
    addon_trigger_type = db.Column(db.String(20), default='any')  # 'any', 'category', 'item'
    addon_trigger_target = db.Column(db.String(100), default='')  # 目標分類名稱或餐點名稱

class ComboOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    main_item_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    additional_price = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(200), default='')
    
    # 新增綁定的配餐關聯 (至多三個)
    item1_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=True)
    item2_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=True)
    item3_id = db.Column(db.Integer, db.ForeignKey('menu_item.id'), nullable=True)

    # 配餐是否開放客製化開關 (預設開放 True)
    item1_customizable = db.Column(db.Boolean, default=True)
    item2_customizable = db.Column(db.Boolean, default=True)
    item3_customizable = db.Column(db.Boolean, default=True)

    # 是否開放加購標籤 (預設開啟 True)
    can_addon = db.Column(db.Boolean, default=True)

    # 關聯設定
    main_item = db.relationship('MenuItem', foreign_keys=[main_item_id], backref=db.backref('combo_options', lazy=True, cascade="all, delete-orphan"))
    item1 = db.relationship('MenuItem', foreign_keys=[item1_id])
    item2 = db.relationship('MenuItem', foreign_keys=[item2_id])
    item3 = db.relationship('MenuItem', foreign_keys=[item3_id])

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)
    table_number = db.Column(db.String(50), default='訪客')
    pickup_number = db.Column(db.Integer, default=1)  # 每日取餐編號 (1~999 循環)
    total_price = db.Column(db.Integer, nullable=False)
    payment_method = db.Column(db.String(50), default='Cash')
    order_type = db.Column(db.String(50), default='內用')
    need_cutlery = db.Column(db.Boolean, default=True)
    note = db.Column(db.String(200), default='')
    status = db.Column(db.String(50), default='Pending')
    points_used = db.Column(db.Integer, default=0)
    points_earned = db.Column(db.Integer, default=0)
    discount_amount = db.Column(db.Integer, default=0)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    customer_log_id = db.Column(db.Integer, nullable=True)  # 關聯顧客/訪客進出日誌 ID
    items = db.relationship('OrderItem', backref='order', lazy=True, cascade="all, delete-orphan")
    @property
    def user(self):
        if self.user_id:
            return db.session.get(User, self.user_id)
        return None
    
class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    item_name = db.Column(db.String(100), nullable=False)
    price = db.Column(db.Integer, nullable=False)
    quantity = db.Column(db.Integer, default=1)
    customization = db.Column(db.String(200), default='')

# --- 優惠券模板資料表 ---
class Coupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(50), unique=True, nullable=False)     # 折扣代碼 (如 NEW50, VIP90)
    title = db.Column(db.String(100), nullable=False)                 # 券名稱 (如 全單現折 $50 優惠券)
    discount_type = db.Column(db.String(20), default='fixed')        # 'fixed' (折現金) 或 'percent' (打折)
    discount_value = db.Column(db.Float, nullable=False)             # 折扣額 (如 50 代表折50元；0.9 代表9折)
    min_spend = db.Column(db.Float, default=0)                       # 最低消費門檻 (如滿 200 可用)
    reward_points = db.Column(db.Integer, default=0)                 # 兌換所需點數
    reward_discount_points = db.Column(db.Integer, default=0)        # 限時特惠點數 (0代表無特惠)
    is_reward = db.Column(db.Boolean, default=True)                  # 是否上架至回饋商城

# --- 各會員專屬持有與兌換紀錄資料表 (分開管理) ---
class UserCoupon(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    coupon_id = db.Column(db.Integer, db.ForeignKey('coupon.id'), nullable=False)
    code = db.Column(db.String(50), nullable=False)                  # 優惠代碼
    is_used = db.Column(db.Boolean, default=False)                   # 是否已核銷使用
    created_at = db.Column(db.DateTime, default=datetime.now)        # 兌換時間
    used_at = db.Column(db.DateTime, nullable=True)                  # 使用時間

    user = db.relationship('User', backref=db.backref('user_coupons', lazy=True, cascade="all, delete-orphan"))
    coupon = db.relationship('Coupon', backref=db.backref('user_coupons', lazy=True, cascade="all, delete-orphan"))

# --- 管理員登入/登出日誌資料表 ---
class AdminLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), nullable=False)
    login_at = db.Column(db.DateTime, default=datetime.now)
    logout_at = db.Column(db.DateTime, nullable=True)
    ip_address = db.Column(db.String(50), default='')

# --- 顧客/訪客進出日誌與點餐紀錄---
class CustomerLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_type = db.Column(db.String(20), nullable=False)   # '會員' 或 '訪客'
    identifier = db.Column(db.String(100), nullable=False) # 會員姓名(含電話) 或 訪客隨機編號
    login_at = db.Column(db.DateTime, default=datetime.now)
    logout_at = db.Column(db.DateTime, nullable=True)
    ordered_items = db.Column(db.Text, default='')          # 該階段下單之餐點內容匯總
    ip_address = db.Column(db.String(50), default='')

# --- 顧客進出工作階段輔助函式 ---
def start_customer_session(user_type, identifier):
    """開啟並記錄顧客/訪客登入階段"""
    close_customer_session()  # 若先前有未結束的 Session 則先自動封存
    log = CustomerLog(
        user_type=user_type,
        identifier=identifier,
        login_at=datetime.now(),
        ip_address=request.remote_addr or ''
    )
    db.session.add(log)
    db.session.commit()
    session['customer_log_id'] = log.id
    return log.id

def clear_customer_session():
    """僅清除前台顧客與會員相關的 Session，保留後台管理員狀態"""
    customer_keys = ['user_id', 'user_name', 'user_points', 'is_guest', 'customer_log_id']
    for key in customer_keys:
        session.pop(key, None)

def close_customer_session():
    """標記顧客/訪客登出離線時間"""
    log_id = session.get('customer_log_id')
    if log_id:
        log = db.session.get(CustomerLog, log_id)
        if log and not log.logout_at:
            log.logout_at = datetime.now()
    
    # 若有會員身分，同步記錄最後登出時間
    user_id = session.get('user_id')
    if user_id:
        user = db.session.get(User, user_id)
        if user:
            user.last_logout_at = datetime.now()

    db.session.commit()
    session.pop('customer_log_id', None)
# ==============================================================================
# 3. 資料庫結構自動檢查與補齊 (Auto Migration)
# ==============================================================================
with app.app_context():
    db.create_all()
    
    # 1. 檢查 User 資料表
    user_info = db.session.execute(db.text("PRAGMA table_info(user)")).fetchall()
    user_cols = [col[1] for col in user_info]
    if 'feature' not in user_cols:
        db.session.execute(db.text("ALTER TABLE user ADD COLUMN feature BLOB"))
    if 'points' not in user_cols:
        db.session.execute(db.text("ALTER TABLE user ADD COLUMN points INTEGER DEFAULT 0"))
    if 'last_login_at' not in user_cols:
        db.session.execute(db.text("ALTER TABLE user ADD COLUMN last_login_at DATETIME"))
    if 'last_logout_at' not in user_cols:
        db.session.execute(db.text("ALTER TABLE user ADD COLUMN last_logout_at DATETIME"))

    # 2. 檢查 MenuItem 資料表
    menu_info = db.session.execute(db.text("PRAGMA table_info(menu_item)")).fetchall()
    menu_cols = [col[1] for col in menu_info]
    if 'modifiers' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN modifiers VARCHAR(50) DEFAULT 'none'"))
    if 'description' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN description VARCHAR(200) DEFAULT ''"))
    if 'image_path' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN image_path VARCHAR(200) DEFAULT ''"))
    if 'is_recommended' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_recommended BOOLEAN DEFAULT 0"))
    if 'is_new' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_new BOOLEAN DEFAULT 0"))
    if 'is_reward' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_reward BOOLEAN DEFAULT 0"))
    if 'reward_points' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN reward_points INTEGER DEFAULT 0"))
    if 'reward_discount_points' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN reward_discount_points INTEGER DEFAULT 0"))
    if 'is_discount' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_discount BOOLEAN DEFAULT 0"))
    if 'discount_price' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN discount_price INTEGER DEFAULT 0"))
    if 'modifiers' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN modifiers VARCHAR(50) DEFAULT 'none'"))
    if 'is_sold_out' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_sold_out BOOLEAN DEFAULT 0"))
    if 'stock' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN stock INTEGER DEFAULT 50"))
    if 'total_stock' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN total_stock INTEGER DEFAULT 50"))
    if 'can_be_add_on' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN can_be_add_on BOOLEAN DEFAULT 0"))
    if 'add_on_price' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN add_on_price INTEGER DEFAULT 0"))
    if 'addon_trigger_type' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN addon_trigger_type VARCHAR(20) DEFAULT 'any'"))
    if 'addon_trigger_target' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN addon_trigger_target VARCHAR(100) DEFAULT ''"))

    # 3. 檢查 Order 資料表
    order_info = db.session.execute(db.text('PRAGMA table_info("order")')).fetchall()
    order_cols = [col[1] for col in order_info]
    if 'user_id' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN user_id INTEGER'))
    if 'points_used' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN points_used INTEGER DEFAULT 0'))
    if 'points_earned' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN points_earned INTEGER DEFAULT 0'))
    if 'discount_amount' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN discount_amount INTEGER DEFAULT 0'))
    if 'payment_method' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN payment_method VARCHAR(50) DEFAULT "Cash"'))
    if 'order_type' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN order_type VARCHAR(50) DEFAULT "內用"'))
    if 'need_cutlery' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN need_cutlery BOOLEAN DEFAULT 1'))
    if 'note' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN note VARCHAR(200) DEFAULT ""'))
    if 'status' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN status VARCHAR(50) DEFAULT "Pending"'))
    if 'is_manual_popular' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_manual_popular BOOLEAN DEFAULT 0"))
    if 'completed_at' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN completed_at DATETIME'))
    if 'pickup_number' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN pickup_number INTEGER DEFAULT 1'))
    if 'customer_log_id' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN customer_log_id INTEGER'))
    # 自動依訂單先後重新賦予 1~999 取餐流水號
    try:
        from collections import defaultdict
        all_orders = Order.query.order_by(Order.created_at.asc(), Order.id.asc()).all()
        
        # 依日期 (YYYY-MM-DD) 分組
        orders_by_date = defaultdict(list)
        for o in all_orders:
            d_key = o.created_at.date() if o.created_at else datetime.now().date()
            orders_by_date[d_key].append(o)
        
        needs_commit = False
        for d_key, d_orders in orders_by_date.items():
            p_nums = [o.pickup_number for o in d_orders]
            if len(d_orders) > 1 and len(set(p_nums)) <= 1:
                cur_no = 1
                for o in d_orders:
                    o.pickup_number = cur_no
                    cur_no = (cur_no % 999) + 1
                needs_commit = True
            elif len(d_orders) == 1 and d_orders[0].pickup_number is None:
                d_orders[0].pickup_number = 1
                needs_commit = True

        if needs_commit:
            db.session.commit()
            print("✅ 歷史訂單取餐流水號校正完成！")
    except Exception as e:
        db.session.rollback()
        print(f"校正歷史取餐編號失敗: {e}")
    
    # 4. 檢查 OrderItem 資料表
    order_item_info = db.session.execute(db.text("PRAGMA table_info(order_item)")).fetchall()
    order_item_cols = [col[1] for col in order_item_info]
    if 'customization' not in order_item_cols:
        db.session.execute(db.text("ALTER TABLE order_item ADD COLUMN customization VARCHAR(200) DEFAULT ''"))

    # 5.檢查 ComboOption 是否需要補齊配餐欄位
    combo_info = db.session.execute(db.text("PRAGMA table_info(combo_option)")).fetchall()
    if combo_info:
        combo_cols = [col[1] for col in combo_info]
        if 'item1_id' not in combo_cols:
            db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item1_id INTEGER REFERENCES menu_item(id)"))
            db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item2_id INTEGER REFERENCES menu_item(id)"))
            db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item3_id INTEGER REFERENCES menu_item(id)"))
        if 'item1_customizable' not in combo_cols:
            db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item1_customizable BOOLEAN DEFAULT 1"))
            db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item2_customizable BOOLEAN DEFAULT 1"))
            db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item3_customizable BOOLEAN DEFAULT 1"))
        if 'can_addon' not in combo_cols:
            db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN can_addon BOOLEAN DEFAULT 1"))
        db.session.commit()

    # 檢查 MenuItem 是否有 is_manual_popular 欄位，沒有就建立
    menu_info = db.session.execute(db.text("PRAGMA table_info(menu_item)")).fetchall()
    menu_cols = [col[1] for col in menu_info]
    if 'is_manual_popular' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_manual_popular BOOLEAN DEFAULT 0"))
        db.session.commit()
    db.session.commit()

# ==============================================================================
# 4. 影像處理與人臉辨識演算法核心 (AI & Image Processing)
# ==============================================================================
def save_and_fix_image(file_storage, dest_path):
    """讀取照片、修正 EXIF 方向、驗證圖檔完整性並壓縮存檔"""
    try:
        # 重置指針防止空檔讀取
        file_storage.seek(0)
        img = Image.open(file_storage)
        
        # 驗證圖檔完整性
        img.verify()
        
        # verify() 會關閉檔案，重新開啟以供處理
        file_storage.seek(0)
        img = Image.open(file_storage)
        
        img = ImageOps.exif_transpose(img)
        img = img.convert("RGB")
        img.thumbnail((800, 800))
        img.save(dest_path, "JPEG", quality=88)
        return True
    except Exception as e:
        print(f"[!] 圖片讀取或壓縮失敗 (檔案損壞或格式不支援): {e}")
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        return False

def extract_feature(image_path):
    """使用 YuNet 與 SFace 進行人臉特徵提取 (含矩陣邊界安全檢查)"""
    if not os.path.exists(YUNET_MODEL) or not os.path.exists(SFACE_MODEL):
        return None

    try:
        detector = cv2.FaceDetectorYN.create(YUNET_MODEL, "", (320, 320), 0.6, 0.3, 5000)
        recognizer = cv2.FaceRecognizerSF.create(SFACE_MODEL, "")

        img = cv2.imread(image_path)
        if img is None or img.size == 0:
            return None

        h, w, _ = img.shape
        if h < 20 or w < 20:  # 尺寸過小不處理
            return None

        detector.setInputSize((w, h))
        _, faces = detector.detect(img)

        if faces is not None and len(faces) > 0:
            aligned_face = recognizer.alignCrop(img, faces[0])
            feature = recognizer.feature(aligned_face)
            return feature
    except Exception as e:
        print(f"[!] 人臉特徵分析過程異常: {e}")
        return None

    return None

def compare_faces(feat1, feat2):
    """比對兩個人臉特徵向量的餘弦相似度 (Cosine Similarity)"""
    if feat1 is None or feat2 is None:
        return 0.0
    recognizer = cv2.FaceRecognizerSF.create(SFACE_MODEL, "")
    score = recognizer.match(feat1, feat2, cv2.FaceRecognizerSF_FR_COSINE)
    return score

def update_popular_items():
    """
    結合「銷量前 3 名」與「後台手動勾選」的品項，統一設為熱門 (is_recommended)
    (已排除點數兌換之品項)
    """
    try:
        # 1. 先將【所有】餐點的 is_recommended 重設為 False
        db.session.query(MenuItem).update({MenuItem.is_recommended: False})
        db.session.flush()

        # 2. 統計有效訂單中銷量最高的前 3 名 (排除包含 [點數兌換] 的品項)
        sales_stats = db.session.query(
            OrderItem.item_name,
            db.func.sum(OrderItem.quantity).label('total_qty')
        ).join(Order, OrderItem.order_id == Order.id
        ).filter(Order.status != 'Cancelled'
        ).filter(~OrderItem.item_name.like('%[點數兌換]%')  #   排除點數兌換
        ).group_by(OrderItem.item_name
        ).order_by(db.desc('total_qty')
        ).all()

        top_3_names = [name for name, qty in sales_stats[:3] if qty and qty > 0]

        # 3. 將銷量前 3 名的餐點設為 True
        if top_3_names:
            MenuItem.query.filter(MenuItem.name.in_(top_3_names)).update(
                {MenuItem.is_recommended: True}, 
                synchronize_session=False
            )

        # 4. 將「手動勾選 (is_manual_popular)」的品項也一併設為 True
        MenuItem.query.filter_by(is_manual_popular=True).update(
            {MenuItem.is_recommended: True},
            synchronize_session=False
        )

        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"自動更新熱門失敗: {e}")

# ==============================================================================
# 全域 Request 限制與 413 例外攔截
# ==============================================================================
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 限制請求上限
@app.errorhandler(413)
def request_entity_too_large(error):
    """防止過大 Payload 造成伺服器 OOM"""
    if request.is_json or request.path.startswith('/api/'):
        return jsonify({'success': False, 'message': '上傳內容超出系統限制 (上限 50MB)！'}), 413
    return "<script>alert('❌ 上傳檔案過大，超出伺服器處理限制 (最大 50MB)！'); window.history.back();</script>", 413

# ==============================================================================
# 5. 前台點餐與行銷優惠路由 (Customer & Marketing Routes)
# ==============================================================================
@app.route('/')
def customer_index():
    update_popular_items()
    items = MenuItem.query.all()
    categories = sorted(list(set(item.category for item in items if item.category)))
    user_points = 0
    user_coupons = []
    personalized_items = []  # 個人化常點餐點清單
    is_guest = session.get('is_guest', False)
    
    if session.get('user_id'):
        user = db.session.get(User, session['user_id'])
        if user:
            user_points = user.points
            session['user_points'] = user.points
            user_coupons = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
            session['is_guest'] = False
            is_guest = False

            user_orders = Order.query.filter(
                Order.user_id == user.id,
                Order.status != 'Cancelled'
            ).all()

            if not user_orders and user.name and user.name.strip() not in ['訪客', 'Guest', '']:
                user_orders = Order.query.filter(
                    Order.user_id.is_(None),
                    Order.table_number == user.name.strip(),
                    Order.status != 'Cancelled'
                ).all()

            if user_orders:
                item_dict = {it.name.strip(): it for it in items}
                user_item_counts = {}

                for order in user_orders:
                    for oi in order.items:
                        # 排除紅利免費兌換、商城贈品
                        if '[點數兌換]' in oi.item_name or oi.item_name.startswith('🎁') or oi.customization == '紅利免費兌換':
                            continue
                        
                        # 清理餐點名稱字串
                        clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', oi.item_name).strip()
                        
                        # 確保該品項存在於目前菜單中
                        if clean_name in item_dict:
                            user_item_counts[clean_name] = user_item_counts.get(clean_name, 0) + int(oi.quantity or 1)

                # 依照「該會員個人點購總次數」由高至低排序，精選前 3 名
                sorted_fav_names = sorted(
                    user_item_counts.keys(), 
                    key=lambda k: user_item_counts[k], 
                    reverse=True
                )[:3]

                personalized_items = [item_dict[name] for name in sorted_fav_names if name in item_dict]
            
    return render_template(
        'customer.html',
        items=items,
        categories=categories,
        user_points=user_points,
        user_coupons=user_coupons,
        personalized_items=personalized_items,
        is_guest=is_guest
    )

@app.route('/api/verify_promo', methods=['POST'])
def verify_promo():
    """驗證促銷優惠代碼（嚴格檢查會員專屬持有與核銷狀態）"""
    data = request.get_json() or {}
    code = str(data.get('promo_code', '')).strip().upper()
    subtotal = float(data.get('subtotal', 0))
    user_id = session.get('user_id')

    if not code:
        return jsonify({'valid': False, 'discount': 0, 'message': '請輸入優惠代碼！'})

    # 1. 優先檢查是否為當前會員已兌換且「未使用」的專屬優惠券
    coupon = None
    if user_id:
        user_coupon = UserCoupon.query.filter_by(user_id=user_id, code=code, is_used=False).first()
        if user_coupon:
            coupon = user_coupon.coupon

    # 2. 若會員未持有，檢查是否為免點數之公開通用促銷碼 (reward_points == 0 且非商城券)
    if not coupon:
        public_coupon = Coupon.query.filter_by(code=code).first()
        if public_coupon and (public_coupon.reward_points == 0 and not public_coupon.is_reward):
            coupon = public_coupon
        elif public_coupon and public_coupon.is_reward:
            if not user_id:
                return jsonify({'valid': False, 'discount': 0, 'message': '此為會員紅利專屬券，請先登入會員！'})
            else:
                return jsonify({'valid': False, 'discount': 0, 'message': '您尚未在回饋商城兌換此券，或該券已被使用！'})
        else:
            return jsonify({'valid': False, 'discount': 0, 'message': '無效的優惠代碼！'})

    # 3. 門檻檢查
    if subtotal < coupon.min_spend:
        return jsonify({'valid': False, 'discount': 0, 'message': f'未達使用門檻！需消費滿 ${int(coupon.min_spend)} 元才可折抵。'})

    # 4. 計算折扣
    if coupon.discount_type == 'fixed':
        discount = min(coupon.discount_value, subtotal)
        msg = f'已折抵現金 ${int(discount)} 元！'
    else:
        discount = round(subtotal * (1.0 - coupon.discount_value))
        msg = f'已套用 {round(coupon.discount_value * 10, 1)} 折優惠，折抵 ${int(discount)} 元！'

    return jsonify({'valid': True, 'discount': discount, 'message': msg})

@app.route('/submit_order', methods=['POST'])
def submit_order():
    """原子扣減庫存、點數與優惠券，互斥鎖保護取餐號碼"""
    data = request.get_json() or {}
    raw_items = data.get('items', [])
    payment_method = data.get('payment_method', 'Cash')
    order_type = data.get('order_type', '內用')
    need_cutlery = bool(data.get('need_cutlery', True)) if order_type == '外帶' else False
    note = str(data.get('note', '')).strip()[:200]
    promo_code = str(data.get('promo_code', '')).strip().upper()
    use_points = int(data.get('use_points', 0))

    if not raw_items:
        return jsonify({'error': '購物車為空'}), 400

    user_id = session.get('user_id')

    # ----------------------------------------------------
    # 步驟 1：後端計價與品項合法性預檢驗
    # ----------------------------------------------------
    verified_items = []
    item_demands = {}

    for client_item in raw_items:
        raw_name = str(client_item.get('name', '')).strip()
        raw_custom = str(client_item.get('customization', '')).strip()
        quantity = max(1, int(client_item.get('quantity', 1)))
        is_add_on = bool(client_item.get('is_add_on', False))
        is_claimed_reward = ('[點數兌換]' in raw_name) or ('reward_' in str(client_item.get('id', '')))

        clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', raw_name).strip()

        menu_item = MenuItem.query.filter_by(name=clean_name).first()
        if not menu_item:
            try:
                menu_item = db.session.get(MenuItem, int(client_item.get('id')))
            except (ValueError, TypeError):
                menu_item = None

        if not menu_item:
            return jsonify({'error': f'餐點【{clean_name}】不存在於系統中！'}), 400

        item_demands[menu_item.id] = item_demands.get(menu_item.id, 0) + quantity

        # 客製化加價
        extra_modifier_price = sum(int(m) for m in re.findall(r'\(\+(\d+)\)', raw_custom))

        if is_claimed_reward:
            if not user_id:
                return jsonify({'error': '非會員無法訂購紅利商城兌換餐點！'}), 403
            if not menu_item.is_reward:
                return jsonify({'error': f'餐點【{menu_item.name}】未開放紅利兌換！'}), 400
            unit_price = extra_modifier_price
        elif is_add_on:
            if not menu_item.can_be_add_on:
                return jsonify({'error': f'餐點【{menu_item.name}】非合法加購品項！'}), 400
            unit_price = menu_item.add_on_price + extra_modifier_price
        else:
            base_price = menu_item.discount_price if (menu_item.is_discount and menu_item.discount_price > 0) else menu_item.price
            combo_extra_price = 0
            combo_match = re.search(r'\[套餐:\s*([^\]]+)\]', raw_custom)
            if combo_match:
                combo_name = combo_match.group(1).strip()
                combo_opt = ComboOption.query.filter_by(main_item_id=menu_item.id, name=combo_name).first()
                if combo_opt:
                    combo_extra_price = combo_opt.additional_price
            unit_price = base_price + combo_extra_price + extra_modifier_price

        verified_items.append({
            'menu_item_id': menu_item.id,
            'menu_item_name': menu_item.name,
            'display_name': raw_name,
            'price': int(unit_price),
            'quantity': quantity,
            'customization': raw_custom
        })

    subtotal = sum(v['price'] * v['quantity'] for v in verified_items)

    # ----------------------------------------------------
    # 步驟 2：進入臨界區
    # ----------------------------------------------------
    with _ORDER_CHECKOUT_LOCK:
        try:
            # 2.1 每日取餐編號 (1 ~ 999 循環)
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)

            max_pickup = db.session.query(db.func.max(Order.pickup_number)).filter(
                Order.created_at >= today_start,
                Order.created_at <= today_end
            ).scalar()

            next_pickup = (max_pickup % 999) + 1 if (max_pickup and max_pickup > 0) else 1

            # 2.2 條件扣減庫存
            for item_id, demanded_qty in item_demands.items():
                result = db.session.execute(
                    db.text("""
                        UPDATE menu_item 
                        SET stock = stock - :qty,
                            is_sold_out = CASE WHEN stock - :qty <= 0 THEN 1 ELSE is_sold_out END
                        WHERE id = :id AND stock >= :qty
                    """),
                    {"id": item_id, "qty": demanded_qty}
                )
                if result.rowcount == 0:
                    db.session.rollback()
                    m_item = db.session.get(MenuItem, item_id)
                    curr = m_item.stock if m_item else 0
                    return jsonify({
                        'success': False,
                        'insufficient_stock': True,
                        'message': f'慢了一步！餐點【{m_item.name if m_item else ""}】庫存僅剩 {curr} 份，已被搶購完畢。'
                    }), 400

            # 2.3 優惠券核銷
            promo_discount = 0
            target_user_coupon = None
            user = db.session.get(User, user_id) if user_id else None

            if promo_code and user:
                target_user_coupon = UserCoupon.query.filter_by(
                    user_id=user.id, code=promo_code, is_used=False
                ).first()

                if target_user_coupon and subtotal >= target_user_coupon.coupon.min_spend:
                    cp = target_user_coupon.coupon
                    if cp.discount_type == 'fixed':
                        promo_discount = min(cp.discount_value, subtotal)
                    else:
                        promo_discount = round(subtotal * (1.0 - cp.discount_value))

                    # 條件式原子更新：確保該優惠券尚未被其他並發請求核銷
                    coupon_update = db.session.execute(
                        db.text("""
                            UPDATE user_coupon 
                            SET is_used = 1, used_at = :now 
                            WHERE id = :id AND is_used = 0
                        """),
                        {"id": target_user_coupon.id, "now": datetime.now()}
                    )
                    if coupon_update.rowcount == 0:
                        db.session.rollback()
                        return jsonify({'error': '該優惠券已被核銷或正在處理中，不可重複套用！'}), 400

            # 公開優惠券核算
            if promo_code and not target_user_coupon:
                pub_cp = Coupon.query.filter_by(code=promo_code, is_reward=False, reward_points=0).first()
                if pub_cp and subtotal >= pub_cp.min_spend:
                    if pub_cp.discount_type == 'fixed':
                        promo_discount = min(pub_cp.discount_value, subtotal)
                    else:
                        promo_discount = round(subtotal * (1.0 - pub_cp.discount_value))

            remaining_amount = max(0, subtotal - promo_discount)

            # 2.4 會員紅利點數扣減
            points_used = 0
            if user and use_points > 0:
                points_used = int(min(user.points, use_points, remaining_amount))
                if points_used > 0:
                    pts_update = db.session.execute(
                        db.text("""
                            UPDATE user 
                            SET points = points - :used 
                            WHERE id = :id AND points >= :used
                        """),
                        {"id": user.id, "used": points_used}
                    )
                    if pts_update.rowcount == 0:
                        db.session.rollback()
                        return jsonify({'error': '會員紅利點數不足或已在其他終端變更，請重新整理！'}), 400

            final_price = max(0, remaining_amount - points_used)
            total_discount = promo_discount + points_used
            points_earned = int(final_price // 100) if user else 0

            if user and points_earned > 0:
                db.session.execute(
                    db.text("UPDATE user SET points = points + :earned WHERE id = :id"),
                    {"id": user.id, "earned": points_earned}
                )

            # 2.5 寫入 Order 與 OrderItem
            user_name = session.get('user_name', '訪客')
            new_order = Order(
                user_id=user_id,
                table_number=user_name,
                total_price=int(final_price),
                payment_method=payment_method,
                order_type=order_type,
                need_cutlery=need_cutlery,
                note=note,
                status='Pending',
                points_used=points_used,
                points_earned=points_earned,
                discount_amount=int(total_discount),
                pickup_number=next_pickup,
                customer_log_id=session.get('customer_log_id')
            )
            db.session.add(new_order)
            db.session.flush()

            receipt_items = []
            for v in verified_items:
                order_item = OrderItem(
                    order_id=new_order.id,
                    item_name=v['display_name'],
                    price=v['price'],
                    quantity=v['quantity'],
                    customization=v['customization']
                )
                db.session.add(order_item)
                receipt_items.append({
                    'name': v['display_name'],
                    'price': v['price'],
                    'quantity': v['quantity'],
                    'subtotal': v['price'] * v['quantity'],
                    'customization': v['customization']
                })
            db.session.commit()

        except Exception as e:
            db.session.rollback()
            print(f"[!] 下單交易並發異常: {e}")
            return jsonify({'error': '系統繁忙，交易未完成，請稍候重試！'}), 500

    # ----------------------------------------------------
    # 步驟 3：交易成功後的狀態更新與廣播
    # ----------------------------------------------------
    if user:
        # 重新整理最新點數至 Session
        refreshed_user = db.session.get(User, user_id)
        session['user_points'] = refreshed_user.points if refreshed_user else 0

    update_popular_items()
    _AI_ADVICE_STATE['last_signature'] = None

    # 更新顧客訪問日誌
    log_id = session.get('customer_log_id')
    if log_id:
        customer_log = db.session.get(CustomerLog, log_id)
        if customer_log:
            items_summary = ", ".join([
                f"{it['name']} x{it['quantity']}" + (f"({it['customization']})" if it['customization'] else "")
                for it in receipt_items
            ])
            entry = f"[取餐#{next_pickup} 單號#{new_order.id}] {items_summary}"
            customer_log.ordered_items = f"{customer_log.ordered_items} ； {entry}" if customer_log.ordered_items else entry
            customer_log.logout_at = datetime.now()
            db.session.commit()
        session.pop('customer_log_id', None)

    latest_user_coupons = []
    if user:
        avail_cps = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
        for uc in avail_cps:
            latest_user_coupons.append({
                'code': uc.code,
                'title': uc.coupon.title,
                'min_spend': uc.coupon.min_spend
            })

    order_event_bus.notify()

    return jsonify({
        'order_id': new_order.id,
        'pickup_number': next_pickup,
        'user_name': user_name,
        'subtotal': subtotal,
        'discount_amount': total_discount,
        'points_used': points_used,
        'points_earned': points_earned,
        'total_price': final_price,
        'payment_method': payment_method,
        'order_type': order_type,
        'need_cutlery': need_cutlery,
        'note': note,
        'items': receipt_items,
        'current_user_points': session.get('user_points', 0),
        'available_coupons': latest_user_coupons
    })

# --- 動態紅利回饋商城頁面 ---
@app.route('/rewards')
def rewards_store():
    if not session.get('user_id'):
        return redirect(url_for('face_login'))
    
    user = db.session.get(User, session['user_id'])
    if not user:
        return redirect(url_for('logout'))
        
    reward_items = MenuItem.query.filter_by(is_reward=True).all()
    reward_coupons = Coupon.query.filter_by(is_reward=True).all()
    session['user_points'] = user.points
    return render_template('rewards.html', user=user, reward_items=reward_items, reward_coupons=reward_coupons)

# --- 點數兌換餐點與專屬優惠券 API ---
@app.route('/api/redeem_reward', methods=['POST'])
def redeem_reward():
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '請先登入會員！'}), 401
        
    user_id = session['user_id']
    data = request.get_json() or {}

    # 1. 兌換優惠券
    if 'coupon_id' in data:
        coupon = db.session.get(Coupon, data.get('coupon_id'))
        if not coupon:
            return jsonify({'success': False, 'message': '無效的優惠券！'}), 400
        
        req_points = coupon.reward_discount_points if coupon.reward_discount_points > 0 else coupon.reward_points
        # 扣減點數：必須點數足夠才能扣減成功
        res = db.session.execute(
            db.text("UPDATE user SET points = points - :pts WHERE id = :id AND points >= :pts"),
            {"id": user_id, "pts": req_points}
        )
        if res.rowcount == 0:
            db.session.rollback()
            return jsonify({'success': False, 'message': f'紅利點數不足或已被扣除！兌換需要 {req_points} 點。'}), 400

        user_coupon = UserCoupon(
            user_id=user_id,
            coupon_id=coupon.id,
            code=coupon.code,
            is_used=False
        )
        db.session.add(user_coupon)
        db.session.commit()

        refreshed_user = db.session.get(User, user_id)
        session['user_points'] = refreshed_user.points

        return jsonify({
            'success': True,
            'is_coupon': True,
            'promo_code': coupon.code,
            'message': f'🎉 成功使用 {req_points} 點兌換【{coupon.title}】！\n已存入您的個人專屬票夾，結帳代碼為：{coupon.code}',
            'remaining_points': refreshed_user.points
        })

    # 2. 兌換餐點 (含加料選項)
    item = db.session.get(MenuItem, data.get('item_id'))
    if not item or not item.is_reward:
        return jsonify({'success': False, 'message': '無效的兌換商品！'}), 400

    if item.is_sold_out or (item.stock is not None and item.stock <= 0):
        return jsonify({'success': False, 'message': f'餐點【{item.name}】已售罄，暫無法兌換！'}), 400
    
    req_points = item.reward_discount_points if item.reward_discount_points > 0 else item.reward_points

    # 扣減點數
    res = db.session.execute(
        db.text("UPDATE user SET points = points - :pts WHERE id = :id AND points >= :pts"),
        {"id": user_id, "pts": req_points}
    )
    if res.rowcount == 0:
        db.session.rollback()
        return jsonify({'success': False, 'message': f'紅利點數不足或已被扣除！兌換需要 {req_points} 點。'}), 400

    db.session.commit()
    refreshed_user = db.session.get(User, user_id)
    session['user_points'] = refreshed_user.points
    
    customization = str(data.get('customization', '')).strip()
    extra_price = int(data.get('extra_price', 0))
    
    return jsonify({
        'success': True,
        'is_coupon': False,
        'message': f'🎉 成功使用 {req_points} 點兌換【{item.name}】！',
        'remaining_points': refreshed_user.points,
        'redeemed_item': {
            'id': f'reward_{item.id}_{int(time.time())}',
            'name': f'🎁 [點數兌換] {item.name}',
            'price': extra_price,
            'customization': customization if customization else '紅利免費兌換',
            'quantity': 1
        }
    })

@app.route('/my_orders')
def my_orders():
    """查詢當前登入使用者的歷史訂單記錄 (加入會員獨立專屬序號)"""
    user_id = session.get('user_id')
    user_name = session.get('user_name')

    if user_id:
        orders = Order.query.filter_by(user_id=user_id).order_by(Order.id.desc()).all()
    elif user_name:
        orders = Order.query.filter_by(table_number=user_name).order_by(Order.id.desc()).all()
    else:
        return jsonify([])

    total_count = len(orders) 
    result = []
    status_map = {
        'Pending': '製作中',
        'Completed': '已出餐',
        'PickedUp': '已取餐',
        'Cancelled': '已取消'
    }
    
    for idx, o in enumerate(orders):
        user_order_no = total_count - idx

        items_data = []
        for i in o.items:
            items_data.append({
                'name': i.item_name,
                'price': i.price,
                'quantity': i.quantity,
                'subtotal': i.price * i.quantity,
                'customization': i.customization or ''
            })
        pickup_val = o.pickup_number if (getattr(o, 'pickup_number', None) is not None and o.pickup_number > 0) else o.id

        result.append({
            'order_id': o.id,
            'user_order_no': user_order_no, 
            'pickup_number': pickup_val,
            'total_price': round(o.total_price),
            'discount_amount': round(o.discount_amount or 0),
            'points_used': round(o.points_used or 0),
            'points_earned': o.points_earned or 0,
            'payment_method': o.payment_method,
            'order_type': o.order_type,
            'status': status_map.get(o.status, o.status),
            'created_at': o.created_at.strftime('%Y-%m-%d %H:%M') if o.created_at else '',
            'items': items_data
        })
    return jsonify(result)

# ==============================================================================
# 6. 會員系統與人臉認證路由 (Member & Authentication Routes)
# ==============================================================================
@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        phone = request.form.get('phone')
        photo = request.files.get('photo')
        
        if not name or not phone or not photo or photo.filename == '':
            return "<script>alert('請填寫完整資訊並拍攝/上傳照片！'); window.history.back();</script>", 400

        filename = f"{phone}_{int(time.time())}.jpg"
        filepath = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], filename)
        success = save_and_fix_image(photo, filepath)
        if not success:
            return "<script>alert('❌ 上傳的相片檔案已損壞或格式不支援，請上傳清晰的 JPG/PNG 圖片！'); window.history.back();</script>", 400

        feature = extract_feature(filepath)
        if feature is None:
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            return "<script>alert('⚠️ 照片未偵測到清晰人臉！請正對鏡頭並重新拍攝。'); window.history.back();</script>", 400


        new_user = User(name=name, phone=phone, photo_path=filename, feature=feature, points=20, last_login_at=datetime.now())
        db.session.add(new_user)
        db.session.commit()

        session['user_id'] = new_user.id
        session['user_name'] = new_user.name
        session['user_points'] = new_user.points
        start_customer_session('會員', f"{new_user.name} ({new_user.phone})")

        return redirect(url_for('customer_index'))
        
    return render_template('register.html', login_mode=False)

@app.route('/face_login', methods=['GET', 'POST'])
def face_login():
    if request.method == 'POST':
        photo = request.files.get('photo')
        if not photo:
            return jsonify({'success': False, 'message': '未接收到拍攝照片'})

        temp_path = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], f"temp_{int(time.time())}.jpg")
        save_and_fix_image(photo, temp_path)

        curr_feature = extract_feature(temp_path)
        if os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

        if curr_feature is None:
            return jsonify({'success': False, 'message': '未在畫面中偵測到清晰人臉，請正對鏡頭！'})

        users = User.query.all()
        best_score = 0.0
        matched_user = None

        for u in users:
            if u.feature is not None:
                score = compare_faces(curr_feature, u.feature)
                if score > best_score:
                    best_score = score
                    matched_user = u

        if best_score >= 0.363 and matched_user:
            matched_user.last_login_at = datetime.now()
            matched_user.last_logout_at = None
            db.session.commit()
            session['user_id'] = matched_user.id
            session['user_name'] = matched_user.name
            session['user_points'] = matched_user.points

            start_customer_session('會員', f"{matched_user.name} ({matched_user.phone})")
            return jsonify({
                'success': True,
                'user_name': matched_user.name,
                'points': matched_user.points
            })
        else:
            return jsonify({'success': False, 'message': '人臉比對未通過，請先註冊或重新對準鏡頭！'})
            
    return render_template('register.html', login_mode=True)

@app.route('/phone_login', methods=['POST'])
def phone_login():
    data = request.get_json() or {}
    phone = str(data.get('phone', '')).strip()

    if not phone:
        return jsonify({'success': False, 'message': '請輸入手機號碼！'}), 400

    user = User.query.filter_by(phone=phone).first()
    if user:
        user.last_login_at = datetime.now()
        user.last_logout_at = None
        db.session.commit()
        session['user_id'] = user.id
        session['user_name'] = user.name
        session['user_points'] = user.points

        start_customer_session('會員', f"{user.name} ({user.phone})")
        return jsonify({
            'success': True,
            'user_name': user.name,
            'points': user.points
        })
    else:
        return jsonify({'success': False, 'message': '查無此手機號碼，請確認號碼或先加入會員！'})

# 訪客快速點餐路由
@app.route('/guest_login')
def guest_login():
    """清除舊登入狀態，產生訪客專屬隨機編號並開啟進出紀錄"""
    clear_customer_session()
    
    random_guest_id = f"訪客-{random.randint(1000, 9999)}"
    
    session['user_id'] = None
    session['user_name'] = random_guest_id
    session['is_guest'] = True
    session['user_points'] = 0
    
    start_customer_session('訪客', random_guest_id)
    return redirect(url_for('customer_index'))

@app.route('/logout')
def logout():
    user_id = session.get('user_id')
    if user_id:
        user = db.session.get(User, user_id)
        if user:
            user.last_logout_at = datetime.now()  # 記錄會員登出時間
            db.session.commit()
    close_customer_session()
    clear_customer_session()
    return redirect(url_for('customer_index'))

# ==============================================================================
# AI 智慧營運建議生成函式 (依訂單狀態更新驅動)
# 本地模型 (SLM) 初始化與推論核心 (Llama-cpp-python)
try:
    from llama_cpp import Llama
except ImportError:
    Llama = None
local_llm = None
if Llama and os.path.exists(LOCAL_MODEL_PATH):
    try:
        local_llm = Llama(
            model_path=LOCAL_MODEL_PATH,
            n_ctx=2048,      # 放寬至 2048 避免張量下溢
            n_batch=512,     # 明確指定批次上限
            n_threads=4,     # 配合 CPU 核心數
            verbose=False
        )
        print("✅ 本地繁中 SLM  載入完成！")
    except Exception as e:
        print(f"本地 SLM 載入失敗: {e}")
else:
    if not os.path.exists(LOCAL_MODEL_PATH):
        print("⚠️ 未檢測到本地模型檔 qwen2.5-1.5b-instruct-q4_k_m.gguf，遇超額將退回本地規則。")

# 建立本地模型專屬的執行緒鎖，防止並發衝突導致 llama_decode returned -1
_LLM_LOCK = threading.Lock()

def run_local_slm(prompt_text, max_tokens=100):
    """純本機 Python 推論，線程安全保護版本"""
    if not local_llm:
        return None

    formatted_prompt = f"""<|im_start|>system
你是一位專業餐飲營運顧問，請嚴格使用台灣繁體中文給出 1~2 句具體營運建議（60字以內），禁止思考草稿。<|im_end|>
<|im_start|>user
{prompt_text}<|im_end|>
<|im_start|>assistant
"""
    acquired = _LLM_LOCK.acquire(timeout=3.0)
    if not acquired:
        print("[!] 本地 SLM 忙碌中，立即啟動第三層動態辭庫進行熔斷備援。")
        return None

    try:
        local_llm.reset()
        res = local_llm(
            formatted_prompt,
            max_tokens=max_tokens,
            temperature=0.3,
            stop=["<|im_end|>", "\n\n"]
        )
        output = res["choices"][0]["text"].strip()
        return output.replace('"', '').replace("'", '').replace('「', '').replace('」', '').strip()
    except Exception as e:
        print(f"[!] 本地 SLM 推論異常: {e}")
        return None
    finally:
        _LLM_LOCK.release()
# ==============================================================================
# AI 智慧營運建議全域快取與狀態管理
# ==============================================================================
_AI_ADVICE_STATE = {
    'last_signature': None,
    'cached_advice': None,
    'is_fetching': False,
    'cooldown_until': 0,
    'last_request_time': 0
}

def get_orders_state_signature():
    """取得今日訂單狀態特徵簽章"""
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    orders = db.session.query(Order.id, Order.status).filter(
        Order.created_at >= today_start,
        Order.created_at <= today_end
    ).order_by(Order.id.asc()).all()
    return ",".join(f"{oid}:{status}" for oid, status in orders)

def fallback_advice(analytics_data):
    """第三層防線：本地動態規則推論"""
    pending = analytics_data.get('pending_count', 0)
    eta = analytics_data.get('eta_minutes', 0)
    aov = analytics_data.get('avg_order_value', 0)
    top_items = analytics_data.get('top_items', [])

    if pending >= 5 or eta > 20:
        return f"⚠️ 廚房負載偏高（{pending} 筆待製作，預估等待 {eta} 分鐘），建議啟動備料支援並暫緩外帶出餐推播。"
    elif aov < 150 and aov > 0:
        hot_item = top_items[0]['name'] if top_items else '熱門餐點'
        return f" 平均客單價（${int(aov)}）偏低，建議前台推廣「{hot_item}」加料升級或推播點數滿額加價購優惠券。"
    elif pending == 0 and analytics_data.get('today_orders', 0) > 0:
        return "✅ 目前出餐流程順暢無積單，可安排前台進行備料盤點與清潔。"
    else:
        return "🌟 今日營業剛起步，請確認廚房出單機與各項食材庫存是否充足。"

def _resolve_offline_advice(analytics_data):
    """嘗試第二層本地 SLM 生成；若無本地模型則退回第三層規則"""
    if local_llm:
        print("[*] 正在透過本機 Qwen2.5-1.5B 進行本地推論，請稍候...")
        top_items_str = ", ".join([f"{i['name']}({i['quantity']}份)" for i in analytics_data.get('top_items', [])]) or "尚無"
        prompt = f"數據：待製作 {analytics_data.get('pending_count', 0)} 筆、預估出餐 {analytics_data.get('eta_minutes', 0)} 分鐘、今日營收 ${analytics_data.get('today_revenue', 0)}、熱銷餐點：{top_items_str}。"
        
        local_res = run_local_slm(prompt)
        if local_res and len(local_res) >= 10:
            if not local_res.endswith(('。', '！', '!')):
                local_res += '。'
            result = f"{local_res}"
            print(f"[V] 本機 SLM 生成完成：{result}")
            return result
        else:
            print("[!] 本機 SLM 輸出為空或異常，改用動態規則兜底。")
    else:
        print("[*] 未載入本機模型，直接套用本地動態規則。")

    return fallback_advice(analytics_data)

def _async_fetch_gemini_advice(analytics_data, signature):
    """背景執行緒：所有耗時運算（Gemini / 本地 SLM）皆在此非同步執行"""
    import requests
    global _AI_ADVICE_STATE

    try:
        api_key_clean = (GEMINI_API_KEY or "").strip().strip("'").strip('"')
        
        # 1. 未配置金鑰：直接交給本地 SLM，不發送網路請求
        if not api_key_clean or api_key_clean in ["YOUR_API_KEY", "1234", "none"]:
            print("[*] 未配置 Gemini API Key，直接啟動本地推論模式...")
            _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
            _AI_ADVICE_STATE['last_signature'] = signature
            return

        # 2. 處於冷卻期：直接交給本地 SLM，不重複打雲端 API
        if time.time() < _AI_ADVICE_STATE.get('cooldown_until', 0):
            print("[*] 處於 API 冷卻期，由本地 SLM 進行生成...")
            _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
            _AI_ADVICE_STATE['last_signature'] = signature
            return

        # 3. 正常模式：呼叫 Gemini API
        top_items_str = ", ".join([f"{i['name']}({i['quantity']}份)" for i in analytics_data.get('top_items', [])]) or "尚無"
        prompt = f"""
你是一位專業的餐飲營運顧問。請根據以下今日餐廳的即時營運數據，用繁體中文給出 1~2 句精準、具體的營運行動建議（字數 60 字以內，語氣專業積極）：
- 今日訂單數：{analytics_data.get('today_orders', 0)} 筆
- 今日營收：NT$ {analytics_data.get('today_revenue', 0)}
- 平均客單價：NT$ {int(analytics_data.get('avg_order_value', 0))}
- 當前待製作訂單：{analytics_data.get('pending_count', 0)} 筆
- 當前預估出餐時間：{analytics_data.get('eta_minutes', 0)} 分鐘
- 今日熱銷餐點：{top_items_str}

直接輸出建議內容，不要加多餘問候語。
"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key_clean}"
        headers = {
            "Content-Type": "application/json",
            "x-goog-api-key": api_key_clean
        }
        payload = {
            "contents": [{"parts": [{"text": "【重要指令】：禁止輸出任何英文思考或草稿，繁體中文介紹，必須有完整句號收尾。\n" + prompt}]}],
            "generationConfig": {
                "maxOutputTokens": 800,
                "temperature": 0.3
            }
        }

        print("[*] 背景工作啟動：向 Gemini 請求最新營運建議...")
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        res_json = resp.json()

        if resp.status_code == 200:
            candidates = res_json.get('candidates', [])
            if candidates and 'content' in candidates[0]:
                text = candidates[0]['content']['parts'][0]['text'].strip()
                clean_text = text.replace('"', '').replace('「', '').replace('」', '').strip()
                
                valid_endings = ('。', '！', '!', '；', ';')
                if len(clean_text) >= 12 and clean_text.endswith(valid_endings):
                    _AI_ADVICE_STATE['cached_advice'] = clean_text
                elif len(clean_text) >= 12:
                    _AI_ADVICE_STATE['cached_advice'] = clean_text + '。'
                else:
                    _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
                
                _AI_ADVICE_STATE['last_signature'] = signature
                print(f"[V] 背景生成完成！建議已更新: {_AI_ADVICE_STATE['cached_advice']}")
        elif resp.status_code in [401, 403, 429]:
            _AI_ADVICE_STATE['cooldown_until'] = time.time() + 60
            print(f"[!] Gemini 回應代碼 ({resp.status_code})，啟動 60 秒冷卻並切換本地 SLM。")
            _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
            _AI_ADVICE_STATE['last_signature'] = signature
        else:
            err_msg = res_json.get('error', {}).get('message', '')
            print(f"Gemini API 異常 ({resp.status_code}): {err_msg}")
            _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
            _AI_ADVICE_STATE['last_signature'] = signature
    except Exception as e:
        print(f"背景連線異常，切換至本地備援: {e}")
        _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
        _AI_ADVICE_STATE['last_signature'] = signature
    finally:
        _AI_ADVICE_STATE['is_fetching'] = False

def generate_ai_business_advice(analytics_data):
    """0 毫秒即時回傳：主線程不卡頓，加入 15 秒防抖保護避免打爆 Gemini 免費配額"""
    global _AI_ADVICE_STATE
    current_signature = get_orders_state_signature()
    now_ts = time.time()

    # 1. 訂單特徵有變動時：
    if _AI_ADVICE_STATE['last_signature'] != current_signature:
        # 若仍在 429 冷卻期內：直接交給本地 SLM/規則運算，不打擾雲端
        if now_ts < _AI_ADVICE_STATE.get('cooldown_until', 0):
            _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
            _AI_ADVICE_STATE['last_signature'] = current_signature
            return _AI_ADVICE_STATE['cached_advice']

        # 若距離上次雲端請求小於 15 秒（防抖間隔）：先用本地 SLM 應急，防止觸發 429
        if (now_ts - _AI_ADVICE_STATE.get('last_request_time', 0)) < 15:
            _AI_ADVICE_STATE['cached_advice'] = _resolve_offline_advice(analytics_data)
            _AI_ADVICE_STATE['last_signature'] = current_signature
            return _AI_ADVICE_STATE['cached_advice']

        # 已過間隔且背景無任務：啟動背景執行緒請求 Gemini
        if not _AI_ADVICE_STATE['is_fetching']:
            _AI_ADVICE_STATE['is_fetching'] = True
            _AI_ADVICE_STATE['last_request_time'] = now_ts
            t = threading.Thread(
                target=_async_fetch_gemini_advice,
                args=(analytics_data, current_signature),
                daemon=True
            )
            t.start()

    # 2. 立即回傳快取建議（無快取則即時運算兜底）
    return _AI_ADVICE_STATE['cached_advice'] or fallback_advice(analytics_data)

# ==============================================================================
# 7. 店家後台管理路由 (Admin Management Routes)
# ==============================================================================
def build_order_analytics(orders, limit=1):
    today = datetime.now().date()
    today_orders = [o for o in orders if o.created_at and o.created_at.date() == today and o.status != 'Cancelled']
    pending_orders = [o for o in today_orders if o.status == 'Pending']
    completed_today_orders = [o for o in today_orders if o.status in ['Completed', 'PickedUp']]

    #   1. 平均每單價格：過濾掉實付金額為 0 元的純點數兌換單
    paid_today_orders = [o for o in today_orders if (o.total_price or 0) > 0]
    total_revenue = sum((o.total_price or 0) for o in paid_today_orders)
    avg_order_value = total_revenue / len(paid_today_orders) if paid_today_orders else 0

    #   2. 平均每單時間 (分鐘)：優先以實際出餐時間計算，若無則依餐點數量推估
    actual_durations = []
    for o in completed_today_orders:
        if getattr(o, 'completed_at', None) and o.created_at:
            diff_min = (o.completed_at - o.created_at).total_seconds() / 60.0
            if 0 < diff_min <= 180:  # 排除跨日或異常紀錄
                actual_durations.append(diff_min)

    if actual_durations:
        avg_order_time = round(sum(actual_durations) / len(actual_durations), 1)
    elif today_orders:
        total_items = sum(sum((i.quantity or 1) for i in o.items) for o in today_orders)
        avg_items_per_order = total_items / len(today_orders) if today_orders else 1
        avg_order_time = round(max(3.0, avg_items_per_order * 2.0 + 3.0), 1)
    else:
        avg_order_time = 0.0

    valid_item_names = {item.name for item in MenuItem.query.all()}

    item_stats = {}
    for order in orders:
        if order.status == 'Cancelled':  
            continue
        for item in order.items:
            if '[點數兌換]' in item.item_name or item.item_name.startswith('🎁') or item.customization == '紅利免費兌換':
                continue

            if item.item_name in valid_item_names:
                key = item.item_name
                if key not in item_stats:
                    item_stats[key] = {'quantity': 0, 'revenue': 0}
                item_stats[key]['quantity'] += item.quantity
                item_stats[key]['revenue'] += (item.price or 0) * (item.quantity or 1)

    top_items = [
        {'name': name, 'quantity': stats['quantity'], 'revenue': stats['revenue']}
        for name, stats in sorted(item_stats.items(), key=lambda kv: kv[1]['quantity'], reverse=True)[:limit]
    ]

    total_items_qty = 0
    for order in pending_orders:
        for item in order.items:
            total_items_qty += (item.quantity or 1)
    
    eta_minutes = max(5, total_items_qty * 2 + 4)
    
    analytics_result = {
        'today_orders': len(today_orders),
        'today_revenue': total_revenue,
        'pending_count': len(pending_orders),
        'avg_order_value': avg_order_value,
        'avg_order_time': avg_order_time,
        'top_items': top_items,
        'eta_minutes': eta_minutes,
        'completed_today': len(completed_today_orders),
        'ai_advice': _AI_ADVICE_STATE.get('cached_advice') or fallback_advice({
            'today_orders': len(today_orders),
            'today_revenue': total_revenue,
            'pending_count': len(pending_orders),
            'avg_order_value': avg_order_value,
            'avg_order_time': avg_order_time,
            'top_items': top_items,
            'eta_minutes': eta_minutes,
            'completed_today': len(completed_today_orders)
        })
    }

    return analytics_result

@app.route('/admin', methods=['GET', 'POST'])
def admin_dashboard():
    if request.method == 'POST' and 'username' in request.form:
        username = request.form.get('username')
        password = request.form.get('password')
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session['admin_logged_in'] = True
            # 記錄管理員登入時間
            log = AdminLog(
                username=username,
                login_at=datetime.now(),
                ip_address=request.remote_addr or ''
            )
            db.session.add(log)
            db.session.commit()
            session['admin_log_id'] = log.id
            return redirect(url_for('admin_dashboard'))
        else:
            return "<script>alert('❌ 帳號或密碼錯誤！'); window.history.back();</script>", 401

    if not session.get('admin_logged_in'):
        return render_template('admin.html', is_admin=False)
    
    limit = max(1, request.args.get('limit', 3, type=int))
    all_orders = Order.query.order_by(Order.id.desc()).all()
    items = MenuItem.query.all()
    reward_items = MenuItem.query.filter_by(is_reward=True).all()
    reward_coupons = Coupon.query.all()
    users = User.query.all()
    
    # 1. 營運分析保持使用全部歷史訂單進行統計
    analytics = build_order_analytics(all_orders, limit=limit)
    
    # 2. 實時訂單看板：每日重製
    today = datetime.now().date()
    today_orders = [o for o in all_orders if o.created_at and o.created_at.date() == today]

    return render_template(
        'admin.html', 
        is_admin=True, 
        orders=today_orders, 
        items=items, 
        reward_items=reward_items, 
        reward_coupons=reward_coupons,
        users=users,
        analytics=analytics,
        current_limit=limit
    )

@app.route('/admin/logout')
def admin_logout():
    # 若有當前登入的 Log ID，補上登出時間
    log_id = session.get('admin_log_id')
    if log_id:
        log = db.session.get(AdminLog, log_id)
        if log and not log.logout_at:
            log.logout_at = datetime.now()
            db.session.commit()
            
    session.pop('admin_logged_in', None)
    session.pop('admin_log_id', None)
    return redirect(url_for('admin_dashboard'))

@app.route('/admin/add_coupon', methods=['POST'])
def add_coupon():
    """新增/上架優惠券至回饋商城"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    title = request.form.get('title')
    code = request.form.get('code', '').strip().upper()
    discount_type = request.form.get('discount_type', 'fixed')
    discount_value = max(0.0, float(request.form.get('discount_value') or 0))
    min_spend = max(0.0, float(request.form.get('min_spend') or 0))
    reward_points = max(0, int(request.form.get('reward_points') or 0))
    reward_discount_points = max(0, int(request.form.get('reward_discount_points') or 0))


    existing = Coupon.query.filter_by(code=code).first()
    if existing:
        db.session.rollback()
        return "<script>alert('❌ 該優惠代碼已存在，請使用不同代碼！'); window.history.back();</script>", 400

    new_coupon = Coupon(
        title=title,
        code=code,
        discount_type=discount_type,
        discount_value=discount_value,
        min_spend=min_spend,
        reward_points=reward_points,
        reward_discount_points=reward_discount_points,
        is_reward=True
    )
    db.session.add(new_coupon)
    db.session.commit()
    return redirect(url_for('admin_dashboard', tab='rewards'))

@app.route('/admin/delete_coupon/<int:id>')
def delete_coupon(id):
    """刪除優惠券模板"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    coupon = Coupon.query.get_or_404(id)
    db.session.delete(coupon)
    db.session.expire_all()
    db.session.commit()
    return redirect(url_for('admin_dashboard', tab='rewards'))

@app.route('/admin/edit_coupon/<int:id>', methods=['POST'])
def edit_coupon(id):
    """編輯優惠券模板設定"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    coupon = Coupon.query.get_or_404(id)
    title = request.form.get('title')
    code = request.form.get('code', '').strip().upper()
    discount_type = request.form.get('discount_type', 'fixed')
    discount_value = max(0.0, float(request.form.get('discount_value') or 0))
    min_spend = max(0.0, float(request.form.get('min_spend') or 0))
    reward_points = max(0, int(request.form.get('reward_points') or 0))
    reward_discount_points = max(0, int(request.form.get('reward_discount_points') or 0))

    # 檢查代碼是否與其他優惠券重複 (排除自己)
    existing = Coupon.query.filter(Coupon.code == code, Coupon.id != id).first()
    if existing:
        db.session.rollback()
        return "<script>alert('❌ 該優惠代碼已被其他優惠券使用！'); window.history.back();</script>", 400

    #   若代碼有更動，同步更新會員已持有的票券代碼
    if coupon.code != code:
        UserCoupon.query.filter_by(coupon_id=coupon.id).update({UserCoupon.code: code})

    coupon.title = title
    coupon.code = code
    coupon.discount_type = discount_type
    coupon.discount_value = discount_value
    coupon.min_spend = min_spend
    coupon.reward_points = reward_points
    coupon.reward_discount_points = reward_discount_points

    db.session.commit()
    return redirect(url_for('admin_dashboard', tab='rewards'))

@app.route('/admin/add_reward', methods=['POST'])
def add_reward():
    """上架現有餐點至回饋商城"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    item_id = request.form.get('item_id')
    reward_points = max(0, int(request.form.get('reward_points') or 0))
    reward_discount_points = max(0, int(request.form.get('reward_discount_points') or 0))

    item = MenuItem.query.get_or_404(item_id)
    item.is_reward = True
    item.reward_points = reward_points
    item.reward_discount_points = reward_discount_points
    db.session.commit()
    return redirect(url_for('admin_dashboard', tab='rewards'))

@app.route('/admin/remove_reward/<int:id>')
def remove_reward_item(id):
    """將餐點從回饋商城下架"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    item = MenuItem.query.get_or_404(id)
    item.is_reward = False
    db.session.commit()
    db.session.expire_all()
    return redirect(url_for('admin_dashboard', tab='rewards'))

@app.route('/admin/add', methods=['POST'])
def add_item():
    """手動新增菜單品項"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    name = request.form.get('name')
    category = request.form.get('category', '主餐')
    modifiers = request.form.get('modifiers', 'none')
    price = request.form.get('price')
    stock = max(0, int(request.form.get('stock') or 0))
    total_stock = max(stock, int(request.form.get('total_stock') or stock))
    desc = request.form.get('description', '')
    image = request.files.get('image')
    is_sold_out = request.form.get('is_sold_out') == '1'
    is_rec = request.form.get('is_recommended') == '1'
    is_new = request.form.get('is_new') == '1'
    is_discount = request.form.get('is_discount') == '1'
    discount_price = max(0, round(float(request.form.get('discount_price') or 0)))
    can_be_add_on = True if request.form.get('can_be_add_on') else False
    add_on_price = max(0, round(float(request.form.get('add_on_price') or 0)))
    addon_trigger_type = request.form.get('addon_trigger_type', 'any')
    addon_trigger_target = request.form.get('addon_trigger_target', '').strip()
    

    image_filename = ''
    if image and image.filename != '':
        image_filename = f"menu_{int(time.time())}.jpg"
        filepath = os.path.join(app.config['UPLOAD_FOLDER_MENU'], image_filename)
        save_and_fix_image(image, filepath)

    if name and price:
        final_price = max(0, round(float(price)))
        add_on_price = max(0, round(float(request.form.get('add_on_price') or 0)))
        can_be_add_on = True if request.form.get('can_be_add_on') else False

        if can_be_add_on:
            if add_on_price <= 0:
                db.session.rollback()
                return "<script>alert('❌ 已開啟加購品標籤，請輸入大於 0 的加購專屬價！'); window.history.back();</script>", 400
            if add_on_price >= final_price:
                db.session.rollback()
                return f"<script>alert('❌ 加購專屬價 (${add_on_price}) 不得高於或等於原單價 (${final_price})！'); window.history.back();</script>", 400
        new_item = MenuItem(
            name=name,
            category=category,
            modifiers=modifiers,
            price=max(0, round(float(price))),
            stock=stock,
            total_stock=total_stock,
            description=desc,
            image_path=image_filename,
            is_sold_out=is_sold_out,
            is_recommended=is_rec,
            is_manual_popular=is_rec,
            is_discount=is_discount,
            discount_price=discount_price,
            is_new=is_new,
            can_be_add_on=can_be_add_on,
            add_on_price=add_on_price,
            addon_trigger_type=addon_trigger_type,
            addon_trigger_target=addon_trigger_target
        )
        db.session.add(new_item)
        db.session.commit()
        
    return redirect(url_for('admin_dashboard', tab='menu'))

# ==============================================================================
# AI 智慧菜單文案生成路由 (AI Copywriter)
# ==============================================================================
# ==============================================================================
# AI 智慧菜單文案生成路由 (AI Copywriter - 具備三層自動降級機制)
# ==============================================================================
def fallback_menu_description(name, category):
    """第三層兜底：本地動態辭庫隨機組裝高品質美食文案 (0ms 延遲保證成功)"""
    import random
    intros = [
        "嚴選優質新鮮食材現點現做",
        "特選產地直送原料慢火細熬",
        "黃金比例獨門秘方精準調配",
        "主廚匠心工藝悉心烹調",
        "保留食材純粹原汁原味"
    ]
    traits = [
        "香氣四溢且口感層次鮮明",
        "風味醇厚濃郁且回味無窮",
        "外酥內嫩且肉汁豐盈飽滿",
        "口感清爽甘醇且韻味悠長",
        "每一口都帶來無與倫比的美味體驗"
    ]
    return f"{random.choice(intros)}，【{name}】{random.choice(traits)}。"
@app.route('/api/generate_item_description', methods=['POST'])
def api_generate_item_description():
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'message': '未授權管理者'}), 401

    data = request.get_json() or {}
    name = str(data.get('name', '')).strip()
    category = str(data.get('category', '')).strip()

    if not name:
        return jsonify({'success': False, 'message': '請先輸入餐點名稱！'}), 400

    prompt = f"""
你是一位專業的餐飲品牌文案策劃師。
請為以下餐點撰寫一段誘人、生動且具體的美食商品介紹：
- 餐點名稱：{name}
- 餐點分類：{category if category else '特色美饌'}

【生成要求】：
1. 繁體中文，篇幅約 2~3 句話（簡潔有力，70字以內）。
2. 聚焦於「口感層次」、「料理手法」或「風味特色」（如：酥脆焦香、慢火細熬、清爽甘甜）。
3. 嚴禁包含字數編號 (如 (1)、(2))、引號、Markdown 符號或思考草稿，直接輸出文案本身。
"""

    api_key_clean = (GEMINI_API_KEY or "").strip().strip("'").strip('"')

    # --------------------------------------------------------------------------
    # 第一層：嘗試呼叫 Google Gemini 3.6 Flash (保留型號，檢查是否在 429 冷卻期)
    is_in_cooldown = time.time() < _AI_ADVICE_STATE.get('cooldown_until', 0)
    
    if api_key_clean and api_key_clean not in ["YOUR_API_KEY", "1234", "none"] and not is_in_cooldown:
        try:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.6-flash:generateContent?key={api_key_clean}"
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": api_key_clean
            }
            system_instruction = "你是一位專業的美食文案師。請直接輸出最終的繁體中文介紹，嚴格禁止輸出任何思考過程、草稿檢查、字數驗證或英文自我問答。"
            payload = {
                "contents": [{"parts": [{"text": f"{system_instruction}\n\n{prompt}"}]}],
                "generationConfig": {
                    "maxOutputTokens": 1200,
                    "temperature": 0.2
                }
            }
            print("[*] 正在向 Gemini (gemini-3.6-flash) 請求生成餐點文案...")

            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            res_json = resp.json() if resp.status_code == 200 else {}
            candidates = res_json.get('candidates', [])

            if resp.status_code == 200 and candidates:
                if 'content' in candidates[0]:
                    parts = candidates[0]['content'].get('parts', [])
                    if parts:
                        text = parts[0].get('text', '').strip()

                        # 移除思考雜質
                        text = re.sub(r'(?i)(?:words|chars|perfect|focus on|texture|cooking|flavor|\b(?:yes|no)\b)[^。\n]*[。\n]?', '', text)
                        text = re.sub(r'[a-zA-Z\?\/]+', '', text)

                        # 移除編號與引號
                        cleaned_desc = re.sub(r'[\(\[\（]\s*\d+\s*[\)\]\）]', '', text)
                        cleaned_desc = cleaned_desc.replace('"', '').replace("'", '').replace('「', '').replace('」', '').replace('*', '').replace('`', '').strip()
                        cleaned_desc = re.sub(r'^[，,。\s]+', '', cleaned_desc)

                        if cleaned_desc and not cleaned_desc.endswith(('。', '！', '!')):
                            cleaned_desc += '。'

                        if len(cleaned_desc) >= 12:
                            print("[V] Gemini 文案生成成功！")
                            return jsonify({'success': True, 'description': cleaned_desc})
            elif resp.status_code in [401, 403, 429]:
                _AI_ADVICE_STATE['cooldown_until'] = time.time() + 60
                print(f"[!] Gemini 回應代碼 ({resp.status_code})，啟動 60 秒冷卻並切換本地第二層...")
            else:
                print(f"[!] Gemini 回應代碼 ({resp.status_code})，準備切換至本地第二層...")
        except Exception as e:
            print(f"[!] Gemini 連線異常 ({e})，準備切換至本地第二層...")

    # --------------------------------------------------------------------------
    # 第二層：嘗試呼叫本機 Qwen2.5-1.5B (GGUF 本地大模型)
    if local_llm:
        print("[*] 正在使用本機 Qwen2.5-1.5B 產生商品文案...")
        local_prompt = f"請為餐點【{name}】（分類：{category or '美饌'}）撰寫一段 50 字以內的誘人繁體中文商品介紹，直接輸出文案。"
        local_desc = run_local_slm(local_prompt, max_tokens=120)
        
        if local_desc and len(local_desc) >= 12:
            cleaned_local = re.sub(r'[\(\[\（]\s*\d+\s*[\)\]\）]', '', local_desc)
            cleaned_local = cleaned_local.replace('"', '').replace("'", '').replace('「', '').replace('」', '').strip()
            if not cleaned_local.endswith(('。', '！', '!')):
                cleaned_local += '。'
            
            print(f"[V] 本地 SLM 文案生成完成：{cleaned_local}")
            return jsonify({'success': True, 'description': cleaned_local})
        else:
            print("[!] 本地 SLM 輸出長度不足或為空，退回第三層動態辭庫...")

    # --------------------------------------------------------------------------
    # 第三層
    print("[*] 啟用本地第三層動態辭庫生成文案。")
    fallback_desc = fallback_menu_description(name, category)
    return jsonify({'success': True, 'description': fallback_desc})

@app.route('/admin/import_smart', methods=['POST'])
def import_smart():
    """智慧匯入功能：自動辨識 Excel 中的工作表與純英文/中文欄位，並匯入/更新對應資料表"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    file = request.files.get('file')
    if not file or file.filename == '':
        return "<script>alert('❌ 請選擇 Excel 檔案！'); window.history.back();</script>", 400

    try:
        excel_file = pd.ExcelFile(file)
        imported_counts = {'User': 0, 'MenuItem': 0, 'Coupon': 0}

        def parse_bool(val):
            if pd.isna(val): return False
            s = str(val).strip().lower()
            return s in ['1', 'true', '是', 'yes', 'y']

        def clean_phone_number(val):
            """清理與格式化手機號碼，避免 Excel 浮點數轉換問題"""
            if pd.isna(val): return ""
            s = str(val).strip()
            if s.endswith('.0'):
                s = s[:-2]
            # 若為台灣 9 碼數字（缺開頭 0），補齊為 10 碼
            if len(s) == 9 and s.startswith('9'):
                s = '0' + s
            return s

        # 走訪檔案內所有的工作表 (Sheet)
        for sheet in excel_file.sheet_names:
            df = pd.read_excel(excel_file, sheet_name=sheet)
            if df.empty: continue

            cols_lower = [str(c).lower() for c in df.columns]

            # ---------------------------------------------------------
            # 1. 辨識是否為「會員資料 (User)」
            # ---------------------------------------------------------
            if 'User' in sheet or '會員' in sheet or '電話(Phone)' in df.columns or 'phone' in cols_lower or '電話' in df.columns:
                col_map = {
                    '姓名(Name)': 'name', '姓名': 'name', 'name': 'name',
                    '電話(Phone)': 'phone', '電話': 'phone', 'phone': 'phone',
                    '紅利點數(Points)': 'points', '紅利點數': 'points', 'points': 'points'
                }
                df_user = df.rename(columns=col_map)
                
                if 'name' in df_user.columns and 'phone' in df_user.columns:
                    for _, row in df_user.iterrows():
                        name = str(row.get('name', '')).strip()
                        phone = clean_phone_number(row.get('phone', ''))
                        if not name or not phone or pd.isna(row.get('name')) or name == '目前無資料': 
                            continue
                        
                        try: 
                            points = int(row.get('points', 0))
                        except: 
                            points = 0
                        
                        existing = User.query.filter_by(phone=phone).first()
                        if existing:
                            existing.name = name
                            existing.points = points
                        else:
                            new_user = User(
                                name=name, 
                                phone=phone, 
                                photo_path='', 
                                feature=None, 
                                points=points if points > 0 else 20, 
                                last_login_at=None
                            )
                            db.session.add(new_user)
                        imported_counts['User'] += 1

            # ---------------------------------------------------------
            # 2. 辨識是否為「菜單品項 (MenuItem)」
            # ---------------------------------------------------------
            elif 'MenuItem' in sheet or '菜單' in sheet or '單價(Price)' in df.columns or 'price' in cols_lower or '單價' in df.columns:
                col_map = {
                    '餐點名稱(Name)': 'name', '名稱(Name)': 'name', '餐點名稱': 'name', 'name': 'name',
                    '分類(Category)': 'category', '分類': 'category', 'category': 'category',
                    '單價(Price)': 'price', '單價': 'price', 'price': 'price',
                    '剩餘數量(Stock)': 'stock', '剩餘數量': 'stock', '庫存': 'stock', 'stock': 'stock',
                    '總量(Total Stock)': 'total_stock', '總量': 'total_stock', 'total_stock': 'total_stock',
                    '客製化群組(Modifiers)': 'modifiers', '客製化類型': 'modifiers', 'modifiers': 'modifiers',
                    '商品描述(Description)': 'description', '簡介': 'description', 'description': 'description',
                    '熱門推薦(Recommended)': 'is_recommended', '熱門(Popular)': 'is_recommended', 'is_recommended': 'is_recommended',
                    '新品上市(Is New)': 'is_new', '新品': 'is_new', 'is_new': 'is_new',
                    '是否特價(Is Discount)': 'is_discount', '是否特價': 'is_discount', 'is_discount': 'is_discount',
                    '特價金額(Discount Price)': 'discount_price', '特價金額': 'discount_price', 'discount_price': 'discount_price',
                    '開放紅利兌換(Is Reward)': 'is_reward', '是否開放紅利兌換': 'is_reward', 'is_reward': 'is_reward',
                    '兌換所需點數(Reward Points)': 'reward_points', '兌換點數': 'reward_points', 'reward_points': 'reward_points',
                    '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '特惠點數': 'reward_discount_points', 'reward_discount_points': 'reward_discount_points'
                }
                df_menu = df.rename(columns=col_map)
                
                if 'name' in df_menu.columns and 'price' in df_menu.columns:
                    for _, row in df_menu.iterrows():
                        name = str(row.get('name', '')).strip()
                        if not name or pd.isna(row.get('name')) or name == '目前無資料': 
                            continue
                        
                        category = str(row.get('category', '主餐')).strip() if not pd.isna(row.get('category')) else '主餐'
                        try: price = max(0, round(float(row.get('price', 0))))
                        except: price = 0
                        try: discount_price = max(0, round(float(row.get('discount_price', 0))))
                        except: discount_price = 0
                        try: reward_points = max(0, int(row.get('reward_points', 0)))
                        except: reward_points = 0
                        try: reward_discount_points = max(0, int(row.get('reward_discount_points', 0)))
                        except: reward_discount_points = 0
                        try: stock = max(0, int(row.get('stock', 50)))
                        except: stock = 50
                        try: total_stock = max(stock, int(row.get('total_stock', 50)))
                        except: total_stock = 50

                        modifiers = str(row.get('modifiers', 'none')).strip() if not pd.isna(row.get('modifiers')) else 'none'
                        if modifiers not in ['none', 'ice_sugar', 'spicy', 'addons']: 
                            modifiers = 'none'

                        description = str(row.get('description', '')).strip() if not pd.isna(row.get('description')) else ''
                        
                        is_rec = parse_bool(row.get('is_recommended'))
                        is_new = parse_bool(row.get('is_new'))
                        is_disc = parse_bool(row.get('is_discount'))
                        is_rew = parse_bool(row.get('is_reward'))

                        existing = MenuItem.query.filter_by(name=name).first()
                        if existing:
                            existing.category = category
                            existing.price = price
                            existing.discount_price = discount_price
                            existing.reward_points = reward_points
                            existing.reward_discount_points = reward_discount_points
                            existing.modifiers = modifiers
                            existing.description = description
                            existing.is_manual_popular = is_rec
                            existing.is_new = is_new
                            existing.is_discount = is_disc
                            existing.is_reward = is_rew
                            existing.stock = stock
                            existing.total_stock = total_stock
                            existing.is_sold_out = (stock <= 0)
                        else:
                            new_item = MenuItem(
                                name=name, 
                                category=category, 
                                price=price, 
                                modifiers=modifiers, 
                                description=description,
                                image_path='', 
                                is_recommended=False, 
                                is_manual_popular=is_rec,
                                is_discount=is_disc, 
                                discount_price=discount_price, 
                                is_new=is_new,
                                is_reward=is_rew, 
                                reward_points=reward_points, 
                                reward_discount_points=reward_discount_points,
                                stock=stock,
                                total_stock=total_stock,
                                is_sold_out=(stock <= 0),
                                can_be_add_on=False,
                                add_on_price=0,
                                addon_trigger_type='any',
                                addon_trigger_target=''
                            )
                            db.session.add(new_item)
                        imported_counts['MenuItem'] += 1

            # ---------------------------------------------------------
            # 3. 辨識是否為「優惠券 (Coupon)」
            # ---------------------------------------------------------
            elif 'Coupon' in sheet or '優惠券' in sheet or '代碼(Code)' in df.columns or 'code' in cols_lower or '代碼' in df.columns:
                col_map = {
                    '代碼(Code)': 'code', '代碼': 'code', 'code': 'code', '優惠代碼': 'code',
                    '標題(Title)': 'title', '標題': 'title', 'title': 'title', '優惠券名稱': 'title', '名稱': 'title',
                    '折抵類型(Discount Type)': 'discount_type', '折抵類型': 'discount_type', '折抵方式': 'discount_type', 'discount_type': 'discount_type',
                    '折抵值(Discount Value)': 'discount_value', '折抵值': 'discount_value', '折抵金額': 'discount_value', 'discount_value': 'discount_value',
                    '門檻(Min Spend)': 'min_spend', '門檻': 'min_spend', '最低消費門檻': 'min_spend', '使用門檻': 'min_spend', 'min_spend': 'min_spend',
                    '兌換所需點數(Reward Points)': 'reward_points', '兌換所需點數': 'reward_points', '兌換點數': 'reward_points', '點數': 'reward_points', 'reward_points': 'reward_points',
                    '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '限時特惠點數': 'reward_discount_points', '特惠點數': 'reward_discount_points', 'reward_discount_points': 'reward_discount_points',
                    '上架回饋商城(Is Reward)': 'is_reward', '是否上架': 'is_reward', '上架商城': 'is_reward', 'is_reward': 'is_reward'
                }
                df_coupon = df.rename(columns=col_map)
                
                if 'code' in df_coupon.columns and 'title' in df_coupon.columns:
                    for _, row in df_coupon.iterrows():
                        code = str(row.get('code', '')).strip().upper()
                        title = str(row.get('title', '')).strip()
                        if not code or not title or pd.isna(row.get('code')) or code == '目前無資料': 
                            continue
                        
                        dtype = str(row.get('discount_type', 'fixed')).strip()
                        if dtype not in ['fixed', 'percent']: 
                            dtype = 'fixed'
                        
                        try: dvalue = max(0.0, float(row.get('discount_value', 0)))
                        except: dvalue = 0.0
                        try: min_sp = max(0.0, float(row.get('min_spend', 0)))
                        except: min_sp = 0.0
                        try: reward_points = max(0, int(row.get('reward_points', 0)))
                        except: reward_points = 0
                        try: reward_discount_points = max(0, int(row.get('reward_discount_points', 0)))
                        except: reward_discount_points = 0
                        
                        if 'is_reward' in df_coupon.columns and not pd.isna(row.get('is_reward')):
                            is_reward = parse_bool(row.get('is_reward'))
                        else:
                            is_reward = True if reward_points > 0 else True

                        existing = Coupon.query.filter_by(code=code).first()
                        if existing:
                            existing.title = title
                            existing.discount_type = dtype
                            existing.discount_value = dvalue
                            existing.min_spend = min_sp
                            existing.reward_points = reward_points
                            existing.reward_discount_points = reward_discount_points
                            existing.is_reward = is_reward
                        else:
                            new_coupon = Coupon(
                                code=code,
                                title=title,
                                discount_type=dtype,
                                discount_value=dvalue,
                                min_spend=min_sp,
                                reward_points=reward_points,
                                reward_discount_points=reward_discount_points,
                                is_reward=is_reward
                            )
                            db.session.add(new_coupon)
                        imported_counts['Coupon'] += 1

        db.session.commit()
        update_popular_items()
        # 組合成功訊息並返回前端
        msg = f"✅ 智慧匯入完成！\\n" \
              f"共處理更新與新增：\\n" \
              f"➤ 會員 (User): {imported_counts['User']} 筆\\n" \
              f"➤ 菜單 (MenuItem): {imported_counts['MenuItem']} 筆\\n" \
              f"➤ 優惠券 (Coupon): {imported_counts['Coupon']} 筆"
        
        return f"<script>alert('{msg}'); window.location.href='/admin';</script>"

    except Exception as e:
        db.session.rollback()
        return f"<script>alert('❌ 檔案解析失敗，請確認檔案格式是否正確！\\n錯誤訊息: {e}'); window.history.back();</script>", 500

@app.route('/admin/export_excel', methods=['POST'])
def export_excel():
    """根據勾選的資料表匯出 SQLite 資料為 Excel"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    selected_tables = request.form.getlist('tables')
    if not selected_tables:
        return "<script>alert('❌ 請至少勾選一個資料表！'); window.history.back();</script>", 400

    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')
    
    start_date = None
    end_date = None
    if start_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
    if end_date_str:
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1, seconds=-1)

    orders_query = Order.query
    if start_date:
        orders_query = orders_query.filter(Order.created_at >= start_date)
    if end_date:
        orders_query = orders_query.filter(Order.created_at <= end_date)

    filtered_orders = orders_query.all()
    filtered_order_ids = [o.id for o in filtered_orders]

    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        
        # 會員資料表：直接納入會員進出時間 (Login At / Logout At)
        if 'User' in selected_tables:
            users = User.query.all()
            user_data = []
            for u in users:
                login_str = u.last_login_at.strftime('%Y-%m-%d %H:%M:%S') if u.last_login_at else '尚未記錄'
                
                if not u.last_login_at:
                    logout_str = '尚未記錄'
                elif not u.last_logout_at or u.last_logout_at < u.last_login_at:
                    logout_str = '尚未登出'
                else:
                    logout_str = u.last_logout_at.strftime('%Y-%m-%d %H:%M:%S')

                user_data.append({
                    'ID': u.id, 
                    '姓名(Name)': u.name,
                    '電話(Phone)': u.phone,
                    '紅利點數(Points)': u.points,
                    '最後登入時間(Last Login)': login_str,
                    '最後登出時間(Last Logout)': logout_str
                })
            df = pd.DataFrame(user_data if user_data else [{'資料': '目前無資料'}])
            df.to_excel(writer, sheet_name='會員資料(User)', index=False)

        if 'MenuItem' in selected_tables:
            items = MenuItem.query.all()
            data = [{
                'ID': i.id,
                '餐點名稱(Name)': i.name,
                '分類(Category)': i.category,
                '單價(Price)': i.price,
                '客製化群組(Modifiers)': i.modifiers or 'none',
                '商品描述(Description)': i.description or '',
                '是否特價(Is Discount)': '是' if i.is_discount else '否',
                '特價金額(Discount Price)': i.discount_price,
                '熱門推薦(Recommended)': '是' if (i.is_manual_popular or i.is_recommended) else '否',
                '新品上市(Is New)': '是' if i.is_new else '否',
                '開放紅利兌換(Is Reward)': '是' if i.is_reward else '否',
                '兌換所需點數(Reward Points)': i.reward_points,
                '限時特惠點數(Reward Discount Points)': i.reward_discount_points
            } for i in items]
            df = pd.DataFrame(data if data else [{'資料': '目前無資料'}])
            df.to_excel(writer, sheet_name='菜單品項(MenuItem)', index=False)
            
        if 'Coupon' in selected_tables:
            coupons = Coupon.query.all()
            data = [{
                'ID': c.id,
                '代碼(Code)': c.code,
                '標題(Title)': c.title,
                '折抵類型(Discount Type)': c.discount_type,
                '折抵值(Discount Value)': c.discount_value,
                '門檻(Min Spend)': c.min_spend,
                '兌換所需點數(Reward Points)': c.reward_points,
                '限時特惠點數(Reward Discount Points)': c.reward_discount_points,
                '上架回饋商城(Is Reward)': '是' if c.is_reward else '否'
            } for c in coupons]
            df = pd.DataFrame(data if data else [{'資料': '目前無資料'}])
            df.to_excel(writer, sheet_name='優惠券(Coupon)', index=False)

        if 'UserCoupon' in selected_tables:
            user_coupons = UserCoupon.query.all()
            data = [{'ID': uc.id, '關聯會員ID': uc.user_id, '優惠代碼(Code)': uc.code, '是否已使用(Is Used)': uc.is_used, '領取時間': uc.created_at.strftime('%Y-%m-%d %H:%M') if uc.created_at else ''} for uc in user_coupons]
            df = pd.DataFrame(data if data else [{'資料': '目前無資料'}])
            df.to_excel(writer, sheet_name='會員持券(UserCoupon)', index=False)

        #  歷史訂單表：納入訪客隨機編號、進場時間、離場時間
        if 'Order' in selected_tables:
            order_data = []
            for o in filtered_orders:
                log = db.session.get(CustomerLog, o.customer_log_id) if getattr(o, 'customer_log_id', None) else None
                
                # 身分識別
                if o.user_id:
                    cust_identifier = o.user_id
                else:
                    if o.table_number and str(o.table_number).startswith('訪客-'):
                        cust_identifier = o.table_number
                    else:
                        cust_identifier = f"訪客-{o.pickup_number:03d}" if getattr(o, 'pickup_number', None) else f"訪客-{o.id}"

                # 判定進場時間：
                # 1. 優先抓 CustomerLog 的進入時間
                # 2. 若為會員且無 Log，抓取會員登入時間 (last_login_at)
                # 3. 若為歷史舊單無日誌，不硬套下單時間，標記為「未記錄」以防混淆
                login_dt = None
                if log and log.login_at:
                    login_dt = log.login_at
                elif o.user and o.user.last_login_at and o.user.last_login_at <= o.created_at:
                    login_dt = o.user.last_login_at

                if login_dt:
                    login_str = login_dt.strftime('%Y-%m-%d %H:%M:%S')
                    seconds = int((o.created_at - login_dt).total_seconds()) if o.created_at else 0
                    if seconds > 0:
                        m, s = divmod(seconds, 60)
                        duration_str = f"{m}分{s}秒" if m > 0 else f"{s}秒"
                    else:
                        duration_str = "即時下單"
                else:
                    login_str = "未記錄"
                    duration_str = "無進場紀錄"

                order_data.append({
                    'ID': o.id,
                    '取餐編號(Pickup No)': f"#{o.pickup_number:03d}" if getattr(o, 'pickup_number', None) else f"#{o.id}",
                    '身分/會員ID(Customer ID)': cust_identifier, 
                    '進場時間(Login At)': login_str,
                    '點餐耗時(Duration)': duration_str,
                    '下單時間(Order Created)': o.created_at.strftime('%Y-%m-%d %H:%M:%S') if o.created_at else '',
                    '出餐時間(Completed At)': o.completed_at.strftime('%Y-%m-%d %H:%M:%S') if o.completed_at else '未出餐',
                    '總金額(Total)': o.total_price,
                    '付款方式(Payment)': o.payment_method,
                    '狀態(Status)': o.status,
                    '使用紅利': o.points_used
                })
            df = pd.DataFrame(order_data if order_data else [{'資料': '所選日期範圍內無訂單'}])
            df.to_excel(writer, sheet_name='訂單總覽(Order)', index=False, startrow=1)

        if 'OrderItem' in selected_tables:
            order_items = OrderItem.query.filter(OrderItem.order_id.in_(filtered_order_ids)).all() if filtered_order_ids else []
            data = [{'ID': oi.id, '關聯訂單ID(Order ID)': oi.order_id, '品名(Item Name)': oi.item_name, '單價(Price)': oi.price, '數量(Qty)': oi.quantity, '客製化(Customization)': oi.customization} for oi in order_items]
            df = pd.DataFrame(data if data else [{'資料': '所選日期範圍內無明細'}])
            df.to_excel(writer, sheet_name='訂單明細(OrderItem)', index=False, startrow=1)

        if 'Analytics' in selected_tables:
            valid_orders = [o for o in filtered_orders if o.status != 'Cancelled']
            total_orders = len(filtered_orders)
            total_valid_orders = len(valid_orders)
            total_revenue = sum(o.total_price or 0 for o in valid_orders)
            total_discount = sum(o.discount_amount or 0 for o in valid_orders)
            total_points = sum(o.points_used or 0 for o in valid_orders)
            avg_order = round(total_revenue / total_valid_orders, 2) if total_valid_orders > 0 else 0

            analytics_data = [
                {'指標 (Metric)': '區間總訂單數 (Total Orders)', '數值 (Value)': total_orders},
                {'指標 (Metric)': '有效訂單數 (Valid Orders)', '數值 (Value)': total_valid_orders},
                {'指標 (Metric)': '總營收 (Total Revenue)', '數值 (Value)': total_revenue},
                {'指標 (Metric)': '總折扣折抵 (Total Discount)', '數值 (Value)': total_discount},
                {'指標 (Metric)': '紅利使用總額 (Points Used)', '數值 (Value)': total_points},
                {'指標 (Metric)': '平均客單價 (AOV)', '數值 (Value)': avg_order}
            ]
            df_analytics = pd.DataFrame(analytics_data)
            df_analytics.to_excel(writer, sheet_name='區間營運總覽(Analytics)', index=False, startrow=1)

            item_stats = {}
            for o in valid_orders:
                for item in o.items:
                    if '[點數兌換]' in item.item_name or item.item_name.startswith('🎁') or item.customization == '紅利免費兌換':
                        continue
                    if item.item_name not in item_stats:
                        item_stats[item.item_name] = {'qty': 0, 'rev': 0}
                    item_stats[item.item_name]['qty'] += (item.quantity or 1)
                    item_stats[item.item_name]['rev'] += ((item.price or 0) * (item.quantity or 1))

            sales_data = [
                {'餐點名稱 (Item Name)': name, '銷售數量 (Qty)': stats['qty'], '創造營收 (Revenue)': stats['rev']}
                for name, stats in sorted(item_stats.items(), key=lambda x: x[1]['qty'], reverse=True)
            ]
            df_sales = pd.DataFrame(sales_data if sales_data else [{'資料': '所選日期範圍內無銷售資料'}])
            df_sales.to_excel(writer, sheet_name='區間熱銷排行(ItemSales)', index=False, startrow=1)

        # 管理員日誌
        if 'AdminLog' in selected_tables:
            logs_query = AdminLog.query
            if start_date:
                logs_query = logs_query.filter(AdminLog.login_at >= start_date)
            if end_date:
                logs_query = logs_query.filter(AdminLog.login_at <= end_date)
            filtered_logs = logs_query.order_by(AdminLog.id.desc()).all()

            data = [{
                'ID': lg.id,
                '管理員帳號(Username)': lg.username,
                '登入時間(Login At)': lg.login_at.strftime('%Y-%m-%d %H:%M:%S') if lg.login_at else '',
                '登出時間(Logout At)': lg.logout_at.strftime('%Y-%m-%d %H:%M:%S') if lg.logout_at else '尚未登出 / 瀏覽器關閉',
                'IP 位址(IP Address)': lg.ip_address or '本機'
            } for lg in filtered_logs]
            df_admin_log = pd.DataFrame(data if data else [{'資料': '所選日期範圍內無管理員登入紀錄'}])
            df_admin_log.to_excel(writer, sheet_name='管理員進出日誌(AdminLog)', index=False, startrow=1)

        # 3. 日期標題與欄位寬度格式化
        display_start = start_date_str if start_date_str else '全部區間 (All)'
        display_end = end_date_str if end_date_str else '全部區間 (All)'
        date_range_text = f"報表資料區間：{display_start} ~ {display_end}"
        
        date_dependent_sheets = {
            '訂單總覽(Order)', '訂單明細(OrderItem)', 
            '區間營運總覽(Analytics)', '區間熱銷排行(ItemSales)', 
            '管理員進出日誌(AdminLog)'
        }

        def calculate_display_width(val):
            if val is None:
                return 0
            text = str(val)
            width = 0.0
            for char in text:
                status = unicodedata.east_asian_width(char)
                if status in ('F', 'W') or ord(char) >= 0x2600 or ord(char) >= 0x1F000:
                    width += 2.2
                else:
                    width += 1.1
            return int(width)

        for sheet_name, ws in writer.sheets.items():
            if sheet_name in date_dependent_sheets:
                ws.cell(row=1, column=1, value=date_range_text).font = Font(bold=True, color="0055aa")

            for col in ws.columns:
                max_len = 0
                column_letter = col[0].column_letter
                for cell in col:
                    if sheet_name in date_dependent_sheets and cell.row == 1 and cell.column == 1:
                        continue
                    cell_len = calculate_display_width(cell.value)
                    if cell_len > max_len:
                        max_len = cell_len
                
                ws.column_dimensions[column_letter].width = max(max_len + 4, 12)

    output.seek(0)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    date_suffix = f"_{start_date_str}_to_{end_date_str}" if start_date_str and end_date_str else ""
    filename = f"Kiosk_Export_{timestamp}{date_suffix}.xlsx"
    
    return send_file(
        output,
        download_name=filename,
        as_attachment=True,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

# 資料庫與相片一鍵打包備份路由
@app.route('/admin/backup_system')
def backup_system():
    """一鍵將 SQLite 資料庫 (menu.db)、會員相片 (static/member) 與餐點照片 (static/menu) 打包為 ZIP 下載"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    try:
        # 1. 強制將 WAL 寫入主庫，確保備份檔案最新
        db.session.execute(db.text("PRAGMA wal_checkpoint(TRUNCATE);"))
        db.session.commit()

        # 2. 建立記憶體二進位串流 (In-Memory Buffer)
        memory_file = io.BytesIO()
        db_path = os.path.join(DATABASE_DIR, 'menu.db')
        member_folder = app.config['UPLOAD_FOLDER_MEMBER']
        menu_folder = app.config['UPLOAD_FOLDER_MENU']

        # 3. 建立 ZIP 壓縮檔案
        with zipfile.ZipFile(memory_file, 'w', zipfile.ZIP_DEFLATED) as zf:
            # 打包資料庫檔案
            if os.path.exists(db_path):
                zf.write(db_path, arcname='menu.db')

            # 打包會員人臉註冊相片 (static/member/)
            if os.path.exists(member_folder):
                for root, _, files in os.walk(member_folder):
                    for file in files:
                        full_path = os.path.join(root, file)
                        arc_name = os.path.join('static', 'member', file).replace('\\', '/')
                        zf.write(full_path, arcname=arc_name)

            # 打包菜單餐點照片 (static/menu/)
            if os.path.exists(menu_folder):
                for root, _, files in os.walk(menu_folder):
                    for file in files:
                        full_path = os.path.join(root, file)
                        arc_name = os.path.join('static', 'menu', file).replace('\\', '/')
                        zf.write(full_path, arcname=arc_name)

        # 4. 指針歸零並回傳檔案串流
        memory_file.seek(0)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        download_name = f"Kiosk_Backup_{timestamp}.zip"

        return send_file(
            memory_file,
            download_name=download_name,
            as_attachment=True,
            mimetype='application/zip'
        )

    except Exception as e:
        return f"<script>alert('❌ 備份作業失敗：{e}'); window.history.back();</script>", 500

# 系統備份還原路由
@app.route('/admin/restore_backup', methods=['POST'])
def restore_backup():
    """接收備份 ZIP 檔，安全還原資料庫、會員相片與餐點照片 (嚴防 Zip Slip 與 Zip Bomb)"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    file = request.files.get('backup_zip')
    if not file or file.filename == '':
        return "<script>alert('❌ 請選擇要還原的備份 ZIP 檔案！'); window.history.back();</script>", 400

    restore_db = True if request.form.get('restore_db') == '1' else False
    restore_photos = True if request.form.get('restore_photos') == '1' else False
    restore_menu_photos = True if request.form.get('restore_menu_photos') == '1' else False
    overwrite_photos = True if request.form.get('overwrite_photos') == '1' else False

    if not restore_db and not restore_photos and not restore_menu_photos:
        return "<script>alert('❌ 請至少勾選一項欲還原的項目（資料庫、會員相片或餐點照片）！'); window.history.back();</script>", 400

    db_path = os.path.abspath(os.path.join(DATABASE_DIR, 'menu.db'))
    member_dir = os.path.abspath(app.config['UPLOAD_FOLDER_MEMBER'])
    menu_dir = os.path.abspath(app.config['UPLOAD_FOLDER_MENU'])
    os.makedirs(member_dir, exist_ok=True)
    os.makedirs(menu_dir, exist_ok=True)

    allowed_img_exts = {'.jpg', '.jpeg', '.png', '.webp'}
    restored_db_status = False
    restored_photo_count = 0
    skipped_photo_count = 0
    restored_menu_photo_count = 0
    skipped_menu_photo_count = 0

    try:
        with zipfile.ZipFile(file, 'r') as zf:
            total_uncompressed_size = 0

            # 1. 預先掃描驗證 (Zip Bomb 與 Symlink 防護)
            for info in zf.infolist():
                if info.file_size > MAX_SINGLE_FILE_SIZE:
                    return f"<script>alert('❌ 壓縮檔內包含異常過大檔案 ({info.filename})，已終止還原！'); window.history.back();</script>", 400

                total_uncompressed_size += info.file_size
                if total_uncompressed_size > MAX_TOTAL_EXTRACT_SIZE:
                    return "<script>alert('❌ 壓縮檔解壓總容量超過系統上限 (150MB)，疑似惡意檔案！'); window.history.back();</script>", 400

                if (info.external_attr >> 16) & 0o120000 == 0o120000:
                    return "<script>alert('❌ 檢測到不安全的符號連結 (Symlink)，終止還原作業！'); window.history.back();</script>", 400

            namelist = zf.namelist()

            # 2. 還原 SQLite 資料庫 (menu.db)
            if restore_db:
                db_entry = next((name for name in namelist if os.path.basename(name.replace('\\', '/')) == 'menu.db'), None)
                if db_entry:
                    db_bytes = zf.read(db_entry)
                    if db_bytes.startswith(b'SQLite format 3\x00'):
                        db.session.remove()
                        db.engine.dispose()
                        with open(db_path, 'wb') as f:
                            f.write(db_bytes)
                        restored_db_status = True
                    else:
                        return "<script>alert('❌ 壓縮檔內的 menu.db 格式不符，非合法 SQLite 檔案！'); window.history.back();</script>", 400

            # 3. 還原圖片檔 (會員相片與餐點照片)
            for entry_name in namelist:
                normalized_entry = entry_name.replace('\\', '/')
                if normalized_entry.endswith('/'):
                    continue

                raw_filename = os.path.basename(normalized_entry)
                safe_filename = secure_filename(raw_filename)
                if not safe_filename:
                    continue

                _, ext = os.path.splitext(safe_filename)
                if ext.lower() not in allowed_img_exts:
                    continue

                path_parts = [p.lower() for p in normalized_entry.split('/')[:-1]]

                # A. 判定為會員照片
                if restore_photos and ('member' in path_parts):
                    dest_file_path = os.path.abspath(os.path.join(member_dir, safe_filename))
                    if os.path.commonpath([member_dir, dest_file_path]) != member_dir:
                        continue

                    if os.path.exists(dest_file_path) and not overwrite_photos:
                        skipped_photo_count += 1
                        continue

                    with open(dest_file_path, 'wb') as img_out:
                        img_out.write(zf.read(entry_name))
                    restored_photo_count += 1

                # B. 判定為菜單餐點照片
                elif restore_menu_photos and ('menu' in path_parts):
                    dest_file_path = os.path.abspath(os.path.join(menu_dir, safe_filename))
                    if os.path.commonpath([menu_dir, dest_file_path]) != menu_dir:
                        continue

                    if os.path.exists(dest_file_path) and not overwrite_photos:
                        skipped_menu_photo_count += 1
                        continue

                    with open(dest_file_path, 'wb') as img_out:
                        img_out.write(zf.read(entry_name))
                    restored_menu_photo_count += 1

        msg_parts = ["✅ 系統備份還原完成！"]
        if restore_db:
            msg_parts.append(f"➤ 資料庫 (menu.db)：{'成功還原覆蓋' if restored_db_status else '壓縮檔中無資料庫檔'}")
        if restore_photos:
            msg_parts.append(f"➤ 會員相片：寫入 {restored_photo_count} 張 (略過既有 {skipped_photo_count} 張)")
        if restore_menu_photos:
            msg_parts.append(f"➤ 餐點照片：寫入 {restored_menu_photo_count} 張 (略過既有 {skipped_menu_photo_count} 張)")

        alert_msg = "\\n".join(msg_parts)
        return f"<script>alert('{alert_msg}'); window.location.href='/admin';</script>"

    except zipfile.BadZipFile:
        return "<script>alert('❌ 上傳的檔案非有效的 ZIP 壓縮檔！'); window.history.back();</script>", 400
    except Exception as e:
        return f"<script>alert('❌ 還原過程發生錯誤：{e}'); window.history.back();</script>", 500

@app.route('/admin/edit/<int:id>', methods=['POST'])
def edit_item(id):
    """店家編輯菜單品項"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    item = MenuItem.query.get_or_404(id)
    name = request.form.get('name')
    price = request.form.get('price')
    from_tab = request.form.get('from_tab') or ('rewards' if item.is_reward else 'menu')
    
    if name and price:
        item_price = max(0, round(float(price)))
        
        # 1. 擷取加購品資料與防呆檢查
        can_be_add_on = True if request.form.get('can_be_add_on') else False
        add_on_price = max(0, round(float(request.form.get('add_on_price') or 0)))
        if can_be_add_on:
            if add_on_price <= 0:
                db.session.rollback()
                return "<script>alert('❌ 已開啟加購品標籤，請輸入大於 0 的加購專屬價！'); window.history.back();</script>", 400
            if add_on_price >= item_price:
                db.session.rollback()
                return f"<script>alert('❌ 加購專屬價 (${add_on_price}) 不得高於或等於原單價 (${item_price})！'); window.history.back();</script>", 400

        # 2. 先定義紅利兌換變數（解決 UnboundLocalError）
        is_reward = True if request.form.get('is_reward') else False
        reward_points = max(0, int(request.form.get('reward_points') or 0))
        reward_discount_points = max(0, int(request.form.get('reward_discount_points') or 0))

        # 3. 執行紅利兌換防呆檢查
        if is_reward:
            if reward_points <= 0:
                db.session.rollback()
                return "<script>alert('❌ 啟用紅利兌換時，「兌換所需點數」必須大於 0！'); window.history.back();</script>", 400
            if reward_points > item_price:
                db.session.rollback()
                return f"<script>alert('❌ 兌換所需點數 ({reward_points} 點) 不得高於原單價 (${item_price})！'); window.history.back();</script>", 400
            if reward_discount_points > 0 and reward_discount_points >= reward_points:
                db.session.rollback()
                return "<script>alert('❌ 「限時優惠點數」必須小於「兌換所需點數」！'); window.history.back();</script>", 400

        # 4. 正式更新資料庫屬性
        item.name = name
        item.category = request.form.get('category', '主餐')
        item.modifiers = request.form.get('modifiers', 'none')
        item.price = item_price
        item.stock = max(0, int(request.form.get('stock') or 0))
        item.total_stock = max(item.stock, int(request.form.get('total_stock') or item.stock))
        item.is_discount = True if request.form.get('is_discount') else False
        item.discount_price = max(0, round(float(request.form.get('discount_price') or 0)))
        item.description = request.form.get('description', '')
        is_sold_out = True if request.form.get('is_sold_out') else (item.stock == 0)
        item.is_sold_out = is_sold_out

        is_pop = request.form.get('is_recommended') or request.form.get('is_manual_popular')
        item.is_manual_popular = True if is_pop in ['1', 'true', 'on', True] else False
        item.is_new = True if request.form.get('is_new') else False

        item.can_be_add_on = can_be_add_on
        item.add_on_price = add_on_price
        item.addon_trigger_type = request.form.get('addon_trigger_type', 'any')
        item.addon_trigger_target = request.form.get('addon_trigger_target', '').strip()

        item.is_reward = is_reward
        item.reward_points = reward_points
        item.reward_discount_points = reward_discount_points

        # 5. 照片處理
        image = request.files.get('image')
        if image and image.filename != '':
            image_filename = f"menu_{int(time.time())}.jpg"
            filepath = os.path.join(app.config['UPLOAD_FOLDER_MENU'], image_filename)
            save_and_fix_image(image, filepath)
            
            if item.image_path:
                old_path = os.path.join(app.config['UPLOAD_FOLDER_MENU'], item.image_path)
                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except Exception:
                        pass
                        
            item.image_path = image_filename

        db.session.commit()
        
    return redirect(url_for('admin_dashboard', tab=from_tab))

@app.route('/admin/add_combo', methods=['POST'])
def add_combo():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
    
    main_item_id = int(request.form.get('main_item_id'))
    name = str(request.form.get('name', '')).strip()
    additional_price = max(0, int(request.form.get('additional_price') or 0))
    description = request.form.get('description', '')
    
    item1_id = int(request.form.get('item1_id')) if request.form.get('item1_id') else None
    item2_id = int(request.form.get('item2_id')) if request.form.get('item2_id') else None
    item3_id = int(request.form.get('item3_id')) if request.form.get('item3_id') else None

    chosen_sides = [s for s in [item1_id, item2_id, item3_id] if s is not None]
    if len(chosen_sides) != len(set(chosen_sides)):
        db.session.rollback()
        return "<script>alert('❌ 配餐不可重複選擇相同的餐點！'); window.location.href='/admin?tab=menu';</script>"

    existing_name = ComboOption.query.filter_by(
        main_item_id=main_item_id,
        name=name
    ).first()
    if existing_name:
        db.session.rollback()
        return f"<script>alert('❌ 該主餐已存在名為【{name}】的套餐，請使用不同名稱！'); window.location.href='/admin?tab=menu';</script>"

    target_sides_set = set(chosen_sides)
    existing_combos = ComboOption.query.filter_by(main_item_id=main_item_id).all()
    for ec in existing_combos:
        ec_sides = [s for s in [ec.item1_id, ec.item2_id, ec.item3_id] if s is not None]
        if set(ec_sides) == target_sides_set:
            db.session.rollback()
            return f"<script>alert('❌ 該主餐已存在包含相同配餐內容的套餐【{ec.name}】，不可重複建立相同組合！'); window.location.href='/admin?tab=menu';</script>"

    item1_customizable = True if request.form.get('item1_customizable') == '1' else False
    item2_customizable = True if request.form.get('item2_customizable') == '1' else False
    item3_customizable = True if request.form.get('item3_customizable') == '1' else False
    can_addon = True if request.form.get('can_addon') == '1' else False
    
    new_combo = ComboOption(
        main_item_id=main_item_id,
        name=name,
        additional_price=additional_price,
        description=description,
        item1_id=item1_id,
        item2_id=item2_id,
        item3_id=item3_id,
        item1_customizable=item1_customizable,
        item2_customizable=item2_customizable,
        item3_customizable=item3_customizable,
        can_addon=can_addon
    )
    db.session.add(new_combo)
    db.session.commit()
    db.session.expire_all() 
    return f"<script>alert('✅ 成功為餐點新增套餐組合！'); window.location.href='/admin?tab=menu';</script>"


@app.route('/admin/edit_combo/<int:id>', methods=['POST'])
def edit_combo(id):
    """編輯現有套餐組合設定"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    combo = ComboOption.query.get_or_404(id)
    main_item_id = int(request.form.get('main_item_id'))
    name = str(request.form.get('name', '')).strip()
    additional_price = max(0, int(request.form.get('additional_price') or 0))
    description = request.form.get('description', '')

    item1_id = int(request.form.get('item1_id')) if request.form.get('item1_id') else None
    item2_id = int(request.form.get('item2_id')) if request.form.get('item2_id') else None
    item3_id = int(request.form.get('item3_id')) if request.form.get('item3_id') else None


    chosen_sides = [s for s in [item1_id, item2_id, item3_id] if s is not None]
    if len(chosen_sides) != len(set(chosen_sides)):
        db.session.rollback()
        return "<script>alert('❌ 配餐不可重複選擇相同的餐點！'); window.location.href='/admin?tab=menu';</script>"

    duplicate_name = ComboOption.query.filter(
        ComboOption.id != id,
        ComboOption.main_item_id == main_item_id,
        ComboOption.name == name
    ).first()
    if duplicate_name:
        db.session.rollback()
        return f"<script>alert('❌ 該主餐已有其他名為【{name}】的套餐組合！'); window.location.href='/admin?tab=menu';</script>"

    target_sides_set = set(chosen_sides)
    existing_combos = ComboOption.query.filter(
        ComboOption.id != id,
        ComboOption.main_item_id == main_item_id
    ).all()
    for ec in existing_combos:
        ec_sides = [s for s in [ec.item1_id, ec.item2_id, ec.item3_id] if s is not None]
        if set(ec_sides) == target_sides_set:
            db.session.rollback()
            return f"<script>alert('❌ 該主餐已有相同配餐組合的套餐【{ec.name}】，請勿重複配置！'); window.location.href='/admin?tab=menu';</script>"

    combo.main_item_id = main_item_id
    combo.name = name
    combo.additional_price = additional_price
    combo.description = description
    combo.item1_id = item1_id
    combo.item2_id = item2_id
    combo.item3_id = item3_id
    combo.item1_customizable = True if request.form.get('item1_customizable') == '1' else False
    combo.item2_customizable = True if request.form.get('item2_customizable') == '1' else False
    combo.item3_customizable = True if request.form.get('item3_customizable') == '1' else False
    combo.can_addon = True if request.form.get('can_addon') == '1' else False

    db.session.commit()
    db.session.expire_all()
    return redirect(url_for('admin_dashboard', tab='menu'))


@app.route('/admin/delete_combo/<int:id>')
def delete_combo(id):
    """刪除套餐組合"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    combo = ComboOption.query.get_or_404(id)
    db.session.delete(combo)
    db.session.commit()
    db.session.expire_all()
    return redirect(url_for('admin_dashboard', tab='menu'))

@app.route('/admin/delete/<int:id>')
def delete_item(id):
    """刪除菜單品項"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    item = MenuItem.query.get_or_404(id)
    if item.image_path:
        filepath = os.path.join(app.config['UPLOAD_FOLDER_MENU'], item.image_path)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass
                
    db.session.delete(item)
    db.session.commit()
    db.session.expire_all()
    return redirect(url_for('admin_dashboard', tab='menu'))

@app.route('/admin/import_excel', methods=['POST'])
def import_menu_excel():
    """Excel 批量匯入菜單 (支援自身匯出的檔案與自訂格式)"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    file = request.files.get('file')
    if not file or file.filename == '':
        return "<script>alert('請選擇 Excel/CSV 檔案！'); window.history.back();</script>", 400

    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(file)
        else:
            # 1. 檢查 Excel 中的所有 Sheet，優先讀取菜單工作表
            excel_file = pd.ExcelFile(file)
            target_sheet = None
            for sheet in excel_file.sheet_names:
                if '菜單' in sheet or 'MenuItem' in sheet or 'menu' in sheet.lower():
                    target_sheet = sheet
                    break
            
            # 若無特定命名則讀取第一個工作表
            df = pd.read_excel(excel_file, sheet_name=target_sheet if target_sheet else 0)

        # 2. 擴充欄位對應表 (包含匯出格式、中文別名與英文)
        column_mapping = {
            '餐點名稱(Name)': 'name', '名稱(Name)': 'name', '餐點名稱': 'name', '名稱': 'name', 'name': 'name',
            '分類(Category)': 'category', '分類': 'category', 'category': 'category',
            '單價(Price)': 'price', '單價': 'price', '原單價': 'price', 'price': 'price',
            '客製化類型(Modifiers)': 'modifiers', '客製化群組(Modifiers)': 'modifiers', '客製化類型': 'modifiers', '客製化': 'modifiers', 'modifiers': 'modifiers',
            '商品描述(Description)': 'description', '簡介': 'description', '描述': 'description', 'description': 'description',
            '熱門推薦(Recommended)': 'is_recommended', '熱門(Popular)': 'is_recommended', '熱門推薦': 'is_recommended', 'is_recommended': 'is_recommended',
            '新品上市(Is New)': 'is_new', '新品': 'is_new', 'is_new': 'is_new',
            '是否特價(Is Discount)': 'is_discount', '是否特價': 'is_discount', '特價': 'is_discount', 'is_discount': 'is_discount',
            '特價金額(Discount Price)': 'discount_price', '特價金額': 'discount_price', 'discount_price': 'discount_price',
            '開放紅利兌換(Is Reward)': 'is_reward', '是否開放紅利兌換': 'is_reward', 'is_reward': 'is_reward',
            '兌換所需點數(Reward Points)': 'reward_points', '兌換點數': 'reward_points', 'reward_points': 'reward_points',
            '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '特惠點數': 'reward_discount_points', 'reward_discount_points': 'reward_discount_points'
        }
        df.rename(columns=column_mapping, inplace=True)

        if 'name' not in df.columns or 'price' not in df.columns:
            return "<script>alert('檔案缺少必要欄位：「餐點名稱」或「單價」！'); window.history.back();</script>", 400

        # 布林值輔助解析函式
        def parse_bool(val):
            if pd.isna(val):
                return False
            s = str(val).strip().lower()
            return s in ['1', 'true', '是', 'yes', 'y']

        for _, row in df.iterrows():
            name = str(row.get('name', '')).strip()
            if not name or pd.isna(row.get('name')) or name == '目前無資料':
                continue

            category = str(row.get('category', '主餐')).strip() if not pd.isna(row.get('category')) else '主餐'
            
            try:
                price = max(0, round(float(row.get('price', 0))))
            except (ValueError, TypeError):
                price = 0

            try:
                discount_price = max(0, round(float(row.get('discount_price', 0))))
            except (ValueError, TypeError):
                discount_price = 0

            try:
                reward_points = max(0, int(row.get('reward_points', 0)))
            except (ValueError, TypeError):
                reward_points = 0

            try:
                reward_discount_points = max(0, int(row.get('reward_discount_points', 0)))
            except (ValueError, TypeError):
                reward_discount_points = 0

            modifiers = str(row.get('modifiers', 'none')).strip() if not pd.isna(row.get('modifiers')) else 'none'
            if modifiers not in ['none', 'ice_sugar', 'spicy', 'addons']:
                modifiers = 'none'

            description = str(row.get('description', '')).strip() if not pd.isna(row.get('description')) else ''
            
            is_recommended = parse_bool(row.get('is_recommended'))
            is_new = parse_bool(row.get('is_new'))
            is_discount = parse_bool(row.get('is_discount'))
            is_reward = parse_bool(row.get('is_reward'))

            new_item = MenuItem(
                name=name,
                category=category,
                price=price,
                modifiers=modifiers,
                description=description,
                image_path='',
                is_recommended=False,
                is_manual_popular=is_recommended,
                is_discount=is_discount, 
                discount_price=discount_price,
                is_new=is_new,
                is_reward=is_reward,
                reward_points=reward_points,
                reward_discount_points=reward_discount_points
            )
            db.session.add(new_item)

        db.session.commit()
        update_popular_items()
        return redirect(url_for('admin_dashboard', tab='menu'))

    except Exception as e:
        db.session.rollback()
        return f"<script>alert('匯入解析失敗：{e}'); window.history.back();</script>", 500

@app.route('/admin/update_order_status/<int:id>', methods=['POST'])
def update_order_status(id):
    """更新訂單製作/出餐狀態"""
    if not session.get('admin_logged_in'):
        return jsonify({'error': '未授權'}), 401
        
    order = Order.query.get_or_404(id)
    data = request.get_json()
    new_status = data.get('status')

    if new_status in ['Pending', 'Completed', 'PickedUp', 'Cancelled']:
        order.status = new_status
        if new_status == 'Completed' and not order.completed_at:
            order.completed_at = datetime.now()
        db.session.commit()
        _AI_ADVICE_STATE['last_signature'] = None
        order_event_bus.notify()

        return jsonify({'message': '狀態更新成功', 'status': new_status})

    return jsonify({'error': '無效的狀態'}), 400

# --- 編輯會員資料 (姓名、手機、紅利點數) ---
@app.route('/admin/edit_user/<int:id>', methods=['POST'])
def edit_user(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    user = User.query.get_or_404(id)
    user.name = request.form.get('name', user.name)
    user.phone = request.form.get('phone', user.phone)
    user.points = int(request.form.get('points') or 0)
    db.session.commit()
    return redirect(url_for('admin_dashboard', tab='users'))

# --- 切換會員專屬優惠券的可用/核銷狀態 ---
@app.route('/admin/toggle_user_coupon/<int:uc_id>', methods=['POST'])
def toggle_user_coupon(uc_id):
    if not session.get('admin_logged_in'):
        return jsonify({'success': False, 'message': '未授權'}), 401
    
    uc = UserCoupon.query.get_or_404(uc_id)
    #   明確進行布林狀態反轉
    uc.is_used = not bool(uc.is_used)
    uc.used_at = datetime.now() if uc.is_used else None
    db.session.commit()
    db.session.expire_all()  #   清除快取
    
    #  直接從資料庫重新撈取該會員所有票券的最新狀態
    all_user_coupons = UserCoupon.query.filter_by(user_id=uc.user_id).order_by(UserCoupon.id.asc()).all()
    coupons_list = [{
        'id': c.id,
        'title': c.coupon.title if c.coupon else '優惠券',
        'code': c.code,
        'is_used': bool(c.is_used)
    } for c in all_user_coupons]
    
    user = User.query.get(uc.user_id)
    return jsonify({
        'success': True,
        'user_name': user.name if user else '',
        'coupons': coupons_list
    })

# ==============================================================================
# 即時查詢當前登入會員最新可用優惠券清單
# ==============================================================================
@app.route('/api/user_available_coupons')
def api_user_available_coupons():
    user_id = session.get('user_id')
    if not user_id:
        return jsonify({'success': False, 'coupons': [], 'points': 0})

    db.session.expire_all()
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({'success': False, 'coupons': [], 'points': 0})

    avail_cps = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
    coupons_list = [{
        'code': uc.code,
        'title': uc.coupon.title if uc.coupon else '優惠券',
        'min_spend': uc.coupon.min_spend if uc.coupon else 0
    } for uc in avail_cps]

    return jsonify({
        'success': True,
        'points': user.points,
        'coupons': coupons_list
    })

# --- 刪除會員 ---
@app.route('/admin/delete_user/<int:id>')
def delete_user(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))

    user = User.query.get_or_404(id)
    
    # 1. 刪除會員的大頭貼檔案
    if user.photo_path:
        filepath = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], user.photo_path)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                pass

    #  2. 新增：查詢並刪除該會員名下的所有訂單
    user_orders = Order.query.filter_by(user_id=id).all()
    for order in user_orders:
        db.session.delete(order)

    # 3. 刪除會員本身
    db.session.delete(user)
    db.session.commit()
    db.session.expire_all()
    
    return redirect(url_for('admin_dashboard', tab='users'))


# ==============================================================================
# 8. KDS 廚房出單看板系統 (Kitchen Display System)
# ==============================================================================
@app.route('/kitchen')
def kitchen_display():
    """廚房專屬出單看板頁面"""
    return render_template('kitchen.html')

@app.route('/api/kitchen_orders')
def api_kitchen_orders():
    """取得今日目前待製作（Pending）的即時訂單清單 (FIFO 先進先出並同類合併)"""
    today = datetime.now().date()
    pending_orders = Order.query.filter(
        Order.status == 'Pending',
        db.func.date(Order.created_at) == today
    ).order_by(Order.created_at.asc()).all()

    orders_data = []
    for o in pending_orders:
        aggregated_items = {}
        for i in o.items:
            clean_name = re.sub(r'^(🎁\s*)?(\[點數兌換\]\s*)?', '', i.item_name).strip()
            custom_note = '' if i.customization == '紅利免費兌換' else (i.customization or '')

            key = (clean_name, custom_note)
            if key not in aggregated_items:
                aggregated_items[key] = {
                    'name': clean_name,
                    'quantity': 0,
                    'customization': custom_note
                }
            # 3. 數量自動累加
            aggregated_items[key]['quantity'] += (i.quantity or 1)

        orders_data.append({
            'id': o.id,
            'pickup_number': o.pickup_number if getattr(o, 'pickup_number', None) else o.id,
            'table_number': o.table_number,
            'order_type': o.order_type,
            'need_cutlery': bool(o.need_cutlery) if getattr(o, 'need_cutlery', None) is not None else True,
            'note': o.note or '',
            'created_at': o.created_at.strftime('%H:%M:%S') if o.created_at else '',
            'timestamp': o.created_at.timestamp() if o.created_at else time.time(),
            'items': list(aggregated_items.values())
        })

    return jsonify({'success': True, 'orders': orders_data})

@app.route('/api/kitchen_update_status/<int:id>', methods=['POST'])
def kitchen_update_status(id):
    """廚房一鍵出餐或取消訂單"""
    order = Order.query.get_or_404(id)
    data = request.get_json() or {}
    new_status = data.get('status', 'Completed')

    if new_status in ['Pending', 'Completed', 'Cancelled']:
        order.status = new_status
        if new_status == 'Completed':
            order.completed_at = datetime.now()
        db.session.commit()
        order_event_bus.notify()
        return jsonify({'success': True, 'message': f'訂單 #{order.id} 狀態已更新為 {new_status}'})
    return jsonify({'success': False, 'message': '無效的狀態'}), 400

@app.route('/api/admin_live_orders')
def api_admin_live_orders():
    """供店家後台、櫃檯與叫號看板即時同步今日訂單看板、營運指標與最新菜單庫存"""
    db.session.expire_all()

    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
    today_orders = Order.query.filter(
        Order.created_at >= today_start,
        Order.created_at <= today_end
    ).order_by(Order.id.desc()).all()

    orders_data = []
    for o in today_orders:
        items_data = [{
            'item_name': i.item_name,
            'quantity': i.quantity,
            'price': i.price,
            'customization': i.customization or ''
        } for i in o.items]

        orders_data.append({
            'id': o.id,
            'pickup_number': o.pickup_number if getattr(o, 'pickup_number', None) else o.id,
            'user_name': o.user.name if o.user else '非會員',
            'order_type': o.order_type,
            'payment_method': o.payment_method,
            'need_cutlery': bool(o.need_cutlery) if getattr(o, 'need_cutlery', None) is not None else True,
            'note': o.note or '',
            'created_at': o.created_at.strftime('%m-%d %H:%M') if o.created_at else '',
            'status': o.status,
            'discount_amount': o.discount_amount or 0,
            'total_price': o.total_price,
            'items': items_data
        })

    all_items = MenuItem.query.all()
    menu_items_data = [{
        'id': item.id,
        'stock': item.stock if item.stock is not None else 0,
        'total_stock': item.total_stock if item.total_stock is not None else 50,
        'is_sold_out': bool(item.is_sold_out) or ((item.stock or 0) <= 0)
    } for item in all_items]

    all_orders = Order.query.order_by(Order.id.desc()).all()
    analytics = build_order_analytics(all_orders, limit=3)
    analytics['ai_advice'] = generate_ai_business_advice(analytics)

    return jsonify({
        'success': True, 
        'orders': orders_data,
        'menu_items': menu_items_data,
        'analytics': analytics
    })

@app.route('/api/kitchen_complete_all', methods=['POST'])
def kitchen_complete_all():
    """廚房看板：一鍵將今日所有待製作 (Pending) 訂單全部標記為已出餐 (Completed)"""
    today = datetime.now().date()
    pending_orders = Order.query.filter(
        Order.status == 'Pending',
        db.func.date(Order.created_at) == today
    ).all()

    if not pending_orders:
        return jsonify({'success': False, 'message': '目前沒有待製作的訂單！'})

    count = len(pending_orders)
    now = datetime.now()
    for o in pending_orders:
        o.status = 'Completed'
        o.completed_at = now

    db.session.commit()
    order_event_bus.notify()
    return jsonify({'success': True, 'message': f'✅ 已成功將 {count} 筆訂單批次完成出餐！', 'count': count})

# ==============================================================================
# 櫃檯取餐路由
# ==============================================================================
@app.route('/pickup')
def pickup_management():
    """供櫃檯人員使用的取餐核銷操作頁面"""
    return render_template('pickup.html')

@app.route('/api/mark_picked_up/<int:id>', methods=['POST'])
def api_mark_picked_up(id):
    """櫃檯人員一鍵核銷取餐"""
    order = Order.query.get_or_404(id)
    order.status = 'PickedUp'
    db.session.commit()
    order_event_bus.notify()
    return jsonify({'success': True, 'message': f'取餐編號 #{order.pickup_number} (單號 #{order.id}) 已完成取餐！'})

# ==============================================================================
# SSE (Server-Sent Events) 即時推播串流路由
# ==============================================================================
class OrderEventBus:
    def __init__(self, max_subscribers=150):
        self._subscribers = set()
        self._lock = threading.Lock()
        self._max_subscribers = max_subscribers

    def subscribe(self):
        """為新連線的客戶端建立專屬 Queue，具備上限保護"""
        with self._lock:
            # 清理過量連線保護伺服器
            if len(self._subscribers) >= self._max_subscribers:
                # 剔除最早的一個佇列
                oldest_q = next(iter(self._subscribers))
                self._subscribers.remove(oldest_q)

            q = queue.Queue(maxsize=10)
            self._subscribers.add(q)
            return q

    def unsubscribe(self, q):
        """客戶端離線時主動移除 Queue，釋放記憶體"""
        with self._lock:
            self._subscribers.discard(q)

    def get_current_payload(self):
        """查詢今日特徵簽章並打包為 JSON 字串"""
        try:
            today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            today_end = datetime.now().replace(hour=23, minute=59, second=59, microsecond=999999)
            orders_status = db.session.query(Order.id, Order.status).filter(
                Order.created_at >= today_start,
                Order.created_at <= today_end
            ).order_by(Order.id.asc()).all()
            
            state_signature = ",".join(f"{oid}:{status}" for oid, status in orders_status)
            pending_count = sum(1 for _, s in orders_status if s == 'Pending')
            
            return json.dumps({
                'timestamp': time.time(),
                'state_signature': state_signature,
                'pending_count': pending_count
            })
        except Exception as e:
            print(f"[!] 取得訂單特徵簽章失敗: {e}")
            return json.dumps({'timestamp': time.time(), 'state_signature': '', 'pending_count': 0})

    def notify(self):
        """當訂單狀態變更時呼叫：查一次 DB 並廣播，自動清除死佇列"""
        try:
            with app.app_context():
                payload = self.get_current_payload()
        except Exception as e:
            print(f"[!] SSE 廣播打包異常: {e}")
            return

        dead_queues = []
        with self._lock:
            for q in list(self._subscribers):
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    # 佇列持續塞滿代表該客戶端已斷線或無回應，標記清除
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except Exception:
                        dead_queues.append(q)

            # 移除失聯的訂閱者
            for dq in dead_queues:
                self._subscribers.discard(dq)

order_event_bus = OrderEventBus()

@app.route('/api/orders_stream')
def orders_stream():
    """向前端推播訂單狀態變更 (加入 Broken Pipe 與網路中斷防護)"""
    def event_stream():
        client_queue = order_event_bus.subscribe()
        try:
            with app.app_context():
                initial_payload = order_event_bus.get_current_payload()
            yield f"data: {initial_payload}\n\n"

            while True:
                try:
                    payload = client_queue.get(timeout=15)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    # 15 秒發送心跳，若連線已死會在 yield 時拋出例外
                    yield ": keep-alive\n\n"
        except (GeneratorExit, BrokenPipeError, ConnectionResetError, IOError):
            pass  # 客戶端正常或非正常關閉視窗，靜默處理
        finally:
            order_event_bus.unsubscribe(client_queue)

    return Response(
        event_stream(), 
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )

# ==============================================================================
# 9. 櫃檯取餐叫號看板路由 (Counter Display System)
# ==============================================================================
@app.route('/counter')
def counter_display():
    """櫃檯專屬取餐叫號即時看板"""
    return render_template('counter.html')
# ==============================================================================
# 10. 程式進入點 (Main Entry)
# ==============================================================================
if __name__ == '__main__':

    # 1. 強制讓 Python 繞過 Click 注入的 Windows Console 包裝器
    os.environ["PYTHONLEGACYWINDOWSSTDIO"] = "1"
    os.environ["NO_COLOR"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"

    # 2. 徹底關閉引發崩潰的 Flask 啟動橫幅輸出
    try:
        from flask import cli
        cli.show_server_banner = lambda *args, **kwargs: None
    except Exception:
        pass

    # 3. 確保標準輸出編碼安全
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    print("\n" + "="*50)
    print("🚀 自助點餐系統正常啟動中...")
    print("前台首頁: http://127.0.0.1:5000/")
    print("店家後台: http://127.0.0.1:5000/admin")
    print("="*50 + "\n")

    # 4. 正常啟動伺服器
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)