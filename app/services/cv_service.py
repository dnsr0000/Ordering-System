import os
import cv2
import numpy as np
from PIL import Image, ImageOps
from app.config import Config

cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)

# 全域模型快取變數
_detector = None
_recognizer = None

def get_face_models():
    global _detector, _recognizer
    if _recognizer is None and os.path.exists(Config.SFACE_MODEL):
        _recognizer = cv2.FaceRecognizerSF.create(Config.SFACE_MODEL, "")
    if _detector is None and os.path.exists(Config.YUNET_MODEL):
        _detector = cv2.FaceDetectorYN.create(Config.YUNET_MODEL, "", (320, 320), 0.6, 0.3, 5000)
    return _detector, _recognizer

def save_and_fix_image(file_storage, dest_path):
    try:
        file_storage.seek(0)
        img = Image.open(file_storage)
        img.verify()
        file_storage.seek(0)
        img = Image.open(file_storage)
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((800, 800))
        img.save(dest_path, "JPEG", quality=88)
        return True
    except Exception as e:
        print(f"[!] 圖片讀取/壓縮失敗: {e}")
        if os.path.exists(dest_path):
            try:
                os.remove(dest_path)
            except OSError:
                pass
        return False

def extract_feature(image_path):
    """
    偵測人臉並提取 SFace 特徵向量。
    若畫面有多張人臉，依 Bounding Box 面積 (w * h) 選取最大者
    """
    detector, recognizer = get_face_models()
    if detector is None or recognizer is None:
        return None
    try:
        img = cv2.imread(image_path)
        if img is None or img.size == 0 or img.shape[0] < 20 or img.shape[1] < 20:
            return None
        detector.setInputSize((img.shape[1], img.shape[0]))
        _, faces = detector.detect(img)
        if faces is not None and len(faces) > 0:
            # 依面積 找出最大人臉
            largest_face = max(faces, key=lambda f: float(f[2]) * float(f[3]))
            aligned_face = recognizer.alignCrop(img, largest_face)
            return recognizer.feature(aligned_face)
    except Exception as e:
        print(f"[!] 人臉特徵分析過程異常: {e}")
    return None

def compare_faces(feat1, feat2):
    """保留單筆比對作為基礎相容介面"""
    if feat1 is None or feat2 is None:
        return 0.0
    _, recognizer = get_face_models()
    if recognizer is None:
        return 0.0
    return recognizer.match(feat1, feat2, cv2.FaceRecognizerSF_FR_COSINE)

def find_best_match_vectorized(target_feature, candidate_users, threshold=0.363):
    """
    多特徵矩陣向量化比對
    利用 NumPy 矩陣乘法一次性計算 target_feature 與所有使用者的 Cosine Similarity。
    """
    if target_feature is None or not candidate_users:
        return None, 0.0

    valid_users = [u for u in candidate_users if u.feature is not None]
    if not valid_users:
        return None, 0.0

    try:
        # 將目標特徵展平成一維 float32 陣列
        q = np.asarray(target_feature, dtype=np.float32).reshape(-1)
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return None, 0.0

        # 堆疊所有候選會員的特徵形成矩陣 (N, D)
        matrix = np.array([np.asarray(u.feature, dtype=np.float32).reshape(-1) for u in valid_users])

        # 計算矩陣各 row 的 L2 Norm
        matrix_norms = np.linalg.norm(matrix, axis=1)

        # 避免除以 0
        denominators = matrix_norms * q_norm
        denominators[denominators == 0] = 1e-8

        # 批次點積並計算 Cosine 相似度 (N,)
        similarities = np.dot(matrix, q) / denominators

        best_idx = int(np.argmax(similarities))
        best_score = float(similarities[best_idx])

        if best_score >= threshold:
            return valid_users[best_idx], best_score
        return None, best_score

    except Exception as e:
        print(f"[!] 矩陣向量比對異常: {e}")
        return None, 0.0