"""
上傳檔 (餐點相片、會員照片) 的路徑規則。

資料庫存的是相對路徑：餐點相片 "<店家 id>/menu_<隨機>.jpg" (舊版可能沒有店家資料夾)、會員照片 "member_<隨機>.jpg"。
這些欄位也會經由同步從其他主機傳來，因此刪除或下載檔案前一律檢查格式並限制在上傳資料夾內，
避免 "../../database/menu.db" 之類的路徑被拿來刪除或覆寫任意檔案。
"""
import os
import re

from werkzeug.utils import safe_join

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp')
_NAME = r'[A-Za-z0-9_-]{1,100}\.(?:jpg|jpeg|png|webp)'
MENU_IMAGE_RE = re.compile(rf'^(?:\d{{1,10}}/)?{_NAME}$', re.IGNORECASE)
MEMBER_PHOTO_RE = re.compile(rf'^{_NAME}$', re.IGNORECASE)


def is_valid_menu_image(rel_path):
    """空字串 (沒有圖片) 或 "<數字>/<檔名>.<圖片副檔名>" / "<檔名>.<圖片副檔名>" """
    return not rel_path or bool(MENU_IMAGE_RE.match(str(rel_path)))


def is_valid_member_photo(rel_path):
    return not rel_path or bool(MEMBER_PHOTO_RE.match(str(rel_path)))


def upload_file_path(base_dir, rel_path):
    """上傳資料夾內的實際路徑；路徑不合法或會跳出資料夾時回傳 None"""
    if not rel_path:
        return None
    return safe_join(base_dir, str(rel_path).replace('\\', '/'))


def remove_upload(base_dir, rel_path):
    """刪除上傳資料夾內的檔案 (路徑跳出資料夾時不動作)；回傳是否刪除"""
    path = upload_file_path(base_dir, rel_path)
    if not path or not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False
