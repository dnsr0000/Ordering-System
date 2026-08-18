import time
import os
import cv2
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_sqlalchemy import SQLAlchemy
from werkzeug.utils import secure_filename
from PIL import Image, ImageOps

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
template_dir = os.path.join(BASE_DIR, 'templates')
app = Flask(__name__, template_folder=template_dir)
app.secret_key = 'kiosk_secret_key_123'

# 修復 1：將 datetime 與 timedelta 註冊至 Jinja2 全域環境，解決 'datetime' 未定義錯誤
app.jinja_env.globals.update(datetime=datetime, timedelta=timedelta)

db_path = os.path.join(BASE_DIR, 'menu.db')
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

menu_path = os.path.join(BASE_DIR, 'static', 'menu')
member_path = os.path.join(BASE_DIR, 'static', 'member')
app.config['UPLOAD_FOLDER_MENU'] = menu_path
app.config['UPLOAD_FOLDER_MEMBER'] = member_path

os.makedirs(app.config['UPLOAD_FOLDER_MENU'], exist_ok=True)
os.makedirs(app.config['UPLOAD_FOLDER_MEMBER'], exist_ok=True)

db = SQLAlchemy(app)

yunet_path = os.path.join(BASE_DIR, "face_detection_yunet_2023mar.onnx")
sface_path = os.path.join(BASE_DIR, "face_recognition_sface_2021dec.onnx")

detector = cv2.FaceDetectorYN.create(yunet_path, "", (320, 320))
recognizer = cv2.FaceRecognizerSF.create(sface_path, "")

# --- 資料庫模型 ---
class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(50), nullable=False, default='主餐') 
    price = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(200))
    image_path = db.Column(db.String(200), nullable=True)
    modifiers = db.Column(db.String(100), default='none')
    is_recommended = db.Column(db.Boolean, default=False)
    is_new = db.Column(db.Boolean, default=False)
    
    # 限時限額特價欄位
    is_special = db.Column(db.Boolean, default=False)
    special_price = db.Column(db.Integer, nullable=True)
    special_stock = db.Column(db.Integer, default=0)
    special_start = db.Column(db.DateTime, nullable=True)
    special_end = db.Column(db.DateTime, nullable=True)

class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    photo_path = db.Column(db.String(200), nullable=False)

class Order(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    table_number = db.Column(db.String(50))
    order_type = db.Column(db.String(20), nullable=False, default='內用')
    total_price = db.Column(db.Integer, nullable=False)
    payment_method = db.Column(db.String(50))
    status = db.Column(db.String(20), default='Pending')
    created_at = db.Column(db.DateTime, default=lambda: datetime.utcnow() + timedelta(hours=8))
    items = db.relationship('OrderItem', backref='order', lazy=True)

class OrderItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('order.id'), nullable=False)
    item_name = db.Column(db.String(100), nullable=False)
    quantity = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Integer, nullable=False)
    customization = db.Column(db.String(200), default='')

with app.app_context():
    db.create_all()

    # SQLite 自動檢測並補充新欄位
    if app.config['SQLALCHEMY_DATABASE_URI'].startswith('sqlite:///'):
        conn = db.engine.raw_connection()
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(menu_item)")
        existing_columns = [row[1] for row in cursor.fetchall()]
        
        new_cols = {
            'is_recommended': 'BOOLEAN DEFAULT 0',
            'is_new': 'BOOLEAN DEFAULT 0',
            'is_special': 'BOOLEAN DEFAULT 0',
            'special_price': 'INTEGER',
            'special_stock': 'INTEGER DEFAULT 0',
            'special_start': 'DATETIME',
            'special_end': 'DATETIME'
        }
        
        for col_name, col_type in new_cols.items():
            if col_name not in existing_columns:
                cursor.execute(f"ALTER TABLE menu_item ADD COLUMN {col_name} {col_type}")
                
        conn.commit()
        cursor.close()
        conn.close()

# --- 圖片處理與人臉辨識函式 ---
def save_and_fix_image(file_storage, save_path):
    try:
        img = Image.open(file_storage)
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((1024, 1024))
        img.save(save_path, "JPEG", quality=90)
        return True
    except Exception as e:
        print(f"圖片轉正失敗: {e}")
        return False

