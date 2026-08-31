import time
import os
import cv2
import json
import io
import unicodedata
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from PIL import Image, ImageOps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify, send_file 
from flask_sqlalchemy import SQLAlchemy
from openpyxl.styles import Font

# ==============================================================================
# 1. 應用程式基礎設定 (App Configuration)
# ==============================================================================
app = Flask(__name__)
app.secret_key = "ordering_system_secret_key"
app.permanent_session_lifetime = timedelta(days=7)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'menu.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# 建立靜態資源上傳路徑
UPLOAD_FOLDER_MEMBER = os.path.join(BASE_DIR, 'static', 'member')
UPLOAD_FOLDER_MENU = os.path.join(BASE_DIR, 'static', 'menu')
os.makedirs(UPLOAD_FOLDER_MEMBER, exist_ok=True)
os.makedirs(UPLOAD_FOLDER_MENU, exist_ok=True)
app.config['UPLOAD_FOLDER_MEMBER'] = UPLOAD_FOLDER_MEMBER
app.config['UPLOAD_FOLDER_MENU'] = UPLOAD_FOLDER_MENU

# OpenCV 人臉辨識模型路徑
YUNET_MODEL = os.path.join(BASE_DIR, "face_detection_yunet_2023mar.onnx")
SFACE_MODEL = os.path.join(BASE_DIR, "face_recognition_sface_2021dec.onnx")

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

class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), default='主餐')
    price = db.Column(db.Integer, nullable=False)
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

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, nullable=True)
    table_number = db.Column(db.String(50), default='訪客')
    total_price = db.Column(db.Integer, nullable=False)
    payment_method = db.Column(db.String(50), default='Cash')
    order_type = db.Column(db.String(50), default='內用')
    status = db.Column(db.String(50), default='Pending')
    points_used = db.Column(db.Integer, default=0)
    points_earned = db.Column(db.Integer, default=0)
    discount_amount = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.now)
    items = db.relationship('OrderItem', backref='order', lazy=True, cascade="all, delete-orphan")
    @property
    def user(self):
        if self.user_id:
            return User.query.get(self.user_id)
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

# --- 💡 各會員專屬持有與兌換紀錄資料表 (分開管理) ---
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
    if 'status' not in order_cols:
        db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN status VARCHAR(50) DEFAULT "Pending"'))
    if 'is_manual_popular' not in menu_cols:
        db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_manual_popular BOOLEAN DEFAULT 0"))
    # 4. 檢查 OrderItem 資料表
    order_item_info = db.session.execute(db.text("PRAGMA table_info(order_item)")).fetchall()
    order_item_cols = [col[1] for col in order_item_info]
    if 'customization' not in order_item_cols:
        db.session.execute(db.text("ALTER TABLE order_item ADD COLUMN customization VARCHAR(200) DEFAULT ''"))

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
    """讀取照片、修正 EXIF 方向旋轉問題並壓縮存檔"""
    img = Image.open(file_storage)
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")
    img.thumbnail((800, 800))
    img.save(dest_path, "JPEG", quality=88)

