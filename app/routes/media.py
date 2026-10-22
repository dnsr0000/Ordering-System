"""
上傳檔案 (餐點相片、會員人臉照片) 的讀取路由。

上傳資料夾不放在 static/ 底下 (預設為 uploads/menu、uploads/member，可用設定改到其他位置)：
- 餐點相片：公開讀取。
- 會員人臉照片：只有同店已登入員工 (廚房除外) 可讀，且檔名必須屬於本店會員 (多店家查詢會自動加上店家條件)。
"""
from flask import Blueprint, abort, current_app, send_from_directory
from flask_login import current_user

from app.extensions import db
from app.models.user import User
from app.services.upload_paths import is_valid_menu_image

media_bp = Blueprint('media', __name__, url_prefix='/media')


@media_bp.route('/menu/<path:filename>')
def menu_image(filename):
    if not is_valid_menu_image(filename):  # 只提供圖片 (避免其他類型的檔案被當成本站內容讀取)
        abort(404)
    return send_from_directory(current_app.config['UPLOAD_FOLDER_MENU'], filename, max_age=3600)


@media_bp.route('/member/<path:filename>')
def member_photo(filename):
    if not current_user.is_authenticated or current_user.role == 'kitchen':  # 廚房不需要看會員照片
        abort(403)
    owner = User.query.filter(db.func.lower(User.photo_path) == filename.lower()).first()
    if owner is None:
        abort(404)
    resp = send_from_directory(current_app.config['UPLOAD_FOLDER_MEMBER'], owner.photo_path, max_age=0)
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp
