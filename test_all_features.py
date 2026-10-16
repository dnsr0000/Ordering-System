
"""
智慧自助點餐、POS 收銀、人臉辨識與多租戶系統 — 全功能與全排列組合自動化驗證腳本
執行方式：
  python test_all_features.py
自訂壓測參數 (選填)：
  python test_all_features.py --browse-users 60 --sse-streams 8 --checkout-users 30 --stock 10

涵蓋 13 大測試模組、65 項細部測試與排列組合：
  • 模組 1 ：系統初始化、資料庫遷移 (舊 Pickle 轉型/重複電話合併/舊訂單補值) 與多租戶隔離 (5 項)
  • 模組 2 ：資安防線、CSRF 反向 403 攔截、RBAC 四級權限矩陣、路徑穿越與相片保護 (6 項)
  • 模組 3 ：菜單 CRUD、刪除路由、紅利商城獨立上下架與三種加購觸發規則 (3 項)
  • 模組 4 ：套餐組合 (Combo) 建立、編輯、刪除、配餐防重複與客製化自動校正 (2 項)
  • 模組 5 ：會員註冊、電腦視覺向量比對、舊會員無 PIN 403、PIN 連續 5 次錯誤鎖定與刪修 (5 項)
  • 模組 6 ：顧客端自助點餐 (/submit_order) 全排列組合、加料查表與付款/用餐白名單 (3 項)
  • 模組 7 ：優惠券 CRUD、紅利兌換憑證 (RewardRedemption)、重複退點攔截與複合折抵 (4 項)
  • 模組 8 ：櫃檯 POS 收銀系統 (/pos) 多筆候選查詢、設 PIN、優惠碼、實收不足與找零稽核 (5 項)
  • 模組 9 ：廚房 KDS、櫃檯核銷、訂單狀態機非法轉換攔截與取消全量回滾 (4 項)
  • 模組 10：營運分析、AI 三層韌性降級、Excel 10 工作表匯出 (含稽核表) 與智慧匯入 (3 項)
  • 模組 11：頻率限制 (Rate Limit 429) 與 SSE 即時串流連線配額 (10/25/60) 獨立驗證 (2 項)
  • 模組 12：多執行緒高併發搶購、點數防雙花、POS 併發結帳與真實 HTTP/SSE 壓力測試 (5 項)
  • 模組 13：補強覆蓋 — 未測路由 (/face_login、/api/verify_promo、/my_orders、/shop/<slug> 等)、免登入外洩、角色越權(含 owner/manager 回歸)、GET 刪除封鎖、跨店隔離掃描、匿名/CSRF 全路由掃描、限流與 SSE (18 項)
================================================================================
"""
import io
import os
import re
import sys
import time
import json
import uuid
import pickle
import shutil
import zipfile
import inspect
import tempfile
import statistics
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from sqlalchemy.exc import IntegrityError

os.environ["PYTHONIOENCODING"] = "utf-8"
# 注意：預設不關閉頻率限制，確保模組 5 與模組 11 能完整實測 PIN 鎖定與 HTTP 429 頻率限制
os.environ["DISABLE_RATE_LIMIT"] = "0"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import openpyxl
from PIL import Image
from werkzeug.security import generate_password_hash
# 1. 建立獨立測試資料庫與獨立暫存相片目錄，徹底防止污染實體 static 與 database
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEST_DB_DIR = tempfile.mkdtemp(prefix="kiosk_test_db_")
TEST_DB_PATH = os.path.join(TEST_DB_DIR, "menu.db")
TEST_UPLOAD_MEMBER_DIR = tempfile.mkdtemp(prefix="kiosk_test_member_")
TEST_UPLOAD_MENU_DIR = tempfile.mkdtemp(prefix="kiosk_test_menu_")

# 清除過去腳本意外殘留在實體 static/member 的歷史測試照片 (例如 0912345678 開頭的圖片)
REAL_STATIC_MEMBER = os.path.join(BASE_DIR, "static", "member")
if os.path.exists(REAL_STATIC_MEMBER):
    for f_name in os.listdir(REAL_STATIC_MEMBER):
        if f_name.startswith(("0912345678", "camera_snap_", "member_to_delete", "temp_", "privacy_test_face")):
            try:
                os.remove(os.path.join(REAL_STATIC_MEMBER, f_name))
            except Exception:
                pass

from app.config import Config

Config.TESTING = False
Config.DATABASE_DIR = TEST_DB_DIR
Config.SQLALCHEMY_DATABASE_URI = "sqlite:///" + TEST_DB_PATH
Config.UPLOAD_FOLDER_MEMBER = TEST_UPLOAD_MEMBER_DIR
Config.UPLOAD_FOLDER_MENU = TEST_UPLOAD_MENU_DIR
Config.ADMIN_USERNAME = "admin_test"
Config.ADMIN_PASSWORD = "AdminPass!2026"
Config.ADMIN_PASSWORD_HASH = generate_password_hash("AdminPass!2026")


class TestConfig(Config):
    TESTING = False
    DATABASE_DIR = TEST_DB_DIR
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + TEST_DB_PATH
    UPLOAD_FOLDER_MEMBER = TEST_UPLOAD_MEMBER_DIR
    UPLOAD_FOLDER_MENU = TEST_UPLOAD_MENU_DIR
    ADMIN_USERNAME = "admin_test"
    ADMIN_PASSWORD = "AdminPass!2026"
    ADMIN_PASSWORD_HASH = Config.ADMIN_PASSWORD_HASH


CSRF_META_RE = re.compile(r'<meta\s+name="csrf-token"\s+content="([^"]+)"')
CSRF_INPUT_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def extract_csrf(html_text):
    if not html_text:
        return ""
    m = CSRF_META_RE.search(html_text)
    if m:
        return m.group(1)
    m2 = CSRF_INPUT_RE.search(html_text)
    return m2.group(1) if m2 else ""


def make_test_jpeg_bytes(width=120, height=120, color=(210, 160, 130)):
    buf = io.BytesIO()
    img = Image.new("RGB", (width, height), color=color)
    img.save(buf, format="JPEG", quality=85)
    buf.seek(0)
    return buf


class TerminalTestRunner:
    """終端機測試進度與結果統計器"""

    def __init__(self):
        self.total = 0
        self.passed = 0
        self.failed = 0
        self.failures = []
        self.suite_stats = {}
        self.current_suite = "未分類"

    def start_suite(self, suite_title):
        self.current_suite = suite_title
        self.suite_stats[suite_title] = {"passed": 0, "failed": 0}
        print(f"\n┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓")
        print(f"┃ 📂 {suite_title:<72} ┃")
        print(f"┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛")

    def check(self, code, name, fn):
        self.total += 1
        t0 = time.perf_counter()
        try:
            detail = fn()
            ms = (time.perf_counter() - t0) * 1000.0
            self.passed += 1
            self.suite_stats[self.current_suite]["passed"] += 1
            suffix = f" -> {detail}" if detail else ""
            print(f"  [✅ 通過] {code:<6} {name:<50} ({ms:>6.1f} ms){suffix}")
            return True
        except Exception as e:
            ms = (time.perf_counter() - t0) * 1000.0
            self.failed += 1
            self.suite_stats[self.current_suite]["failed"] += 1
            err_msg = str(e) or e.__class__.__name__
            print(f"  [❌ 失敗] {code:<6} {name:<50} ({ms:>6.1f} ms) -> {err_msg}")
            self.failures.append((code, self.current_suite, name, err_msg))
            return False

    def print_summary(self, elapsed_sec):
        print("\n" + "═" * 86)
        print("📊 【全系統功能、資安、POS、資料遷移與高併發測試總結報告】")
        print("═" * 86)
        for s_name, st in self.suite_stats.items():
            s_tot = st["passed"] + st["failed"]
            status_icon = "✅ 全數通過" if st["failed"] == 0 else f"❌ 失敗 {st['failed']} 項"
            print(f"  • {s_name:<54} : {st['passed']:>2}/{s_tot:<2} ({status_icon})")
        print("─" * 86)
        rate = (self.passed / self.total * 100.0) if self.total else 0.0
        print(f"  總執行項目數 : {self.total} 項")
        print(f"  成功通過項目 : {self.passed} 項")
        print(f"  失敗異常項目 : {self.failed} 項")
        print(f"  整體通過率   : {rate:.1f}%  (總耗時: {elapsed_sec:.2f} 秒)")
        print("═" * 86)
        if self.failures:
            print("\n⚠️ 失敗項目明細：")
            for code, s_name, name, err in self.failures:
                print(f"  - [{code}] ({s_name}) {name}: {err}")
        else:
            print("\n🎉 恭喜！所有 13 大模組、65 項功能細節、資安防線與高併發壓測 100% 通過！")