def extract_feature(image_path):
    """使用 YuNet 與 SFace 進行人臉特徵提取"""
    if not os.path.exists(YUNET_MODEL) or not os.path.exists(SFACE_MODEL):
        return None

    detector = cv2.FaceDetectorYN.create(YUNET_MODEL, "", (320, 320), 0.6, 0.3, 5000)
    recognizer = cv2.FaceRecognizerSF.create(SFACE_MODEL, "")

    img = cv2.imread(image_path)
    if img is None:
        return None

    h, w, _ = img.shape
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)

    if faces is not None and len(faces) > 0:
        aligned_face = recognizer.alignCrop(img, faces[0])
        feature = recognizer.feature(aligned_face)
        return feature
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
        ).filter(~OrderItem.item_name.like('%[點數兌換]%')  # 💡 排除點數兌換
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
# 5. 前台點餐與行銷優惠路由 (Customer & Marketing Routes)
# ==============================================================================
@app.route('/')
def customer_index():
    update_popular_items()
    items = MenuItem.query.all()
    categories = sorted(list(set(item.category for item in items if item.category)))
    user_points = 0
    user_coupons = []
    is_guest = session.get('is_guest', False)
    
    if session.get('user_id'):
        user = User.query.get(session['user_id'])
        if user:
            user_points = user.points
            session['user_points'] = user.points
            user_coupons = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
            session['is_guest'] = False
            is_guest = False
            
    return render_template(
        'customer.html',
        items=items,
        categories=categories,
        user_points=user_points,
        user_coupons=user_coupons,
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
    """處理結帳、核銷會員專屬優惠券、扣抵點數、累積新點數"""
    data = request.get_json()
    items = data.get('items', [])
    payment_method = data.get('payment_method', 'Cash')
    order_type = data.get('order_type', '內用')
    promo_code = str(data.get('promo_code', '')).strip().upper()
    use_points = int(data.get('use_points', 0))

    if not items:
        return jsonify({'error': '購物車為空'}), 400

    # 1. 計算商品原始小計
    subtotal = sum(item['price'] * item['quantity'] for item in items)
    
    # 2. 促銷代碼計算與核銷驗證
    promo_discount = 0
    target_user_coupon = None
    user_id = session.get('user_id')
    user = User.query.get(user_id) if user_id else None

    if promo_code:
        if user:
            target_user_coupon = UserCoupon.query.filter_by(user_id=user.id, code=promo_code, is_used=False).first()
            if target_user_coupon and subtotal >= target_user_coupon.coupon.min_spend:
                cp = target_user_coupon.coupon
                if cp.discount_type == 'fixed':
                    promo_discount = min(cp.discount_value, subtotal)
                else:
                    promo_discount = round(subtotal * (1.0 - cp.discount_value))
        
        # 若非會員持有券，檢查公開免點數券
        if not target_user_coupon:
            pub_cp = Coupon.query.filter_by(code=promo_code, is_reward=False, reward_points=0).first()
            if pub_cp and subtotal >= pub_cp.min_spend:
                if pub_cp.discount_type == 'fixed':
                    promo_discount = min(pub_cp.discount_value, subtotal)
                else:
                    promo_discount = round(subtotal * (1.0 - pub_cp.discount_value))

    remaining_amount = max(0, subtotal - promo_discount)

    # 3. 計算會員紅利折抵 (1 點 = $1)
    points_used = 0
    if user and use_points > 0:
        points_used = int(min(user.points, use_points, remaining_amount))

    final_price = max(0, remaining_amount - points_used)
    total_discount = promo_discount + points_used

    # 4. 累積新點數 (實付金額每滿 $10 累積 1 點)
    points_earned = int(final_price // 100) if user else 0

    if user:
        user.points = user.points - points_used + points_earned
        session['user_points'] = user.points
        
        # 💡 將會員持有的專屬優惠券正式標記為「已使用」
        if target_user_coupon:
            target_user_coupon.is_used = True
            target_user_coupon.used_at = datetime.now()

    user_name = session.get('user_name', '訪客')
    new_order = Order(
        user_id=session.get('user_id'),
        table_number=user_name,
        total_price=int(final_price),
        payment_method=payment_method,
        order_type=order_type,
        status='Pending',
        points_used=points_used,
        points_earned=points_earned,
        discount_amount=int(total_discount)
    )
    db.session.add(new_order)
    db.session.flush()

    for item in items:
        order_item = OrderItem(
            order_id=new_order.id,
            item_name=item['name'],
            price=int(item['price']),
            quantity=item['quantity'],
            customization=item.get('customization', '')
        )
        db.session.add(order_item)

    db.session.commit()
    update_popular_items()

    receipt_items = []
    for item in items:
        receipt_items.append({
            'name': item['name'],
            'price': item['price'],
            'quantity': item['quantity'],
            'subtotal': item['price'] * item['quantity'],
            'customization': item.get('customization', '')
        })

    return jsonify({
        'order_id': new_order.id,
        'user_name': user_name,
        'subtotal': subtotal,
        'discount_amount': total_discount,
        'points_used': points_used,
        'points_earned': points_earned,
        'total_price': final_price,
        'payment_method': payment_method,
        'order_type': order_type,
        'items': receipt_items,
        'current_user_points': user.points if user else 0
    })

# --- 🎁 動態紅利回饋商城頁面 ---
@app.route('/rewards')
def rewards_store():
    if not session.get('user_id'):
        return redirect(url_for('face_login'))
    
    user = User.query.get(session['user_id'])
    if not user:
        return redirect(url_for('logout'))
        
    reward_items = MenuItem.query.filter_by(is_reward=True).all()
    reward_coupons = Coupon.query.filter_by(is_reward=True).all()
    session['user_points'] = user.points
    return render_template('rewards.html', user=user, reward_items=reward_items, reward_coupons=reward_coupons)

# --- 🎁 點數兌換餐點與專屬優惠券 API ---
@app.route('/api/redeem_reward', methods=['POST'])
def redeem_reward():
    if not session.get('user_id'):
        return jsonify({'success': False, 'message': '請先登入會員！'}), 401
        
    user = User.query.get(session['user_id'])
    data = request.get_json() or {}

    # 1. 兌換優惠券：建立獨立 UserCoupon 實體
    if 'coupon_id' in data:
        coupon = Coupon.query.get(data.get('coupon_id'))
        if not coupon:
            return jsonify({'success': False, 'message': '無效的優惠券！'}), 400
        
        req_points = coupon.reward_discount_points if coupon.reward_discount_points > 0 else coupon.reward_points
        if user.points < req_points:
            return jsonify({'success': False, 'message': f'紅利點數不足！兌換需要 {req_points} 點。'}), 400
            
        user.points -= req_points
        session['user_points'] = user.points

        # 💡 新增該會員專屬未使用的優惠券實體
        user_coupon = UserCoupon(
            user_id=user.id,
            coupon_id=coupon.id,
            code=coupon.code,
            is_used=False
        )
        db.session.add(user_coupon)
        db.session.commit()

        return jsonify({
            'success': True,
            'is_coupon': True,
            'promo_code': coupon.code,
            'message': f'🎉 成功使用 {req_points} 點兌換【{coupon.title}】！\n已存入您的個人專屬票夾，結帳代碼為：{coupon.code}',
            'remaining_points': user.points
        })

    # 2. 兌換餐點
    item = MenuItem.query.get(data.get('item_id'))
    if not item or not item.is_reward:
        return jsonify({'success': False, 'message': '無效的兌換商品！'}), 400
    
    req_points = item.reward_discount_points if item.reward_discount_points > 0 else item.reward_points
    if user.points < req_points:
        return jsonify({'success': False, 'message': f'紅利點數不足！兌換需要 {req_points} 點。'}), 400
        
    user.points -= req_points
    session['user_points'] = user.points
    db.session.commit()
    
    return jsonify({
        'success': True,
        'is_coupon': False,
        'message': f'🎉 成功使用 {req_points} 點兌換【{item.name}】！',
        'remaining_points': user.points,
        'redeemed_item': {
            'id': f'reward_{item.id}_{int(time.time())}',
            'name': f'🎁 [點數兌換] {item.name}',
            'price': 0,
            'customization': '紅利免費兌換',
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
        
        result.append({
            'order_id': o.id,
            'user_order_no': user_order_no, 
            'table_number': o.table_number,
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
        save_and_fix_image(photo, filepath)

        feature = extract_feature(filepath)
        if feature is None:
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except Exception:
                    pass
            return "<script>alert('⚠️ 照片未偵測到清晰人臉！請正對鏡頭並重新拍攝。'); window.history.back();</script>", 400

        new_user = User(name=name, phone=phone, photo_path=filename, feature=feature, points=20)
        db.session.add(new_user)
        db.session.commit()

        session['user_id'] = new_user.id
        session['user_name'] = new_user.name
        session['user_points'] = new_user.points
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
            session['user_id'] = matched_user.id
            session['user_name'] = matched_user.name
            session['user_points'] = matched_user.points
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
        session['user_id'] = user.id
        session['user_name'] = user.name
        session['user_points'] = user.points
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
    """清除舊登入狀態並建立訪客 Session"""
    session.clear()
    session['user_id'] = None
    session['user_name'] = '訪客'
    session['is_guest'] = True
    session['user_points'] = 0
    return redirect(url_for('customer_index'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('customer_index'))

# ==============================================================================
# 7. 店家後台管理路由 (Admin Management Routes)
# ==============================================================================
def build_order_analytics(orders, limit=1):
    today = datetime.now().date()
    today_orders = [o for o in orders if o.created_at and o.created_at.date() == today]
    pending_orders = [o for o in orders if o.status == 'Pending']

    total_revenue = sum((o.total_price or 0) for o in today_orders)
    avg_order_value = total_revenue / len(today_orders) if today_orders else 0

    valid_item_names = {item.name for item in MenuItem.query.all()}

    item_stats = {}
    for order in orders:
        if order.status == 'Cancelled':  
            continue
        for item in order.items:
            # 💡 排除點數兌換品項
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
    return {
        'today_orders': len(today_orders),
        'today_revenue': total_revenue,
        'pending_count': len(pending_orders),
        'avg_order_value': avg_order_value,
        'top_items': top_items,
        'eta_minutes': eta_minutes,
        'completed_today': sum(1 for o in today_orders if o.status == 'Completed')
    }

@app.route('/admin', methods=['GET', 'POST'])
def admin_dashboard():
    if request.method == 'POST' and 'username' in request.form:
        username = request.form.get('username')
        password = request.form.get('password')
        if username == '1234' and password == '1234':
            session['admin_logged_in'] = True
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
    
    # 2. 💡 實時訂單看板：過濾只留下「今天」建立的訂單（實現每日重製）
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
    session.pop('admin_logged_in', None)
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
    desc = request.form.get('description', '')
    image = request.files.get('image')
    is_rec = request.form.get('is_recommended') == '1'
    is_new = request.form.get('is_new') == '1'
    is_discount = request.form.get('is_discount') == '1'
    discount_price = max(0, round(float(request.form.get('discount_price') or 0)))

    image_filename = ''
    if image and image.filename != '':
        image_filename = f"menu_{int(time.time())}.jpg"
        filepath = os.path.join(app.config['UPLOAD_FOLDER_MENU'], image_filename)
        save_and_fix_image(image, filepath)

    if name and price:
        new_item = MenuItem(
            name=name,
            category=category,
            modifiers=modifiers,
            price=max(0, round(float(price))),
            description=desc,
            image_path=image_filename,
            is_recommended=is_rec,
            is_manual_popular=is_rec,
            is_discount=is_discount,
            discount_price=discount_price,
            is_new=is_new
        )
        db.session.add(new_item)
        db.session.commit()
        
    return redirect(url_for('admin_dashboard', tab='menu'))

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

        # 走訪檔案內所有的工作表 (Sheet)
        for sheet in excel_file.sheet_names:
            df = pd.read_excel(excel_file, sheet_name=sheet)
            if df.empty: continue

            # 將所有欄位名稱轉小寫，用作純英文特徵辨識輔助
            cols_lower = [str(c).lower() for c in df.columns]

            # ---------------------------------------------------------
            # 1. 辨識是否為「會員資料 (User)」
            # ---------------------------------------------------------
            if 'User' in sheet or '會員' in sheet or '電話(Phone)' in df.columns or 'phone' in cols_lower or '電話' in df.columns:
                col_map = {'姓名(Name)': 'name', '姓名': 'name', '電話(Phone)': 'phone', '電話': 'phone', '紅利點數(Points)': 'points', '紅利點數': 'points'}
                df_user = df.rename(columns=col_map)
                
                if 'name' in df_user.columns and 'phone' in df_user.columns:
                    for _, row in df_user.iterrows():
                        name = str(row.get('name', '')).strip()
                        phone = str(row.get('phone', '')).strip()
                        if not name or not phone or pd.isna(row.get('name')) or name == '目前無資料': continue
                        
                        try: points = int(row.get('points', 0))
                        except: points = 0
                        
                        existing = User.query.filter_by(phone=phone).first()
                        if existing:
                            existing.name = name
                            existing.points = points
                        else:
                            new_user = User(name=name, phone=phone, points=points, photo_path='', feature=None)
                            db.session.add(new_user)
                        imported_counts['User'] += 1

            # ---------------------------------------------------------
            # 2. 辨識是否為「菜單品項 (MenuItem)」
            # ---------------------------------------------------------
            elif 'MenuItem' in sheet or '菜單' in sheet or '單價(Price)' in df.columns or 'price' in cols_lower or '單價' in df.columns:
                col_map = {
                    '餐點名稱(Name)': 'name', '名稱(Name)': 'name', '餐點名稱': 'name',
                    '分類(Category)': 'category', '分類': 'category',
                    '單價(Price)': 'price', '單價': 'price',
                    '客製化群組(Modifiers)': 'modifiers', '客製化類型': 'modifiers',
                    '商品描述(Description)': 'description', '簡介': 'description',
                    '熱門推薦(Recommended)': 'is_recommended', '熱門(Popular)': 'is_recommended',
                    '新品上市(Is New)': 'is_new', '新品': 'is_new',
                    '是否特價(Is Discount)': 'is_discount', '是否特價': 'is_discount',
                    '特價金額(Discount Price)': 'discount_price', '特價金額': 'discount_price',
                    '開放紅利兌換(Is Reward)': 'is_reward', '是否開放紅利兌換': 'is_reward',
                    '兌換所需點數(Reward Points)': 'reward_points', '兌換點數': 'reward_points',
                    '限時特惠點數(Reward Discount Points)': 'reward_discount_points', '特惠點數': 'reward_discount_points'
                }
                df_menu = df.rename(columns=col_map)
                
                if 'name' in df_menu.columns and 'price' in df_menu.columns:
                    for _, row in df_menu.iterrows():
                        name = str(row.get('name', '')).strip()
                        if not name or pd.isna(row.get('name')) or name == '目前無資料': continue
                        
                        category = str(row.get('category', '主餐')).strip() if not pd.isna(row.get('category')) else '主餐'
                        try: price = max(0, round(float(row.get('price', 0))))
                        except: price = 0
                        try: discount_price = max(0, round(float(row.get('discount_price', 0))))
                        except: discount_price = 0
                        try: reward_points = max(0, int(row.get('reward_points', 0)))
                        except: reward_points = 0
                        try: reward_discount_points = max(0, int(row.get('reward_discount_points', 0)))
                        except: reward_discount_points = 0

                        modifiers = str(row.get('modifiers', 'none')).strip() if not pd.isna(row.get('modifiers')) else 'none'
                        if modifiers not in ['none', 'ice_sugar', 'spicy', 'addons']: modifiers = 'none'

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
                        else:
                            new_item = MenuItem(
                                name=name, category=category, price=price, modifiers=modifiers, description=description,
                                image_path='', is_recommended=False, is_manual_popular=is_rec,
                                is_discount=is_disc, discount_price=discount_price, is_new=is_new,
                                is_reward=is_rew, reward_points=reward_points, reward_discount_points=reward_discount_points
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
                        if not code or not title or pd.isna(row.get('code')) or code == '目前無資料': continue
                        
                        dtype = str(row.get('discount_type', 'fixed')).strip()
                        if dtype not in ['fixed', 'percent']: dtype = 'fixed'
                        
                        try: dvalue = max(0.0, float(row.get('discount_value', 0)))
                        except: dvalue = 0.0
                        try: min_sp = max(0.0, float(row.get('min_spend', 0)))
                        except: min_sp = 0.0
                        try: reward_points = max(0, int(row.get('reward_points', 0)))
                        except: reward_points = 0
                        try: reward_discount_points = max(0, int(row.get('reward_discount_points', 0)))
                        except: reward_discount_points = 0
                        
                        # 判斷是否上架回饋商城：若欄位有明確指定則依欄位；無欄位時若點數大於 0 則自動設為 True
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
        update_popular_items()  # 重算一次菜單熱門推薦
        
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
    """根據勾選的資料表匯出 SQLite 資料為 Excel (精確計算 Emoji 寬度與按需顯示日期區間)"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    selected_tables = request.form.getlist('tables')
    if not selected_tables:
        return "<script>alert('❌ 請至少勾選一個資料表！'); window.history.back();</script>", 400

    start_date_str = request.form.get('start_date')
    end_date_str = request.form.get('end_date')
    
    orders_query = Order.query
    if start_date_str:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d')
        orders_query = orders_query.filter(Order.created_at >= start_date)
    if end_date_str:
        end_date = datetime.strptime(end_date_str, '%Y-%m-%d') + timedelta(days=1, seconds=-1)
        orders_query = orders_query.filter(Order.created_at <= end_date)

    filtered_orders = orders_query.all()
    filtered_order_ids = [o.id for o in filtered_orders]

    output = io.BytesIO()
    
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        
        # 1. 靜態資料表（無時間區間，直接從第 1 列開始寫入）
        if 'User' in selected_tables:
            users = User.query.all()
            data = [{'ID': u.id, '姓名(Name)': u.name, '電話(Phone)': u.phone, '紅利點數(Points)': u.points} for u in users]
            df = pd.DataFrame(data if data else [{'資料': '目前無資料'}])
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

        # 2. 動態資料表（受時間區間影響，預留第 1 列放置區間文字 startrow=1）
        if 'Order' in selected_tables:
            data = [{'ID': o.id, '會員ID(User ID)': o.user_id, '桌號/稱呼': o.table_number, '總金額(Total)': o.total_price, '付款方式(Payment)': o.payment_method, '狀態(Status)': o.status, '使用紅利': o.points_used, '建立時間': o.created_at.strftime('%Y-%m-%d %H:%M') if o.created_at else ''} for o in filtered_orders]
            df = pd.DataFrame(data if data else [{'資料': '所選日期範圍內無訂單'}])
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

        # 3. 欄位寬度與時間標示後處理
        display_start = start_date_str if start_date_str else '全部區間 (All)'
        display_end = end_date_str if end_date_str else '全部區間 (All)'
        date_range_text = f"報表資料區間：{display_start} ~ {display_end}"
        
        # 僅限以下具備時間條件的工作表加入日期標題
        date_dependent_sheets = {'訂單總覽(Order)', '訂單明細(OrderItem)', '區間營運總覽(Analytics)', '區間熱銷排行(ItemSales)'}

        def calculate_display_width(val):
            if val is None:
                return 0
            text = str(val)
            width = 0.0
            for char in text:
                status = unicodedata.east_asian_width(char)
                # 處理寬字元、中文字與 Emoji (如 🎁)
                if status in ('F', 'W') or ord(char) >= 0x2600 or ord(char) >= 0x1F000:
                    width += 2.2
                else:
                    width += 1.1
            return int(width)

        for sheet_name, ws in writer.sheets.items():
            # 只有時間相關工作表在 A1 填入標題
            if sheet_name in date_dependent_sheets:
                ws.cell(row=1, column=1, value=date_range_text).font = Font(bold=True, color="0055aa")

            # 依欄位最長內容自動計算寬度
            for col in ws.columns:
                max_len = 0
                column_letter = col[0].column_letter
                for cell in col:
                    # 排除 A1 標題以避免第一欄寬度被撐過大
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

@app.route('/admin/edit/<int:id>', methods=['POST'])
def edit_item(id):
    """店家編輯菜單品項"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    item = MenuItem.query.get_or_404(id)
    name = request.form.get('name')
    price = request.form.get('price')
    
    if name and price:
        item.name = name
        item.category = request.form.get('category', '主餐')
        item.modifiers = request.form.get('modifiers', 'none')
        item.price = max(0, round(float(price)))
        item.is_discount = True if request.form.get('is_discount') else False
        item.discount_price = max(0, round(float(request.form.get('discount_price') or 0)))
        item.description = request.form.get('description', '')
        
# 取得熱門勾選狀態（手動勾選只存入 is_manual_popular，不碰 is_recommended）
        is_pop = request.form.get('is_recommended') or request.form.get('is_manual_popular')
        item.is_manual_popular = True if is_pop in ['1', 'true', 'on', True] else False
        
        item.is_new = True if request.form.get('is_new') else False

        is_reward = True if request.form.get('is_reward') else False
        reward_points = max(0, int(request.form.get('reward_points') or 0))
        reward_discount_points = max(0, int(request.form.get('reward_discount_points') or 0))

        if is_reward:
            if reward_points <= 0:
                return "<script>alert('❌ 啟用紅利兌換時，「兌換所需點數」必須大於 0！'); window.history.back();</script>", 400
            if reward_discount_points > 0 and reward_discount_points >= reward_points:
                return "<script>alert('❌ 「限時優惠點數」必須小於「兌換所需點數」！'); window.history.back();</script>", 400

        item.is_reward = is_reward
        item.reward_points = reward_points
        item.reward_discount_points = reward_discount_points

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
        
    from_tab = request.form.get('from_tab') or ('rewards' if item.is_reward else 'menu')
    return redirect(url_for('admin_dashboard', tab=from_tab))

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
    
    if new_status in ['Pending', 'Completed', 'Cancelled']:
        order.status = new_status
        db.session.commit()
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
    uc.is_used = not uc.is_used  
    uc.used_at = datetime.now() if uc.is_used else None
    db.session.commit()
    
    # 取得該會員最新的所有票券清單回傳給前端
    user = User.query.get(uc.user_id)
    coupons_list = [{
        'id': c.id,
        'title': c.coupon.title,
        'code': c.code,
        'is_used': c.is_used
    } for c in user.user_coupons]
    
    return jsonify({
        'success': True,
        'user_name': user.name,
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
    
    return redirect(url_for('admin_dashboard', tab='users'))


# ==============================================================================
# 8. 程式進入點 (Main Entry)
# ==============================================================================
if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)