def get_face_feature(image_path):
    img = cv2.imread(image_path)
    if img is None: 
        return None
    h, w, _ = img.shape
    detector.setInputSize((w, h))
    _, faces = detector.detect(img)
    if faces is None or len(faces) == 0: 
        return None
    face_align = recognizer.alignCrop(img, faces[0])
    return recognizer.feature(face_align)

# --- 店家後台管理 ---
@app.route('/admin', methods=['GET', 'POST'])
def admin_index():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == '1234' and password == '1234':
            session['admin_logged_in'] = True
            return redirect(url_for('admin_index'))
        else:
            return "<h1>帳號或密碼錯誤！</h1><a href='/admin'>重新登入</a>", 401

    if not session.get('admin_logged_in'):
        return render_template('admin.html')
        
    items = MenuItem.query.all()
    users = User.query.all()
    orders = Order.query.order_by(Order.created_at.desc()).all()
    return render_template('admin.html', items=items, users=users, orders=orders)

@app.route('/admin/update_order_status/<int:order_id>', methods=['POST'])
def update_order_status(order_id):
    if not session.get('admin_logged_in'): 
        return jsonify({'error': 'Unauthorized'}), 401
        
    data = request.json
    new_status = data.get('status')
    order = Order.query.get_or_404(order_id)
    if new_status:
        order.status = new_status
        db.session.commit()
        return jsonify({'message': '狀態更新成功', 'status': new_status})
    return jsonify({'error': '無效的狀態'}), 400

@app.route('/admin_logout')
def admin_logout():
    session.pop('admin_logged_in', None)
    return redirect(url_for('customer_index'))

@app.route('/admin/add', methods=['POST'])
def add_item():
    if not session.get('admin_logged_in'): 
        return redirect(url_for('admin_index'))
    
    name = request.form.get('name')
    category = request.form.get('category', '主餐') 
    modifiers = request.form.get('modifiers', 'none')
    price = request.form.get('price')
    desc = request.form.get('description')
    
    # 修復 2：使用小寫 image 避免與 PIL.Image 模組名稱衝突
    image = request.files.get('image')
    
    is_recommended = request.form.get('is_recommended') == 'on'
    is_new = request.form.get('is_new') == 'on'
    is_special = request.form.get('is_special') == 'on'
    
    special_price = request.form.get('special_price')
    special_stock = request.form.get('special_stock', 0)
    special_start_str = request.form.get('special_start')
    special_end_str = request.form.get('special_end')

    special_start = datetime.strptime(special_start_str, '%Y-%m-%dT%H:%M') if special_start_str else None
    special_end = datetime.strptime(special_end_str, '%Y-%m-%dT%H:%M') if special_end_str else None

    if name and price:
        image_filename = ""
        if image and image.filename != '':
            ext = os.path.splitext(image.filename)[1] or '.jpg'
            image_filename = f"menu_{int(time.time())}{ext}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER_MENU'], image_filename)
            image.save(filepath)
            
        new_item = MenuItem(
            name=name, 
            category=category, 
            modifiers=modifiers, 
            price=int(price), 
            description=desc, 
            image_path=image_filename,
            is_recommended=is_recommended,
            is_new=is_new,
            is_special=is_special,
            special_price=int(special_price) if special_price else None,
            special_stock=int(special_stock) if special_stock else 0,
            special_start=special_start,
            special_end=special_end
        )
        db.session.add(new_item)
        db.session.commit()
        
    return redirect(url_for('admin_index', tab='menu'))

@app.route('/admin/import_excel', methods=['POST'])
def import_menu_excel():
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_index'))
    
    file = request.files.get('excel_file')
    if not file or file.filename == '':
        return "<h1>請選擇 Excel 檔案</h1><a href='/admin'>返回後台</a>", 400

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
            return "<h1>檔案缺少必要欄位：「餐點名稱(name)」或「單價(price)」</h1><a href='/admin'>返回後台</a>", 400

        for _, row in df.iterrows():
            name = str(row.get('name', '')).strip()
            if not name or pd.isna(row.get('name')):
                continue

            category = str(row.get('category', '主餐')).strip() if not pd.isna(row.get('category')) else '主餐'
            
            try:
                price = int(float(row.get('price', 0)))
            except (ValueError, TypeError):
                price = 0

            modifiers = str(row.get('modifiers', 'none')).strip() if not pd.isna(row.get('modifiers')) else 'none'
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
                is_new=is_new
            )
            db.session.add(new_item)

        db.session.commit()
        return redirect(url_for('admin_index', tab='menu'))

    except Exception as e:
        db.session.rollback()
        return f"<h1>匯入解析失敗：{e}</h1><a href='/admin'>返回後台</a>", 500