def run_all_tests():
    t_global_start = time.perf_counter()
    runner = TerminalTestRunner()

    from app import create_app
    from app.extensions import db
    from app.models.tenant import Tenant, StaffAccount
    import app.models.user as user_mod
    try:
        from app.models.user import (
            User, AdminLog, StaffAuditLog, CustomerLog, RewardSetting, RewardRedemption
        )
    except ImportError as imp_err:
        print("\n" + "!" * 84)
        print("❌ [版本不同步提示] 偵測到您本地的 app/models/user.py 為舊版檔案 (缺少 StaffAuditLog / RewardRedemption)！")
        print("👉 原因：先前處理配色問題時的壓縮包未包含新版後端模型，請先將最新提供的 kiosk_project_fixed.zip")
        print("   解壓縮覆蓋至專案目錄 (c:\\Users\\a0929\\Desktop\\project\\)，再執行 python test_all_features.py！")
        print("!" * 84 + "\n")
        raise imp_err
    from app.models.menu import MenuItem, ComboOption, ModifierOption
    from app.models.order import Order, OrderItem
    from app.models.coupon import Coupon, UserCoupon
    from app.services.cv_service import find_best_match_vectorized
    import app.services.cv_service as cv_service_mod
    from app.services.migration_service import run_database_migrations
    import app.utils as utils_mod

    # 確保無論本地 app/utils.py 是否已手動加入 DISABLE_RATE_LIMIT 判斷，皆能支援環境變數動態開關
    if hasattr(utils_mod, "_check_rate_limit_redis_or_local"):
        _orig_rate_check = utils_mod._check_rate_limit_redis_or_local

        def _patched_rate_check(bucket_key, max_requests, window_seconds):
            if os.environ.get("DISABLE_RATE_LIMIT") == "1":
                return True, 0
            return _orig_rate_check(bucket_key, max_requests, window_seconds)

        utils_mod._check_rate_limit_redis_or_local = _patched_rate_check

    def clear_rate_buckets():
        if hasattr(utils_mod, "_LOCAL_RATE_BUCKETS"):
            utils_mod._LOCAL_RATE_BUCKETS.clear()

    app = create_app(TestConfig)

    def get_client_csrf(client, path="/"):
        resp = client.get(path)
        token = extract_csrf(resp.get_data(as_text=True))
        if not token:
            with client.session_transaction() as sess:
                token = sess.get("csrf_token") or sess.get("_csrf_token") or ""
        return token

    # ==========================================================================
    # 模組 1：系統初始化、資料庫遷移 (Migration) 與多租戶隔離基礎建設
    # ==========================================================================
    runner.start_suite("模組 1：系統初始化、資料庫遷移與多租戶隔離基礎建設")

    # 1.1 預設店家與負責人自動遷移建立
    def t1_1():
        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            assert t is not None, "預設店家 (default) 未建立"
            admin_acc = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(
                tenant_id=t.id, username="admin_test"
            ).first()
            assert admin_acc is not None and admin_acc.role == "owner", "預設管理員 (owner) 未建立"
            assert admin_acc.check_password("AdminPass!2026"), "預設管理員密碼驗證失敗"
            mods = ModifierOption.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t.id).all()
            assert len(mods) >= 2, f"預設加料選項未初始化: {len(mods)}"
            return f"店家={t.slug}, 負責人={admin_acc.username}, 預設加料={len(mods)}項"
    runner.check("1.1", "預設店家 (default)、負責人帳號與預設加料自動遷移建立", t1_1)

    # 1.2 舊版 Pickle 人臉特徵轉型遷移實測 + 惡意反序列化阻擋
    def t1_2():
        decode_fn = getattr(user_mod, "decode_feature_bytes_safely", None)
        assert decode_fn is not None, "app/models/user.py 缺少 decode_feature_bytes_safely"

        # (a) 先以原生 SQL 寫入舊版 pickle.dumps(np.ndarray) 二進位資料，再執行 run_database_migrations
        legacy_vec = np.linspace(0.1, 0.9, 128, dtype=np.float32)
        legacy_pickle_blob = pickle.dumps(legacy_vec, protocol=4)
        assert not legacy_pickle_blob.startswith(b"NPF32v1:"), "測試前置應為舊版 Pickle 格式"

        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            db.session.execute(
                db.text(
                    "INSERT INTO user (tenant_id, name, phone, photo_path, feature, points) "
                    "VALUES (:tid, :name, :phone, :photo, :feat, :pts)"
                ),
                {
                    "tid": t.id,
                    "name": "舊版Pickle測試員",
                    "phone": "0910000001",
                    "photo": "legacy_pkl.jpg",
                    "feat": legacy_pickle_blob,
                    "pts": 25,
                },
            )
            db.session.commit()

        # 執行資料庫遷移：應自動將舊版 Pickle 轉為 NPF32v1: 安全二進位格式
        run_database_migrations(app)

        with app.app_context():
            raw_row = db.session.execute(
                db.text("SELECT feature FROM user WHERE phone = '0910000001'")
            ).fetchone()
            assert raw_row is not None and raw_row[0] is not None, "找不到遷移後的會員特徵"
            raw_bytes = bytes(raw_row[0]) if not isinstance(raw_row[0], bytes) else raw_row[0]
            assert raw_bytes.startswith(b"NPF32v1:"), f"舊版 Pickle 未被遷移轉為 NPF32v1 格式: {raw_bytes[:12]}"

            u_orm = User.query.execution_options(skip_tenant_filter=True).filter_by(phone="0910000001").first()
            assert isinstance(u_orm.feature, np.ndarray) and u_orm.feature.shape == (128,), "ORM 讀取遷移後特徵失敗"
            assert np.allclose(u_orm.feature, legacy_vec, atol=1e-6), "遷移前後特徵向量數值不一致"

            db.session.delete(u_orm)
            db.session.commit()

        # (b) 驗證惡意 Pickle Payload (嘗試執行 eval) 被 RestrictedUnpickler 阻擋
        class _MaliciousPayload:
            def __reduce__(self):
                return (eval, ("'PWNED'",))

        evil_bytes = pickle.dumps(_MaliciousPayload())
        assert decode_fn(evil_bytes) is None, "惡意 Pickle 反序列化未被攔截！"
        return "舊版 Pickle 自動轉型 NPF32v1 成功 & 惡意 Payload 攔截通過"
    runner.check("1.2", "舊版 Pickle 人臉特徵自動轉型遷移實測與惡意反序列化阻擋", t1_2)

    # 1.3 歷史重複電話號碼自動合併 (保留高點數/歸戶訂單) + 複合唯一索引攔截
    def t1_3():
        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            # 暫時移除唯一索引並重建為無內嵌 UNIQUE 約束的舊版 user 表結構，以模擬歷史重複電話資料
            db.session.execute(db.text("DROP INDEX IF EXISTS ux_user_tenant_phone"))
            db.session.execute(db.text("""
                CREATE TABLE user_legacy_tmp (
                    id INTEGER NOT NULL PRIMARY KEY,
                    name VARCHAR(100) NOT NULL,
                    phone VARCHAR(50) NOT NULL,
                    pin_hash VARCHAR(255),
                    photo_path VARCHAR(200) NOT NULL,
                    feature BLOB,
                    points INTEGER,
                    last_login_at DATETIME,
                    last_logout_at DATETIME,
                    tenant_id INTEGER NOT NULL REFERENCES tenant(id)
                )
            """))
            db.session.execute(db.text("""
                INSERT INTO user_legacy_tmp (id, name, phone, pin_hash, photo_path, feature, points, last_login_at, last_logout_at, tenant_id)
                SELECT id, name, phone, pin_hash, photo_path, feature, points, last_login_at, last_logout_at, tenant_id FROM user
            """))
            db.session.execute(db.text("DROP TABLE user"))
            db.session.execute(db.text("ALTER TABLE user_legacy_tmp RENAME TO user"))
            db.session.commit()

            # 插入兩筆同店同電話 (0910000002) 的重複會員：一筆 20 點、一筆 95 點
            db.session.execute(
                db.text(
                    "INSERT INTO user (tenant_id, name, phone, photo_path, points) "
                    "VALUES (:tid, '重複舊帳A', '0910000002', 'dup_a.jpg', 20)"
                ),
                {"tid": t.id},
            )
            dup_a_id = db.session.execute(db.text("SELECT last_insert_rowid()")).scalar()

            db.session.execute(
                db.text(
                    "INSERT INTO user (tenant_id, name, phone, photo_path, points) "
                    "VALUES (:tid, '重複舊帳B_高點數', '0910000002', 'dup_b.jpg', 95)"
                ),
                {"tid": t.id},
            )

            # 在低點數舊帳 dup_a_id 名下掛載一張歷史訂單
            db.session.execute(
                db.text(
                    'INSERT INTO "order" (tenant_id, user_id, table_number, pickup_number, total_price, '
                    "payment_method, order_type, status) VALUES (:tid, :uid, '測試桌', 99, 180, 'Cash', '內用', 'Completed')"
                ),
                {"tid": t.id, "uid": dup_a_id},
            )
            mig_order_id = db.session.execute(db.text("SELECT last_insert_rowid()")).scalar()
            db.session.commit()

        # 執行遷移：應自動合併重複電話、保留最高點數 95 點、將訂單歸戶並重建唯一索引
        run_database_migrations(app)

        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            users_after = User.query.execution_options(skip_tenant_filter=True).filter_by(
                tenant_id=t.id, phone="0910000002"
            ).all()
            assert len(users_after) == 1, f"重複電話會員未被自動合併，剩餘 {len(users_after)} 筆"
            survivor = users_after[0]
            assert survivor.points == 95, f"合併後未保留最高點數 (期望 95，實際 {survivor.points})"

            ord_after = db.session.get(Order, mig_order_id)
            assert ord_after is not None and ord_after.user_id == survivor.id, "重複帳號的歷史訂單未自動歸戶至保留帳號"

            # 驗證唯一索引已重建：再次插入同店同電話必拋出 IntegrityError
            dup_blocked = False
            try:
                db.session.add(User(tenant_id=t.id, name="再次重複", phone="0910000002", photo_path="x.jpg", points=10))
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                dup_blocked = True
            assert dup_blocked, "重建後的 (tenant_id, phone) 唯一索引未攔截同店重複電話"

            db.session.delete(ord_after)
            db.session.delete(survivor)
            db.session.commit()
        return "2筆重複電話自動合併為1筆(保留95點 & 訂單歸戶) + 唯一索引攔截通過"
    runner.check("1.3", "歷史重複電話自動合併 (保留最高點數與訂單歸戶) 及唯一索引驗證", t1_3)

    # 1.4 歷史舊訂單欄位補值 (order_source='Kiosk', cash_received=total_price) 遷移驗證
    def t1_4():
        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            db.session.execute(
                db.text(
                    'INSERT INTO "order" (tenant_id, table_number, pickup_number, total_price, '
                    "payment_method, order_type, order_source, cash_received, cash_change, status) "
                    "VALUES (:tid, '舊單測試', 88, 320, 'Cash', '內用', '', 0, 0, 'Completed')"
                ),
                {"tid": t.id},
            )
            legacy_oid = db.session.execute(db.text("SELECT last_insert_rowid()")).scalar()
            db.session.commit()

        run_database_migrations(app)

        with app.app_context():
            ord_obj = db.session.get(Order, legacy_oid)
            assert ord_obj is not None, "找不到測試舊訂單"
            assert ord_obj.order_source == "Kiosk", f"舊訂單 order_source 未補值為 Kiosk: {ord_obj.order_source}"
            assert ord_obj.cash_received == 320, f"舊訂單 cash_received 未補值為 total_price(320): {ord_obj.cash_received}"
            db.session.delete(ord_obj)
            db.session.commit()
        return "歷史訂單 cash_received=0 自動回填為 total_price(320) & order_source='Kiosk'"
    runner.check("1.4", "歷史舊訂單新欄位補值 (cash_received=total_price & order_source) 遷移驗證", t1_4)

    # 1.5 公開註冊新店家 (/tenant/register) 與預設加料/紅利初始化
    def t1_5():
        c = app.test_client()
        csrf = get_client_csrf(c, "/tenant/register")
        resp = c.post(
            "/tenant/register",
            data={
                "shop_name": "星光二號店",
                "slug": "branch-two",
                "username": "owner_b2",
                "password": "Branch2Pass!2026",
                "csrf_token": csrf,
            },
        )
        assert resp.status_code == 302, f"註冊新店家失敗: {resp.status_code}"
        with app.app_context():
            t2 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="branch-two").first()
            assert t2 is not None, "新店家 branch-two 未寫入資料庫"
            mods_b2 = ModifierOption.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t2.id).all()
            rs_b2 = RewardSetting.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t2.id).first()
            assert len(mods_b2) >= 2 and rs_b2 is not None, "新註冊店家未自動初始化預設加料與紅利設定"
        return "新店家 branch-two 建立成功，自動初始化加料與紅利設定"
    runner.check("1.5", "公開註冊新店家 (/tenant/register) 與預設加料/紅利設定初始化", t1_5)

    # ==========================================================================
    # 模組 2：資安防線、CSRF 反向 403 攔截、RBAC 四級權限矩陣與系統備份保護
    # ==========================================================================
    runner.start_suite("模組 2：資安防線、CSRF 反向 403 攔截、RBAC 四級權限矩陣與相片保護")

    admin_client = app.test_client()
    manager_client = app.test_client()
    staff_client = app.test_client()
    kitchen_client = app.test_client()

    # 2.1 全域 CSRF 防護反向測試（不帶 Token 或帶錯 Token 必回 403）
    def t2_1():
        anon = app.test_client()
        anon.get("/?shop=default")
        # (a) 完全不帶 CSRF Token 發送 POST
        r_order_no_csrf = anon.post("/submit_order", json={"items": [{"id": 1, "name": "x", "quantity": 1}]})
        r_login_no_csrf = anon.post("/admin", data={"username": "admin_test", "password": "AdminPass!2026"})
        r_phone_no_csrf = anon.post("/phone_login", json={"phone": "0912345678", "pin": "1234"})
        r_pos_no_csrf = anon.post("/api/pos/checkout", json={"items": []})
        # (b) 帶偽造錯誤的 CSRF Token 發送 POST
        r_bad_csrf = anon.post(
            "/submit_order",
            json={"items": [{"id": 1, "name": "x", "quantity": 1}]},
            headers={"X-CSRFToken": "forged_invalid_token_999"},
        )
        codes = [
            r_order_no_csrf.status_code,
            r_login_no_csrf.status_code,
            r_phone_no_csrf.status_code,
            r_pos_no_csrf.status_code,
            r_bad_csrf.status_code,
        ]
        assert all(c == 403 for c in codes), f"缺 Token 或偽造 Token 未全數回傳 403: {codes}"
        return f"無 Token 與偽造 Token POST 請求 100% 攔截 (狀態碼={codes})"
    runner.check("2.1", "全域 CSRF 防護反向測試：不帶 Token 或帶偽造 Token 必回傳 403", t2_1)

    # 2.2 店家負責人後台登入、錯誤密碼攔截、登出 (/admin/logout) 與 AdminLog 記錄
    def t2_2():
        login_html = admin_client.get("/admin").get_data(as_text=True)
        assert 'name="csrf_token"' in login_html, "admin.html 未登入表單缺少 <input type=\"hidden\" name=\"csrf_token\">，瀏覽器直接登入會失敗！"
        csrf = extract_csrf(login_html)
        r_bad = admin_client.post(
            "/admin",
            data={"username": "admin_test", "password": "wrong_password", "csrf_token": csrf},
            headers={"X-Forwarded-For": "10.10.1.1"},
        )
        assert r_bad.status_code == 401, f"錯誤密碼未回傳 401: {r_bad.status_code}"

        r_ok = admin_client.post(
            "/admin",
            data={"username": "admin_test", "password": "AdminPass!2026", "csrf_token": csrf},
            headers={"X-Forwarded-For": "10.10.1.2"},
        )
        assert r_ok.status_code == 302, f"正確負責人帳密登入失敗: {r_ok.status_code}"

        # 測試登出 /admin/logout 會寫入 AdminLog.logout_at 並清除後台權限
        r_logout = admin_client.get("/admin/logout")
        assert r_logout.status_code == 302, f"/admin/logout 未正常跳轉: {r_logout.status_code}"
        with app.app_context():
            last_log = AdminLog.query.filter_by(username="admin_test").order_by(AdminLog.id.desc()).first()
            assert last_log is not None and last_log.logout_at is not None, "登出後 AdminLog.logout_at 未寫入時間"

        # 登出後重新登入供後續測試使用
        csrf2 = get_client_csrf(admin_client, "/admin")
        r_relogin = admin_client.post(
            "/admin",
            data={"username": "admin_test", "password": "AdminPass!2026", "csrf_token": csrf2},
            headers={"X-Forwarded-For": "10.10.1.3"},
        )
        assert r_relogin.status_code == 302, "負責人重新登入失敗"
        return "錯誤密碼攔截(401) -> 登入成功(302) -> 登出紀錄 logout_at(/admin/logout) 驗證通過"
    runner.check("2.2", "負責人 (owner) 後台登入、錯誤密碼攔截、/admin/logout 登出與日誌記錄", t2_2)

    # 2.3 員工帳號建立、停用/啟用切換、重設密碼、防自鎖保護與稽核紀錄
    def t2_3():
        csrf = get_client_csrf(admin_client, "/admin/staff")
        # 1. 短密碼 (<10碼) 攔截
        r_short = admin_client.post(
            "/admin/staff",
            data={"action": "create", "username": "short_u", "password": "123", "role": "staff", "csrf_token": csrf},
        )
        assert r_short.status_code == 400, f"短密碼未被攔截: {r_short.status_code}"

        # 2. 建立 manager / staff / kitchen 三種角色
        for uname, role in [("mgr_user", "manager"), ("pos_user", "staff"), ("kds_user", "kitchen")]:
            r = admin_client.post(
                "/admin/staff",
                data={"action": "create", "username": uname, "password": "StaffPass!2026", "role": role, "csrf_token": csrf},
            )
            assert r.status_code == 302, f"建立員工 {uname} 失敗: {r.status_code}"

        # 3. 重複帳號攔截 (409)
        r_dup = admin_client.post(
            "/admin/staff",
            data={"action": "create", "username": "pos_user", "password": "StaffPass!2026", "role": "staff", "csrf_token": csrf},
        )
        assert r_dup.status_code == 409, f"重複員工帳號未回 409: {r_dup.status_code}"

        with app.app_context():
            owner_acc = StaffAccount.query.filter_by(username="admin_test").first()
            pos_acc = StaffAccount.query.filter_by(username="pos_user").first()
            owner_id = owner_acc.id
            pos_id = pos_acc.id

        # 4. 防自鎖保護：owner 嘗試停用自己必回 400
        r_self_lock = admin_client.post(
            "/admin/staff",
            data={"action": "toggle_active", "account_id": owner_id, "csrf_token": csrf},
        )
        assert r_self_lock.status_code == 400, f"負責人停用自己未被攔截: {r_self_lock.status_code}"

        # 5. 停用 pos_user -> 驗證無法登入 (401) -> 再重新啟用
        r_disable = admin_client.post(
            "/admin/staff",
            data={"action": "toggle_active", "account_id": pos_id, "csrf_token": csrf},
        )
        assert r_disable.status_code == 302
        s_csrf = get_client_csrf(staff_client, "/admin")
        r_disabled_login = staff_client.post(
            "/admin",
            data={"username": "pos_user", "password": "StaffPass!2026", "csrf_token": s_csrf},
            headers={"X-Forwarded-For": "10.10.1.4"},
        )
        assert r_disabled_login.status_code == 401, "已停用員工仍可登入"

        r_enable = admin_client.post(
            "/admin/staff",
            data={"action": "toggle_active", "account_id": pos_id, "csrf_token": csrf},
        )
        assert r_enable.status_code == 302

        # 6. 重設密碼 (reset_password)
        r_reset = admin_client.post(
            "/admin/staff",
            data={"action": "reset_password", "account_id": pos_id, "new_password": "NewStaffPass!2026", "csrf_token": csrf},
        )
        assert r_reset.status_code == 200, f"重設員工密碼失敗: {r_reset.status_code}"

        with app.app_context():
            audits = {a.action_type for a in StaffAuditLog.query.all()}
            assert {"STAFF_CREATE", "STAFF_TOGGLE_ACTIVE", "STAFF_RESET_PASSWORD"}.issubset(audits), f"員工管理稽核缺失: {audits}"
        return "建立3角色、重複攔截(409)、防自鎖(400)、停用/啟用與重設密碼稽核通過"
    runner.check("2.3", "員工帳號建立、停用/啟用切換、重設密碼、防自鎖與 StaffAuditLog 驗證", t2_3)

    # 2.4 RBAC 四級角色權限矩陣與未授權存取 POS 攔截
    def t2_4():
        m_csrf = get_client_csrf(manager_client, "/admin")
        assert manager_client.post(
            "/admin",
            data={"username": "mgr_user", "password": "StaffPass!2026", "csrf_token": m_csrf},
            headers={"X-Forwarded-For": "10.10.2.1"},
        ).status_code == 302

        s_csrf = get_client_csrf(staff_client, "/admin")
        assert staff_client.post(
            "/admin",
            data={"username": "pos_user", "password": "NewStaffPass!2026", "csrf_token": s_csrf},
            headers={"X-Forwarded-For": "10.10.2.2"},
        ).status_code == 302

        k_csrf = get_client_csrf(kitchen_client, "/admin")
        assert kitchen_client.post(
            "/admin",
            data={"username": "kds_user", "password": "StaffPass!2026", "csrf_token": k_csrf},
            headers={"X-Forwarded-For": "10.10.2.3"},
        ).status_code == 302

        # (a) 未授權訪客存取 POS / 廚房 / 後台
        anon = app.test_client()
        anon_csrf = get_client_csrf(anon, "/")
        assert anon.get("/pos").status_code in (302, 401, 403), "未登入訪客可進入 /pos"
        assert anon.post("/api/pos/member_lookup", json={"query": "09"}, headers={"X-CSRFToken": anon_csrf}).status_code == 401
        assert anon.post("/api/pos/checkout", json={"items": []}, headers={"X-CSRFToken": anon_csrf}).status_code == 401
        assert anon.get("/api/kitchen_orders").status_code == 401

        # (b) kitchen 角色：可進入 /kitchen (200)，不可存取 /pos (403) 或後台修改菜單 (403)
        k_csrf_tok = get_client_csrf(kitchen_client, "/kitchen")
        assert kitchen_client.get("/kitchen").status_code == 200, "kitchen 無法進入 /kitchen"
        assert kitchen_client.get("/pos").status_code == 403, "kitchen 越權進入 /pos"
        assert kitchen_client.post("/api/pos/checkout", json={"items": []}, headers={"X-CSRFToken": k_csrf_tok}).status_code == 403
        assert kitchen_client.post("/admin/add", data={"name": "越權餐", "price": "100", "csrf_token": k_csrf_tok}).status_code == 403
        assert kitchen_client.get("/admin/staff").status_code == 403

        # (c) staff 角色：可進入 /pos (200) 與 /pickup (200)，不可新增/刪除菜單 (403) 或進入 /admin/staff (403)
        s_csrf_tok = get_client_csrf(staff_client, "/pos")
        assert staff_client.get("/pos").status_code == 200, "staff 無法進入 /pos"
        assert staff_client.get("/pickup").status_code == 200, "staff 無法進入 /pickup"
        assert staff_client.post("/admin/add", data={"name": "越權餐", "price": "100", "csrf_token": s_csrf_tok}).status_code == 403
        assert staff_client.post("/admin/delete/1", data={"csrf_token": s_csrf_tok}).status_code == 403
        assert staff_client.get("/admin/staff").status_code == 403

        # (d) manager 角色：可管理後台 (/admin 200) 與 /pos (200)，但不可存取 owner 專屬 /admin/staff (403)
        assert manager_client.get("/admin").status_code == 200
        assert manager_client.get("/pos").status_code == 200
        assert manager_client.get("/admin/staff").status_code == 403, "manager 越權進入 /admin/staff"
        return "訪客/kitchen/staff/manager 四級權限與 POS 存取邊界 100% 符合"
    runner.check("2.4", "RBAC 四級角色權限矩陣 (owner/manager/staff/kitchen) 與未授權 POS 攔截", t2_4)

    # 2.5 會員人臉照片 (/static/member/) 私密存取控制驗證
    def t2_5():
        # 確保寫入 Flask static_folder 對應的實體位置以供路由讀取
        static_member_dir = os.path.join(app.static_folder, "member")
        os.makedirs(static_member_dir, exist_ok=True)
        sample_path = os.path.join(static_member_dir, "privacy_test_face.jpg")

        try:
            with open(sample_path, "wb") as f:
                f.write(make_test_jpeg_bytes().read())

            anon = app.test_client()
            r_anon = anon.get("/static/member/privacy_test_face.jpg")
            assert r_anon.status_code == 403, f"未授權訪客可下載會員人臉照片: {r_anon.status_code}"

            r_staff = staff_client.get("/static/member/privacy_test_face.jpg")
            assert r_staff.status_code == 200, f"已登入員工無法讀取會員照片: {r_staff.status_code}"
            return "訪客讀取 /static/member/ 攔截(403) & 授權員工讀取(200) 通過"
        finally:
            # 測完立即物理刪除，絕不在磁碟留下任何殘留檔案
            if os.path.exists(sample_path):
                try:
                    os.remove(sample_path)
                except Exception:
                    pass
    runner.check("2.5", "會員人臉照片 (/static/member/) 私密存取控制驗證", t2_5)

    # 2.6 系統完整備份包匯出 (/admin/export_excel) 與還原防護 (/admin/restore_backup) 驗證
    def t2_6():
        csrf = get_client_csrf(admin_client, "/admin")
        
        # 1. 測試整合至 export_excel 的完整備份包匯出 (包含 menu.db 實體資料庫)
        r_bak = admin_client.post(
            "/admin/export_excel",
            data={"export_type": "backup_zip", "csrf_token": csrf},
            headers={"X-CSRFToken": csrf},
        )
        assert r_bak.status_code == 200, f"完整備份包匯出失敗: {r_bak.status_code}"
        assert r_bak.mimetype == "application/zip", f"備份包 MIME 類型錯誤: {r_bak.mimetype}"
        
        # 檢驗下載的 ZIP 壓縮包內確實包含 menu.db 實體資料庫
        with zipfile.ZipFile(io.BytesIO(r_bak.data), 'r') as zf:
            assert "menu.db" in zf.namelist(), "完整備份包 ZIP 缺少 menu.db 資料庫檔案"

        # 2. 測試未上傳實體檔案時，還原路由具備安全阻斷防護 (403)
        r_rest = admin_client.post(
            "/admin/restore_backup",
            data={"csrf_token": csrf},
            headers={"X-CSRFToken": csrf},
        )
        assert r_rest.status_code == 403, f"未選取檔案之還原請求未回傳 403: {r_rest.status_code}"
        return "export_excel(backup_zip 成功打包 menu.db) 與 restore_backup(安全阻斷 403) 驗證通過"
    runner.check("2.6", "系統完整備份包匯出 (含 menu.db) 與還原安全防護驗證", t2_6)

    # ==========================================================================
    # 模組 3：菜單 CRUD、刪除路由、紅利獨立上下架與三種加購觸發規則
    # ==========================================================================
    runner.start_suite("模組 3：菜單 CRUD、刪除路由、紅利獨立上下架與三種加購觸發規則")

    menu_ids = {}

    # 3.1 建立 5 道菜單品項與 3 種條件加購品
    def t3_1():
        csrf = get_client_csrf(admin_client, "/admin")
        items_to_create = [
            {
                "name": "招牌和牛堡", "category": "主餐", "price": "150", "stock": "30",
                "is_discount": "1", "discount_price": "120", "modifiers": "addons",
                "description": "頂級多汁和牛漢堡",
            },
            {
                "name": "錫蘭紅茶", "category": "飲料", "price": "40", "stock": "50",
                "modifiers": "ice_sugar", "can_be_add_on": "1", "add_on_price": "25",
                "addon_trigger_type": "any", "addon_trigger_target": "",
            },
            {
                "name": "美式脆薯", "category": "副餐", "price": "60", "stock": "40",
                "modifiers": "spicy", "can_be_add_on": "1", "add_on_price": "45",
                "addon_trigger_type": "category", "addon_trigger_target": "主餐",
            },
            {
                "name": "黃金雞塊", "category": "點心", "price": "70", "stock": "25",
                "modifiers": "none",
            },
        ]
        for payload in items_to_create:
            data = dict(payload)
            data["csrf_token"] = csrf
            r = admin_client.post("/admin/add", data=data)
            assert r.status_code == 302, f"新增餐點 {payload['name']} 失敗: {r.status_code}"

        with app.app_context():
            burger = MenuItem.query.filter_by(name="招牌和牛堡").first()
            assert burger is not None and burger.is_discount is True and burger.discount_price == 120, "招牌和牛堡特價設定失敗"
            burger_id = burger.id

        # 新增指定單品觸發 (item) 的加購品「松露沾醬」
        r_item_addon = admin_client.post(
            "/admin/add",
            data={
                "name": "松露沾醬", "category": "點心", "price": "35", "stock": "30",
                "modifiers": "none", "can_be_add_on": "1", "add_on_price": "20",
                "addon_trigger_type": "item", "addon_trigger_target": str(burger_id),
                "csrf_token": csrf,
            },
        )
        assert r_item_addon.status_code == 302

        with app.app_context():
            for it in MenuItem.query.all():
                menu_ids[it.name] = it.id
        assert len(menu_ids) >= 5, f"菜單建立數量不足: {menu_ids}"
        return f"成功建立 {len(menu_ids)} 道餐點 (含特價與 any/category/item 三種加購觸發)"
    runner.check("3.1", "建立主餐、飲料、副餐、點心與 3 種條件加購品 (any/category/item)", t3_1)

    # 3.2 菜單編輯 (/admin/edit/<id>)、刪除 (/admin/delete/<id>)、防呆與 DELETE_MENU_ITEM 稽核
    def t3_2():
        csrf = get_client_csrf(admin_client, "/admin")
        # (a) 加購價高於原價防呆：新增應回傳 400 且不寫入
        r_bad_addon = admin_client.post(
            "/admin/add",
            data={
                "name": "違法高價加購品", "category": "點心", "price": "50", "stock": "10",
                "can_be_add_on": "1", "add_on_price": "80", "csrf_token": csrf,
            },
        )
        assert r_bad_addon.status_code == 400
        with app.app_context():
            assert MenuItem.query.filter_by(name="違法高價加購品").first() is None, "加購價高於原價未被攔截"

        # (b) 建立臨時品項 -> 編輯 (/admin/edit/<id>) -> 刪除 (/admin/delete/<id>)
        admin_client.post(
            "/admin/add",
            data={"name": "季節限定濃湯", "category": "副餐", "price": "55", "stock": "15", "csrf_token": csrf},
        )
        with app.app_context():
            soup = MenuItem.query.filter_by(name="季節限定濃湯").first()
            assert soup is not None
            soup_id = soup.id

        r_edit = admin_client.post(
            f"/admin/edit/{soup_id}",
            data={
                "name": "主廚松露濃湯", "category": "副餐", "price": "65", "stock": "20",
                "modifiers": "none", "csrf_token": csrf,
            },
        )
        assert r_edit.status_code == 302
        with app.app_context():
            soup_after = db.session.get(MenuItem, soup_id)
            assert soup_after.name == "主廚松露濃湯" and soup_after.price == 65

        r_del = admin_client.post(f"/admin/delete/{soup_id}", data={"csrf_token": csrf})
        assert r_del.status_code == 302
        with app.app_context():
            assert db.session.get(MenuItem, soup_id) is None, "/admin/delete/<id> 未成功刪除餐點"
            del_audit = StaffAuditLog.query.filter_by(action_type="DELETE_MENU_ITEM").first()
            assert del_audit is not None and "主廚松露濃湯" in del_audit.details, "刪除餐點未寫入 DELETE_MENU_ITEM 稽核紀錄"
        return "加購防呆(400)、編輯(/admin/edit)、刪除(/admin/delete)與 DELETE_MENU_ITEM 稽核通過"
    runner.check("3.2", "菜單編輯 (/admin/edit)、刪除 (/admin/delete)、加購防呆與刪除稽核驗證", t3_2)

    # 3.3 紅利商城獨立上下架路由 (/admin/add_reward、/admin/remove_reward/<id>) 驗證
    def t3_3():
        csrf = get_client_csrf(admin_client, "/admin")
        fries_id = menu_ids["美式脆薯"]
        nugget_id = menu_ids["黃金雞塊"]
        burger_id = menu_ids["招牌和牛堡"]

        # 將黃金雞塊 (50點) 與招牌和牛堡 (100點/特惠80點) 上架至紅利商城供後續模組使用
        assert admin_client.post(
            "/admin/add_reward",
            data={"item_id": str(nugget_id), "reward_points": "50", "reward_discount_points": "0", "csrf_token": csrf},
        ).status_code == 302
        assert admin_client.post(
            "/admin/add_reward",
            data={"item_id": str(burger_id), "reward_points": "100", "reward_discount_points": "80", "csrf_token": csrf},
        ).status_code == 302

        # 測試將美式脆薯上架 (/admin/add_reward) 後再下架 (/admin/remove_reward/<id>)
        r_add_rew = admin_client.post(
            "/admin/add_reward",
            data={
                "item_id": str(fries_id),
                "reward_points": "45",
                "reward_discount_points": "35",
                "csrf_token": csrf,
            },
        )
        assert r_add_rew.status_code == 302
        with app.app_context():
            fries = db.session.get(MenuItem, fries_id)
            assert fries.is_reward is True and fries.reward_points == 45 and fries.reward_discount_points == 35

        r_rem_rew = admin_client.post(f"/admin/remove_reward/{fries_id}", data={"csrf_token": csrf})
        assert r_rem_rew.status_code == 302
        with app.app_context():
            fries = db.session.get(MenuItem, fries_id)
            assert fries.is_reward is False
        return "/admin/add_reward 上架 & /admin/remove_reward 下架驗證通過"
    runner.check("3.3", "紅利商城獨立上下架路由 (/admin/add_reward、/admin/remove_reward) 驗證", t3_3)

    # ==========================================================================
    # 模組 4：套餐組合 (Combo) 建立、編輯、刪除、配餐防重複與客製化自動校正
    # ==========================================================================
    runner.start_suite("模組 4：套餐組合 (Combo) 建立、編輯、刪除、配餐防重複與客製化自動校正")

    # 4.1 建立套餐與無客製化配餐旗標自動校正
    def t4_1():
        csrf = get_client_csrf(admin_client, "/admin")
        # 套餐 A：美式脆薯 (spicy) + 錫蘭紅茶 (ice_sugar) -> item1_customizable 應為 True
        r_combo_a = admin_client.post(
            "/admin/add_combo",
            data={
                "main_item_id": str(menu_ids["招牌和牛堡"]),
                "name": "A套餐(薯條+紅茶)",
                "additional_price": "75",
                "item1_id": str(menu_ids["美式脆薯"]),
                "item2_id": str(menu_ids["錫蘭紅茶"]),
                "item3_id": "",
                "item1_customizable": "1",
                "item2_customizable": "1",
                "can_addon": "1",
                "csrf_token": csrf,
            },
        )
        assert r_combo_a.status_code in (200, 302)

        # 套餐 B：僅配黃金雞塊 (modifiers='none')，即使前端傳 item1_customizable='1'，後端 check_side_customizable 必強制校正為 False
        r_combo_b = admin_client.post(
            "/admin/add_combo",
            data={
                "main_item_id": str(menu_ids["招牌和牛堡"]),
                "name": "B套餐(單配雞塊)",
                "additional_price": "55",
                "item1_id": str(menu_ids["黃金雞塊"]),
                "item2_id": "",
                "item3_id": "",
                "item1_customizable": "1",
                "can_addon": "0",
                "csrf_token": csrf,
            },
        )
        assert r_combo_b.status_code in (200, 302)

        with app.app_context():
            ca = ComboOption.query.filter_by(name="A套餐(薯條+紅茶)").first()
            cb = ComboOption.query.filter_by(name="B套餐(單配雞塊)").first()
            assert ca is not None and ca.item1_customizable is True and ca.can_addon is True
            assert cb is not None and cb.item1_customizable is False, "無客製化配餐之 item1_customizable 未被自動校正為 False"
        return "A套餐(item1_customizable=True) & B套餐自動校正(item1_customizable=False) 通過"
    runner.check("4.1", "建立雙配餐套餐與無客製化配餐旗標 (check_side_customizable) 自動校正", t4_1)

    # 4.2 套餐編輯 (/admin/edit_combo)、套餐刪除 (/admin/delete_combo) 與重複防呆攔截
    def t4_2():
        csrf = get_client_csrf(admin_client, "/admin")
        # (a) 同單重複配餐攔截 (item1 == item2)
        admin_client.post(
            "/admin/add_combo",
            data={
                "main_item_id": str(menu_ids["招牌和牛堡"]),
                "name": "重複配餐套餐",
                "additional_price": "60",
                "item1_id": str(menu_ids["錫蘭紅茶"]),
                "item2_id": str(menu_ids["錫蘭紅茶"]),
                "csrf_token": csrf,
            },
        )
        with app.app_context():
            assert ComboOption.query.filter_by(name="重複配餐套餐").first() is None, "同單重複配餐未被攔截"
            cb = ComboOption.query.filter_by(name="B套餐(單配雞塊)").first()
            cb_id = cb.id

        # (b) 編輯套餐 (/admin/edit_combo/<id>)：加入松露沾醬並調整價格為 68
        r_edit_cb = admin_client.post(
            f"/admin/edit_combo/{cb_id}",
            data={
                "main_item_id": str(menu_ids["招牌和牛堡"]),
                "name": "B套餐(雞塊+松露醬)",
                "additional_price": "68",
                "item1_id": str(menu_ids["黃金雞塊"]),
                "item2_id": str(menu_ids["松露沾醬"]),
                "item3_id": "",
                "csrf_token": csrf,
            },
        )
        assert r_edit_cb.status_code in (200, 302)
        with app.app_context():
            cb_updated = db.session.get(ComboOption, cb_id)
            assert cb_updated.name == "B套餐(雞塊+松露醬)" and cb_updated.additional_price == 68

        # (c) 建立臨時套餐並透過 /admin/delete_combo/<id> 刪除
        admin_client.post(
            "/admin/add_combo",
            data={
                "main_item_id": str(menu_ids["招牌和牛堡"]),
                "name": "臨時待刪套餐",
                "additional_price": "20",
                "item1_id": str(menu_ids["松露沾醬"]),
                "csrf_token": csrf,
            },
        )
        with app.app_context():
            tmp_cb = ComboOption.query.filter_by(name="臨時待刪套餐").first()
            assert tmp_cb is not None
            tmp_cb_id = tmp_cb.id

        r_del_cb = admin_client.post(f"/admin/delete_combo/{tmp_cb_id}", data={"csrf_token": csrf})
        assert r_del_cb.status_code == 302
        with app.app_context():
            assert db.session.get(ComboOption, tmp_cb_id) is None, "/admin/delete_combo 未刪除套餐"
        return "重複配餐攔截、/admin/edit_combo 編輯與 /admin/delete_combo 刪除通過"
    runner.check("4.2", "套餐防呆、編輯 (/admin/edit_combo) 與刪除 (/admin/delete_combo) 驗證", t4_2)

    # ==========================================================================
    # 模組 5：會員註冊、電腦視覺向量比對、舊會員無 PIN 防護、PIN 鎖定與刪修
    # ==========================================================================
    runner.start_suite("模組 5：會員註冊、電腦視覺向量比對、PIN 碼防暴力鎖定與會員刪修")

    # 提供確定性特徵向量提取 Mock (同時綁定 extract_feature 與 get_face_embedding)
    _orig_extract_feature = getattr(cv_service_mod, "extract_feature", None)
    _mock_seed = {"val": 1}

    def _deterministic_face_embedding(img_path):
        v = np.zeros(128, dtype=np.float32)
        idx = _mock_seed["val"] % 128
        v[idx] = 1.0
        _mock_seed["val"] += 1
        return v

    cv_service_mod.extract_feature = _deterministic_face_embedding
    cv_service_mod.get_face_embedding = _deterministic_face_embedding
    import app.routes.auth as auth_route_mod
    auth_route_mod.extract_feature = _deterministic_face_embedding
    auth_route_mod.get_face_embedding = _deterministic_face_embedding

    # 5.1 會員註冊、路徑穿越檔名清洗、初始贈點 (20點) 與顧客登出 (/logout)
    def t5_1():
        c = app.test_client()
        c.get("/?shop=default")
        csrf = get_client_csrf(c, "/register")

        # 刻意傳入帶有路徑穿越字元的上傳檔名 "../../evil_traversal.jpg"
        r_reg = c.post(
            "/register",
            data={
                "name": "陳大文",
                "phone": "0912345678",
                "pin": "2468",
                "csrf_token": csrf,
                "photo": (make_test_jpeg_bytes(), "../../evil_traversal.jpg"),
            },
            content_type="multipart/form-data",
            headers={"X-Forwarded-For": "10.20.1.1"},
        )
        assert r_reg.status_code in (200, 302), f"會員註冊失敗: {r_reg.status_code}"

        with app.app_context():
            u = User.query.filter_by(phone="0912345678").first()
            assert u is not None and u.points == 20, "註冊初始贈點應為 20 點"
            assert ".." not in u.photo_path and "/" not in os.path.basename(u.photo_path), f"檔名路徑穿越未清洗: {u.photo_path}"
            assert re.match(r"^member_[0-9a-f]{16}\.jpg$", os.path.basename(u.photo_path)), f"檔名非隨機 UUID: {u.photo_path}"

        # 測試顧客登出 (/logout)
        r_out = c.get("/logout")
        assert r_out.status_code == 302
        with c.session_transaction() as sess:
            assert sess.get("user_id") is None, "/logout 未清除顧客 session['user_id']"
        return f"註冊贈20點、路徑穿越清洗為 {os.path.basename(u.photo_path)} & /logout 登出通過"
    runner.check("5.1", "會員註冊、路徑穿越檔名清洗 (UUID)、初始贈點 (20點) 與 /logout 登出", t5_1)

    # 5.2 電腦視覺矩陣餘弦相似度比對與跨店隔離驗證
    def t5_2():
        with app.app_context():
            u = User.query.filter_by(phone="0912345678").first()
            target_vec = u.feature.copy()
            matched_u, score = find_best_match_vectorized(target_vec, [u], threshold=0.5)
            assert matched_u is not None and matched_u.id == u.id and score > 0.99, "向量比對未能命中本人"

            orthogonal_vec = np.zeros(128, dtype=np.float32)
            orthogonal_vec[99] = 1.0
            miss_u, _ = find_best_match_vectorized(orthogonal_vec, [u], threshold=0.5)
            assert miss_u is None, "不相似向量不應通過門檻"
        return f"本人特徵命中率={score:.4f} & 異質向量精準排除"
    runner.check("5.2", "電腦視覺 find_best_match_vectorized 矩陣餘弦相似度比對驗證", t5_2)

    # 5.3 舊會員未設 PIN 碼登入攔截 (403) 與防電話列舉 (統一 401 回應)
    def t5_3():
        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            legacy_u = User(
                tenant_id=t.id,
                name="陳小美",
                phone="0912999888",
                pin_hash=None,  # 模擬舊會員尚未設定 PIN 碼
                photo_path="member_legacy.jpg",
                points=150,
            )
            db.session.add(legacy_u)
            db.session.commit()

        c = app.test_client()
        c.get("/?shop=default")
        csrf = get_client_csrf(c, "/register")

        # (a) 舊會員無 PIN 碼嘗試手機登入 -> 應回傳 403
        r_legacy = c.post(
            "/phone_login",
            json={"phone": "0912999888", "pin": "1234"},
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.20.2.1"},
        )
        assert r_legacy.status_code == 403, f"舊會員無 PIN 碼未回傳 403: {r_legacy.status_code}"

        # (b) 查無此電話 vs 已設 PIN 會員密碼錯誤 -> 皆應回傳 401
        r_no_phone = c.post(
            "/phone_login",
            json={"phone": "0988777666", "pin": "1234"},
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.20.2.2"},
        )
        r_wrong_pin = c.post(
            "/phone_login",
            json={"phone": "0912345678", "pin": "9999"},
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.20.2.3"},
        )
        assert r_no_phone.status_code == 401 and r_wrong_pin.status_code == 401
        return "無 PIN 舊會員回 403 & 空號/錯密碼統一回 401 驗證通過"
    runner.check("5.3", "舊會員無 PIN 碼登入攔截 (403) 與防電話列舉 (統一 401) 驗證", t5_3)

    # 5.4 單一帳號/IP 連續輸錯 5 次 PIN 觸發 15 分鐘鎖定 (429)
    def t5_4():
        c = app.test_client()
        c.get("/?shop=default")
        csrf = get_client_csrf(c, "/register")
        attacker_ip = "10.20.3.99"

        statuses = []
        for _ in range(5):
            r = c.post(
                "/phone_login",
                json={"phone": "0912345678", "pin": "0000"},
                headers={"X-CSRFToken": csrf, "X-Forwarded-For": attacker_ip},
            )
            statuses.append(r.status_code)

        # 第 6 次即使輸入正確 PIN (2468)，在鎖定期間內也必須回傳 429
        r_locked = c.post(
            "/phone_login",
            json={"phone": "0912345678", "pin": "2468"},
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": attacker_ip},
        )
        statuses.append(r_locked.status_code)
        assert statuses[:5] == [401, 401, 401, 401, 401] and statuses[5] == 429, f"PIN 連續 5 次錯誤鎖定異常: {statuses}"
        return f"連續5次錯誤(401)後第6次觸發鎖定({statuses[5]}) 驗證通過"
    runner.check("5.4", "連續 5 次 PIN 錯誤觸發 15 分鐘防暴力破解鎖定 (429) 驗證", t5_4)

    # 5.5 後台編輯會員 (/admin/edit_user/<id>)、刪除會員 (/admin/delete_user/<id>) 與相片檔案物理清除驗證
    def t5_5():
        csrf = get_client_csrf(admin_client, "/admin")
        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            u_chen = User.query.filter_by(phone="0912345678").first()
            chen_id = u_chen.id

            # 建立帶有實體照片檔案的待刪測試會員
            temp_filename = f"member_del_verify_{uuid.uuid4().hex[:8]}.jpg"
            temp_photo_path = os.path.join(Config.UPLOAD_FOLDER_MEMBER, temp_filename)
            with open(temp_photo_path, "wb") as f:
                f.write(make_test_jpeg_bytes().read())
            assert os.path.exists(temp_photo_path), "待刪測試相片未成功寫入磁碟"

            u_temp = User(tenant_id=t.id, name="待刪會員", phone="0955111222", photo_path=temp_filename, points=30)
            db.session.add(u_temp)
            db.session.commit()
            temp_uid = u_temp.id

            # 建立關聯訂單以驗證刪除會員時訂單 user_id 自動解綁 (改為 '待刪會員 (已註銷會員)')
            db.session.execute(
                db.text('INSERT INTO "order" (tenant_id, user_id, table_number, total_price, status) VALUES (:tid, :uid, :tbl, 100, "Pending")'),
                {"tid": t.id, "uid": temp_uid, "tbl": "A1"}
            )
            linked_order_id = db.session.execute(db.text("SELECT last_insert_rowid()")).scalar()
            db.session.commit()

        # 1. 測試編輯陳大文點數為 300 點並重設 PIN 為 1357
        r_edit_u = admin_client.post(
            f"/admin/edit_user/{chen_id}",
            data={"name": "陳大文", "phone": "0912345678", "points": "300", "pin": "1357", "csrf_token": csrf},
        )
        assert r_edit_u.status_code == 302
        with app.app_context():
            u_after = db.session.get(User, chen_id)
            assert u_after.points == 300 and u_after.check_pin("1357")
            edit_au = StaffAuditLog.query.filter_by(action_type="EDIT_USER", target_user_id=chen_id).first()
            assert edit_au is not None and edit_au.points_delta == 280, "編輯會員點數未寫入 EDIT_USER 稽核紀錄"

        # 2. 測試刪除會員 (/admin/delete_user/<id>)
        r_del_u = admin_client.post(f"/admin/delete_user/{temp_uid}", data={"csrf_token": csrf})
        assert r_del_u.status_code == 302
        with app.app_context():
            # (a) 驗證會員資料已從資料庫徹底刪除
            assert db.session.get(User, temp_uid) is None, "/admin/delete_user 未刪除會員資料庫記錄"

            # (b) 驗證實體照片檔案確實被物理刪除，無孤兒檔案殘留
            assert not os.path.exists(temp_photo_path), "刪除會員後，實體相片檔案未被物理刪除！"

            # (c) 驗證關聯訂單之 user_id 已解綁為 None 且桌號標註為已註銷會員
            linked_ord = db.session.get(Order, linked_order_id)
            assert linked_ord.user_id is None and "已註銷會員" in linked_ord.table_number, "刪除會員未解除關聯訂單 user_id"

            # (d) 驗證寫入 DELETE_USER 稽核紀錄
            del_au = StaffAuditLog.query.filter_by(action_type="DELETE_USER", target_user_id=temp_uid).first()
            assert del_au is not None and del_au.points_delta == -30, "刪除會員未寫入 DELETE_USER 稽核紀錄"

            # 清理該筆臨時訂單
            db.session.delete(linked_ord)
            db.session.commit()

        return "編輯會員點數/重設PIN & /admin/delete_user 同步物理刪除相片、解綁訂單與稽核驗證通過"
    runner.check("5.5", "後台編輯會員 (/admin/edit_user)、刪除會員 (/admin/delete_user) 與相片檔案物理清除驗證", t5_5)

    # ==========================================================================
    # 模組 6：顧客端自助點餐結帳 (/submit_order) 全排列組合與白名單驗證
    # ==========================================================================
    runner.start_suite("模組 6：顧客端自助點餐結帳 (/submit_order) 全排列組合與白名單驗證")

    # 6.1 排列組合 1：訪客單點特價主餐 + 多項加料查表計價 + 偽造 (+0) 標籤清洗
    def t6_1():
        c = app.test_client()
        c.get("/?shop=default")
        c.get("/guest_login")
        csrf = get_client_csrf(c, "/")

        with app.app_context():
            mods = ModifierOption.query.filter_by(is_active=True).order_by(ModifierOption.id.asc()).all()
            mod_ids = [m.id for m in mods[:2]]
            expected_mod_sum = sum(m.price for m in mods[:2])

        # 招牌和牛堡特價 120 + 兩項加料 (10+15=25) = 145 元；前端惡意傳入 price=1 及偽造 "加荷包蛋(+0)、不要洋蔥(+999)"
        r = c.post(
            "/submit_order",
            json={
                "items": [
                    {
                        "id": menu_ids["招牌和牛堡"],
                        "name": "招牌和牛堡",
                        "price": 1,
                        "quantity": 2,
                        "modifier_ids": mod_ids,
                        "customization": "加荷包蛋(+0)、不要洋蔥(+999)",
                        "is_add_on": False,
                    }
                ],
                "payment_method": "Cash",
                "order_type": "外帶",
                "need_cutlery": True,
                "note": "訪客單點測試",
            },
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.30.1.1"},
        )
        assert r.status_code == 200, f"訪客單點結帳失敗: {r.get_data(as_text=True)}"
        data = r.get_json()
        expected_total = (120 + expected_mod_sum) * 2
        assert data["total_price"] == expected_total, f"後端查表計價錯誤: 期望 {expected_total}, 實際 {data['total_price']}"
        cust_str = data["items"][0]["customization"]
        assert "(+0)" not in cust_str and "(+999)" not in cust_str and "不要洋蔥" in cust_str, f"偽造標籤未清洗: {cust_str}"
        return f"後端防竄改計價=${expected_total} & 客製化清洗='{cust_str}'"
    runner.check("6.1", "排列組合 1：訪客單點特價主餐 + 多項加料查表計價 + 偽造標籤清洗", t6_1)

    # 6.2 排列組合 2：套餐 (Combo) + 配餐客製化 + 3 種條件加購品 + 主餐與配餐庫存同步扣減
    def t6_2():
        c = app.test_client()
        c.get("/?shop=default")
        c.get("/guest_login")
        csrf = get_client_csrf(c, "/")

        with app.app_context():
            b_before = db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock
            f_before = db.session.get(MenuItem, menu_ids["美式脆薯"]).stock
            t_before = db.session.get(MenuItem, menu_ids["錫蘭紅茶"]).stock
            s_before = db.session.get(MenuItem, menu_ids["松露沾醬"]).stock

        # 招牌和牛堡(特價120) + A套餐(薯條+紅茶, +75) + 加購松露沾醬(加購價20) = 215
        r = c.post(
            "/submit_order",
            json={
                "items": [
                    {
                        "id": menu_ids["招牌和牛堡"],
                        "name": "招牌和牛堡",
                        "price": 195,
                        "quantity": 1,
                        "customization": "[套餐: A套餐(薯條+紅茶)]、美式脆薯(小辣)、錫蘭紅茶(少冰、半糖)",
                        "is_add_on": False,
                    },
                    {
                        "id": menu_ids["松露沾醬"],
                        "name": "松露沾醬",
                        "price": 20,
                        "quantity": 1,
                        "customization": "",
                        "is_add_on": True,
                    },
                ],
                "payment_method": "Line Pay",
                "order_type": "內用",
            },
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.30.1.2"},
        )
        assert r.status_code == 200, f"套餐與加購結帳失敗: {r.get_data(as_text=True)}"
        data = r.get_json()
        assert data["total_price"] == 215, f"套餐+加購總價應為 215，實際 {data['total_price']}"

        with app.app_context():
            assert db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock == b_before - 1, "主餐庫存未扣減"
            assert db.session.get(MenuItem, menu_ids["美式脆薯"]).stock == f_before - 1, "套餐配餐1(脆薯)庫存未同步扣減"
            assert db.session.get(MenuItem, menu_ids["錫蘭紅茶"]).stock == t_before - 1, "套餐配餐2(紅茶)庫存未同步扣減"
            assert db.session.get(MenuItem, menu_ids["松露沾醬"]).stock == s_before - 1, "加購品庫存未扣減"
        return "套餐+配餐客製化+加購計價($215) & 主餐/雙配餐/加購品庫存同步扣減通過"
    runner.check("6.2", "排列組合 2：套餐 + 配餐客製化 + 加購品 + 主餐與配餐庫存同步扣減", t6_2)

    # 6.3 payment_method 與 order_type 白名單防禦驗證
    def t6_3():
        c = app.test_client()
        c.get("/?shop=default")
        c.get("/guest_login")
        csrf = get_client_csrf(c, "/")

        r_bad_pay = c.post(
            "/submit_order",
            json={
                "items": [{"id": menu_ids["錫蘭紅茶"], "name": "錫蘭紅茶", "quantity": 1}],
                "payment_method": "Bitcoin_Hack",
                "order_type": "內用",
            },
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.30.1.3"},
        )
        r_bad_type = c.post(
            "/submit_order",
            json={
                "items": [{"id": menu_ids["錫蘭紅茶"], "name": "錫蘭紅茶", "quantity": 1}],
                "payment_method": "Cash",
                "order_type": "無人機空投",
            },
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.30.1.4"},
        )
        assert r_bad_pay.status_code == 400 and r_bad_type.status_code == 400, (
            f"非法付款或用餐方式未被白名單攔截: pay={r_bad_pay.status_code}, type={r_bad_type.status_code}"
        )
        return "非法 payment_method(400) 與非法 order_type(400) 白名單攔截通過"
    runner.check("6.3", "payment_method 與 order_type 白名單防禦驗證 (非法字串必回 400)", t6_3)

    # ==========================================================================
    # 模組 7：優惠券 CRUD、紅利兌換憑證 (RewardRedemption)、重複退點攔截與複合折抵
    # ==========================================================================
    runner.start_suite("模組 7：優惠券 CRUD、紅利兌換憑證、重複退點攔截與複合折抵結帳")

    # 7.1 優惠券建立 (/admin/add_coupon)、編輯 (/admin/edit_coupon)、刪除 (/admin/delete_coupon)
    def t7_1():
        csrf = get_client_csrf(admin_client, "/admin")
        # (a) 建立公開促銷碼 (reward_points=0 -> is_reward=False)
        admin_client.post(
            "/admin/add_coupon",
            data={
                "title": "全館滿百折20公開碼", "code": "OPEN20", "discount_type": "fixed",
                "discount_value": "20", "min_spend": "100", "reward_points": "0",
                "per_user_limit": "", "csrf_token": csrf,
            },
        )
        # (b) 建立會員紅利專屬 85 折券 (輸入 85 自動正規化為 0.85，每人限領 1 張)
        admin_client.post(
            "/admin/add_coupon",
            data={
                "title": "VIP 85折神券", "code": "VIP85", "discount_type": "percent",
                "discount_value": "85", "min_spend": "100", "reward_points": "30",
                "reward_discount_points": "20", "per_user_limit": "1", "csrf_token": csrf,
            },
        )
        # (c) 建立臨時優惠券 -> 編輯 (/admin/edit_coupon/<id>) -> 刪除 (/admin/delete_coupon/<id>)
        admin_client.post(
            "/admin/add_coupon",
            data={
                "title": "臨時測試券", "code": "TMP10", "discount_type": "fixed",
                "discount_value": "10", "min_spend": "50", "reward_points": "10", "csrf_token": csrf,
            },
        )
        with app.app_context():
            c_open = Coupon.query.filter_by(code="OPEN20").first()
            c_vip = Coupon.query.filter_by(code="VIP85").first()
            c_tmp = Coupon.query.filter_by(code="TMP10").first()
            assert c_open is not None and c_open.is_reward is False, "0點優惠券應自動設為公開碼(is_reward=False)"
            assert c_vip is not None and c_vip.is_reward is True and abs(c_vip.discount_value - 0.85) < 1e-6, "85折未正規化為0.85"
            tmp_id = c_tmp.id

        r_edit_cp = admin_client.post(
            f"/admin/edit_coupon/{tmp_id}",
            data={
                "title": "已編輯測試券", "code": "TMP15", "discount_type": "fixed",
                "discount_value": "15", "min_spend": "80", "reward_points": "15",
                "reward_discount_points": "0", "per_user_limit": "2", "csrf_token": csrf,
            },
        )
        assert r_edit_cp.status_code == 302
        with app.app_context():
            c_tmp_after = db.session.get(Coupon, tmp_id)
            assert c_tmp_after.code == "TMP15" and c_tmp_after.discount_value == 15

        r_del_cp = admin_client.post(f"/admin/delete_coupon/{tmp_id}", data={"csrf_token": csrf})
        assert r_del_cp.status_code == 302
        with app.app_context():
            assert db.session.get(Coupon, tmp_id) is None, "/admin/delete_coupon 未刪除優惠券"
        return "公開碼(OPEN20)、紅利券85折正規化(0.85)、/admin/edit_coupon 與 /admin/delete_coupon 通過"
    runner.check("7.1", "優惠券建立、折數正規化、編輯 (/admin/edit_coupon) 與刪除 (/admin/delete_coupon)", t7_1)

    member_client = app.test_client()

    # 7.2 紅利兌換優惠券 (/api/redeem_reward) 與每人限領上限 (per_user_limit) 攔截
    def t7_2():
        with app.app_context():
            u_chen = User.query.filter_by(phone="0912345678").first()
            vip_cp = Coupon.query.filter_by(code="VIP85").first()
            vip_cp_id = vip_cp.id

        # 先透過 POS 協助陳大文重新設定 PIN "1357" 並清除 5.4 的鎖定狀態
        s_csrf = get_client_csrf(staff_client, "/pos")
        staff_client.post(
            "/api/pos/set_member_pin",
            json={"user_id": u_chen.id, "pin": "1357"},
            headers={"X-CSRFToken": s_csrf},
        )

        member_client.get("/?shop=default")
        csrf = get_client_csrf(member_client, "/register")
        r_login = member_client.post(
            "/phone_login",
            json={"phone": "0912345678", "pin": "1357"},
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.40.1.1"},
        )
        assert r_login.status_code == 200 and r_login.get_json().get("success") is True

        # 第一次兌換 VIP85 (特惠 20 點，限領 1 張) -> 成功 (300 - 20 = 280 點)
        r_red1 = member_client.post(
            "/api/redeem_reward",
            json={"coupon_id": vip_cp_id},
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.40.1.1"},
        )
        assert r_red1.status_code == 200 and r_red1.get_json().get("remaining_points") == 280

        # 第二次兌換同一張限領 1 張的 VIP85 -> 必回 400 攔截
        r_red2 = member_client.post(
            "/api/redeem_reward",
            json={"coupon_id": vip_cp_id},
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.40.1.1"},
        )
        assert r_red2.status_code == 400, f"超過 per_user_limit=1 未被攔截: {r_red2.status_code}"
        return "兌換 VIP85 扣 20 點(剩280點) & 第 2 次兌換觸發限領上限攔截(400)"
    runner.check("7.2", "紅利兌換優惠券 (/api/redeem_reward) 與每人限領上限 (per_user_limit) 驗證", t7_2)

    # 7.3 紅利商品兌換憑證 (RewardRedemption) 與退點防洗點保護 (/api/refund_reward_item)
    def t7_3():
        csrf = get_client_csrf(member_client, "/rewards")
        nugget_id = menu_ids["黃金雞塊"]  # 50 點

        # (a) 尚未兌換憑證就嘗試直接呼叫 /api/refund_reward_item 洗點數 -> 必回 400
        r_wash = member_client.post(
            "/api/refund_reward_item",
            json={"item_id": nugget_id, "quantity": 1},
            headers={"X-CSRFToken": csrf},
        )
        assert r_wash.status_code == 400, f"無兌換憑證空手套白狼退點未被攔截: {r_wash.status_code}"

        # (b) 正常兌換黃金雞塊 (扣 50 點 -> 280 - 50 = 230 點)，產生 RewardRedemption 憑證
        r_claim = member_client.post(
            "/api/redeem_reward",
            json={"item_id": nugget_id},
            headers={"X-CSRFToken": csrf},
        )
        assert r_claim.status_code == 200 and r_claim.get_json().get("remaining_points") == 230

        # (c) 第一次正常退還黃金雞塊 -> 成功退回 50 點 (230 + 50 = 280 點)，憑證標記 is_refunded=True
        r_ref1 = member_client.post(
            "/api/refund_reward_item",
            json={"item_id": nugget_id, "quantity": 1},
            headers={"X-CSRFToken": csrf},
        )
        d_ref1 = r_ref1.get_json() or {}
        new_pts = d_ref1.get("current_points") if d_ref1.get("current_points") is not None else d_ref1.get("new_points")
        assert r_ref1.status_code == 200 and new_pts == 280

        # (d) 拿同一張已退還憑證嘗試第二次重複退點 -> 必回 400 攔截，點數維持 280 點
        r_ref2 = member_client.post(
            "/api/refund_reward_item",
            json={"item_id": nugget_id, "quantity": 1},
            headers={"X-CSRFToken": csrf},
        )
        assert r_ref2.status_code == 400, f"重複退還同一紅利商品未被攔截: {r_ref2.status_code}"
        with app.app_context():
            u = User.query.filter_by(phone="0912345678").first()
            assert u.points == 280, f"重複退點導致點數異常: {u.points}"
        return "無憑證退點攔截(400) -> 正常兌換與退點(280點) -> 重複退點攔截(400) 通過"
    runner.check("7.3", "紅利商品憑證 (RewardRedemption) 與重複退點防洗點保護 (/api/refund_reward_item)", t7_3)

    # 7.4 排列組合 3：會員複合結帳 (紅利商品憑證核銷不重複扣點 + VIP85 折券 + 點數折抵上限 + 回饋點數)
    def t7_4():
        csrf_adm = get_client_csrf(admin_client, "/admin")
        # 設定紅利規則：1 點折 $1，單筆最多折抵 $30，每消費滿 $50 送 1 點
        r_set = admin_client.post(
            "/admin/update_reward_setting",
            data={
                "is_enabled": "1",
                "points_per_dollar": "1",
                "max_discount_per_order": "30",
                "spend_per_point": "50",
                "csrf_token": csrf_adm,
            },
        )
        assert r_set.status_code == 302

        csrf = get_client_csrf(member_client, "/")
        # 先在紅利商城兌換 1 份「黃金雞塊」(扣 50 點：280 -> 230 點，建立未使用憑證)
        r_claim = member_client.post(
            "/api/redeem_reward",
            json={"item_id": menu_ids["黃金雞塊"]},
            headers={"X-CSRFToken": csrf},
        )
        assert r_claim.status_code == 200 and r_claim.get_json().get("remaining_points") == 230

        # 結帳購物車：
        # 1. 招牌和牛堡 x2 (特價 120 * 2 = 240)
        # 2. 🎁 [點數兌換] 黃金雞塊 x1 ($0，核銷剛才的憑證，不再二次扣 50 點)
        # 小計 = 240；套用 VIP85 (85折) -> 折抵 round(240*(1-0.85)) = 36 元，剩餘 204 元
        # 會員欲使用 100 點折抵，受單筆折抵上限 $30 限制 -> 僅扣 30 點折 $30，應付總額 = 174 元
        # 實付 $174，每滿 $50 送 1 點 -> 獲得 3 點
        # 最終點數應為：230 - 30 (折抵) + 3 (回饋) = 203 點！
        r_order = member_client.post(
            "/submit_order",
            json={
                "items": [
                    {"id": menu_ids["招牌和牛堡"], "name": "招牌和牛堡", "price": 120, "quantity": 2},
                    {"id": f"reward_{menu_ids['黃金雞塊']}", "name": "🎁 [點數兌換] 黃金雞塊", "price": 0, "quantity": 1},
                ],
                "payment_method": "Credit Card",
                "order_type": "內用",
                "promo_code": "VIP85",
                "use_points": 100,
            },
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.40.1.2"},
        )
        assert r_order.status_code == 200, f"會員複合結帳失敗: {r_order.get_data(as_text=True)}"
        d = r_order.get_json()
        assert d["total_price"] == 174, f"複合結帳總額應為 174，實際 {d['total_price']}"
        assert d["points_used"] == 30, f"點數折抵應受上限限制為 30 點，實際 {d['points_used']}"
        assert d["points_earned"] == 3, f"回饋點數應為 3 點，實際 {d['points_earned']}"
        assert d["remaining_points"] == 203, f"結帳後會員剩餘點數應為 203 點(不重複扣兌換品點數)，實際 {d['remaining_points']}"
        return "小計$240 -> VIP85折$36 -> 點數上限折$30 -> 實付$174(贈3點) -> 餘203點(零雙重扣點)"
    runner.check("7.4", "排列組合 3：會員複合結帳 (憑證核銷零雙扣 + 優惠券 + 點數上限 + 回饋點數)", t7_4)

    # ==========================================================================
    # 模組 8：櫃檯 POS 收銀系統 (/pos) 模糊查詢多筆候選、設 PIN、優惠碼與收銀找零稽核
    # ==========================================================================
    runner.start_suite("模組 8：櫃檯 POS 收銀系統 (/pos) 多筆候選查詢、設 PIN、優惠碼與找零稽核")

    # 8.1 POS 介面載入與 /api/pos/member_lookup (模糊比對多筆候選清單 + 指定 member_id 精準綁定)
    def t8_1():
        r_page = staff_client.get("/pos")
        assert r_page.status_code == 200 and "posMemberSelectModal" in r_page.get_data(as_text=True)
        csrf = get_client_csrf(staff_client, "/pos")

        # 搜尋「陳」應同時命中「陳大文 (0912345678)」與「陳小美 (0912999888)」，回傳 multiple=True 與 candidates
        r_multi = staff_client.post(
            "/api/pos/member_lookup",
            json={"query": "陳"},
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.50.1.1"},
        )
        assert r_multi.status_code == 200
        dm = r_multi.get_json()
        assert dm.get("success") is True and dm.get("multiple") is True, f"多筆同名會員未回傳 multiple=True: {dm}"
        assert len(dm.get("candidates", [])) >= 2, "候選清單人數不足 2 人"

        # 店員點選其中一位候選會員 (傳入 member_id) -> 應精準回傳單筆 member
        target_cand_id = dm["candidates"][0]["id"]
        r_exact = staff_client.post(
            "/api/pos/member_lookup",
            json={"member_id": target_cand_id},
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.50.1.1"},
        )
        de = r_exact.get_json()
        assert r_exact.status_code == 200 and de.get("multiple") is False and de["member"]["id"] == target_cand_id
        return f"輸入'陳'回傳 {len(dm['candidates'])} 筆候選清單(multiple=True) & 指定 member_id 精準綁定通過"
    runner.check("8.1", "POS 會員查詢 (/api/pos/member_lookup)：多筆候選清單與精準 ID 綁定", t8_1)

    # 8.2 POS 櫃檯協助設定/重設會員 PIN 碼 (/api/pos/set_member_pin)、解除鎖定與稽核紀錄
    def t8_2():
        csrf = get_client_csrf(staff_client, "/pos")
        with app.app_context():
            u_legacy = User.query.filter_by(phone="0912999888").first()
            legacy_uid = u_legacy.id

        # (a) 格式錯誤 PIN 攔截 (400)
        r_bad = staff_client.post(
            "/api/pos/set_member_pin",
            json={"user_id": legacy_uid, "pin": "12a"},
            headers={"X-CSRFToken": csrf},
        )
        assert r_bad.status_code == 400

        # (b) 為原本無 PIN 的舊會員陳小美設定 PIN "6688"
        r_ok = staff_client.post(
            "/api/pos/set_member_pin",
            json={"user_id": legacy_uid, "pin": "6688"},
            headers={"X-CSRFToken": csrf},
        )
        assert r_ok.status_code == 200 and r_ok.get_json().get("success") is True

        # 驗證陳小美現在可以用手機 + PIN "6688" 成功登入，且寫入 POS_SET_MEMBER_PIN 稽核
        c = app.test_client()
        c.get("/?shop=default")
        c_csrf = get_client_csrf(c, "/register")
        r_login = c.post(
            "/phone_login",
            json={"phone": "0912999888", "pin": "6688"},
            headers={"X-CSRFToken": c_csrf, "X-Forwarded-For": "10.50.1.2"},
        )
        assert r_login.status_code == 200 and r_login.get_json().get("success") is True

        with app.app_context():
            au = StaffAuditLog.query.filter_by(action_type="POS_SET_MEMBER_PIN", target_user_id=legacy_uid).first()
            assert au is not None and au.staff_username == "pos_user", "POS 設定 PIN 未寫入 StaffAuditLog"
        return "舊會員櫃檯設 PIN(6688) 後成功登入 & POS_SET_MEMBER_PIN 稽核紀錄通過"
    runner.check("8.2", "POS 櫃檯設定會員 PIN 碼 (/api/pos/set_member_pin) 與稽核紀錄驗證", t8_2)

    # 8.3 POS 優惠碼驗證 (/api/pos/verify_promo)：公開碼、專屬券與低消門檻
    def t8_3():
        csrf = get_client_csrf(staff_client, "/pos")
        # 公開碼 OPEN20：未達低消 $100 (subtotal=60) -> valid=False
        r_low = staff_client.post(
            "/api/pos/verify_promo",
            json={"promo_code": "OPEN20", "subtotal": 60},
            headers={"X-CSRFToken": csrf},
        )
        assert r_low.status_code == 200 and r_low.get_json().get("valid") is False

        # 公開碼 OPEN20：達低消 $100 (subtotal=150) -> valid=True, discount=20
        r_ok = staff_client.post(
            "/api/pos/verify_promo",
            json={"promo_code": "OPEN20", "subtotal": 150},
            headers={"X-CSRFToken": csrf},
        )
        assert r_ok.status_code == 200 and r_ok.get_json().get("valid") is True and r_ok.get_json().get("discount") == 20

        # 會員專屬紅利券 VIP85：未綁定會員 (user_id=None) -> valid=False
        r_no_member = staff_client.post(
            "/api/pos/verify_promo",
            json={"promo_code": "VIP85", "subtotal": 200},
            headers={"X-CSRFToken": csrf},
        )
        assert r_no_member.status_code == 200 and r_no_member.get_json().get("valid") is False
        return "低消攔截(valid=False)、公開碼折$20(valid=True)與專屬券未綁會員攔截通過"
    runner.check("8.3", "POS 優惠碼驗證 (/api/pos/verify_promo)：公開碼、專屬券與低消門檻", t8_3)

    # 8.4 POS 結帳防呆 (/api/pos/checkout)：現金實收不足 (cash_received < total_price) 與白名單攔截
    def t8_4():
        csrf = get_client_csrf(staff_client, "/pos")
        # (a) 招牌和牛堡特價 $120，但店員輸入實收現金 $50 (< $120) -> 必回 400
        r_short_cash = staff_client.post(
            "/api/pos/checkout",
            json={
                "items": [{"id": menu_ids["招牌和牛堡"], "name": "招牌和牛堡", "quantity": 1}],
                "payment_method": "Cash",
                "order_type": "內用",
                "cash_received": 50,
            },
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.50.1.3"},
        )
        assert r_short_cash.status_code == 400 and "實收現金" in (r_short_cash.get_json().get("error") or ""), (
            f"實收現金不足未回傳 400: {r_short_cash.get_data(as_text=True)}"
        )

        # (b) 非法付款方式或用餐方式 -> 必回 400
        r_bad_pay = staff_client.post(
            "/api/pos/checkout",
            json={
                "items": [{"id": menu_ids["招牌和牛堡"], "name": "招牌和牛堡", "quantity": 1}],
                "payment_method": "EvilPay",
                "order_type": "內用",
            },
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.50.1.4"},
        )
        assert r_bad_pay.status_code == 400
        return "實收現金不足($50 < $120)攔截(400) & 非法付款白名單攔截(400) 通過"
    runner.check("8.4", "POS 結帳防呆 (/api/pos/checkout)：實收現金不足與白名單攔截驗證", t8_4)

    pos_order_id_holder = {"id": None}

    # 8.5 POS 完整結帳帳務與追責：找零計算、order_source='POS'、cashier_username、CustomerLog 與稽核紀錄
    def t8_5():
        csrf = get_client_csrf(staff_client, "/pos")
        with app.app_context():
            u_mei = User.query.filter_by(phone="0912999888").first()
            mei_id = u_mei.id  # 陳小美目前有 150 點

        # 陳小美點：招牌和牛堡(特價120) + A套餐(+75) = 195 元
        # 套用公開碼 OPEN20 (-$20) -> 剩 175 元；再使用紅利點數折抵 25 點 (-$25) -> 應收總額 = 150 元
        # 現金實收 $500 -> 找零應為 $350；滿 $50 送 1 點 -> 回饋 3 點 (150 - 25 + 3 = 128 點)
        r_pos = staff_client.post(
            "/api/pos/checkout",
            json={
                "user_id": mei_id,
                "table_number": "A3桌",
                "items": [
                    {
                        "id": menu_ids["招牌和牛堡"],
                        "name": "招牌和牛堡",
                        "quantity": 1,
                        "customization": "[套餐: A套餐(薯條+紅茶)]、美式脆薯(不辣)、錫蘭紅茶(去冰、無糖)",
                    }
                ],
                "payment_method": "Cash",
                "order_type": "內用",
                "promo_code": "OPEN20",
                "use_points": 25,
                "cash_received": 500,
                "note": "POS 櫃檯結帳測試",
            },
            headers={"X-CSRFToken": csrf, "X-Forwarded-For": "10.50.1.5"},
        )
        assert r_pos.status_code == 200, f"POS 結帳失敗: {r_pos.get_data(as_text=True)}"
        d = r_pos.get_json()
        assert d["total_price"] == 150 and d["cash_received"] == 500 and d["cash_change"] == 350
        assert d["order_source"] == "POS" and d["cashier"] == "pos_user"
        assert d["remaining_points"] == 128
        pos_order_id_holder["id"] = d["order_id"]

        with app.app_context():
            ord_db = db.session.get(Order, d["order_id"])
            assert ord_db.order_source == "POS" and ord_db.cashier_username == "pos_user"
            assert ord_db.cash_received == 500 and ord_db.cash_change == 350
            assert ord_db.customer_log_id is not None, "POS 訂單未建立並關聯 CustomerLog"
            clog = db.session.get(CustomerLog, ord_db.customer_log_id)
            assert clog is not None and clog.user_type == "POS會員" and "陳小美" in clog.identifier
            au = StaffAuditLog.query.filter_by(action_type="POS_CREATE_ORDER", target_order_id=ord_db.id).first()
            assert au is not None and au.staff_username == "pos_user" and au.points_delta == (3 - 25)
        return "應收$150/實收$500/找零$350、source='POS'、cashier='pos_user'、CustomerLog 與稽核通過"
    runner.check("8.5", "POS 完整結帳：實收/找零持久化、收銀員、CustomerLog 與 POS_CREATE_ORDER 稽核", t8_5)

    # ==========================================================================
    # 模組 9：廚房 KDS、櫃檯核銷、訂單狀態機非法轉換攔截與取消全量回滾
    # ==========================================================================
    runner.start_suite("模組 9：廚房 KDS、櫃檯核銷、訂單狀態機非法轉換攔截與取消全量回滾")

    # 9.1 廚房 KDS 讀取、單筆出餐 (Completed)、櫃檯核銷 (PickedUp) 與 MARK_PICKED_UP 稽核
    def t9_1():
        r_kds = kitchen_client.get("/api/kitchen_orders")
        assert r_kds.status_code == 200
        orders = r_kds.get_json().get("orders", [])
        assert len(orders) >= 1, "廚房 KDS 未讀出待製作訂單"
        first_oid = orders[0]["id"]

        # 廚房將第一張訂單標記為 Completed
        k_csrf = get_client_csrf(kitchen_client, "/kitchen")
        r_comp = kitchen_client.post(
            f"/api/kitchen_update_status/{first_oid}",
            json={"status": "Completed"},
            headers={"X-CSRFToken": k_csrf},
        )
        assert r_comp.status_code == 200 and r_comp.get_json().get("success") is True

        # 公開叫號看板 API (/api/counter_orders) 應可免登入讀取，且該單出現在 ready_orders
        anon = app.test_client()
        r_cnt = anon.get("/api/counter_orders")
        assert r_cnt.status_code == 200
        ready_ids = [o["id"] for o in r_cnt.get_json().get("ready_orders", [])]
        assert first_oid in ready_ids, "Completed 訂單未出現在公開叫號看板 ready_orders"

        # 櫃檯人員核銷取餐 (/api/mark_picked_up/<id>)
        s_csrf = get_client_csrf(staff_client, "/pickup")
        r_pick = staff_client.post(
            f"/api/mark_picked_up/{first_oid}",
            headers={"X-CSRFToken": s_csrf},
        )
        assert r_pick.status_code == 200 and r_pick.get_json().get("success") is True
        with app.app_context():
            assert db.session.get(Order, first_oid).status == "PickedUp"
            au = StaffAuditLog.query.filter_by(action_type="MARK_PICKED_UP", target_order_id=first_oid).first()
            assert au is not None, "櫃檯取餐核銷未寫入 MARK_PICKED_UP 稽核紀錄"
        return f"訂單 #{first_oid} Pending -> Completed(叫號牆同步) -> PickedUp(含稽核) 通過"
    runner.check("9.1", "廚房 KDS 出餐、公開叫號牆同步、櫃檯核銷與 MARK_PICKED_UP 稽核", t9_1)

    # 9.2 訂單狀態機非法轉換嚴格攔截 (Pending 直接取餐、Cancelled 復活回 Pending/Completed/PickedUp)
    def t9_2():
        pos_oid = pos_order_id_holder["id"]
        s_csrf = get_client_csrf(staff_client, "/pickup")
        k_csrf = get_client_csrf(kitchen_client, "/kitchen")
        a_csrf = get_client_csrf(admin_client, "/admin")

        # (a) pos_oid 目前為 Pending，嘗試未出餐直接呼叫 /api/mark_picked_up/<id> -> 必回 400
        r_premature = staff_client.post(
            f"/api/mark_picked_up/{pos_oid}",
            headers={"X-CSRFToken": s_csrf},
        )
        assert r_premature.status_code == 400, f"Pending 訂單未出餐直接取餐未被攔截: {r_premature.status_code}"

        # (b) 建立一張測試單並先取消為 Cancelled，再測試各種非法復活轉換
        c = app.test_client()
        c.get("/?shop=default")
        c.get("/guest_login")
        c_csrf = get_client_csrf(c, "/")
        r_new = c.post(
            "/submit_order",
            json={
                "items": [{"id": menu_ids["錫蘭紅茶"], "name": "錫蘭紅茶", "quantity": 1}],
                "payment_method": "Cash",
                "order_type": "內用",
            },
            headers={"X-CSRFToken": c_csrf, "X-Forwarded-For": "10.60.1.1"},
        )
        cancel_oid = r_new.get_json()["order_id"]

        # 透過店長取消該單
        r_cancel1 = manager_client.post(
            f"/api/kitchen_update_status/{cancel_oid}",
            json={"status": "Cancelled"},
            headers={"X-CSRFToken": get_client_csrf(manager_client, "/kitchen")},
        )
        assert r_cancel1.status_code == 200

        # 嘗試重複取消同一張已取消訂單 -> 必回 400
        r_cancel2 = manager_client.post(
            f"/api/kitchen_update_status/{cancel_oid}",
            json={"status": "Cancelled"},
            headers={"X-CSRFToken": get_client_csrf(manager_client, "/kitchen")},
        )
        assert r_cancel2.status_code == 400, f"重複取消已取消訂單未回 400: {r_cancel2.status_code}"

        # 嘗試透過 KDS 將 Cancelled 改回 Completed -> 必回 400
        r_revive_kds = kitchen_client.post(
            f"/api/kitchen_update_status/{cancel_oid}",
            json={"status": "Completed"},
            headers={"X-CSRFToken": k_csrf},
        )
        assert r_revive_kds.status_code == 400, f"KDS 將 Cancelled 訂單復活為 Completed 未被攔截: {r_revive_kds.status_code}"

        # 嘗試透過後台 /admin/update_order_status/<id> 將 Cancelled 改回 Pending -> 必回 400
        r_revive_adm = admin_client.post(
            f"/admin/update_order_status/{cancel_oid}",
            json={"status": "Pending"},
            headers={"X-CSRFToken": a_csrf},
        )
        assert r_revive_adm.status_code == 400, f"後台將 Cancelled 訂單復活為 Pending 未被攔截: {r_revive_adm.status_code}"

        # 嘗試將 Cancelled 訂單標記為 PickedUp -> 必回 400
        r_revive_pick = staff_client.post(
            f"/api/mark_picked_up/{cancel_oid}",
            headers={"X-CSRFToken": s_csrf},
        )
        assert r_revive_pick.status_code == 400, f"Cancelled 訂單直接取餐未被攔截: {r_revive_pick.status_code}"
        return "Pending直接取餐(400)、重複取消(400)與Cancelled復活(KDS/Admin/Pickup全400) 攔截通過"
    runner.check("9.2", "訂單狀態機非法轉換攔截 (Pending直接取餐、Cancelled復活與重複取消)", t9_2)

    # 9.3 取消訂單全量回滾 (主餐庫存、全部套餐配餐庫存、優惠券、折抵點數与贈點) + CANCEL_ORDER 稽核
    def t9_3():
        pos_oid = pos_order_id_holder["id"]
        with app.app_context():
            b_stock_before = db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock
            f_stock_before = db.session.get(MenuItem, menu_ids["美式脆薯"]).stock
            t_stock_before = db.session.get(MenuItem, menu_ids["錫蘭紅茶"]).stock
            u_mei = User.query.filter_by(phone="0912999888").first()
            pts_before = u_mei.points  # 128 點

        # 由後台取消 8.5 建立的 POS 套餐訂單 (含主餐+雙配餐、折抵 25 點、贈 3 點)
        a_csrf = get_client_csrf(admin_client, "/admin")
        r_cancel = admin_client.post(
            f"/admin/update_order_status/{pos_oid}",
            json={"status": "Cancelled"},
            headers={"X-CSRFToken": a_csrf},
        )
        assert r_cancel.status_code in (200, 302)

        with app.app_context():
            assert db.session.get(Order, pos_oid).status == "Cancelled"
            assert db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock == b_stock_before + 1, "取消訂單主餐庫存未回補"
            assert db.session.get(MenuItem, menu_ids["美式脆薯"]).stock == f_stock_before + 1, "取消訂單配餐1(脆薯)庫存未回補"
            assert db.session.get(MenuItem, menu_ids["錫蘭紅茶"]).stock == t_stock_before + 1, "取消訂單配餐2(紅茶)庫存未回補"
            u_mei_after = User.query.filter_by(phone="0912999888").first()
            assert u_mei_after.points == pts_before + 25 - 3, f"取消訂單會員點數未精準回滾(期望 150, 實際 {u_mei_after.points})"
            au = StaffAuditLog.query.filter_by(action_type="CANCEL_ORDER", target_order_id=pos_oid).first()
            assert au is not None and au.points_delta == 22, "取消訂單未寫入 CANCEL_ORDER 稽核紀錄"
        return "主餐+雙配餐庫存回補(+1)、會員點數回滾(128->150點) & CANCEL_ORDER 稽核通過"
    runner.check("9.3", "取消訂單全量回滾 (主餐/套餐配餐庫存、點數、優惠券) 與 CANCEL_ORDER 稽核", t9_3)

    # 9.4 廚房一鍵全部出餐 (/api/kitchen_complete_all)
    def t9_4():
        k_csrf = get_client_csrf(kitchen_client, "/kitchen")
        r_all = kitchen_client.post("/api/kitchen_complete_all", headers={"X-CSRFToken": k_csrf})
        assert r_all.status_code == 200 and r_all.get_json().get("success") is True
        with app.app_context():
            pending_cnt = Order.query.filter_by(status="Pending").count()
            assert pending_cnt == 0, f"一鍵出餐後仍有 {pending_cnt} 筆 Pending 訂單"
        return "當日所有 Pending 訂單一鍵批次出餐完成"
    runner.check("9.4", "廚房一鍵全部出餐 (/api/kitchen_complete_all) 批次更新驗證", t9_4)

    # ==========================================================================
    # 模組 10：營運分析、AI 三層韌性降級、Excel 10 工作表匯出與智慧匯入
    # ==========================================================================
    runner.start_suite("模組 10：營運分析、AI 三層韌性降級、Excel 10 工作表匯出與智慧匯入")

    # 10.1 營運分析與 AI 三層韌性降級 (/api/admin_live_orders & /api/generate_item_description)
    def t10_1():
        r_live = admin_client.get("/api/admin_live_orders")
        assert r_live.status_code == 200
        d = r_live.get_json()
        advice = d.get("analytics", {}).get("ai_advice", "")
        assert advice and len(advice) > 10, "AI 營運建議回傳內容為空"

        csrf = get_client_csrf(admin_client, "/admin")
        r_copy = admin_client.post(
            "/api/generate_item_description",
            json={"name": "招牌和牛堡", "category": "主餐"},
            headers={"X-CSRFToken": csrf},
        )
        assert r_copy.status_code == 200 and r_copy.get_json().get("description"), "AI 菜單文案三層降級失敗"
        return f"AI 營運建議 ({len(advice)} 字) & AI 菜單文案降級生成通過"
    runner.check("10.1", "營運分析數據計算與 AI 三層韌性降級 (營運建議與菜單文案) 驗證", t10_1)

    exported_xlsx_bytes = {"data": None}

    # 10.2 Excel 完整 10 工作表匯出 (/admin/export_excel)
    def t10_2():
        csrf = get_client_csrf(admin_client, "/admin")
        r_exp = admin_client.post(
            "/admin/export_excel",
            data={
                "tables": [
                    "User", "MenuItem", "Coupon", "UserCoupon",
                    "Order", "OrderItem", "Analytics", "AdminLog",
                ],
                "csrf_token": csrf,
            },
        )
        assert r_exp.status_code == 200, f"匯出 Excel 失敗: {r_exp.status_code}"
        exported_xlsx_bytes["data"] = r_exp.data

        wb = openpyxl.load_workbook(io.BytesIO(r_exp.data))
        expected_sheets = {
            "會員資料(User)",
            "菜單品項(MenuItem)",
            "優惠券(Coupon)",
            "會員持券(UserCoupon)",
            "訂單總覽(Order)",
            "訂單明細(OrderItem)",
            "區間營運總覽(Analytics)",
            "區間熱銷排行(ItemSales)",
            "管理員進出日誌(AdminLog)",
            "員工操作稽核(StaffAuditLog)",
        }
        missing = expected_sheets - set(wb.sheetnames)
        assert not missing, f"匯出之 Excel 缺少工作表: {missing}"

        # 檢查訂單總覽工作表是否包含 POS 對帳欄位 (startrow=1，第 2 列為欄位標題)
        ws_orders = wb["訂單總覽(Order)"]
        header_row = [cell.value for cell in ws_orders[2]]
        for col_name in ["訂單來源(Source)", "收銀員(Cashier)", "應付總額(Total)", "實收金額(Received)", "找零金額(Change)"]:
            assert col_name in header_row, f"訂單總覽 Excel 缺少對帳欄位: {col_name}"

        # 檢查員工操作稽核工作表是否有資料列
        ws_audit = wb["員工操作稽核(StaffAuditLog)"]
        assert ws_audit.max_row >= 5, "員工操作稽核(StaffAuditLog) 工作表無資料列"
        return f"成功匯出 {len(wb.sheetnames)} 張工作表 (含 POS 對帳欄位與 StaffAuditLog 稽核表)"
    runner.check("10.2", "Excel 完整匯出 (/admin/export_excel)：10 張工作表與 POS/稽核欄位驗證", t10_2)

    # 10.3 Excel 菜單匯入 (/admin/import_excel) 與智慧匯入 (/admin/import_smart) 驗證
    def t10_3():
        csrf = get_client_csrf(admin_client, "/admin")

        # (a) 測試 /admin/import_excel：建立包含新菜色「宇治抹茶拿鐵 ($65)」的 Excel 並匯入
        wb_new = openpyxl.Workbook()
        ws_new = wb_new.active
        ws_new.title = "菜單品項(MenuItem)"
        ws_new.append(["餐點名稱(Name)", "分類(Category)", "單價(Price)", "客製化群組(Modifiers)", "商品描述(Description)"])
        ws_new.append(["宇治抹茶拿鐵", "飲料", 65, "ice_sugar", "京都宇治抹茶鮮奶"])
        buf_new = io.BytesIO()
        wb_new.save(buf_new)
        buf_new.seek(0)

        r_imp_menu = admin_client.post(
            "/admin/import_excel",
            data={"csrf_token": csrf, "file": (buf_new, "new_menu_items.xlsx")},
            content_type="multipart/form-data",
        )
        assert r_imp_menu.status_code in (200, 302), f"/admin/import_excel 失敗: {r_imp_menu.status_code}"

        # (b) 測試 /admin/import_smart：修改剛才匯出的全店 Excel 中「錫蘭紅茶」單價為 45，上傳進行 Upsert 同步
        wb = openpyxl.load_workbook(io.BytesIO(exported_xlsx_bytes["data"]))
        ws_menu = wb["菜單品項(MenuItem)"]
        headers = [c.value for c in ws_menu[1]]
        name_col = headers.index("餐點名稱(Name)") + 1
        price_col = headers.index("單價(Price)") + 1

        for r_idx in range(2, ws_menu.max_row + 1):
            if ws_menu.cell(row=r_idx, column=name_col).value == "錫蘭紅茶":
                ws_menu.cell(row=r_idx, column=price_col).value = 45
                break

        buf_smart = io.BytesIO()
        wb.save(buf_smart)
        buf_smart.seek(0)

        r_imp_smart = admin_client.post(
            "/admin/import_smart",
            data={"csrf_token": csrf, "file": (buf_smart, "updated_store_data.xlsx")},
            content_type="multipart/form-data",
        )
        assert r_imp_smart.status_code in (200, 302), f"/admin/import_smart 失敗: {r_imp_smart.status_code}"

        with app.app_context():
            matcha = MenuItem.query.filter_by(name="宇治抹茶拿鐵").first()
            tea = MenuItem.query.filter_by(name="錫蘭紅茶").first()
            assert matcha is not None and matcha.price == 65, "/admin/import_excel 未成功匯入宇治抹茶拿鐵"
            assert tea.price == 45, f"/admin/import_smart 未同步更新錫蘭紅茶價格: {tea.price}"
        return "import_excel 新增'宇治抹茶拿鐵($65)' & import_smart 更新'錫蘭紅茶($45)' 通過"
    runner.check("10.3", "Excel 菜單匯入 (/admin/import_excel) 與智慧匯入 (/admin/import_smart) 驗證", t10_3)

    # ==========================================================================
    # 模組 11：頻率限制 (Rate Limit 429) 與 SSE 即時串流連線配額獨立驗證模組
    # ==========================================================================
    runner.start_suite("模組 11：頻率限制 (Rate Limit 429) 與 SSE 即時串流連線配額獨立驗證")

    # 11.1 頻率限制 (@rate_limit) 實測：不關閉限流，連續發送超過上限請求驗證回傳 429
    def t11_1():
        os.environ["DISABLE_RATE_LIMIT"] = "0"
        clear_rate_buckets()

        c = app.test_client()
        c.get("/?shop=default")
        csrf = get_client_csrf(c, "/admin")
        test_ip = "203.0.113.199"

        # /admin 登入限制為每 60 秒最多 10 次 POST
        codes = []
        for _ in range(12):
            resp = c.post(
                "/admin",
                data={"username": "admin_test", "password": "wrong", "csrf_token": csrf},
                headers={"X-Forwarded-For": test_ip},
            )
            codes.append(resp.status_code)

        assert codes[:10] == [401] * 10 and 429 in codes[10:], f"頻率限制未在第 11 次觸發 429: {codes}"
        clear_rate_buckets()
        return f"前10次401、第11~12次回傳 {codes[10:]} (HTTP 429 限流保護觸發)"
    runner.check("11.1", "API 頻率限制 (@rate_limit) 實測：超過配額精準觸發 HTTP 429 攔截", t11_1)

    # 11.2 SSE 即時串流連線配額 (OrderEventBus：單 IP 10 條、公開 25 條、員工 60 條) 驗證
    def t11_2():
        from app.services.event_bus import order_event_bus

        sig = inspect.signature(order_event_bus.subscribe)
        params = list(sig.parameters.keys())

        with order_event_bus._lock:
            order_event_bus._subscribers.clear()

        created_queues = []
        try:
            if len(params) >= 3:
                # (a) 單一 IP 公開看板上限 (10 條)：同一 IP 連續訂閱 15 次，應自動淘汰最舊連線，維持 10 條
                for _ in range(15):
                    q = order_event_bus.subscribe(1, client_ip="198.51.100.50", is_staff=False)
                    created_queues.append(q)
                ip_active = len(order_event_bus._subscribers)
                assert ip_active == 10, f"單一 IP 公開 SSE 連線數應限制為 10 條，實際為 {ip_active} 條"

                # (b) 公開看板總上限 (25 條)：來自 35 個不同 IP 的公開訂閱，總數應維持 25 條
                with order_event_bus._lock:
                    order_event_bus._subscribers.clear()
                for i in range(35):
                    q = order_event_bus.subscribe(1, client_ip=f"198.51.100.{i + 1}", is_staff=False)
                    created_queues.append(q)
                pub_active = len(order_event_bus._subscribers)
                assert pub_active == 25, f"公開 SSE 總連線數應限制為 25 條，實際為 {pub_active} 條"

                # (c) 登入員工看板上限 (60 條)：即使同一個門市內網 IP 訂閱 70 條，不受單 IP 10 條限制，並以 60 條為上限
                with order_event_bus._lock:
                    order_event_bus._subscribers.clear()
                for _ in range(70):
                    q = order_event_bus.subscribe(1, client_ip="192.168.1.10", is_staff=True)
                    created_queues.append(q)
                staff_active = len(order_event_bus._subscribers)
                assert staff_active == 60, f"員工 SSE 總連線數應限制為 60 條，實際為 {staff_active} 條"
                return f"單IP上限={ip_active}條、公開總上限={pub_active}條、員工總上限={staff_active}條 淘汰保護通過"
            else:
                for _ in range(200):
                    q = order_event_bus.subscribe(1)
                    created_queues.append(q)
                tot_active = len(order_event_bus._subscribers)
                assert tot_active <= getattr(order_event_bus, "_max_subscribers", 150)
                return f"SSE 全域訂閱上限淘汰保護 ({tot_active} 條) 通過"
        finally:
            with order_event_bus._lock:
                order_event_bus._subscribers.clear()
    runner.check("11.2", "SSE 即時串流配額驗證：單 IP 10 條、公開 25 條、員工 60 條上限保護", t11_2)

    # ==========================================================================
    # 模組 13：補強覆蓋 — 先前未被測試的路由、免登入資料外洩與 admin_required 角色越權
    # ==========================================================================
    runner.start_suite("模組 13：補強覆蓋 (未測路由、免登入資料外洩、admin_required 角色越權與跨店隔離)")
    clear_rate_buckets()
    state13 = {}

    def _tid13():
        with app.app_context():
            return Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first().id

    def _make_member13():
        """建立專屬於模組 13 的會員 (手機 0933130013 / PIN 2468 / 77 點)，只建立一次。"""
        if "uid" not in state13:
            with app.app_context():
                u = User(tenant_id=_tid13(), name="模組13會員", phone="0933130013", photo_path="m13.jpg", points=77)
                u.set_pin("2468")
                db.session.add(u)
                db.session.commit()
                state13["uid"] = u.id
        return state13["uid"]

    def _login13():
        _make_member13()
        c = app.test_client()
        c.get("/?shop=default")
        csrf = get_client_csrf(c, "/register")
        clear_rate_buckets()
        r = c.post("/phone_login", json={"phone": "0933130013", "pin": "2468"}, headers={"X-CSRFToken": csrf})
        assert r.status_code == 200 and r.get_json().get("success") is True, f"模組13會員登入失敗: {r.status_code}"
        return c

    # 13.1 免登入資料外洩：/api/admin_live_orders 與 AI 文案 API 必須要求後台登入
    def t13_1():
        anon = app.test_client()
        anon.get("/?shop=default")
        csrf = get_client_csrf(anon, "/")
        r_live = anon.get("/api/admin_live_orders")
        r_ai = anon.post("/api/generate_item_description", json={"name": "測試"}, headers={"X-CSRFToken": csrf})
        assert r_live.status_code in (401, 403), (
            f"/api/admin_live_orders 未登入即可讀取當日訂單/會員姓名/收銀員/營運分析: HTTP {r_live.status_code}"
        )
        assert r_ai.status_code == 401, f"/api/generate_item_description 未登入未回 401: {r_ai.status_code}"
        return f"匿名存取 admin_live_orders({r_live.status_code}) 與 AI 文案 API(401) 皆被攔截"
    runner.check("13.1", "免登入資料外洩防護：/api/admin_live_orders 與 AI 文案 API 需登入", t13_1)

    # 13.2 只掛 @admin_required 的路由是否擋掉 kitchen / staff 角色
    def t13_2():
        tid = _tid13()
        with app.app_context():
            rs = RewardSetting.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=tid).first()
            snap = None if rs is None else (rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point)
        probes = [
            ("/admin/update_reward_setting", {"data": {"points_per_dollar": "1", "max_discount_per_order": "0", "spend_per_point": "100"}}),
            ("/admin/toggle_user_coupon/987654", {}),
            ("/admin/import_smart", {}),
            ("/admin/import_excel", {}),
            ("/admin/export_excel", {}),
            ("/api/generate_item_description", {"json": {"name": "越權測試", "category": "主餐"}}),
        ]
        offenders = []
        try:
            for role, client, csrf_path in (("kitchen", kitchen_client, "/kitchen"), ("staff", staff_client, "/pos")):
                csrf = get_client_csrf(client, csrf_path)
                for path, kw in probes:
                    kw = dict(kw)
                    kw["headers"] = {"X-CSRFToken": csrf}
                    if "data" in kw:
                        kw["data"] = dict(kw["data"], csrf_token=csrf)
                    r = client.post(path, **kw)
                    if r.status_code != 403:
                        offenders.append(f"{role}:{path}={r.status_code}")
        finally:
            # 若有越權成功寫入紅利設定，還原，避免污染後續模組
            if snap is not None:
                with app.app_context():
                    rs = RewardSetting.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=tid).first()
                    rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point = snap
                    db.session.commit()
        assert not offenders, "角色越權 (應回 403，建議改用 @role_required('owner','manager')): " + "; ".join(offenders)
        return "kitchen/staff 對 6 個管理路由 100% 回傳 403"
    runner.check("13.2", "admin_required 路由角色越權：kitchen / staff 不得改紅利設定、匯入匯出、呼叫 AI", t13_2)

    # 13.3 /face_login：GET 版面、缺照片、無人臉照片與暫存檔清除
    def _t13_3_body():
        c = app.test_client()
        c.get("/?shop=default")
        assert c.get("/face_login").status_code == 200
        csrf = get_client_csrf(c, "/face_login")
        clear_rate_buckets()
        r_none = c.post("/face_login", data={}, headers={"X-CSRFToken": csrf})
        assert r_none.status_code == 400 and r_none.get_json().get("success") is False, f"缺照片未回 400: {r_none.status_code}"
        r_blank = c.post(
            "/face_login",
            data={"photo": (make_test_jpeg_bytes(200, 200, color=(255, 255, 255)), "blank.jpg")},
            content_type="multipart/form-data",
            headers={"X-CSRFToken": csrf},
        )
        assert r_blank.status_code == 400, f"純色無人臉照片未回 400: {r_blank.status_code}"
        assert "人臉" in (r_blank.get_json().get("message") or ""), "無人臉提示訊息不含『人臉』"
        leftovers = [f for f in os.listdir(Config.UPLOAD_FOLDER_MEMBER) if f.startswith("temp_")]
        assert not leftovers, f"/face_login 暫存照片未清除: {leftovers}"
        return "GET(200)、缺照片(400)、無人臉(400) 與 temp_ 暫存檔清除驗證通過"

    def t13_3():
        # 本測試需使用「真實」人臉偵測器驗證無人臉照片；測完務必把模組 5 的確定性 mock 掛回去，
        # 避免後續需要人臉特徵的測試 (例如 13.17 註冊防呆) 受真實模型影響。
        if _orig_extract_feature is not None:
            cv_service_mod.extract_feature = _orig_extract_feature
            auth_route_mod.extract_feature = _orig_extract_feature
        try:
            return _t13_3_body()
        finally:
            cv_service_mod.extract_feature = _deterministic_face_embedding
            cv_service_mod.get_face_embedding = _deterministic_face_embedding
            auth_route_mod.extract_feature = _deterministic_face_embedding
            auth_route_mod.get_face_embedding = _deterministic_face_embedding
    runner.check("13.3", "人臉登入路由 (/face_login) 缺照片、無人臉與暫存檔清除驗證", t13_3)

    # 13.4 顧客端優惠碼驗證 /api/verify_promo
    def t13_4():
        tid = _tid13()
        with app.app_context():
            db.session.add(Coupon(tenant_id=tid, code="X13PUB", title="模組13公開券", discount_type="fixed",
                                  discount_value=10, min_spend=100, reward_points=0, is_reward=False))
            db.session.add(Coupon(tenant_id=tid, code="X13RWD", title="模組13紅利券", discount_type="fixed",
                                  discount_value=50, min_spend=0, reward_points=50, is_reward=True))
            db.session.commit()
        c = app.test_client()
        c.get("/?shop=default")
        csrf = get_client_csrf(c, "/")
        clear_rate_buckets()

        def verify(code, subtotal):
            return c.post("/api/verify_promo", json={"promo_code": code, "subtotal": subtotal},
                          headers={"X-CSRFToken": csrf}).get_json()

        assert verify("", 200)["valid"] is False, "空白代碼不應有效"
        assert verify("NO_SUCH_CODE", 200)["valid"] is False, "不存在代碼不應有效"
        low = verify("X13PUB", 50)
        assert low["valid"] is False and "門檻" in low["message"], f"未達低消未攔截: {low}"
        ok = verify("x13pub", 200)  # 小寫輸入應自動轉大寫
        assert ok["valid"] is True and abs(ok["discount"] - 10) < 1e-6, f"公開券折抵錯誤: {ok}"
        rwd = verify("X13RWD", 200)
        assert rwd["valid"] is False and "登入" in rwd["message"], f"紅利專屬券未登入應提示登入: {rwd}"
        return "空碼/無效碼/低消門檻/公開券(含小寫)/紅利專屬券未登入 五種情境驗證通過"
    runner.check("13.4", "顧客端優惠碼驗證 (/api/verify_promo) 公開券、低消與紅利專屬券", t13_4)

    # 13.5 /api/user_available_coupons
    def t13_5():
        anon = app.test_client()
        anon.get("/?shop=default")
        j0 = anon.get("/api/user_available_coupons").get_json()
        assert j0["success"] is False and j0["coupons"] == [] and j0["points"] == 0, f"未登入回應異常: {j0}"
        uid = _make_member13()
        tid = _tid13()
        with app.app_context():
            cp = Coupon(tenant_id=tid, code="X13OWN", title="模組13專屬券", discount_type="fixed",
                        discount_value=30, min_spend=0, reward_points=10, is_reward=True)
            db.session.add(cp)
            db.session.flush()
            db.session.add(UserCoupon(tenant_id=tid, user_id=uid, coupon_id=cp.id, code="X13OWN-A", is_used=False))
            db.session.add(UserCoupon(tenant_id=tid, user_id=uid, coupon_id=cp.id, code="X13OWN-B", is_used=True))
            db.session.commit()
        c = _login13()
        j = c.get("/api/user_available_coupons").get_json()
        codes = [x["code"] for x in j["coupons"]]
        assert j["success"] is True and j["points"] == 77, f"會員點數/狀態錯誤: {j}"
        assert codes == ["X13OWN-A"], f"只應列出未使用的券，實際: {codes}"
        assert "折$30" in j["coupons"][0]["title"], f"券標題格式異常: {j['coupons'][0]['title']}"
        return "未登入空清單；登入後僅列出未使用券 (已使用券被排除) 且點數正確"
    runner.check("13.5", "可用優惠券 API (/api/user_available_coupons) 登入/未登入與已使用券排除", t13_5)

    # 13.6 /my_orders
    def t13_6():
        anon = app.test_client()
        anon.get("/?shop=default")
        assert anon.get("/my_orders").get_json() == [], "未登入應回傳空陣列"
        uid = _make_member13()
        tid = _tid13()
        with app.app_context():
            db.session.add(Order(tenant_id=tid, user_id=uid, table_number="模組13會員", total_price=100, status="Pending", pickup_number=1))
            db.session.add(Order(tenant_id=tid, user_id=uid, table_number="模組13會員", total_price=200, status="Completed", pickup_number=2))
            db.session.add(Order(tenant_id=tid, user_id=uid + 100000, table_number="他人", total_price=999, status="Pending", pickup_number=3))
            db.session.commit()
        c = _login13()
        data = c.get("/my_orders").get_json()
        assert [d["total_price"] for d in data] == [200, 100], f"應只回傳本人訂單且新到舊排序: {[d['total_price'] for d in data]}"
        assert [d["user_order_no"] for d in data] == [2, 1], f"個人流水號錯誤: {[d['user_order_no'] for d in data]}"
        assert data[0]["status"] == "已出餐" and data[1]["status"] == "製作中", "狀態中文對照錯誤"
        return "未登入空陣列；會員僅看到自己的 2 筆訂單 (他人 999 元訂單未外洩)"
    runner.check("13.6", "我的訂單 (/my_orders) 歸戶隔離、排序、個人流水號與狀態對照", t13_6)

    # 13.7 /shop/<slug>
    def t13_7():
        from urllib.parse import urlparse
        with app.app_context():
            on_t = Tenant(name="啟用店13", slug="on-shop-13", is_active=True)
            off_t = Tenant(name="停用店13", slug="off-shop-13", is_active=False)
            db.session.add_all([on_t, off_t])
            db.session.commit()
            on_id = on_t.id
        c = app.test_client()
        r_ok = c.get("/shop/on-shop-13")
        assert r_ok.status_code == 302 and urlparse(r_ok.headers["Location"]).path == "/", f"有效店家未導向首頁: {r_ok.status_code}"
        with c.session_transaction() as s:
            assert s.get("tenant_id") == on_id, f"session 未綁定店家: {s.get('tenant_id')}"
        assert app.test_client().get("/shop/no-such-shop-13").status_code == 404, "不存在的店家未回 404"
        assert app.test_client().get("/shop/off-shop-13").status_code == 404, "已停用店家未回 404"
        return "有效 slug(302 並綁定 session)、不存在(404)、已停用店家(404) 驗證通過"
    runner.check("13.7", "店家入口 (/shop/<slug>) 啟用/停用/不存在三種情境", t13_7)

    # 13.8 /admin/toggle_user_coupon：同店切換與跨店隔離
    def t13_8():
        uid = _make_member13()
        tid = _tid13()
        with app.app_context():
            tb = Tenant(name="跨店B13", slug="tenant-b-13", is_active=True)
            db.session.add(tb)
            db.session.flush()
            ub = User(tenant_id=tb.id, name="B店會員", phone="0933130099", photo_path="b13.jpg", points=5)
            cb = Coupon(tenant_id=tb.id, code="X13B", title="B店券", discount_type="fixed", discount_value=5,
                        min_spend=0, reward_points=1, is_reward=True)
            db.session.add_all([ub, cb])
            db.session.flush()
            ucb = UserCoupon(tenant_id=tb.id, user_id=ub.id, coupon_id=cb.id, code="X13B-1", is_used=False)
            db.session.add(ucb)
            db.session.commit()
            ucb_id = ucb.id
            own = UserCoupon.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=tid, code="X13OWN-A").first()
            own_id = own.id
        csrf = get_client_csrf(admin_client, "/admin")
        hdr = {"X-CSRFToken": csrf}
        # (a) 跨店：預設店負責人操作 B 店券 -> 404 且狀態不變
        r_x = admin_client.post(f"/admin/toggle_user_coupon/{ucb_id}", headers=hdr)
        assert r_x.status_code == 404, f"跨店切換未被租戶隔離攔截: {r_x.status_code}"
        with app.app_context():
            assert UserCoupon.query.execution_options(skip_tenant_filter=True).get(ucb_id).is_used is False, "B 店券被跨店修改"
        # (b) 同店：切換為已使用 -> 再切回未使用
        r1 = admin_client.post(f"/admin/toggle_user_coupon/{own_id}", headers=hdr)
        j1 = r1.get_json()
        assert r1.status_code == 200 and j1["success"] is True
        assert any(c["code"] == "X13OWN-A" and c["is_used"] is True for c in j1["coupons"]), f"第一次切換結果錯誤: {j1}"
        j2 = admin_client.post(f"/admin/toggle_user_coupon/{own_id}", headers=hdr).get_json()
        assert any(c["code"] == "X13OWN-A" and c["is_used"] is False for c in j2["coupons"]), f"第二次切換結果錯誤: {j2}"
        return "跨店切換回 404 且資料未變；同店 is_used 可往返切換並回傳完整券清單"
    runner.check("13.8", "後台會員券核銷切換 (/admin/toggle_user_coupon) 同店切換與跨店隔離", t13_8)

    # ==========================================================================
    # 13.9 ~ 13.18：伺服器端修正後的回歸與補強測試
    # ==========================================================================
    def _reward_setting_snapshot13():
        with app.app_context():
            rs = RewardSetting.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=_tid13()).first()
            return None if rs is None else (rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point)

    def _restore_reward_setting13(snap):
        if snap is None:
            return
        with app.app_context():
            rs = RewardSetting.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=_tid13()).first()
            rs.is_enabled, rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point = snap
            db.session.commit()

    # 13.9 owner / manager 對 13.2 那六個路由仍可使用 (防止 role_required 修正過頭)
    def t13_9():
        snap = _reward_setting_snapshot13()
        blocked = []
        try:
            for role, client in (("owner", admin_client), ("manager", manager_client)):
                csrf = get_client_csrf(client, "/admin")
                hdr = {"X-CSRFToken": csrf}
                r = client.post(
                    "/admin/update_reward_setting",
                    data={"is_enabled": "1", "points_per_dollar": "2", "max_discount_per_order": "15",
                          "spend_per_point": "80", "csrf_token": csrf},
                    headers=hdr,
                )
                if r.status_code != 302:
                    blocked.append(f"{role}:/admin/update_reward_setting={r.status_code}")
                else:
                    with app.app_context():
                        rs = RewardSetting.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=_tid13()).first()
                        assert (rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point) == (2, 15, 80), \
                            f"{role} 紅利設定未寫入: {(rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point)}"
                # 這些路由在缺少檔案/欄位時會回 400 或 404，重點是「進得了 handler」而不是 401/403
                probes = [
                    ("/admin/toggle_user_coupon/987654", {}, 404),
                    ("/admin/import_smart", {}, 400),
                    ("/admin/import_excel", {}, 400),
                    ("/admin/export_excel", {}, 400),
                    ("/api/generate_item_description", {"json": {"name": "", "category": ""}}, 400),
                ]
                for path, kw, expect in probes:
                    r = client.post(path, headers=hdr, **kw)
                    if r.status_code != expect:
                        blocked.append(f"{role}:{path}={r.status_code}(預期{expect})")
        finally:
            _restore_reward_setting13(snap)
        assert not blocked, "owner/manager 被誤擋或行為異常: " + "; ".join(blocked)
        return "owner 與 manager 對 6 個管理路由皆可進入 handler，紅利設定可實際寫入"
    runner.check("13.9", "角色修正回歸：owner / manager 仍可使用紅利設定、匯入匯出、AI 文案與券核銷", t13_9)

    # 13.10 刪除 / 下架路由不得被 GET 觸發 (GET 會繞過 CSRF 防護)
    def t13_10():
        tid = _tid13()
        with app.app_context():
            item = MenuItem(tenant_id=tid, name="GET刪除測試品13", category="主餐", price=10, stock=5, total_stock=5, modifiers="none")
            rwd = MenuItem(tenant_id=tid, name="GET下架測試品13", category="主餐", price=10, stock=5, total_stock=5,
                           modifiers="none", is_reward=True, reward_points=10)
            cp = Coupon(tenant_id=tid, code="X13GETDEL", title="GET刪除券13", discount_type="fixed", discount_value=5,
                        min_spend=0, reward_points=0, is_reward=False)
            usr = User(tenant_id=tid, name="GET刪除會員13", phone="0933130120", photo_path="g13.jpg", points=1)
            db.session.add_all([item, rwd, cp, usr])
            db.session.flush()
            cb = ComboOption(tenant_id=tid, main_item_id=item.id, name="GET刪除套餐13", additional_price=10)
            db.session.add(cb)
            db.session.commit()
            ids = {"item": item.id, "rwd": rwd.id, "cp": cp.id, "usr": usr.id, "cb": cb.id}
        offenders = []
        try:
            for path, key, model in (
                (f"/admin/delete/{ids['item']}", "item", MenuItem),
                (f"/admin/delete_combo/{ids['cb']}", "cb", ComboOption),
                (f"/admin/remove_reward/{ids['rwd']}", "rwd", MenuItem),
                (f"/admin/delete_coupon/{ids['cp']}", "cp", Coupon),
                (f"/admin/delete_user/{ids['usr']}", "usr", User),
            ):
                r = admin_client.get(path)
                if r.status_code != 405:
                    offenders.append(f"GET {path}={r.status_code}")
                with app.app_context():
                    row = model.query.execution_options(skip_tenant_filter=True).get(ids[key])
                    if row is None:
                        offenders.append(f"GET {path} 已刪除資料")
                    elif key == "rwd" and not row.is_reward:
                        offenders.append(f"GET {path} 已下架紅利商品")
        finally:
            with app.app_context():
                for model, key in ((ComboOption, "cb"), (Coupon, "cp"), (User, "usr"), (MenuItem, "rwd"), (MenuItem, "item")):
                    row = model.query.execution_options(skip_tenant_filter=True).get(ids[key])
                    if row is not None:
                        db.session.delete(row)
                db.session.commit()
        assert not offenders, "刪除/下架路由仍可被 GET 觸發 (可繞過 CSRF，應只允許 POST): " + "; ".join(offenders)
        return "5 個刪除/下架路由以 GET 呼叫皆回 405 且資料完整保留"
    runner.check("13.10", "狀態變更路由不得允許 GET：delete / delete_combo / remove_reward / delete_coupon / delete_user", t13_10)

    # 13.11 跨店隔離掃描：預設店負責人不得讀寫 B 店的資料 (全數應回 404 且資料不變)
    def t13_11():
        with app.app_context():
            tb = Tenant(name="跨店B13X", slug="tenant-b-13x", is_active=True)
            db.session.add(tb)
            db.session.flush()
            tbid = tb.id
            b_item = MenuItem(tenant_id=tbid, name="B店紅利品", category="主餐", price=50, stock=9, total_stock=9,
                              modifiers="none", is_reward=True, reward_points=10)
            b_cp = Coupon(tenant_id=tbid, code="X13BSWEEP", title="B店掃描券", discount_type="fixed", discount_value=5,
                          min_spend=0, reward_points=0, is_reward=False)
            b_usr = User(tenant_id=tbid, name="B店掃描會員", phone="0933130198", photo_path="b13x.jpg", points=50)
            db.session.add_all([b_item, b_cp, b_usr])
            db.session.flush()
            b_cb = ComboOption(tenant_id=tbid, main_item_id=b_item.id, name="B店掃描套餐", additional_price=5)
            o_pending = Order(tenant_id=tbid, user_id=None, table_number="B店", total_price=10, status="Pending", pickup_number=1)
            o_kds = Order(tenant_id=tbid, user_id=None, table_number="B店", total_price=10, status="Pending", pickup_number=2)
            o_done = Order(tenant_id=tbid, user_id=None, table_number="B店", total_price=10, status="Completed", pickup_number=3)
            db.session.add_all([b_cb, o_pending, o_kds, o_done])
            db.session.commit()
            ids = {"item": b_item.id, "cb": b_cb.id, "cp": b_cp.id, "usr": b_usr.id,
                   "o1": o_pending.id, "o2": o_kds.id, "o3": o_done.id}
        csrf = get_client_csrf(admin_client, "/admin")
        hdr = {"X-CSRFToken": csrf}

        def _form(**kw):
            return {"data": dict(kw, csrf_token=csrf)}

        probes = [
            ("POST", f"/admin/edit/{ids['item']}", _form(name="被入侵", price="1")),
            ("POST", f"/admin/edit_combo/{ids['cb']}", _form(main_item_id=str(ids["item"]), name="被入侵", additional_price="1")),
            ("POST", f"/admin/edit_coupon/{ids['cp']}", _form(code="HACKED13", title="被入侵", discount_type="fixed", discount_value="1")),
            ("POST", f"/admin/edit_user/{ids['usr']}", _form(name="被入侵", phone="0933130197", points="9999")),
            ("POST", f"/admin/delete/{ids['item']}", _form()),
            ("POST", f"/admin/delete_combo/{ids['cb']}", _form()),
            ("POST", f"/admin/remove_reward/{ids['item']}", _form()),
            ("POST", f"/admin/delete_coupon/{ids['cp']}", _form()),
            ("POST", f"/admin/delete_user/{ids['usr']}", _form()),
            ("POST", f"/admin/update_order_status/{ids['o1']}", {"json": {"status": "Completed"}}),
            ("POST", f"/api/kitchen_update_status/{ids['o2']}", {"json": {"status": "Completed"}}),
            ("POST", f"/api/mark_picked_up/{ids['o3']}", {}),
        ]
        offenders = []
        for _m, path, kw in probes:
            r = admin_client.post(path, headers=hdr, **kw)
            if r.status_code != 404:
                offenders.append(f"{path}={r.status_code}")
        with app.app_context():
            q = lambda model, i: model.query.execution_options(skip_tenant_filter=True).get(i)
            it, cb, cp, us = q(MenuItem, ids["item"]), q(ComboOption, ids["cb"]), q(Coupon, ids["cp"]), q(User, ids["usr"])
            assert it is not None and cb is not None and cp is not None and us is not None, "B 店資料被跨店刪除"
            assert (it.name, it.price, it.is_reward) == ("B店紅利品", 50, True), f"B 店品項被跨店修改: {(it.name, it.price, it.is_reward)}"
            assert cb.name == "B店掃描套餐", f"B 店套餐被跨店修改: {cb.name}"
            assert (cp.code, cp.title) == ("X13BSWEEP", "B店掃描券"), f"B 店優惠券被跨店修改: {(cp.code, cp.title)}"
            assert (us.name, us.points) == ("B店掃描會員", 50), f"B 店會員被跨店修改: {(us.name, us.points)}"
            st = [q(Order, ids[k]).status for k in ("o1", "o2", "o3")]
            assert st == ["Pending", "Pending", "Completed"], f"B 店訂單狀態被跨店修改: {st}"
        assert not offenders, "跨店操作未被租戶隔離攔截 (應回 404): " + "; ".join(offenders)
        return "12 個依 ID 操作的路由對 B 店資料全數回 404，且 B 店資料完全未變"
    runner.check("13.11", "跨店隔離掃描：編輯/刪除/下架/訂單狀態/廚房/核銷路由不得操作 B 店資料", t13_11)

    # 13.12 未登入存取掃描：所有後台/POS/廚房路由匿名一律被攔截
    def t13_12():
        anon = app.test_client()
        anon.get("/?shop=default")
        csrf = get_client_csrf(anon, "/")
        hdr = {"X-CSRFToken": csrf}
        clear_rate_buckets()
        gets = ["/pos", "/kitchen", "/pickup", "/admin/staff", "/api/kitchen_orders", "/api/admin_live_orders"]
        posts = [
            "/api/pos/member_lookup", "/api/pos/set_member_pin", "/api/pos/verify_promo", "/api/pos/checkout",
            "/api/kitchen_update_status/1", "/api/kitchen_complete_all", "/api/mark_picked_up/1",
            "/admin/staff", "/admin/add", "/admin/edit/1", "/admin/delete/1", "/admin/add_combo", "/admin/edit_combo/1",
            "/admin/delete_combo/1", "/admin/add_reward", "/admin/remove_reward/1", "/admin/add_coupon",
            "/admin/delete_coupon/1", "/admin/edit_coupon/1", "/admin/delete_user/1", "/admin/edit_user/1",
            "/admin/toggle_user_coupon/1", "/admin/update_reward_setting", "/admin/update_order_status/1",
            "/admin/restore_backup", "/admin/export_excel", "/admin/import_excel", "/admin/import_smart",
            "/api/generate_item_description",
        ]
        allowed = (301, 302, 303, 401, 403)
        offenders = []
        for p in gets:
            r = anon.get(p)
            if r.status_code not in allowed:
                offenders.append(f"GET {p}={r.status_code}")
        for p in posts:
            r = anon.post(p, json={}, headers=hdr)
            if r.status_code not in allowed:
                offenders.append(f"POST {p}={r.status_code}")
        assert not offenders, "匿名使用者可進入受保護路由: " + "; ".join(offenders)
        return f"匿名對 {len(gets)} 個 GET 與 {len(posts)} 個 POST 受保護路由全數被攔截 (302/401/403)"
    runner.check("13.12", "未登入存取掃描：後台 / POS / 廚房 / 核銷 / 匯入匯出路由匿名必被攔截", t13_12)

    # 13.13 /admin/update_reward_setting 非法輸入不得造成 500，且數值需被夾限
    def t13_13():
        snap = _reward_setting_snapshot13()
        try:
            csrf = get_client_csrf(admin_client, "/admin")
            r = admin_client.post(
                "/admin/update_reward_setting",
                data={"is_enabled": "1", "points_per_dollar": "abc", "max_discount_per_order": "-5",
                      "spend_per_point": "0", "csrf_token": csrf},
                headers={"X-CSRFToken": csrf},
            )
            assert r.status_code == 302, f"非法輸入應被安全處理並導回 (302)，實際 {r.status_code}"
            with app.app_context():
                rs = RewardSetting.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=_tid13()).first()
                vals = (rs.points_per_dollar, rs.max_discount_per_order, rs.spend_per_point)
                assert vals == (1, 0, 1), f"非法值夾限結果錯誤，預期 (1, 0, 1)，實際 {vals}"
        finally:
            _restore_reward_setting13(snap)
        return "非數字/負數/零 皆不產生 500，並夾限為 (1, 0, 1)"
    runner.check("13.13", "紅利設定非法輸入防呆：非數字、負數與 0 不得造成 500", t13_13)

    # 13.14 /rewards 紅利商城頁面：訪客導向登入、會員可見紅利商品且不含一般商品
    def t13_14():
        tid = _tid13()
        with app.app_context():
            db.session.add_all([
                MenuItem(tenant_id=tid, name="模組13紅利品Z", category="主餐", price=10, stock=5, total_stock=5,
                         modifiers="none", is_reward=True, reward_points=5),
                MenuItem(tenant_id=tid, name="模組13一般品Z", category="主餐", price=10, stock=5, total_stock=5,
                         modifiers="none", is_reward=False),
            ])
            db.session.commit()
        try:
            anon = app.test_client()
            anon.get("/?shop=default")
            r_anon = anon.get("/rewards")
            assert r_anon.status_code == 302 and "face_login" in r_anon.headers.get("Location", ""), \
                f"訪客應導向 /face_login，實際 {r_anon.status_code} {r_anon.headers.get('Location')}"
            c = _login13()
            r = c.get("/rewards")
            html = r.get_data(as_text=True)
            assert r.status_code == 200, f"會員進入紅利商城失敗: {r.status_code}"
            assert "模組13紅利品Z" in html, "紅利商城未顯示紅利商品"
            assert "模組13一般品Z" not in html, "紅利商城混入非紅利商品"
        finally:
            with app.app_context():
                for nm in ("模組13紅利品Z", "模組13一般品Z"):
                    row = MenuItem.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=tid, name=nm).first()
                    if row is not None:
                        db.session.delete(row)
                db.session.commit()
        return "訪客 302 導向登入；會員 200 且只顯示紅利商品"
    runner.check("13.14", "紅利商城頁面 (/rewards)：訪客導向、會員只看到紅利商品", t13_14)

    # 13.15 /api/orders_stream：SSE 標頭、首筆事件格式與連線釋放
    def t13_15():
        from app.services.event_bus import order_event_bus
        with order_event_bus._lock:
            order_event_bus._subscribers.clear()
        c = app.test_client()
        c.get("/?shop=default")
        resp = c.get("/api/orders_stream", buffered=False)
        try:
            assert resp.status_code == 200, f"SSE 狀態碼應為 200，實際 {resp.status_code}"
            assert resp.mimetype == "text/event-stream", f"SSE Content-Type 錯誤: {resp.mimetype}"
            assert "no-cache" in (resp.headers.get("Cache-Control") or ""), "SSE 缺少 Cache-Control: no-cache"
            first = next(iter(resp.response))
            first = first.decode("utf-8") if isinstance(first, bytes) else first
            assert first.startswith("data: ") and first.endswith("\n\n"), f"首筆 SSE 事件格式錯誤: {first!r}"
            with order_event_bus._lock:
                active = len(order_event_bus._subscribers)
            assert active == 1, f"連線期間訂閱數應為 1，實際 {active}"
        finally:
            resp.close()
        with order_event_bus._lock:
            left = len(order_event_bus._subscribers)
            order_event_bus._subscribers.clear()
        assert left == 0, f"連線關閉後訂閱未釋放 (剩 {left} 條)"
        return "SSE 200、text/event-stream、首筆 data 事件格式正確，關閉後訂閱釋放"
    runner.check("13.15", "即時串流 (/api/orders_stream)：標頭、首筆事件格式與連線釋放", t13_15)

    # 13.16 /face_login 與 /phone_login 的頻率限制 (各 15 次 / 60 秒)
    def t13_16():
        os.environ["DISABLE_RATE_LIMIT"] = "0"
        clear_rate_buckets()
        try:
            c = app.test_client()
            c.get("/?shop=default")
            csrf = get_client_csrf(c, "/register")
            phone_codes = []
            for i in range(17):
                # 每次換不同手機，避免觸發「PIN 連錯 5 次鎖定」的 429 干擾判讀
                r = c.post("/phone_login", json={"phone": f"0900{i:06d}", "pin": "0000"},
                           headers={"X-CSRFToken": csrf, "X-Forwarded-For": "203.0.113.213"})
                phone_codes.append(r.status_code)
            assert 429 not in phone_codes[:15] and 429 in phone_codes[15:], f"/phone_login 限流未在第 16 次觸發: {phone_codes}"
            clear_rate_buckets()
            face_codes = []
            for _ in range(17):
                r = c.post("/face_login", data={}, headers={"X-CSRFToken": csrf, "X-Forwarded-For": "203.0.113.214"})
                face_codes.append(r.status_code)
            assert 429 not in face_codes[:15] and 429 in face_codes[15:], f"/face_login 限流未在第 16 次觸發: {face_codes}"
        finally:
            clear_rate_buckets()
        return f"/phone_login 前15次≠429、之後 {phone_codes[15:]}；/face_login 前15次≠429、之後 {face_codes[15:]}"
    runner.check("13.16", "會員登入頻率限制：/phone_login 與 /face_login 超過 15 次/分鐘回 429", t13_16)

    # 13.17 /register：GET 版面與各類缺漏/格式錯誤/重複電話防呆
    def t13_17():
        _make_member13()
        clear_rate_buckets()
        c = app.test_client()
        c.get("/?shop=default")
        assert c.get("/register").status_code == 200, "GET /register 未回 200"
        csrf = get_client_csrf(c, "/register")
        hdr = {"X-CSRFToken": csrf}

        def _post(**fields):
            data = {k: v for k, v in fields.items() if k != "photo"}
            if "photo" in fields:
                data["photo"] = fields["photo"]
            return c.post("/register", data=data, content_type="multipart/form-data", headers=hdr)

        def _jpg(name="p.jpg"):
            return (make_test_jpeg_bytes(200, 200, color=(255, 255, 255)), name)

        checks = {
            "缺照片": (_post(name="A", phone="0933130201", pin="1234").status_code, 400),
            "缺姓名": (_post(name="", phone="0933130201", pin="1234", photo=_jpg()).status_code, 400),
            "手機格式錯誤": (_post(name="A", phone="12345", pin="1234", photo=_jpg()).status_code, 400),
            "PIN 格式錯誤": (_post(name="A", phone="0933130201", pin="12", photo=_jpg()).status_code, 400),
            "重複電話": (_post(name="A", phone="0933130013", pin="1234", photo=_jpg()).status_code, 409),
            "損壞圖片": (_post(name="A", phone="0933130202", pin="1234",
                              photo=(io.BytesIO(b"not-an-image"), "bad.jpg")).status_code, 400),
        }
        bad = [f"{k}={got}(預期{exp})" for k, (got, exp) in checks.items() if got != exp]
        assert not bad, "/register 防呆回應碼不符: " + "; ".join(bad)
        with app.app_context():
            assert User.query.execution_options(skip_tenant_filter=True).filter(User.phone.in_(["0933130201", "0933130202"])).count() == 0, \
                "防呆失敗的請求不應建立會員"
        return "GET(200)、缺照片/缺姓名/手機/PIN 格式/損壞圖片(400)、重複電話(409) 且未誤建會員"
    runner.check("13.17", "會員註冊 (/register) 版面與缺漏、格式錯誤、重複電話與損壞圖片防呆", t13_17)

    # 13.18 全域 CSRF 掃描：依路由表自動走訪所有 POST 路由，缺 Token 一律 403
    def t13_18():
        anon = app.test_client()
        anon.get("/?shop=default")
        clear_rate_buckets()
        offenders, total = [], 0
        for rule in app.url_map.iter_rules():
            if rule.endpoint == "static" or "POST" not in rule.methods:
                continue
            path = re.sub(r"<int:[^>]+>", "1", rule.rule)
            path = re.sub(r"<[^>]+>", "x", path)
            total += 1
            r = anon.post(path, json={})
            if r.status_code != 403:
                offenders.append(f"{path}={r.status_code}")
        assert not offenders, "下列 POST 路由缺少 CSRF Token 時未回 403: " + "; ".join(offenders)
        return f"路由表中 {total} 個 POST 路由缺 Token 全數回 403"
    runner.check("13.18", "全域 CSRF 掃描：依路由表自動驗證每個 POST 路由缺 Token 必回 403", t13_18)

    # ==========================================================================
    # 模組 12：多執行緒高併發搶購、點數防雙花、POS 併發結帳與真實 HTTP 壓力測試
    # ==========================================================================
    runner.start_suite("模組 12：高併發搶購、點數防雙花、POS 同時結帳與真實 HTTP/SSE 壓力測試")

    # 進入高併發壓力測試前開啟 DISABLE_RATE_LIMIT="1"，避免 30/60 人同時搶購被單 IP 限流干擾
    os.environ["DISABLE_RATE_LIMIT"] = "1"
    clear_rate_buckets()

    # 12.1 記憶體內 12 執行緒同時搶購 5 份限量商品
    def t12_1():
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            limited = MenuItem(
                tenant_id=t1.id, name="限量龍蝦堡", category="主餐", price=300,
                stock=5, total_stock=5, is_sold_out=False, modifiers="none",
            )
            db.session.add(limited)
            db.session.commit()
            lim_id = limited.id

        def _worker(idx):
            c = app.test_client()
            c.get("/?shop=default")
            c.get("/guest_login")
            csrf = get_client_csrf(c, "/")
            resp = c.post(
                "/submit_order",
                json={
                    "items": [{"id": lim_id, "name": "限量龍蝦堡", "price": 300, "quantity": 1}],
                    "payment_method": "Cash",
                    "order_type": "內用",
                },
                headers={"X-CSRFToken": csrf, "X-Forwarded-For": f"10.70.1.{idx + 1}"},
            )
            return resp.status_code, resp.get_json() or {}

        results = []
        with ThreadPoolExecutor(max_workers=12) as ex:
            futs = [ex.submit(_worker, i) for i in range(12)]
            for f in as_completed(futs):
                results.append(f.result())

        ok_list = [d for code, d in results if code == 200 and d.get("order_id")]
        blocked_list = [d for code, d in results if code == 400 and d.get("insufficient_stock")]
        pickups = [d["pickup_number"] for d in ok_list]

        with app.app_context():
            final_stock = db.session.get(MenuItem, lim_id).stock

        assert len(ok_list) == 5, f"成功筆數應為 5 筆，實際為 {len(ok_list)} 筆"
        assert len(blocked_list) == 7, f"攔截筆數應為 7 筆，實際為 {len(blocked_list)} 筆"
        assert final_stock == 0, f"最終庫存應為 0，實際為 {final_stock}"
        assert len(pickups) == len(set(pickups)), f"取餐號發生重複: {pickups}"
        return f"12人搶5份 -> 成交5筆、攔截7筆、最終庫存=0、取餐號={sorted(pickups)}"
    runner.check("12.1", "記憶體內 12 執行緒同時搶購 5 份限量商品：防超賣與取餐號互斥驗證", t12_1)

    # 12.2 會員點數高併發防雙重花費 (Double-Spending)：100 點同時發起 10 筆扣 60 點請求
    def t12_2():
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            u_ds = User(
                tenant_id=t1.id, name="防雙花測試員", phone="0977000111",
                photo_path="ds.jpg", points=100,
            )
            u_ds.set_pin("1111")
            rew_item = MenuItem(
                tenant_id=t1.id, name="60點高價兌換餐", category="點心", price=120,
                stock=50, total_stock=50, is_sold_out=False,
                is_reward=True, reward_points=60, modifiers="none",
            )
            db.session.add_all([u_ds, rew_item])
            db.session.commit()
            ds_uid = u_ds.id
            rew_item_id = rew_item.id

        def _redeem_worker(idx):
            c = app.test_client()
            with c.session_transaction() as sess:
                sess["tenant_id"] = 1
                sess["tenant_slug"] = "default"
                sess["user_id"] = ds_uid
                sess["user_name"] = "防雙花測試員"
                sess["csrf_token"] = "ds_csrf_tok"
                sess["_csrf_token"] = "ds_csrf_tok"
            resp = c.post(
                "/api/redeem_reward",
                json={"item_id": rew_item_id},
                headers={"X-CSRFToken": "ds_csrf_tok", "X-Forwarded-For": f"10.70.2.{idx + 1}"},
            )
            return resp.status_code, resp.get_json() or {}

        results = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(_redeem_worker, i) for i in range(10)]
            for f in as_completed(futs):
                results.append(f.result())

        ok_redeems = [d for code, d in results if code == 200 and d.get("success")]
        blocked_redeems = [d for code, d in results if code == 400]

        with app.app_context():
            rem_pts = db.session.get(User, ds_uid).points

        assert len(ok_redeems) == 1, f"100 點同時扣 60 點應僅 1 筆成功，實際成功 {len(ok_redeems)} 筆"
        assert len(blocked_redeems) == 9, f"應攔截 9 筆，實際攔截 {len(blocked_redeems)} 筆"
        assert rem_pts == 40, f"剩餘點數應為 40 點(絕無負數或雙花)，實際為 {rem_pts} 點"
        return f"10執行緒同時扣60點(原100點) -> 僅1筆成功、9筆攔截、剩餘點數={rem_pts}點"
    runner.check("12.2", "會員點數高併發防雙重花費 (Double-Spending)：100點同時扣60點原子保護", t12_2)

    # 12.3 櫃檯 POS 多收銀台同時結帳 + 同時取消訂單競態測試
    def t12_3():
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            pos_staff = StaffAccount.query.filter_by(username="pos_user").first()
            mgr_staff = StaffAccount.query.filter_by(username="mgr_user").first()
            pos_staff_id = pos_staff.id
            mgr_staff_id = mgr_staff.id

            race_item = MenuItem(
                tenant_id=t1.id, name="POS併發測試餐", category="主餐", price=100,
                stock=20, total_stock=20, is_sold_out=False, modifiers="none",
            )
            db.session.add(race_item)
            db.session.commit()
            race_item_id = race_item.id

        # 先建立 4 筆待取消的 POS 訂單 (佔用 4 份庫存 -> 庫存剩 16)
        pre_orders = []
        for i in range(4):
            c = app.test_client()
            with c.session_transaction() as sess:
                sess["admin_logged_in"] = True
                sess["staff_account_id"] = pos_staff_id
                sess["admin_username"] = "pos_user"
                sess["admin_role"] = "staff"
                sess["staff_role"] = "staff"
                sess["tenant_id"] = 1
                sess["tenant_slug"] = "default"
                sess["csrf_token"] = "pos_race_tok"
                sess["_csrf_token"] = "pos_race_tok"
            r = c.post(
                "/api/pos/checkout",
                json={
                    "items": [{"id": race_item_id, "name": "POS併發測試餐", "quantity": 1}],
                    "payment_method": "Cash",
                    "order_type": "內用",
                    "cash_received": 100,
                },
                headers={"X-CSRFToken": "pos_race_tok"},
            )
            assert r.status_code == 200
            pre_orders.append(r.get_json()["order_id"])

        # 同時啟動：6 個 POS 收銀執行緒結帳新單 (-6 份) + 4 個店長執行緒取消上述 4 張舊單 (+4 份)
        # 最終淨庫存應為：16 - 6 + 4 = 14 份！
        def _pos_checkout_task(idx):
            c = app.test_client()
            with c.session_transaction() as sess:
                sess["admin_logged_in"] = True
                sess["staff_account_id"] = pos_staff_id
                sess["admin_username"] = "pos_user"
                sess["admin_role"] = "staff"
                sess["staff_role"] = "staff"
                sess["tenant_id"] = 1
                sess["tenant_slug"] = "default"
                sess["csrf_token"] = "pos_race_tok"
                sess["_csrf_token"] = "pos_race_tok"
            r = c.post(
                "/api/pos/checkout",
                json={
                    "items": [{"id": race_item_id, "name": "POS併發測試餐", "quantity": 1}],
                    "payment_method": "Cash",
                    "order_type": "內用",
                    "cash_received": 100,
                },
                headers={"X-CSRFToken": "pos_race_tok", "X-Forwarded-For": f"10.70.3.{idx + 1}"},
            )
            return ("checkout", r.status_code, r.get_json() or {})

        def _cancel_task(oid):
            c = app.test_client()
            with c.session_transaction() as sess:
                sess["admin_logged_in"] = True
                sess["staff_account_id"] = mgr_staff_id
                sess["admin_username"] = "mgr_user"
                sess["admin_role"] = "manager"
                sess["staff_role"] = "manager"
                sess["tenant_id"] = 1
                sess["tenant_slug"] = "default"
                sess["csrf_token"] = "mgr_race_tok"
                sess["_csrf_token"] = "mgr_race_tok"
            r = c.post(
                f"/api/kitchen_update_status/{oid}",
                json={"status": "Cancelled"},
                headers={"X-CSRFToken": "mgr_race_tok"},
            )
            return ("cancel", r.status_code, r.get_json() or {})

        race_results = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = [ex.submit(_pos_checkout_task, i) for i in range(6)] + [
                ex.submit(_cancel_task, oid) for oid in pre_orders
            ]
            for f in as_completed(futs):
                race_results.append(f.result())

        chk_ok = [d for kind, code, d in race_results if kind == "checkout" and code == 200 and d.get("success")]
        can_ok = [d for kind, code, d in race_results if kind == "cancel" and code == 200 and d.get("success")]
        new_pickups = [d["pickup_number"] for d in chk_ok]

        with app.app_context():
            final_race_stock = db.session.get(MenuItem, race_item_id).stock

        assert len(chk_ok) == 6 and len(can_ok) == 4, f"POS 併發建單({len(chk_ok)})或同時取消({len(can_ok)})失敗"
        assert final_race_stock == 14, f"併發建單與取消後庫存應為 14 (20-4-6+4)，實際為 {final_race_stock}"
        assert len(new_pickups) == len(set(new_pickups)), f"POS 併發取餐號重複: {new_pickups}"
        return f"6筆POS併發建單(-6) + 4筆同時取消回補(+4) -> 庫存精準=14、取餐號全唯一"
    runner.check("12.3", "櫃檯 POS 多收銀台同時結帳 + 同時取消訂單競態與庫存平衡測試", t12_3)

    # 啟動內嵌真實多執行緒 HTTP 伺服器以執行 test_lock.py 實體網路壓測 (12.4 & 12.5)
    import requests
    from werkzeug.serving import make_server, WSGIRequestHandler
    from app.services.event_bus import order_event_bus

    class _QuietWSGIHandler(WSGIRequestHandler):
        def log_request(self, *args, **kwargs):
            pass

        def log_error(self, *args, **kwargs):
            pass

        def log_message(self, *args, **kwargs):
            pass

    live_server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=_QuietWSGIHandler)
    live_server.daemon_threads = True
    live_server.block_on_close = False
    live_port = live_server.server_port
    live_base_url = f"http://127.0.0.1:{live_port}"
    server_thread = threading.Thread(target=live_server.serve_forever, daemon=True)
    server_thread.start()

    def _calc_latency_summary(latencies_ms):
        if not latencies_ms:
            return "N/A"
        lat_sorted = sorted(latencies_ms)
        p50 = lat_sorted[int(len(lat_sorted) * 0.50)]
        p95 = lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * 0.95))]
        p99 = lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * 0.99))]
        avg = statistics.mean(lat_sorted)
        return f"平均 {avg:.1f}ms | P50:{p50:.1f}ms | P95:{p95:.1f}ms | P99:{p99:.1f}ms"

    try:
        # 12.4 真實 HTTP 網路壓測（階段 1）：背景 8 條 SSE 長連線 + 60 位同時在線瀏覽測試
        def t12_4():
            sse_streams = int(os.environ.get("STRESS_SSE_STREAMS", "8"))
            browse_users = int(os.environ.get("STRESS_BROWSE_USERS", "60"))
            stop_sse = threading.Event()
            active_sse = 0
            sse_lock = threading.Lock()

            with requests.Session() as admin_sess:
                r_login_page = admin_sess.get(f"{live_base_url}/admin", timeout=10)
                l_csrf = extract_csrf(r_login_page.text)
                admin_sess.post(
                    f"{live_base_url}/admin",
                    data={"username": "admin_test", "password": "AdminPass!2026", "csrf_token": l_csrf},
                    timeout=10,
                )
                admin_cookies = admin_sess.cookies.get_dict()

            def _sse_worker(idx):
                nonlocal active_sse
                try:
                    with requests.get(
                        f"{live_base_url}/api/orders_stream",
                        cookies=admin_cookies,
                        headers={"X-Forwarded-For": f"10.88.0.{idx + 1}"},
                        stream=True,
                        timeout=15,
                    ) as resp:
                        if resp.status_code == 200:
                            with sse_lock:
                                active_sse += 1
                            next(resp.iter_lines(), None)
                            stop_sse.wait(timeout=10.0)
                except Exception:
                    pass

            for i in range(sse_streams):
                t = threading.Thread(target=_sse_worker, args=(i,), daemon=True)
                t.start()
            time.sleep(0.3)

            def _simulate_browsing_user(user_idx):
                t0 = time.perf_counter()
                try:
                    with requests.Session() as s:
                        r1 = s.get(
                            f"{live_base_url}/guest_login",
                            headers={"X-Forwarded-For": f"10.89.{user_idx // 250}.{(user_idx % 250) + 1}"},
                            timeout=10,
                        )
                        r2 = s.get(
                            f"{live_base_url}/api/counter_orders",
                            headers={"X-Forwarded-For": f"10.89.{user_idx // 250}.{(user_idx % 250) + 1}"},
                            timeout=10,
                        )
                        elapsed_ms = (time.perf_counter() - t0) * 1000.0
                        ok = r1.status_code == 200 and r2.status_code == 200
                        return {"ok": ok, "status": r1.status_code, "ms": elapsed_ms}
                except Exception as e:
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    return {"ok": False, "status": str(e), "ms": elapsed_ms}

            t_start = time.perf_counter()
            browse_results = []
            with ThreadPoolExecutor(max_workers=min(browse_users, 32)) as ex:
                futs = [ex.submit(_simulate_browsing_user, i) for i in range(browse_users)]
                for fut in as_completed(futs):
                    browse_results.append(fut.result())
            t_browse_total = time.perf_counter() - t_start

            stop_sse.set()
            for q in list(getattr(order_event_bus, "_subscribers", {}).keys()):
                try:
                    q.put_nowait("{}")
                except Exception:
                    pass

            ok_browse = [r for r in browse_results if r["ok"]]
            rps = (len(browse_results) * 2) / max(0.001, t_browse_total)
            lat_str = _calc_latency_summary([r["ms"] for r in browse_results])

            assert active_sse >= 1, f"SSE 背景串流未能成功建立 (active={active_sse})"
            assert len(ok_browse) == len(browse_results), f"在線瀏覽成功率未達 100%: {len(ok_browse)}/{len(browse_results)}"
            return f"活躍SSE={active_sse}條 | {len(ok_browse)}/{len(browse_results)}人瀏覽成功 ({rps:.1f} req/s) | {lat_str}"
        runner.check("12.4", "真實 HTTP 壓測 (階段 1)：背景 8 條 SSE 長連線 + 60 位同時在線瀏覽與延遲統計", t12_4)

        # 12.5 真實 HTTP 網路壓測（階段 2）：30 位併發顧客搶購 10 份限量和牛漢堡
        def t12_5():
            checkout_users = int(os.environ.get("STRESS_CHECKOUT_USERS", "30"))
            target_stock = int(os.environ.get("STRESS_STOCK", "10"))

            with app.app_context():
                t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
                tid = t1.id if t1 else 1
                item = MenuItem.query.execution_options(skip_tenant_filter=True).filter_by(
                    tenant_id=tid, name="【壓測專用】限量和牛漢堡"
                ).first()
                if not item:
                    item = MenuItem(
                        tenant_id=tid,
                        name="【壓測專用】限量和牛漢堡",
                        category="主餐",
                        price=150,
                        stock=target_stock,
                        total_stock=target_stock,
                        is_sold_out=False,
                        modifiers="none",
                    )
                    db.session.add(item)
                else:
                    item.stock = target_stock
                    item.total_stock = target_stock
                    item.is_sold_out = False
                db.session.commit()
                item_id = item.id
                item_name = item.name

            def _simulate_checkout_user(user_idx):
                t0 = time.perf_counter()
                client_ip = f"10.90.{user_idx // 250}.{(user_idx % 250) + 1}"
                try:
                    with requests.Session() as s:
                        s.get(f"{live_base_url}/?shop=default", headers={"X-Forwarded-For": client_ip}, timeout=10)
                        r_login = s.get(f"{live_base_url}/guest_login", headers={"X-Forwarded-For": client_ip}, timeout=10)
                        csrf_token = extract_csrf(r_login.text)
                        headers = {
                            "Content-Type": "application/json",
                            "X-CSRFToken": csrf_token,
                            "X-Forwarded-For": client_ip,
                        }
                        payload = {
                            "items": [
                                {
                                    "id": item_id,
                                    "name": item_name,
                                    "price": 150,
                                    "quantity": 1,
                                    "customization": "",
                                    "is_add_on": False,
                                }
                            ],
                            "payment_method": "Cash",
                            "order_type": "內用",
                            "need_cutlery": False,
                            "note": f"壓測併發顧客 #{user_idx}",
                            "promo_code": "",
                            "use_points": 0,
                        }
                        r_order = s.post(f"{live_base_url}/submit_order", json=payload, headers=headers, timeout=15)
                        elapsed_ms = (time.perf_counter() - t0) * 1000.0
                        data = r_order.json() if r_order.headers.get("content-type", "").startswith("application/json") else {}
                        return {
                            "status_code": r_order.status_code,
                            "ms": elapsed_ms,
                            "order_id": data.get("order_id"),
                            "pickup_number": data.get("pickup_number"),
                            "insufficient_stock": data.get("insufficient_stock", False),
                            "msg": data.get("message") or data.get("error") or "",
                        }
                except Exception as e:
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    return {
                        "status_code": 0,
                        "ms": elapsed_ms,
                        "order_id": None,
                        "pickup_number": None,
                        "insufficient_stock": False,
                        "msg": str(e),
                    }

            checkout_results = []
            with ThreadPoolExecutor(max_workers=min(checkout_users, 30)) as ex:
                futs = [ex.submit(_simulate_checkout_user, i) for i in range(checkout_users)]
                for fut in as_completed(futs):
                    checkout_results.append(fut.result())

            success_orders = [r for r in checkout_results if r["status_code"] == 200 and r["order_id"]]
            sold_out_blocked = [r for r in checkout_results if r["status_code"] == 400 and r["insufficient_stock"]]
            rate_limited = [r for r in checkout_results if r["status_code"] == 429]
            pickup_numbers = [r["pickup_number"] for r in success_orders if r["pickup_number"] is not None]
            unique_pickups = set(pickup_numbers)
            lat_str = _calc_latency_summary([r["ms"] for r in checkout_results])

            with app.app_context():
                rem_stock = db.session.get(MenuItem, item_id).stock

            assert len(rate_limited) == 0, f"壓測期間不應觸發 429 限流 (實際 429={len(rate_limited)} 筆)"
            assert len(success_orders) == target_stock, (
                f"成功搶購應精準為 {target_stock} 筆，實際 {len(success_orders)} 筆 "
                f"(攔截={len(sold_out_blocked)}, 限流={len(rate_limited)})"
            )
            assert len(sold_out_blocked) == checkout_users - target_stock, (
                f"庫存不足攔截應精準為 {checkout_users - target_stock} 筆，實際 {len(sold_out_blocked)} 筆"
            )
            assert rem_stock == 0, f"剩餘庫存應為 0，實際為 {rem_stock}"
            assert len(pickup_numbers) == len(unique_pickups), f"取餐號重複: {pickup_numbers}"
            return (
                f"{checkout_users}人搶{target_stock}份 -> 成交{len(success_orders)}筆、"
                f"防超賣攔截{len(sold_out_blocked)}筆、429限流=0筆、取餐號全唯一 | {lat_str}"
            )
        runner.check("12.5", "真實 HTTP 壓測 (階段 2)：30 人同時結帳搶購 10 份限量漢堡、防超賣與取餐號互斥驗證", t12_5)
    finally:
        os.environ["DISABLE_RATE_LIMIT"] = "0"
        if _orig_extract_feature is not None:
            cv_service_mod.extract_feature = _orig_extract_feature
            auth_route_mod.extract_feature = _orig_extract_feature  # <-- 補上這行
        try:
            for q in list(getattr(order_event_bus, "_subscribers", {}).keys()):
                try:
                    q.put_nowait("{}")
                except Exception:
                    pass
            live_server.shutdown()
            live_server.server_close()
        except Exception:
            pass

    elapsed = time.perf_counter() - t_global_start
    runner.print_summary(elapsed)

    # 清理暫存測試目錄
    try:
        shutil.rmtree(TEST_DB_DIR, ignore_errors=True)
        shutil.rmtree(TEST_UPLOAD_MEMBER_DIR, ignore_errors=True)
        shutil.rmtree(TEST_UPLOAD_MENU_DIR, ignore_errors=True)
    except Exception:
        pass

    return 0 if runner.failed == 0 else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="自助點餐與 POS 系統 - 全功能排列組合、資安與高併發壓力測試腳本")
    parser.add_argument("--browse-users", type=int, default=60, help="階段 1 同時在線瀏覽人數 (預設: 60)")
    parser.add_argument("--sse-streams", type=int, default=8, help="階段 1 背景同時開啟的 SSE 看板連線數 (預設: 8)")
    parser.add_argument("--checkout-users", type=int, default=30, help="階段 2 同時搶購結帳人數 (預設: 30)")
    parser.add_argument("--stock", type=int, default=10, help="階段 2 限量測試商品初始庫存 (預設: 10)")
    args, _ = parser.parse_known_args()

    os.environ["STRESS_BROWSE_USERS"] = str(args.browse_users)
    os.environ["STRESS_SSE_STREAMS"] = str(args.sse_streams)
    os.environ["STRESS_CHECKOUT_USERS"] = str(args.checkout_users)
    os.environ["STRESS_STOCK"] = str(args.stock)

    sys.exit(run_all_tests())