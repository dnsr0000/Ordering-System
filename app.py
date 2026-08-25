import time
import os
import cv2
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from PIL import Image, ImageOps
from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy

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
    modifiers = db.Column(db.String(50), default='none')
    description = db.Column(db.String(200), default='')
    image_path = db.Column(db.String(200), default='')
    is_recommended = db.Column(db.Boolean, default=False)
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

    # 4. 檢查 OrderItem 資料表
    order_item_info = db.session.execute(db.text("PRAGMA table_info(order_item)")).fetchall()
    order_item_cols = [col[1] for col in order_item_info]
    if 'customization' not in order_item_cols:
        db.session.execute(db.text("ALTER TABLE order_item ADD COLUMN customization VARCHAR(200) DEFAULT ''"))

    # 5. 初始化預設優惠券模板
    if Coupon.query.count() == 0:
        default_coupons = [
            Coupon(code='NEW50', title='全單現折 $50 優惠券', discount_type='fixed', discount_value=50, min_spend=200, reward_points=45, reward_discount_points=0, is_reward=True),
            Coupon(code='VIP90', title='全館消費 9 折券', discount_type='percent', discount_value=0.9, min_spend=100, reward_points=60, reward_discount_points=40, is_reward=True),
            Coupon(code='SAVE30', title='滿額折 $30 抵用券', discount_type='fixed', discount_value=30, min_spend=150, reward_points=25, reward_discount_points=0, is_reward=True)
        ]
        db.session.bulk_save_objects(default_coupons)
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

# ==============================================================================
# 5. 前台點餐與行銷優惠路由 (Customer & Marketing Routes)
# ==============================================================================
@app.route('/')
def customer_index():
    items = MenuItem.query.all()
    categories = sorted(list(set(item.category for item in items if item.category)))
    user_points = 0
    user_coupons = []
    
    if session.get('user_id'):
        user = User.query.get(session['user_id'])
        if user:
            user_points = user.points
            session['user_points'] = user.points
            user_coupons = UserCoupon.query.filter_by(user_id=user.id, is_used=False).all()
            
    return render_template(
        'customer.html',
        items=items,
        categories=categories,
        user_points=user_points,
        user_coupons=user_coupons
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
            'status': o.status,
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
    
@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('customer_index'))

# ==============================================================================
# 7. 店家後台管理路由 (Admin Management Routes)
# ==============================================================================
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

    orders = Order.query.order_by(Order.id.desc()).all()
    items = MenuItem.query.all()
    reward_items = MenuItem.query.filter_by(is_reward=True).all()
    reward_coupons = Coupon.query.all()
    users = User.query.all()
    return render_template(
        'admin.html', 
        is_admin=True, 
        orders=orders, 
        items=items, 
        reward_items=reward_items, 
        reward_coupons=reward_coupons,
        users=users
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
            is_discount=is_discount,
            discount_price=discount_price,
            is_new=is_new
        )
        db.session.add(new_item)
        db.session.commit()
        
    return redirect(url_for('admin_dashboard', tab='menu'))

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
        item.is_recommended = True if request.form.get('is_recommended') else False
        item.is_new = True if request.form.get('is_new') else False
        is_reward = True if request.form.get('is_reward') else False
        reward_points = max(0, int(request.form.get('reward_points') or 0))
        reward_discount_points = max(0, int(request.form.get('reward_discount_points') or 0))

        #  後端防呆驗證
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
    """Excel 批量匯入菜單"""
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_dashboard'))
        
    file = request.files.get('file')
    if not file or file.filename == '':
        return "<script>alert('請選擇 Excel/CSV 檔案！'); window.history.back();</script>", 400

    try:
        if file.filename.endswith('.csv'):
            df = pd.read_csv(file)
        else:
            df = pd.read_excel(file)

        column_mapping = {
            '餐點名稱': 'name',
            '分類': 'category',
            '單價': 'price',
            '客製化類型': 'modifiers',
            '簡介': 'description',
            '熱門推薦': 'is_recommended',
            '新品': 'is_new'
        }
        df.rename(columns=column_mapping, inplace=True)

        if 'name' not in df.columns or 'price' not in df.columns:
            return "<script>alert('檔案缺少必要欄位：「餐點名稱(name)」或「單價(price)」！'); window.history.back();</script>", 400

        for _, row in df.iterrows():
            name = str(row.get('name', '')).strip()
            if not name or pd.isna(row.get('name')):
                continue

            category = str(row.get('category', '主餐')).strip() if not pd.isna(row.get('category')) else '主餐'
            
        try:
            price = max(0, round(float(row.get('price', 0))))
        except (ValueError, TypeError):
            price = 0

            modifiers = str(row.get('modifiers', 'none')).strip() if not pd.isna(row.get('modifiers')) else 'none'
            if modifiers not in ['none', 'ice_sugar', 'spicy', 'addons']:
                modifiers = 'none'

            description = str(row.get('description', '')).strip() if not pd.isna(row.get('description')) else ''
            
            rec_val = str(row.get('is_recommended', '')).lower()
            is_recommended = rec_val in ['1', 'true', '是', 'yes']

            new_val = str(row.get('is_new', '')).lower()
            is_new = new_val in ['1', 'true', '是', 'yes']

            new_item = MenuItem(
                name=name,
                category=category,
                price=price,
                modifiers=modifiers,
                description=description,
                image_path='',
                is_recommended=is_recommended,
                is_discount=False, 
                discount_price=0,
                is_new=is_new
            )
            db.session.add(new_item)

        db.session.commit()
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