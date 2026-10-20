import os
import threading
import cv2
import numpy as np
from PIL import Image, ImageOps
from app.config import Config

cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)

# 上傳圖片限制：註冊/刷臉是「免登入」端點，單張超大像素圖 (例如 2 萬 x 2 萬的壓縮炸彈) 解碼會吃掉數百 MB 記憶體
MAX_UPLOAD_IMAGE_PIXELS = 25_000_000
ALLOWED_UPLOAD_IMAGE_FORMATS = {'JPEG', 'PNG', 'WEBP', 'MPO'}  # MPO = 手機相機常見的 JPEG 變體
Image.MAX_IMAGE_PIXELS = MAX_UPLOAD_IMAGE_PIXELS

# 全域模型快取變數
_detector = None
_recognizer = None

# OpenCV 的 FaceDetectorYN / FaceRecognizerSF 為全域共用單例，且 detect 前必須 setInputSize (會改寫模型內部狀態)，
# 多個 Waitress 執行緒同時推論會互相覆寫輸入尺寸而崩潰，因此所有「建立模型 + 推論」一律在此鎖內序列化。
# 使用 RLock：同一執行緒在持鎖狀態下再呼叫 get_face_models() 不會自我死鎖。
CV_LOCK = threading.RLock()

def get_face_models():
    global _detector, _recognizer
    with CV_LOCK:
        if _recognizer is None and os.path.exists(Config.SFACE_MODEL):
            _recognizer = cv2.FaceRecognizerSF.create(Config.SFACE_MODEL, "")
        if _detector is None and os.path.exists(Config.YUNET_MODEL):
            _detector = cv2.FaceDetectorYN.create(Config.YUNET_MODEL, "", (320, 320), 0.6, 0.3, 5000)
        return _detector, _recognizer

def save_and_fix_image(file_storage, dest_path):
    try:
        file_storage.seek(0)
        img = Image.open(file_storage)
        # 解碼前先檢查格式與像素數 (只讀檔頭，不會真的解碼整張圖)
        if img.format not in ALLOWED_UPLOAD_IMAGE_FORMATS:
            raise ValueError(f"不支援的圖片格式: {img.format}")
        if img.width * img.height > MAX_UPLOAD_IMAGE_PIXELS:
            raise ValueError(f"圖片像素過大: {img.width}x{img.height}")
        img.verify()
        file_storage.seek(0)
        img = Image.open(file_storage)
        if img.format in ('JPEG', 'MPO'):
            img.draft('RGB', (1600, 1600))  # JPEG 可在解碼階段就縮小，大幅降低記憶體用量
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
        # setInputSize → detect → alignCrop → feature 必須在同一把鎖內完成 (中途不可被其他執行緒改寫輸入尺寸)
        with CV_LOCK:
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
    with CV_LOCK:
        return recognizer.match(feat1, feat2, cv2.FaceRecognizerSF_FR_COSINE)

def find_best_match_vectorized(target_feature, candidate_users, threshold=0.363):
    """
    多特徵矩陣向量化比對
    利用 NumPy 矩陣乘法一次性計算 target_feature 與所有使用者的 Cosine Similarity。
    """
    if target_feature is None or not candidate_users:
        return None, 0.0

    try:
        # 將目標特徵展平成一維 float32 陣列
        q = np.asarray(target_feature, dtype=np.float32).reshape(-1)
        if q.size == 0 or not np.all(np.isfinite(q)):
            return None, 0.0
        q_norm = np.linalg.norm(q)
        if q_norm == 0:
            return None, 0.0

        # 逐一驗證候選會員特徵：維度須與目標一致且數值為有限值。
        # 單一筆壞資料 (匯入的舊庫、損毀 blob) 只能被略過，不可讓整批矩陣堆疊失敗而使全店人臉登入失效。
        valid_users = []
        rows = []
        skipped = 0
        for u in candidate_users:
            raw_feat = getattr(u, 'feature', None)
            if raw_feat is None:
                continue
            try:
                vec = np.asarray(raw_feat, dtype=np.float32).reshape(-1)
            except Exception:
                skipped += 1
                continue
            if vec.shape != q.shape or not np.all(np.isfinite(vec)):
                skipped += 1
                continue
            valid_users.append(u)
            rows.append(vec)
        if skipped:
            print(f"[!] 人臉比對略過 {skipped} 筆維度不符或內容異常的會員特徵")
        if not rows:
            return None, 0.0

        # 堆疊所有候選會員的特徵形成矩陣 (N, D)
        matrix = np.vstack(rows)

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