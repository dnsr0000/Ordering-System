import os
import time
from datetime import datetime
from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify
from app.config import Config
from app.extensions import db
from app.models.user import User
from app.services.cv_service import save_and_fix_image, extract_feature, find_best_match_vectorized
from app.routes.common import start_customer_session, clear_customer_session, close_customer_session

auth_bp = Blueprint('auth', __name__)

@auth_bp.route('/register', methods=['GET', 'POST'], endpoint='register')
def register():
    if request.method == 'POST':
        name = request.form.get('name')
        phone = request.form.get('phone')
        photo = request.files.get('photo')
        if not name or not phone or not photo or photo.filename == '':
            return "<script>alert('請填寫完整資訊並拍攝/上傳照片！'); window.history.back();</script>", 400

        filename = f"{phone}_{int(time.time())}.jpg"
        filepath = os.path.join(Config.UPLOAD_FOLDER_MEMBER, filename)
        
        if not save_and_fix_image(photo, filepath):
            return "<script>alert('❌ 上傳的相片檔案已損壞或格式不支援，請上傳清晰的 JPG/PNG 圖片！'); window.history.back();</script>", 400

        feature = extract_feature(filepath)
        if feature is None:
            if os.path.exists(filepath):
                try: os.remove(filepath)
                except Exception: pass
            return "<script>alert('⚠️ 照片未偵測到清晰人臉！請正對鏡頭並重新拍攝。'); window.history.back();</script>", 400

        new_user = User(name=name, phone=phone, photo_path=filename, feature=feature, points=20, last_login_at=datetime.now())
        db.session.add(new_user)
        db.session.commit()

        session['user_id'] = new_user.id
        session['user_name'] = new_user.name
        session['user_points'] = new_user.points
        start_customer_session('會員', f"{new_user.name} ({new_user.phone})")
        return redirect(url_for('customer.customer_index'))

    return render_template('register.html', login_mode=False)

@auth_bp.route('/face_login', methods=['GET', 'POST'], endpoint='face_login')
def face_login():
    if request.method == 'POST':
        photo = request.files.get('photo')
        if not photo:
            return jsonify({'success': False, 'message': '未接收到拍攝照片'})

        temp_path = os.path.join(Config.UPLOAD_FOLDER_MEMBER, f"temp_{int(time.time())}.jpg")
        save_and_fix_image(photo, temp_path)
        curr_feature = extract_feature(temp_path)
        if os.path.exists(temp_path):
            try: os.remove(temp_path)
            except Exception: pass

        if curr_feature is None:
            return jsonify({'success': False, 'message': '未在畫面中偵測到清晰人臉，請正對鏡頭！'})

        # 只撈取具有特徵向量的會員資料，節省記憶體
        users = User.query.filter(User.feature.isnot(None)).all()
        matched_user, best_score = find_best_match_vectorized(curr_feature, users, threshold=0.363)

        if matched_user:
            matched_user.last_login_at = datetime.now()
            matched_user.last_logout_at = None
            db.session.commit()
            session['user_id'] = matched_user.id
            session['user_name'] = matched_user.name
            session['user_points'] = matched_user.points
            start_customer_session('會員', f"{matched_user.name} ({matched_user.phone})")
            return jsonify({'success': True, 'user_name': matched_user.name, 'points': matched_user.points})
        
        return jsonify({'success': False, 'message': '人臉比對未通過，請先註冊或重新對準鏡頭！'})

    return render_template('register.html', login_mode=True)

@auth_bp.route('/phone_login', methods=['POST'], endpoint='phone_login')
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
        return jsonify({'success': True, 'user_name': user.name, 'points': user.points})
    return jsonify({'success': False, 'message': '查無此號碼！'})

@auth_bp.route('/guest_login', endpoint='guest_login')
def guest_login():
    import random
    clear_customer_session()
    random_guest_id = f"訪客-{random.randint(1000, 9999)}"
    session['user_id'] = None
    session['user_name'] = random_guest_id
    session['is_guest'] = True
    session['user_points'] = 0
    start_customer_session('訪客', random_guest_id)
    return redirect(url_for('customer.customer_index'))

@auth_bp.route('/logout', endpoint='logout')
def logout():
    close_customer_session()
    clear_customer_session()
    return redirect(url_for('customer.customer_index'))