@app.route('/admin/edit/<int:id>', methods=['POST'])
def edit_item(id):
    if not session.get('admin_logged_in'):
        return redirect(url_for('admin_index'))
    
    item = MenuItem.query.get_or_404(id)
    
    name = request.form.get('name')
    category = request.form.get('category', '主餐')
    modifiers = request.form.get('modifiers', 'none')
    price = request.form.get('price')
    desc = request.form.get('description', '')
    
    # 修復 2：使用小寫 image 變數
    image = request.files.get('image')
    
    is_recommended = request.form.get('is_recommended') == 'on'
    is_new = request.form.get('is_new') == 'on'
    is_special = request.form.get('is_special') == 'on'
    
    special_price = request.form.get('special_price')
    special_stock = request.form.get('special_stock', 0)
    special_start_str = request.form.get('special_start')
    special_end_str = request.form.get('special_end')

    if name and price:
        item.name = name
        item.category = category
        item.modifiers = modifiers
        item.price = int(float(price))
        item.description = desc
        item.is_recommended = is_recommended
        item.is_new = is_new
        item.is_special = is_special
        item.special_price = int(special_price) if special_price else None
        item.special_stock = int(special_stock) if special_stock else 0
        item.special_start = datetime.strptime(special_start_str, '%Y-%m-%dT%H:%M') if special_start_str else None
        item.special_end = datetime.strptime(special_end_str, '%Y-%m-%dT%H:%M') if special_end_str else None

        # 正確檢查小寫 image
        if image and image.filename != '':
            ext = os.path.splitext(image.filename)[1] or '.jpg'
            image_filename = f"menu_{int(time.time())}{ext}"
            filepath = os.path.join(app.config['UPLOAD_FOLDER_MENU'], image_filename)
            image.save(filepath)
            
            if item.image_path:
                old_path = os.path.join(app.config['UPLOAD_FOLDER_MENU'], item.image_path)
                try:
                    if os.path.exists(old_path):
                        os.remove(old_path)
                except Exception:
                    pass
            
            item.image_path = image_filename
            
        db.session.commit()
        
    return redirect(url_for('admin_index', tab='menu'))

@app.route('/admin/delete/<int:id>')
def delete_item(id):
    if not session.get('admin_logged_in'): 
        return redirect(url_for('admin_index'))
    item_to_delete = MenuItem.query.get_or_404(id)
    
    if item_to_delete.image_path:
        filepath = os.path.join(app.config['UPLOAD_FOLDER_MENU'], item_to_delete.image_path)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except Exception:
            pass
            
    db.session.delete(item_to_delete)
    db.session.commit()
    return redirect(url_for('admin_index', tab='menu'))

@app.route('/admin/delete_user/<int:id>')
def delete_user(id):
    if not session.get('admin_logged_in'): 
        return redirect(url_for('admin_index'))
    user_to_delete = User.query.get_or_404(id)
    
    try:
        if os.path.exists(user_to_delete.photo_path):
            os.remove(user_to_delete.photo_path)
    except Exception:
        pass
        
    db.session.delete(user_to_delete)
    db.session.commit()
    return redirect(url_for('admin_index'))

@app.route('/')
def customer_index():
    user_name = session.get('user_name')
    is_member = False
    
    if user_name:
        user = User.query.filter_by(name=user_name).first()
        if not user:
            session.pop('user_name', None)
        else:
            is_member = True

    items = MenuItem.query.all()
    categories = sorted({item.category or '未分類' for item in items})
    
    return render_template('customer.html', 
                           items=items, 
                           categories=categories,
                           is_member=is_member)

@app.route('/logout')
def logout():
    session.pop('user_name', None)
    return redirect(url_for('customer_index'))

