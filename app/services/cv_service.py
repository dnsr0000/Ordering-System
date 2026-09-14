import os
import cv2
from PIL import Image, ImageOps
from app.config import Config

cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)

#  全域模型快取變數
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
            aligned_face = recognizer.alignCrop(img, faces[0])
            return recognizer.feature(aligned_face)
    except Exception as e:
        print(f"[!] 人臉特徵分析過程異常: {e}")
    return None

def compare_faces(feat1, feat2):
    if feat1 is None or feat2 is None:
        return 0.0
    _, recognizer = get_face_models()
    if recognizer is None:
        return 0.0
    return recognizer.match(feat1, feat2, cv2.FaceRecognizerSF_FR_COSINE)