@app.route('/submit_order', methods=['POST'])
def submit_order():
    try:
        data = request.json or {}
        user_name = session.get('user_name') or data.get('table_number') or '一般顧客'
        
        new_order = Order(
            table_number=user_name,
            order_type=data.get('order_type', '內用'),
            total_price=int(data.get('total_price', 0)),
            payment_method=data.get('payment_method', 'Cash')
        )
        db.session.add(new_order)
        db.session.flush()

        order_items_detail = []
        for item in data.get('items', []):
            # 支援 'name' 或 'item_name' 兩種 key 名稱
            item_name = item.get('name') or item.get('item_name', '')
            item_price = int(item.get('price', 0))
            item_qty = int(item.get('quantity', 1))
            custom_text = item.get('customization', '')

            order_item = OrderItem(
                order_id=new_order.id,
                item_name=item_name,
                quantity=item_qty,
                price=item_price,
                customization=custom_text
            )
            db.session.add(order_item)
            
            # 扣減特價商品庫存
            db_item = MenuItem.query.filter_by(name=item_name).first()
            if db_item and db_item.is_special and db_item.special_stock > 0:
                db_item.special_stock = max(0, db_item.special_stock - item_qty)

            order_items_detail.append({
                'name': item_name,
                'quantity': item_qty,
                'price': item_price,
                'subtotal': item_price * item_qty,
                'customization': custom_text
            })

        db.session.commit()
        
        return jsonify({
            'message': '訂單已成功送出！',
            'order_id': new_order.id,
            'user_name': user_name,
            'order_type': new_order.order_type,
            'payment_method': new_order.payment_method,
            'total_price': new_order.total_price,
            'items': order_items_detail
        })

    except Exception as e:
        db.session.rollback()
        print(f"下單處理失敗: {e}")
        return jsonify({'message': f'訂單送出失敗：{str(e)}'}), 500

@app.route('/my_orders')
def my_orders():
    user_name = session.get('user_name')
    if not user_name:
        return jsonify({'error': '未登入'}), 401

    try:
        orders = Order.query.filter_by(table_number=user_name).order_by(Order.created_at.desc()).all()
        result = []
        for o in orders:
            items_detail = []
            for item in o.items:
                items_detail.append({
                    'name': item.item_name,
                    'quantity': item.quantity,
                    'price': item.price,
                    'subtotal': item.price * item.quantity,
                    'customization': item.customization
                })
                
            result.append({
                'order_id': o.id,
                'user_name': o.table_number,
                'order_type': getattr(o, 'order_type', '內用'),
                'payment_method': o.payment_method,
                'total_price': o.total_price,
                'status': o.status,
                'created_at': o.created_at.strftime('%Y-%m-%d %H:%M:%S') if o.created_at else '',
                'items': items_detail
            })
            
        return jsonify(result)
    except Exception as e:
        print(f"取得訂單記錄失敗: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        name = request.form.get('name', '')
        phone = request.form.get('phone', '')
        action = request.form.get('action')
        captured_photo = request.form.get('captured_photo', '')
        
        if not name or not phone:
            return "<h1>請填寫完整資料</h1><a href='/register'>返回</a>", 400

        if action == 'webcam':
            cap = cv2.VideoCapture(0)
            win_name = 'Auto Capture - Please look at the camera'
            cv2.namedWindow(win_name)
            
            temp_filename = f"temp_{phone}_{int(time.time())}.jpg"
            temp_filepath = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], temp_filename)
            success_capture = False

            while cap.isOpened():
                ret, frame = cap.read()
                if not ret: break
                display_frame = frame.copy()
                cv2.putText(display_frame, "Detecting face...", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)

                detector.setInputSize((frame.shape[1], frame.shape[0]))
                _, faces = detector.detect(frame)
                
                if faces is not None and len(faces) > 0:
                    face_align = recognizer.alignCrop(frame, faces[0])
                    feature = recognizer.feature(face_align)
                    if feature is not None:
                        cv2.imwrite(temp_filepath, frame)
                        success_capture = True
                        cv2.putText(display_frame, "Face Detected! Captured...", (10, 70), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                        cv2.imshow(win_name, display_frame)
                        cv2.waitKey(1000) 
                        break

                cv2.imshow(win_name, display_frame)
                if cv2.waitKey(1) & 0xFF == ord('q'): break
                if cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) < 1: break

            cap.release()
            cv2.destroyAllWindows()

            if not success_capture:
                return "<h1>相機已關閉或未偵測到人臉</h1><a href='/register'>返回重新註冊</a>", 400
            return render_template('register.html', name=name, phone=phone, captured_photo=temp_filename)

        elif action == 'register':
            photo = request.files.get('photo')
            source_path = ""
            
            if photo and photo.filename != '':
                source_path = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], f"temp_upload_{phone}.jpg")
                save_and_fix_image(photo, source_path)
            elif captured_photo:
                source_path = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], captured_photo)
                if not os.path.exists(source_path):
                    return "<h1>找不到拍攝的照片，請重新操作！</h1><a href='/register'>返回</a>", 400
            else:
                return "<h1>請上傳照片或使用相機拍攝！</h1><a href='/register'>返回</a>", 400

            feature = get_face_feature(source_path)
            if feature is None:
                if os.path.exists(source_path):
                    os.remove(source_path)
                return "<h1>照片中未偵測到人臉，請重新提供！</h1><a href='/register'>返回</a>", 400

            existing_users = User.query.order_by(User.id).all()
            available_id = 1
            for u in existing_users:
                if u.id == available_id: available_id += 1
                else: break

            new_user = User(id=available_id, name=name, phone=phone, photo_path="temp")
            db.session.add(new_user)
            db.session.flush() 

            final_filename = f"member_{new_user.id}_{phone}.jpg"
            final_filepath = os.path.join(app.config['UPLOAD_FOLDER_MEMBER'], final_filename)
            
            os.rename(source_path, final_filepath)
            new_user.photo_path = final_filepath
            db.session.commit()
            
            session['user_name'] = new_user.name
            return redirect(url_for('customer_index'))

    return render_template('register.html', name='', phone='', captured_photo='')

@app.route('/face_login')
def face_login():
    users = User.query.all()
    whitelist = []
    
    for u in users:
        feat = get_face_feature(u.photo_path)
        if feat is not None:
            whitelist.append({"name": u.name, "feature": feat})
            
    if not whitelist:
        return "<h1>系統中尚未有任何有效的會員特徵，請先註冊！</h1><a href='/register'>前往註冊</a>", 400

    cap = cv2.VideoCapture(0)
    win_name = 'Face Login - Press Q to Exit'
    cv2.namedWindow(win_name)
    recognized_user = None
    login_success = False

    while cap.isOpened():
        ret, frame = cap.read()
        if not ret: break
        detector.setInputSize((frame.shape[1], frame.shape[0]))
        _, faces = detector.detect(frame)

        if faces is not None:
            for face in faces:
                face_align = recognizer.alignCrop(frame, face)
                feature = recognizer.feature(face_align)
                
                best_score = 0
                best_name = "Unknown"
                
                for w_user in whitelist:
                    score = recognizer.match(w_user["feature"], feature, cv2.FaceRecognizerSF_FR_COSINE)
                    if score > best_score:
                        best_score = score
                        if score > 0.36:
                            best_name = w_user["name"]

                coords = face[:-1].astype(np.int32)
                if best_name != "Unknown":
                    recognized_user = best_name
                    login_success = True
                    color = (0, 255, 0)
                    cv2.rectangle(frame, (coords[0], coords[1]), (coords[0]+coords[2], coords[1]+coords[3]), color, 2)
                    cv2.putText(frame, f"{best_name} - Login Success!", (coords[0], coords[1]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
                    break 
                else:
                    color = (0, 0, 255)
                    cv2.rectangle(frame, (coords[0], coords[1]), (coords[0]+coords[2], coords[1]+coords[3]), color, 2)
                    cv2.putText(frame, f"Unknown: {best_score:.2f}", (coords[0], coords[1]-10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)

        cv2.imshow(win_name, frame)
        if login_success:
            cv2.waitKey(1500)
            break
        if cv2.waitKey(1) & 0xFF == ord('q'): break
        if cv2.getWindowProperty(win_name, cv2.WND_PROP_VISIBLE) < 1: break

    cap.release()
    cv2.destroyAllWindows()
    
    if recognized_user:
        session['user_name'] = recognized_user
        return redirect(url_for('customer_index'))
    else:
        return "<h1>未能辨識身份</h1><a href='/'>返回首頁</a>"

if __name__ == '__main__':
    app.run(host="0.0.0.0", port=5000, debug=True)