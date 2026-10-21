"""
智慧自助點餐、POS 收銀、人臉辨識與多租戶系統 — 全功能與全排列組合自動化驗證腳本
執行方式：
  python test_all_features.py
自訂壓測參數 (選填)：
  python test_all_features.py --browse-users 60 --sse-streams 8 --checkout-users 30 --stock 10

涵蓋 16 大測試模組、116 項細部測試與排列組合：
  • 模組 1 ：系統初始化、資料庫遷移 (舊 Pickle 轉型/重複電話合併/舊訂單補值) 與多租戶隔離 (6 項)
  • 模組 2 ：資安防線、CSRF 反向 403 攔截、RBAC 四級權限矩陣、路徑穿越與相片保護 (6 項)
  • 模組 3 ：菜單 CRUD、刪除路由、紅利商城獨立上下架與三種加購觸發規則 (3 項)
  • 模組 4 ：套餐組合 (Combo) 建立、編輯、刪除、配餐防重複與客製化自動校正 (2 項)
  • 模組 5 ：會員註冊、電腦視覺向量比對、舊會員無 PIN 403、PIN 連續 5 次錯誤鎖定與刪修 (5 項)
  • 模組 6 ：顧客端自助點餐 (/submit_order) 全排列組合、加料查表與付款/用餐白名單 (4 項)
  • 模組 7 ：優惠券 CRUD、紅利兌換憑證 (RewardRedemption)、重複退點攔截與複合折抵 (4 項)
  • 模組 8 ：櫃檯 POS 收銀系統 (/pos) 多筆候選查詢、設 PIN、優惠碼、實收不足與找零稽核 (5 項)
  • 模組 9 ：廚房 KDS、櫃檯核銷、訂單狀態機非法轉換攔截與取消全量回滾 (11 項)
  • 模組 10：營運分析、AI 三層韌性降級、Excel 10 工作表匯出 (含稽核表) 與智慧匯入 (7 項)
  • 模組 11：頻率限制 (Rate Limit 429) 與 SSE 即時串流連線配額 (10/25/60) 獨立驗證 (2 項)
  • 模組 12：多執行緒高併發搶購、點數防雙花、POS 併發結帳與真實 HTTP/SSE 壓力測試 (5 項)
  • 模組 13：補強覆蓋 — 未測路由 (/face_login、/api/verify_promo、/my_orders、/shop/<slug> 等)、免登入外洩、角色越權(含 owner/manager 回歸)、GET 刪除封鎖、跨店隔離掃描、匿名/CSRF 全路由掃描、限流與 SSE (18 項)
  • 模組 14：高風險修補回歸 — 特徵維度驗證、來源 IP 不可偽造與 PIN 帳號/IP 鎖定、備份隔離與還原安全 (含 Windows 檔案占用備援)、XSS 跳脫、兌換憑證原子性 (9 項)
  • 模組 15：中低風險修補回歸 — 結帳售完/取消券與點數/金額一致/送單冪等、叫號看板遮罩與訪客訂單歸戶、後台差額編輯與表單解析、Excel 防公式注入、安全標頭與 CSP、金鑰與預設帳號、Redis 中斷降級、SSE 淘汰、照片保護、註冊防重複人臉、上傳限制、紅利設定唯一、稽核精確記錄、營運分析口徑、換日自動結案、預估出餐排除極端值、熱門排行銷售額 (27 項)
  • 模組 16：舊版備份還原後即時升級 (固定最後執行：會以舊結構資料庫取代目前資料庫) (2 項)
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
from datetime import datetime, timedelta
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
Config.ADMIN_CREDENTIALS_GENERATED = False
Config.ADMIN_PASSWORD_HASH = generate_password_hash("AdminPass!2026")


class TestConfig(Config):
    TESTING = False
    DATABASE_DIR = TEST_DB_DIR
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + TEST_DB_PATH
    UPLOAD_FOLDER_MEMBER = TEST_UPLOAD_MEMBER_DIR
    UPLOAD_FOLDER_MENU = TEST_UPLOAD_MENU_DIR
    ADMIN_USERNAME = "admin_test"
    ADMIN_PASSWORD = "AdminPass!2026"
    ADMIN_CREDENTIALS_GENERATED = False
    ADMIN_PASSWORD_HASH = Config.ADMIN_PASSWORD_HASH
    # 測試以 X-Forwarded-For 模擬不同來源 IP，等同「前面有一層反向代理」；正式環境預設為 0 (不信任)
    TRUSTED_PROXY_COUNT = 1


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
            print(f"\n🎉 恭喜！所有 {len(self.suite_stats)} 大模組、{self.total} 項功能細節、資安與高併發壓測、各項修補回歸測試 100% 通過！")


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

     # 1.2b 無法解析的舊特徵：遷移不得覆寫為 NULL
    def t1_2b():
        garbage = b"\x80\x04not-a-valid-pickle-or-npf32"
        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            db.session.execute(
                db.text("INSERT INTO user (tenant_id, name, phone, photo_path, feature, points) "
                        "VALUES (:tid, :name, :phone, :photo, :feat, 0)"),
                {"tid": t.id, "name": "壞特徵測試員", "phone": "0910000002",
                 "photo": "bad_feat.jpg", "feat": garbage},
            )
            db.session.commit()

        run_database_migrations(app)

        with app.app_context():
            row = db.session.execute(
                db.text("SELECT feature FROM user WHERE phone = '0910000002'")
            ).fetchone()
            assert row is not None and row[0] is not None, "無法解析的舊特徵被覆寫成 NULL"
            assert bytes(row[0]) == garbage, "無法解析的舊特徵原始內容被改動"
            db.session.execute(db.text("DELETE FROM user WHERE phone = '0910000002'"))
            db.session.commit()
        return "無法解析的舊特徵遷移後原始資料完整保留"
    runner.check("1.2b", "無法解析的舊特徵不得被遷移覆寫為 NULL", t1_2b)
    
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
        for uname, role in [("mgr_user", "manager"), ("pos_user", "staff"), ("kds_user", "kitchen"), ("delete_user", "staff")]:
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
            delete_acc = StaffAccount.query.filter_by(username="delete_user").first()
            owner_id = owner_acc.id
            pos_id = pos_acc.id
            delete_id = delete_acc.id

        # 4. 防自鎖保護：owner 嘗試停用自己必回 400
        r_self_lock = admin_client.post(
            "/admin/staff",
            data={"action": "toggle_active", "account_id": owner_id, "csrf_token": csrf},
        )
        assert r_self_lock.status_code == 400, f"負責人停用自己未被攔截: {r_self_lock.status_code}"
        r_self_delete = admin_client.post(
            "/admin/staff",
            data={"action": "delete_account", "account_id": owner_id, "csrf_token": csrf},
        )
        assert r_self_delete.status_code == 400, f"負責人刪除自己未被攔截: {r_self_delete.status_code}"

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

        r_delete = admin_client.post(
            "/admin/staff",
            data={"action": "delete_account", "account_id": delete_id, "csrf_token": csrf},
        )
        assert r_delete.status_code == 302, f"刪除員工帳號失敗: {r_delete.status_code}"

        with app.app_context():
            assert db.session.get(StaffAccount, delete_id) is None, "已刪除的員工帳號仍存在"
            audits = {a.action_type for a in StaffAuditLog.query.all()}
            assert {"STAFF_CREATE", "STAFF_TOGGLE_ACTIVE", "STAFF_RESET_PASSWORD", "STAFF_DELETE_ACCOUNT"}.issubset(audits), f"員工管理稽核缺失: {audits}"
        return "建立3角色、重複攔截(409)、自我停用/刪除攔截(400)、停用/啟用、重設密碼、刪除與稽核通過"
    runner.check("2.3", "員工帳號建立、停用/啟用、重設/刪除、防自鎖與稽核驗證", t2_3)

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

    # 2.6 系統完整備份包匯出 (/admin/backup_system) 與還原防護 (/admin/restore_backup) 驗證
    def t2_6():
        csrf = get_client_csrf(admin_client, "/admin")
        
        # 1. 測試負責人 (owner) 調用 /admin/backup_system 成功匯出包含實體 menu.db 的 ZIP 備份包
        r_bak = admin_client.post(
            "/admin/backup_system",
            data={"csrf_token": csrf},
            headers={"X-CSRFToken": csrf},
        )
        assert r_bak.status_code == 200, f"完整備份包匯出失敗: {r_bak.status_code}"
        assert r_bak.mimetype == "application/zip", f"備份包 MIME 類型錯誤: {r_bak.mimetype}"
        
        with zipfile.ZipFile(io.BytesIO(r_bak.data), 'r') as zf:
            assert "menu.db" in zf.namelist(), "完整備份包 ZIP 缺少 menu.db 資料庫檔案"

        # 2. 測試店長 (manager) 嘗試下載完整備份包被 403 阻斷（資安防護）
        m_csrf = get_client_csrf(manager_client, "/admin")
        r_mgr_bak = manager_client.post(
            "/admin/backup_system",
            data={"csrf_token": m_csrf},
            headers={"X-CSRFToken": m_csrf},
        )
        assert r_mgr_bak.status_code == 403, f"店長下載完整備份未回傳 403: {r_mgr_bak.status_code}"

        # 3. 測試未上傳實體檔案時，還原路由具備安全阻斷防護 (403)
        r_rest = admin_client.post(
            "/admin/restore_backup",
            data={"csrf_token": csrf},
            headers={"X-CSRFToken": csrf},
        )
        assert r_rest.status_code == 403, f"未選取檔案之還原請求未回傳 403: {r_rest.status_code}"
        return "backup_system(owner 成功打包 menu.db & manager 403 阻斷) 與 restore_backup(安全阻斷 403) 驗證通過"
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

    # 6.2a 加購配額後端驗證 (二分圖配對)：加購不可超過合格正價餐點，且每份加購都要能配到符合觸發條件的餐點
    def t6_2a():
        c = app.test_client()
        c.get("/?shop=default")
        c.get("/guest_login")
        csrf = get_client_csrf(c, "/")
        seq = [40]

        def post(items):
            seq[0] += 1
            return c.post(
                "/submit_order",
                json={"items": items, "payment_method": "Line Pay", "order_type": "內用"},
                headers={"X-CSRFToken": csrf, "X-Forwarded-For": f"10.31.1.{seq[0]}"},
            )

        def line(name, qty=1, is_add_on=False):
            return {"id": menu_ids[name], "name": name, "price": 100, "quantity": qty, "customization": "", "is_add_on": is_add_on}

        r = post([line("招牌和牛堡"), line("松露沾醬", 2, True)])
        assert r.status_code == 400, f"加購數量超過主餐數量應被拒絕: {r.status_code}"
        r = post([line("松露沾醬", 1, True)])
        assert r.status_code == 400, f"沒有任何合格主餐的加購應被拒絕: {r.status_code}"
        r = post([line("美式脆薯"), line("松露沾醬", 1, True)])
        assert r.status_code == 400, f"松露沾醬限搭和牛堡，搭副餐應被拒絕: {r.status_code}"
        # 總量足夠 (2 份加購 / 2 份正價) 但兩份加購都只能搭同一個和牛堡 → 配對失敗
        r = post([line("招牌和牛堡"), line("美式脆薯"), line("錫蘭紅茶", 1, True), line("松露沾醬", 1, True)])
        assert r.status_code == 400, f"總量足夠但無法一對一配對應被拒絕: {r.status_code}"
        # 兩個和牛堡：通用加購 + 單品加購可各配一個 → 合法
        r = post([line("招牌和牛堡", 2), line("錫蘭紅茶", 1, True), line("松露沾醬", 1, True)])
        assert r.status_code == 200, f"可一對一配對的加購應通過: {r.status_code} {r.get_data(as_text=True)}"
        return "加購超量 / 無合格主餐 / 不符單品條件 / 總量足夠但配對失敗 皆回 400，可配對組合通過"
    runner.check("6.2a", "加購配額後端驗證：二分圖配對 (超量、不符條件、配對失敗皆擋下)", t6_2a)

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

    # 6.4 取餐號流水：1~999 循環，達 999 後回繞為 1 並持續遞增（不得卡在 1）
    def t6_4():
        clear_rate_buckets()
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            wrap_item = MenuItem(
                tenant_id=t1.id, name="取餐號回繞測試餐", category="主餐", price=20,
                stock=10, total_stock=10, is_sold_out=False, modifiers="none",
            )
            db.session.add(wrap_item)
            # 模擬「當日最後一張訂單」取餐號已到 999 (Cancelled：不佔用廚房看板)
            db.session.add(Order(
                tenant_id=t1.id, user_id=None, table_number="取餐號回繞前置", total_price=0,
                status="Cancelled", pickup_number=999,
            ))
            db.session.commit()
            wrap_item_id = wrap_item.id

        def _guest_order():
            c = app.test_client()
            c.get("/?shop=default")
            c.get("/guest_login")
            csrf = get_client_csrf(c, "/")
            r = c.post(
                "/submit_order",
                json={
                    "items": [{"id": wrap_item_id, "name": "取餐號回繞測試餐", "quantity": 1}],
                    "payment_method": "Cash",
                    "order_type": "內用",
                },
                headers={"X-CSRFToken": csrf},
            )
            assert r.status_code == 200, f"建立訪客訂單失敗: {r.status_code} {r.get_data(as_text=True)[:120]}"
            return r.get_json()["pickup_number"]

        seq = [_guest_order() for _ in range(3)]
        assert seq == [1, 2, 3], f"取餐號達 999 後應回繞為 1 並持續遞增 (預期 [1, 2, 3])，實際 {seq}"
        return f"當日最後一張為 #999 -> 接續結帳取餐號依序為 {seq}，不再卡在 1"
    runner.check("6.4", "取餐號流水：達 999 後回繞為 1 並持續遞增 (不卡死在 1)", t6_4)

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
    # 9.1 廚房 KDS 出餐、叫號牆同步、櫃檯核銷與 MARK_PICKED_UP 稽核
    def t9_1():
        r_kds = kitchen_client.get("/api/kitchen_orders")
        assert r_kds.status_code == 200
        orders = r_kds.get_json().get("orders", [])
        assert len(orders) >= 1, "廚房 KDS 未讀出待製作訂單"
        first_oid = orders[0]["id"]

        # 廚房透過統一端點標記為 Completed
        k_csrf = get_client_csrf(kitchen_client, "/kitchen")
        r_comp = kitchen_client.post(
            f"/api/orders/{first_oid}/status",
            json={"status": "Completed"},
            headers={"X-CSRFToken": k_csrf},
        )
        assert r_comp.status_code == 200 and r_comp.get_json().get("success") is True

        # 公開叫號看板 API
        anon = app.test_client()
        r_cnt = anon.get("/api/counter_orders")
        assert r_cnt.status_code == 200
        ready_ids = [o["id"] for o in r_cnt.get_json().get("ready_orders", [])]
        assert first_oid in ready_ids, "Completed 訂單未出現在公開叫號看板"

        # 櫃檯人員透過統一端點核銷取餐 (PickedUp)
        s_csrf = get_client_csrf(staff_client, "/pickup")
        r_pick = staff_client.post(
            f"/api/orders/{first_oid}/status",
            json={"status": "PickedUp"},
            headers={"X-CSRFToken": s_csrf},
        )
        assert r_pick.status_code == 200 and r_pick.get_json().get("success") is True
        with app.app_context():
            assert db.session.get(Order, first_oid).status == "PickedUp"
            au = StaffAuditLog.query.filter_by(action_type="MARK_PICKED_UP", target_order_id=first_oid).first()
            assert au is not None, "櫃檯取餐核銷未寫入 MARK_PICKED_UP 稽核紀錄"
        return f"訂單 #{first_oid} Pending -> Completed -> PickedUp(統一端點) 通過"
    runner.check("9.1", "廚房出餐、叫號牆同步、櫃檯核銷與 MARK_PICKED_UP 稽核", t9_1)

    # 9.2 訂單狀態機非法轉換嚴格攔截 (對齊 409 Conflict 慣例)
    def t9_2():
        pos_oid = pos_order_id_holder["id"]
        s_csrf = get_client_csrf(staff_client, "/pickup")
        k_csrf = get_client_csrf(kitchen_client, "/kitchen")
        a_csrf = get_client_csrf(admin_client, "/admin")

        # (a) Pending 未出餐直接嘗試取餐 -> 409 Conflict
        r_premature = staff_client.post(
            f"/api/orders/{pos_oid}/status",
            json={"status": "PickedUp"},
            headers={"X-CSRFToken": s_csrf},
        )
        assert r_premature.status_code == 409, f"Pending 訂單未出餐直接取餐未回 409: {r_premature.status_code}"

        # (b) 建立訪客單並取消為 Cancelled
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
            headers={"X-CSRFToken": c_csrf},
        )
        cancel_oid = r_new.get_json()["order_id"]

        r_cancel1 = manager_client.post(
            f"/api/orders/{cancel_oid}/status",
            json={"status": "Cancelled"},
            headers={"X-CSRFToken": get_client_csrf(manager_client, "/admin")},
        )
        assert r_cancel1.status_code == 200

        # (c) 重複取消同一張已取消訂單 -> 409 Conflict
        r_cancel2 = manager_client.post(
            f"/api/orders/{cancel_oid}/status",
            json={"status": "Cancelled"},
            headers={"X-CSRFToken": get_client_csrf(manager_client, "/admin")},
        )
        assert r_cancel2.status_code == 409, f"重複取消已取消訂單未回 409: {r_cancel2.status_code}"

        # (d) Cancelled 復活為 Completed -> 409 Conflict
        r_revive_kds = manager_client.post(
            f"/api/orders/{cancel_oid}/status",
            json={"status": "Completed"},
            headers={"X-CSRFToken": get_client_csrf(manager_client, "/admin")},
        )
        assert r_revive_kds.status_code == 409, f"Cancelled 訂單復活為 Completed 未回 409: {r_revive_kds.status_code}"

        # (e) Cancelled 復活為 Pending -> 409 Conflict
        r_revive_adm = admin_client.post(
            f"/api/orders/{cancel_oid}/status",
            json={"status": "Pending"},
            headers={"X-CSRFToken": a_csrf},
        )
        assert r_revive_adm.status_code == 409, f"Cancelled 訂單復活為 Pending 未回 409: {r_revive_adm.status_code}"

        # (f) Cancelled 直接取餐 PickedUp -> 409 Conflict
        r_revive_pick = manager_client.post(
            f"/api/orders/{cancel_oid}/status",
            json={"status": "PickedUp"},
            headers={"X-CSRFToken": get_client_csrf(manager_client, "/admin")},
        )
        assert r_revive_pick.status_code == 409, f"Cancelled 訂單直接取餐未回 409: {r_revive_pick.status_code}"
        return "Pending直接取餐(409)、重複取消(409)與Cancelled各類復活(全409) 語意攔截通過"
    runner.check("9.2", "訂單狀態機非法轉換攔截 (409 Conflict 語意對齊與不可逆性)", t9_2)

    # 9.3 取消訂單全量回滾
    def t9_3():
        pos_oid = pos_order_id_holder["id"]
        with app.app_context():
            b_stock_before = db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock
            f_stock_before = db.session.get(MenuItem, menu_ids["美式脆薯"]).stock
            t_stock_before = db.session.get(MenuItem, menu_ids["錫蘭紅茶"]).stock
            u_mei = User.query.filter_by(phone="0912999888").first()
            pts_before = u_mei.points

        a_csrf = get_client_csrf(admin_client, "/admin")
        r_cancel = admin_client.post(
            f"/api/orders/{pos_oid}/status",
            json={"status": "Cancelled"},
            headers={"X-CSRFToken": a_csrf},
        )
        assert r_cancel.status_code == 200

        with app.app_context():
            assert db.session.get(Order, pos_oid).status == "Cancelled"
            assert db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock == b_stock_before + 1
            assert db.session.get(MenuItem, menu_ids["美式脆薯"]).stock == f_stock_before + 1
            assert db.session.get(MenuItem, menu_ids["錫蘭紅茶"]).stock == t_stock_before + 1
            u_mei_after = User.query.filter_by(phone="0912999888").first()
            assert u_mei_after.points == pts_before + 25 - 3
            au = StaffAuditLog.query.filter_by(action_type="CANCEL_ORDER", target_order_id=pos_oid).first()
            assert au is not None and au.points_delta == 22
        return "主餐+雙配餐庫存回補(+1)、會員點數回滾 & 統一端點取消成功"
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

    # 9.5 訪客訂單取消狀態確實流轉為 Cancelled 且二次取消回 409
    def t9_5():
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
            headers={"X-CSRFToken": c_csrf},
        )
        g_oid = r_new.get_json()["order_id"]

        a_csrf = get_client_csrf(admin_client, "/admin")
        r1 = admin_client.post(f"/api/orders/{g_oid}/status", json={"status": "Cancelled"}, headers={"X-CSRFToken": a_csrf})
        assert r1.status_code == 200
        with app.app_context():
            assert db.session.get(Order, g_oid).status == "Cancelled", "訪客訂單取消後狀態未變更為 Cancelled！"

        r2 = admin_client.post(f"/api/orders/{g_oid}/status", json={"status": "Cancelled"}, headers={"X-CSRFToken": a_csrf})
        assert r2.status_code == 409, f"已取消之訪客訂單重複取消未回 409: {r2.status_code}"
        return "訪客訂單狀態=Cancelled 且重複取消回 409 通過"
    runner.check("9.5", "訪客訂單取消狀態流轉與防重複取消回補庫存驗證 (409)", t9_5)

    # 9.6 角色矩陣卡控：廚房人員嘗試取消或取餐必回 403
    def t9_6():
        with app.app_context():
            o = Order.query.filter_by(status="Completed").first()
            target_oid = o.id
        k_csrf = get_client_csrf(kitchen_client, "/kitchen")
        r_illegal = kitchen_client.post(
            f"/api/orders/{target_oid}/status",
            json={"status": "PickedUp"},
            headers={"X-CSRFToken": k_csrf}
        )
        assert r_illegal.status_code == 403, f"廚房人員越權執行 PickedUp 未回 403: {r_illegal.status_code}"
        return "角色矩陣成功阻斷廚房人員執行 PickedUp (403)"
    runner.check("9.6", "角色矩陣卡控：廚房人員嘗試核銷取餐 (PickedUp) 必回 403", t9_6)

    # 9.7 角色矩陣卡控：一般店員嘗試取消已取餐訂單必回 403
    def t9_7():
        with app.app_context():
            o = Order.query.filter_by(status="PickedUp").first()
            picked_oid = o.id
        s_csrf = get_client_csrf(staff_client, "/pos")
        r_cancel_picked = staff_client.post(
            f"/api/orders/{picked_oid}/status",
            json={"status": "Cancelled"},
            headers={"X-CSRFToken": s_csrf}
        )
        assert r_cancel_picked.status_code == 403, f"店員擅自取消已取餐訂單未回 403: {r_cancel_picked.status_code}"
        return "角色矩陣成功阻斷一般店員取消已取餐訂單 (403)"
    runner.check("9.7", "角色矩陣卡控：一般店員嘗試取消已取餐訂單必回 403", t9_7)

    # 9.8 業務完整性：取消紅利兌換訂單之全額點數回補
    def t9_8():
        with app.app_context():
            u = User.query.filter_by(phone="0912345678").first()
            uid = u.id
            init_pts = u.points

        s_csrf = get_client_csrf(staff_client, "/pos")
        r_checkout = staff_client.post(
            "/api/pos/checkout",
            json={
                "user_id": uid,
                "table_number": "退點測試桌",
                "items": [{"id": f"reward_{menu_ids['黃金雞塊']}", "name": "🎁 [點數兌換] 黃金雞塊", "quantity": 1, "is_reward": True}],
                "payment_method": "Cash",
                "order_type": "內用"
            },
            headers={"X-CSRFToken": s_csrf}
        )
        assert r_checkout.status_code == 200
        r_oid = r_checkout.get_json()["order_id"]

        with app.app_context():
            assert db.session.get(User, uid).points == init_pts - 50

        a_csrf = get_client_csrf(admin_client, "/admin")
        r_cancel = admin_client.post(f"/api/orders/{r_oid}/status", json={"status": "Cancelled"}, headers={"X-CSRFToken": a_csrf})
        assert r_cancel.status_code == 200

        with app.app_context():
            final_pts = db.session.get(User, uid).points
            assert final_pts == init_pts, f"取消兌換訂單後點數未全額退還！預期 {init_pts}，實際 {final_pts}"
        return "含有點數兌換商品的訂單取消後，50 點全額回補會員"
    runner.check("9.8", "業務完整性：取消紅利兌換訂單之全額點數回補驗證", t9_8)

    # 9.9 同一張訂單併發重複取消 (SQL 條件式更新 CAS)：只有 1 次成功，其餘 409，庫存與稽核只處理 1 次
    def t9_9():
        n_threads = 8
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            t1_id = t1.id
            mgr_staff_id = StaffAccount.query.filter_by(username="mgr_user").first().id
            cas_item = MenuItem(
                tenant_id=t1_id, name="CAS併發取消測試餐", category="主餐", price=50,
                stock=9, total_stock=10, is_sold_out=False, modifiers="none",
            )
            db.session.add(cas_item)
            db.session.flush()
            cas_item_id = cas_item.id
            # 模擬已售出 1 份 (10 -> 9)；取消一次後庫存應精準回到 10
            cas_order = Order(
                tenant_id=t1_id, user_id=None, table_number="CAS併發取消", total_price=50,
                status="Pending", pickup_number=1,
                created_at=datetime.now() - timedelta(days=1),   # 建立於昨天：不干擾當日取餐號流水與今日營運資料
            )
            db.session.add(cas_order)
            db.session.flush()
            db.session.add(OrderItem(
                tenant_id=t1_id, order_id=cas_order.id, menu_item_id=cas_item_id,
                item_name="CAS併發取消測試餐", price=50, quantity=1, customization="",
            ))
            db.session.commit()
            cas_oid = cas_order.id

        barrier = threading.Barrier(n_threads)

        def _cancel_worker(_idx):
            c = app.test_client()
            with c.session_transaction() as sess:
                sess["admin_logged_in"] = True
                sess["staff_account_id"] = mgr_staff_id
                sess["admin_username"] = "mgr_user"
                sess["admin_role"] = "manager"
                sess["staff_role"] = "manager"
                sess["tenant_id"] = t1_id
                sess["tenant_slug"] = "default"
                sess["csrf_token"] = "cas_race_tok"
                sess["_csrf_token"] = "cas_race_tok"
            barrier.wait(timeout=10)
            r = c.post(
                f"/api/orders/{cas_oid}/status",
                json={"status": "Cancelled"},
                headers={"X-CSRFToken": "cas_race_tok"},
            )
            return r.status_code

        with ThreadPoolExecutor(max_workers=n_threads) as ex:
            codes = [f.result() for f in [ex.submit(_cancel_worker, i) for i in range(n_threads)]]

        with app.app_context():
            final_status = db.session.get(Order, cas_oid).status
            final_stock = db.session.get(MenuItem, cas_item_id).stock
            audit_cnt = StaffAuditLog.query.filter_by(action_type="CANCEL_ORDER", target_order_id=cas_oid).count()

        assert codes.count(200) == 1, f"同一訂單併發取消應僅 1 次成功，實際回應碼: {sorted(codes)}"
        assert all(c == 409 for c in codes if c != 200), f"其餘併發請求應全回 409，實際回應碼: {sorted(codes)}"
        assert final_status == "Cancelled", f"訂單最終狀態應為 Cancelled，實際 {final_status}"
        assert final_stock == 10, f"庫存應只回補 1 次 (9 -> 10)，實際 {final_stock}（>10 代表重複回補）"
        assert audit_cnt == 1, f"CANCEL_ORDER 稽核紀錄應僅 1 筆，實際 {audit_cnt}"
        return f"{n_threads} 執行緒同時取消同一單 -> 成功 1、409 共 {codes.count(409)}，庫存 9->10 僅回補 1 次、稽核 1 筆"
    runner.check("9.9", "併發防護：同一訂單同時重複取消只成功 1 次 (CAS，庫存與稽核不重複)", t9_9)

    # 9.10 角色 × 狀態轉換完整矩陣：4 角色 × 16 種 (當前狀態→目標狀態) 組合逐一驗證
    def t9_10():
        statuses = ["Pending", "Completed", "PickedUp", "Cancelled"]
        legal = {
            "Pending": {"Completed", "Cancelled"},
            "Completed": {"PickedUp", "Cancelled"},
            "PickedUp": {"Cancelled"},
            "Cancelled": set(),
        }
        all_legal_pairs = {(c, t) for c, ts in legal.items() for t in ts}
        role_allowed = {
            "owner": set(all_legal_pairs),
            "manager": set(all_legal_pairs),
            "staff": all_legal_pairs - {("PickedUp", "Cancelled")},
            "kitchen": {("Pending", "Completed")},
        }
        role_clients = {
            "owner": (admin_client, "/admin"),
            "manager": (manager_client, "/admin"),
            "staff": (staff_client, "/pos"),
            "kitchen": (kitchen_client, "/kitchen"),
        }
        tokens = {role: get_client_csrf(cl, path) for role, (cl, path) in role_clients.items()}

        # 一次建立所有測試訂單 (無明細，避免影響庫存)；取餐號需為整數供稽核格式化使用 (取消稽核會格式化 pickup_number)
        case_orders = {}
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            seq = 0
            for role in role_clients:
                for cur in statuses:
                    for tgt in statuses:
                        seq += 1
                        o = Order(
                            tenant_id=t1.id, user_id=None, table_number=f"矩陣{role}", total_price=10,
                            status=cur, pickup_number=1,
                            created_at=datetime.now() - timedelta(days=1),   # 建立於昨天：不干擾當日取餐號流水
                        )
                        db.session.add(o)
                        db.session.flush()
                        case_orders[(role, cur, tgt)] = o.id
            db.session.commit()

        role_targets = {r: {t for (_c, t) in pairs} for r, pairs in role_allowed.items()}
        reachable_targets = {t for ts in legal.values() for t in ts}   # 任何流程皆無法轉入 Pending

        def _expected(role, cur, tgt):
            # 判定順序：目標不可達 (409) -> 角色是否可能執行該目標 (403) -> 同狀態/非法轉換 (409)
            #           -> 細部轉換權限 (403) -> 成功 (200)
            if tgt not in reachable_targets:
                return 409
            if tgt not in role_targets[role]:
                return 403
            if cur == tgt or tgt not in legal[cur]:
                return 409
            if (cur, tgt) not in role_allowed[role]:
                return 403
            return 200

        problems = []
        for (role, cur, tgt), oid in case_orders.items():
            client, _path = role_clients[role]
            r = client.post(
                f"/api/orders/{oid}/status",
                json={"status": tgt},
                headers={"X-CSRFToken": tokens[role]},
            )
            with app.app_context():
                after = db.session.get(Order, oid).status
            exp = _expected(role, cur, tgt)
            tag = f"[{role}] {cur}->{tgt}"
            if r.status_code != exp:
                problems.append(f"{tag} 預期 {exp}，實際 {r.status_code}")
            expected_after = tgt if exp == 200 else cur
            if after != expected_after:
                problems.append(f"{tag} 狀態應為 {expected_after}，實際 {after}")

        assert not problems, f"狀態矩陣有 {len(problems)} 項不符：" + "；".join(problems[:8])
        ok_cnt = sum(len(v) for v in role_allowed.values())
        return f"4 角色 × 16 組合共 {len(case_orders)} 次逐一比對 200/403/409 預期通過（允許 {ok_cnt} 種、其餘皆被拒且狀態不變）"
    runner.check("9.10", "角色 × 狀態轉換完整矩陣 (owner/manager/staff/kitchen × 16 種轉換)", t9_10)

    # 9.11 無權限角色不論訂單狀態/是否存在，回應一律相同 (403)，不得藉由 404/409 探測訂單資訊
    def t9_11():
        k_csrf = get_client_csrf(kitchen_client, "/kitchen")
        probe_ids = {}
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            for st in ("Pending", "Completed", "PickedUp", "Cancelled"):
                o = Order(
                    tenant_id=t1.id, user_id=None, table_number="探測測試", total_price=10,
                    status=st, pickup_number=1,
                    created_at=datetime.now() - timedelta(days=1),
                )
                db.session.add(o)
                db.session.flush()
                probe_ids[st] = o.id
            db.session.commit()

        responses = {}
        for st, oid in probe_ids.items():
            r = kitchen_client.post(
                f"/api/orders/{oid}/status",
                json={"status": "Cancelled"},
                headers={"X-CSRFToken": k_csrf},
            )
            responses[st] = (r.status_code, (r.get_json() or {}).get("message"))
        # 不存在的訂單編號
        r_missing = kitchen_client.post(
            "/api/orders/987654321/status",
            json={"status": "Cancelled"},
            headers={"X-CSRFToken": k_csrf},
        )
        responses["(不存在)"] = (r_missing.status_code, (r_missing.get_json() or {}).get("message"))

        assert all(code == 403 for code, _ in responses.values()), f"無權限角色應一律回 403，實際: {responses}"
        assert len({msg for _, msg in responses.values()}) == 1, f"不同訂單狀態的回應訊息不一致，會洩漏狀態: {responses}"
        with app.app_context():
            unchanged = {st: db.session.get(Order, oid).status for st, oid in probe_ids.items()}
        assert unchanged == {st: st for st in probe_ids}, f"無權限請求不應改動任何訂單狀態: {unchanged}"
        return "廚房對 4 種狀態與不存在的訂單送出取消，全數回應相同 403 訊息、訂單狀態不變"
    runner.check("9.11", "權限優先於狀態判斷：無權限角色無法藉 404/409 探測訂單資訊", t9_11)

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

    # 10.3 智慧匯入 (/admin/import_smart) 支援單一 CSV 菜單與 Excel 多工作表 Upsert 同步驗證
    def t10_3():
        csrf = get_client_csrf(admin_client, "/admin")

        # (a) 測試 /admin/import_smart 支援單一 CSV 菜單匯入：建立「宇治抹茶拿鐵 ($65)」並匯入
        csv_content = "餐點名稱,分類,單價,客製化群組,商品描述\n宇治抹茶拿鐵,飲料,65,ice_sugar,京都宇治抹茶鮮奶\n"
        buf_csv = io.BytesIO(csv_content.encode('utf-8'))

        r_imp_csv = admin_client.post(
            "/admin/import_smart",
            data={"csrf_token": csrf, "file": (buf_csv, "new_menu_items.csv")},
            content_type="multipart/form-data",
        )
        assert r_imp_csv.status_code in (200, 302), f"/admin/import_smart CSV 匯入失敗: {r_imp_csv.status_code}"

        # (b) 測試 /admin/import_smart 支援 Excel 多表智慧 Upsert：修改全店 Excel 中「錫蘭紅茶」單價為 45
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

        r_imp_excel = admin_client.post(
            "/admin/import_smart",
            data={"csrf_token": csrf, "file": (buf_smart, "updated_store_data.xlsx")},
            content_type="multipart/form-data",
        )
        assert r_imp_excel.status_code in (200, 302), f"/admin/import_smart Excel 匯入失敗: {r_imp_excel.status_code}"

        with app.app_context():
            matcha = MenuItem.query.filter_by(name="宇治抹茶拿鐵").first()
            tea = MenuItem.query.filter_by(name="錫蘭紅茶").first()
            assert matcha is not None and matcha.price == 65, "/admin/import_smart 未成功匯入宇治抹茶拿鐵"
            assert tea.price == 45, f"/admin/import_smart 未同步更新錫蘭紅茶價格: {tea.price}"
        return "import_smart 成功支援 CSV 新增'宇治抹茶拿鐵($65)' 與 Excel 更新'錫蘭紅茶($45)'"
    runner.check("10.3", "智慧匯入 (/admin/import_smart) 新增菜單與全店資料 Upsert 同步驗證", t10_3)

    # 10.4 智慧匯入庫存保護：檔案未提供庫存欄位時，既有品項庫存與售完狀態不被覆蓋
    def t10_4():
        csrf = get_client_csrf(admin_client, "/admin")

        def _import_csv(text):
            return admin_client.post(
                "/admin/import_smart",
                data={"csrf_token": csrf, "file": (io.BytesIO(text.encode("utf-8")), "stock_test.csv")},
                content_type="multipart/form-data",
            )

        def _get(name):
            return MenuItem.query.filter_by(name=name).first()

        try:
            # (a) 含庫存欄位新增 -> 庫存=7
            r = _import_csv("餐點名稱,分類,單價,庫存\n庫存保留測試茶,飲料,40,7\n")
            assert r.status_code in (200, 302), f"CSV 匯入失敗: {r.status_code}"
            with app.app_context():
                assert _get("庫存保留測試茶").stock == 7, "含庫存欄位新增品項庫存應為 7"
                it = _get("庫存保留測試茶")
                it.stock = 3            # 模擬已售出 4 份
                it.is_sold_out = False
                db.session.commit()

            # (b) 無庫存欄位更新 -> 價格更新、庫存維持 3；全新品項套用預設庫存 50
            r = _import_csv("餐點名稱,分類,單價\n庫存保留測試茶,飲料,55\n庫存保留測試茶二號,飲料,35\n")
            assert r.status_code in (200, 302)
            with app.app_context():
                it = _get("庫存保留測試茶")
                assert it.price == 55, f"價格應被更新為 55，實際 {it.price}"
                assert it.stock == 3, f"檔案未提供庫存欄位，既有庫存應維持 3，實際 {it.stock}"
                new_it = _get("庫存保留測試茶二號")
                assert new_it is not None and new_it.stock == 50, "全新品項未提供庫存時應套用預設庫存 50"
                it.stock = 0            # 模擬售完
                it.is_sold_out = True
                db.session.commit()

            # (c) 售完品項再次無庫存欄位匯入 -> 仍維持售完 (不得被誤補回)
            r = _import_csv("餐點名稱,分類,單價\n庫存保留測試茶,飲料,60\n")
            assert r.status_code in (200, 302)
            with app.app_context():
                it = _get("庫存保留測試茶")
                assert it.price == 60 and it.stock == 0 and it.is_sold_out is True, \
                    f"售完品項不應被覆蓋: price={it.price}, stock={it.stock}, sold_out={it.is_sold_out}"

            # (d) 明確提供庫存欄位 -> 才允許覆蓋並解除售完
            r = _import_csv("餐點名稱,分類,單價,庫存\n庫存保留測試茶,飲料,60,12\n")
            assert r.status_code in (200, 302)
            with app.app_context():
                it = _get("庫存保留測試茶")
                assert it.stock == 12 and it.is_sold_out is False, \
                    f"明確提供庫存時應覆蓋為 12 並解除售完，實際 stock={it.stock}, sold_out={it.is_sold_out}"
            return "無庫存欄位不覆蓋(3/售完保持)、新品預設 50、明確提供庫存才覆蓋(12) 驗證通過"
        finally:
            with app.app_context():
                for nm in ("庫存保留測試茶", "庫存保留測試茶二號"):
                    obj = _get(nm)
                    if obj:
                        db.session.delete(obj)
                db.session.commit()
    runner.check("10.4", "智慧匯入庫存保護：未提供庫存欄位時既有庫存/售完狀態不被覆蓋", t10_4)

    # 10.5 智慧匯入單一交易原子性：任一菜單工作表解析失敗時整批 rollback，不留半套資料
    def t10_5():
        csrf = get_client_csrf(admin_client, "/admin")

        def _build_xlsx(include_bad_sheet):
            wb = openpyxl.Workbook()
            ws1 = wb.active
            ws1.title = "菜單品項(MenuItem)"
            ws1.append(["餐點名稱(Name)", "分類(Category)", "單價(Price)"])
            ws1.append(["原子匯入測試餐A", "點心", 30])
            ws2 = wb.create_sheet("會員資料(User)")
            ws2.append(["姓名(Name)", "電話(Phone)", "紅利點數(Points)"])
            ws2.append(["原子匯入測試會員", "0900111222", 10])
            if include_bad_sheet:
                ws3 = wb.create_sheet("菜單_壞資料")      # 欄位無法對應「餐點名稱 / 單價」
                ws3.append(["品名", "金額"])
                ws3.append(["原子匯入測試餐B", 40])
            buf = io.BytesIO()
            wb.save(buf)
            buf.seek(0)
            return buf

        def _post(buf):
            return admin_client.post(
                "/admin/import_smart",
                data={"csrf_token": csrf, "file": (buf, "atomic_test.xlsx")},
                content_type="multipart/form-data",
            )

        try:
            # (a) 含壞工作表 -> 400，且前面已解析的菜單與會員都不得寫入
            r_bad = _post(_build_xlsx(include_bad_sheet=True))
            assert r_bad.status_code == 400, f"含壞工作表的匯入應回 400，實際 {r_bad.status_code}"
            with app.app_context():
                assert MenuItem.query.filter_by(name="原子匯入測試餐A").first() is None, \
                    "匯入失敗後，前一張菜單工作表資料不應殘留 (未 rollback)"
                assert MenuItem.query.filter_by(name="原子匯入測試餐B").first() is None, "壞工作表資料不應寫入"
                assert User.query.filter_by(phone="0900111222").first() is None, \
                    "匯入失敗後，會員工作表資料不應殘留 (未 rollback)"

            # (b) 對照組：移除壞工作表後同樣檔案應完整匯入成功
            r_ok = _post(_build_xlsx(include_bad_sheet=False))
            assert r_ok.status_code in (200, 302), f"移除壞工作表後應可正常匯入，實際 {r_ok.status_code}"
            with app.app_context():
                a_item = MenuItem.query.filter_by(name="原子匯入測試餐A").first()
                member = User.query.filter_by(phone="0900111222").first()
                assert a_item is not None and a_item.price == 30, "對照組菜單品項未成功匯入"
                assert member is not None and member.points == 10, "對照組會員未成功匯入"
            return "壞工作表 -> 400 且整批 rollback (菜單/會員皆未殘留)；移除後完整匯入成功"
        finally:
            with app.app_context():
                for nm in ("原子匯入測試餐A", "原子匯入測試餐B"):
                    obj = MenuItem.query.filter_by(name=nm).first()
                    if obj:
                        db.session.delete(obj)
                m = User.query.filter_by(phone="0900111222").first()
                if m:
                    db.session.delete(m)
                db.session.commit()
    runner.check("10.5", "智慧匯入原子性：多工作表任一表失敗整批 rollback，不留半套資料", t10_5)

    # 10.6 智慧匯入紅利防呆：兌換點數不得高於單價、特惠點數須小於兌換點數 (新增與更新路徑)
    def t10_6():
        csrf = get_client_csrf(admin_client, "/admin")
        names = ("紅利防呆測試A", "紅利防呆測試B", "紅利防呆測試C", "紅利防呆測試D")

        def _import_csv(text):
            return admin_client.post(
                "/admin/import_smart",
                data={"csrf_token": csrf, "file": (io.BytesIO(text.encode("utf-8")), "reward_guard.csv")},
                content_type="multipart/form-data",
            )

        def _get(name):
            return MenuItem.query.filter_by(name=name).first()

        try:
            header = "餐點名稱,分類,單價,是否開放紅利兌換,兌換點數,特惠點數\n"
            rows = (
                "紅利防呆測試A,點心,50,是,80,0\n"    # 兌換點數 80 > 單價 50 -> 自動關閉紅利
                "紅利防呆測試B,點心,50,是,30,40\n"   # 特惠點數 40 >= 兌換點數 30 -> 特惠歸 0，紅利保留
                "紅利防呆測試C,點心,50,是,30,10\n"   # 合法
                "紅利防呆測試D,點心,50,是,0,0\n"     # 兌換點數 0 -> 自動關閉紅利
            )
            r = _import_csv(header + rows)
            assert r.status_code in (200, 302), f"CSV 匯入失敗: {r.status_code}"
            with app.app_context():
                a, b, c, d = (_get(n) for n in names)
                assert (a.is_reward, a.reward_points, a.reward_discount_points) == (False, 0, 0), \
                    f"A：點數高於單價應關閉紅利，實際 {(a.is_reward, a.reward_points, a.reward_discount_points)}"
                assert (b.is_reward, b.reward_points, b.reward_discount_points) == (True, 30, 0), \
                    f"B：特惠點數不合法應歸 0 並保留紅利，實際 {(b.is_reward, b.reward_points, b.reward_discount_points)}"
                assert (c.is_reward, c.reward_points, c.reward_discount_points) == (True, 30, 10), \
                    f"C：合法設定不應被改動，實際 {(c.is_reward, c.reward_points, c.reward_discount_points)}"
                assert (d.is_reward, d.reward_points) == (False, 0), \
                    f"D：兌換點數 0 應關閉紅利，實際 {(d.is_reward, d.reward_points)}"

            # 更新路徑：既有合法紅利品再次匯入不合法點數 -> 同樣被防呆關閉
            r2 = _import_csv(header + "紅利防呆測試C,點心,50,是,99,0\n")
            assert r2.status_code in (200, 302)
            with app.app_context():
                c2 = _get("紅利防呆測試C")
                assert (c2.is_reward, c2.reward_points, c2.reward_discount_points) == (False, 0, 0), \
                    f"更新路徑：不合法點數應關閉紅利，實際 {(c2.is_reward, c2.reward_points, c2.reward_discount_points)}"
            return "新增/更新路徑皆套用紅利防呆：點數>單價與點數=0 關閉紅利、不合法特惠點數歸 0、合法設定保留"
        finally:
            with app.app_context():
                for nm in names:
                    obj = _get(nm)
                    if obj:
                        db.session.delete(obj)
                db.session.commit()
    runner.check("10.6", "智慧匯入紅利防呆：點數不得高於單價、特惠點數須小於兌換點數", t10_6)

    # 10.7 匯入庫存/總量一致性：只給庫存或只給較小總量時，總量自動修正為不小於庫存
    def t10_7():
        csrf = get_client_csrf(admin_client, "/admin")
        name = "總量修正測試茶"

        def _import_csv(text):
            r = admin_client.post(
                "/admin/import_smart",
                data={"csrf_token": csrf, "file": (io.BytesIO(text.encode("utf-8")), "total_stock_test.csv")},
                content_type="multipart/form-data",
            )
            assert r.status_code in (200, 302), f"CSV 匯入失敗: {r.status_code}"

        def _snap():
            with app.app_context():
                it = MenuItem.query.filter_by(name=name).first()
                return it.stock, it.total_stock

        try:
            _import_csv("餐點名稱,分類,單價,庫存,總量\n總量修正測試茶,飲料,30,10,10\n")
            assert _snap() == (10, 10), f"初始應為 (10, 10)，實際 {_snap()}"

            # (a) 只給庫存 25 (> 總量 10) -> 總量需上調為 25
            _import_csv("餐點名稱,分類,單價,庫存\n總量修正測試茶,飲料,30,25\n")
            stock, total = _snap()
            assert stock == 25 and total >= stock, f"只給庫存 25：總量應 >= 庫存，實際 (stock={stock}, total={total})"

            # (b) 只給較小總量 5 (< 目前庫存 25) -> 庫存不變，總量不得低於庫存
            _import_csv("餐點名稱,分類,單價,總量\n總量修正測試茶,飲料,30,5\n")
            stock, total = _snap()
            assert stock == 25 and total == 25, f"只給較小總量 5：總量應被夾至庫存 25，實際 (stock={stock}, total={total})"

            # (c) 同時給庫存 8、總量 20 -> 原樣套用
            _import_csv("餐點名稱,分類,單價,庫存,總量\n總量修正測試茶,飲料,30,8,20\n")
            assert _snap() == (8, 20), f"同時給庫存與總量應原樣套用 (8, 20)，實際 {_snap()}"

            # (d) 只給較大總量 40 -> 庫存不變，總量 40
            _import_csv("餐點名稱,分類,單價,總量\n總量修正測試茶,飲料,30,40\n")
            assert _snap() == (8, 40), f"只給較大總量 40：應為 (8, 40)，實際 {_snap()}"
            return "庫存>總量時總量上調、較小總量被夾至庫存、同時提供或較大總量則原樣套用，stock<=total 恆成立"
        finally:
            with app.app_context():
                obj = MenuItem.query.filter_by(name=name).first()
                if obj:
                    db.session.delete(obj)
                db.session.commit()
    runner.check("10.7", "智慧匯入庫存/總量一致性：庫存不得大於總量", t10_7)

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
                f"/api/orders/{oid}/status",
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
        # 模組 12 收尾：還原頻率限制設定並關閉真實 HTTP 壓測伺服器
        os.environ["DISABLE_RATE_LIMIT"] = "0"
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

    os.environ["DISABLE_RATE_LIMIT"] = "0"   # 模組 12 結束後恢復頻率限制，模組 13 在正常限流狀態下執行
    try:
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
                ("/admin/backup_system", {}),
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
            return "owner 與 manager 對管理路由皆可進入 handler，紅利設定可實際寫入"
        runner.check("13.9", "角色修正回歸：owner / manager 仍可使用紅利設定、智慧匯入、匯出、AI 文案與券核銷", t13_9)

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
                ("POST", f"/api/orders/{ids['o1']}/status", {"json": {"status": "Completed"}}),
                ("POST", f"/api/orders/{ids['o2']}/status", {"json": {"status": "Completed"}}),
                ("POST", f"/api/orders/{ids['o3']}/status", {"json": {"status": "PickedUp"}}),
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
            return "11 個依 ID 操作的路由對 B 店資料全數回 404，且 B 店資料完全未變"
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
                "/api/orders/1/status",
                "/api/kitchen_complete_all",
                "/admin/staff", "/admin/add", "/admin/edit/1", "/admin/delete/1", "/admin/add_combo", "/admin/edit_combo/1",
                "/admin/delete_combo/1", "/admin/add_reward", "/admin/remove_reward/1", "/admin/add_coupon",
                "/admin/delete_coupon/1", "/admin/edit_coupon/1", "/admin/delete_user/1", "/admin/edit_user/1",
                "/admin/toggle_user_coupon/1", "/admin/update_reward_setting",
                "/admin/restore_backup", "/admin/export_excel", "/admin/import_smart", "/admin/backup_system",
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
        # 模組 14：高風險問題修補回歸 — 特徵維度、IP 偽造與帳號鎖定、備份隔離與還原安全、XSS、憑證原子性
        # ==========================================================================
        runner.start_suite("模組 14：高風險修補回歸 (特徵維度、IP 偽造、備份隔離、XSS、憑證原子性)")
        clear_rate_buckets()

        def _db_path14():
            return TEST_DB_PATH

        # 14.1 單一壞特徵不可使全店人臉比對失效；維度不符/NaN/截斷資料一律視為無效
        def t14_1():
            import app.models.user as um
            good = np.linspace(0.1, 0.9, 128, dtype=np.float32)
            nan_vec = good.copy()
            nan_vec[3] = np.nan

            # (a) 解碼層：維度不符、NaN、截斷 -> None
            assert um.decode_feature_bytes_safely(um.FEATURE_MAGIC_PREFIX + np.zeros(64, dtype=np.float32).tobytes()) is None, "64 維 NPF32v1 未被拒絕"
            assert um.decode_feature_bytes_safely(pickle.dumps(np.zeros(64, dtype=np.float32), protocol=4)) is None, "64 維舊版 Pickle 未被拒絕"
            assert um.decode_feature_bytes_safely(um.FEATURE_MAGIC_PREFIX + nan_vec.tobytes()) is None, "含 NaN 的特徵未被拒絕"
            assert um.decode_feature_bytes_safely(um.FEATURE_MAGIC_PREFIX + good.tobytes()[:-2]) is None, "截斷的特徵未被拒絕"
            ok = um.decode_feature_bytes_safely(um.encode_feature_array(good))
            assert ok is not None and ok.shape == (128,) and np.allclose(ok, good), "合法 128 維特徵解碼失敗"
            try:
                um.encode_feature_array(np.zeros(64, dtype=np.float32))
                raise AssertionError("寫入端未拒絕 64 維特徵")
            except ValueError:
                pass

            # (b) 比對層：混入壞資料時，仍能正確比對出合法會員
            class _U:
                def __init__(self, f):
                    self.feature = f
            users = [_U(np.zeros(64, dtype=np.float32)), _U(nan_vec), _U(None), _U(good.copy())]
            matched, score = find_best_match_vectorized(good, users, threshold=0.5)
            assert matched is users[3] and score > 0.99, f"混入壞特徵時比對失效: matched={matched}, score={score}"

            # (c) 資料庫層：NPF32v1 但維度錯誤的列，遷移後原始資料不被改寫，ORM 讀出 None，且不影響其他會員比對
            bad_blob = um.FEATURE_MAGIC_PREFIX + np.zeros(64, dtype=np.float32).tobytes()
            tid = _tid13()
            with app.app_context():
                db.session.execute(
                    db.text("INSERT INTO user (tenant_id, name, phone, photo_path, feature, points) VALUES (:t, :n, :p, :ph, :f, 0)"),
                    {"t": tid, "n": "壞維度會員", "p": "0933140101", "ph": "bad_dim.jpg", "f": bad_blob},
                )
                good_u = User(tenant_id=tid, name="好特徵會員", phone="0933140102", photo_path="good_dim.jpg", points=0)
                good_u.feature = good.copy()
                db.session.add(good_u)
                db.session.commit()
            try:
                run_database_migrations(app)
                with app.app_context():
                    raw = db.session.execute(db.text("SELECT feature FROM user WHERE phone = '0933140101'")).fetchone()
                    assert bytes(raw[0]) == bad_blob, "維度錯誤的特徵在遷移時被改寫"
                    rows = User.query.execution_options(skip_tenant_filter=True).filter(
                        User.phone.in_(["0933140101", "0933140102"])
                    ).all()
                    bad_orm = next(r for r in rows if r.phone == "0933140101")
                    assert bad_orm.feature is None, "維度錯誤的特徵不應被 ORM 當成有效特徵"
                    m2, s2 = find_best_match_vectorized(good, rows, threshold=0.5)
                    assert m2 is not None and m2.phone == "0933140102", "資料庫中混有壞特徵時，合法會員無法被比對"
            finally:
                with app.app_context():
                    db.session.execute(db.text("DELETE FROM user WHERE phone IN ('0933140101', '0933140102')"))
                    db.session.commit()
            return "維度/NaN/截斷特徵一律視為無效、寫入端拒絕、混入壞資料仍可比對、遷移保留原始 blob"
        runner.check("14.1", "人臉特徵維度驗證：單一壞資料不得使全店人臉比對失效", t14_1)

        # 14.2 預設不信任 X-Forwarded-For (防偽造來源 IP)；僅在明確設定代理層數時才採用
        def t14_2():
            NoProxyCfg = type("NoProxyCfg", (TestConfig,), {"TRUSTED_PROXY_COUNT": 0})
            app0 = create_app(NoProxyCfg)
            c0 = app0.test_client()
            c0.get("/?shop=default")
            c0.get("/guest_login", headers={"X-Forwarded-For": "203.0.113.77"}, environ_overrides={"REMOTE_ADDR": "198.51.100.9"})
            with app0.app_context():
                log0 = CustomerLog.query.execution_options(skip_tenant_filter=True).order_by(CustomerLog.id.desc()).first()
                ip0 = log0.ip_address
            assert ip0 == "198.51.100.9", f"TRUSTED_PROXY_COUNT=0 時仍採用了偽造的 X-Forwarded-For: {ip0}"

            c1 = app.test_client()
            c1.get("/?shop=default")
            c1.get("/guest_login", headers={"X-Forwarded-For": "203.0.113.78"}, environ_overrides={"REMOTE_ADDR": "198.51.100.9"})
            with app.app_context():
                log1 = CustomerLog.query.execution_options(skip_tenant_filter=True).order_by(CustomerLog.id.desc()).first()
                ip1 = log1.ip_address
            assert ip1 == "203.0.113.78", f"TRUSTED_PROXY_COUNT=1 時未採用代理傳入的來源 IP: {ip1}"
            # 註：Config.TRUSTED_PROXY_COUNT 取自 .env，本機 .env 可能已設為 1，因此不對其「值」做斷言，
            # 只驗證「未設定/設為 0 時不信任標頭、設為 1 時才信任」的行為。
            return "TRUSTED_PROXY_COUNT=0 忽略偽造的 X-Forwarded-For；=1 才採用代理標頭"
        runner.check("14.2", "來源 IP 不可偽造：預設不信任 X-Forwarded-For，僅明確設定代理層數才採用", t14_2)

        # 14.3 PIN 帳號層級鎖定：不分來源 IP 累計；換 IP 也不能繞過；櫃檯重設 PIN 可解除
        def t14_3():
            from app.routes.auth import clear_pin_lockout
            clear_rate_buckets()
            tid = _tid13()
            with app.app_context():
                u = User(tenant_id=tid, name="模組14鎖定會員", phone="0933140001", photo_path="m14.jpg", points=0)
                u.set_pin("2468")
                db.session.add(u)
                db.session.commit()
            try:
                c = app.test_client()
                c.get("/?shop=default")
                csrf = get_client_csrf(c, "/register")
                codes = []
                for i in range(10):  # 每次都換一個來源 IP，模擬攻擊者輪換 IP 猜 PIN
                    r = c.post("/phone_login", json={"phone": "0933140001", "pin": "0000"},
                               headers={"X-CSRFToken": csrf, "X-Forwarded-For": f"198.18.{i}.1"})
                    codes.append(r.status_code)
                r_locked = c.post("/phone_login", json={"phone": "0933140001", "pin": "2468"},
                                  headers={"X-CSRFToken": csrf, "X-Forwarded-For": "198.18.99.1"})
                assert codes == [401] * 10, f"前 10 次錯誤應皆為 401: {codes}"
                assert r_locked.status_code == 429, f"換 IP 後帳號仍應被鎖定 (429)，實際 {r_locked.status_code}"

                # 空號也以相同方式計數，鎖定行為與已註冊號碼一致，無法藉由 429 列舉會員
                ghost = []
                for i in range(10):
                    r = c.post("/phone_login", json={"phone": "0933149999", "pin": "0000"},
                               headers={"X-CSRFToken": csrf, "X-Forwarded-For": f"198.19.{i}.1"})
                    ghost.append(r.status_code)
                r_ghost = c.post("/phone_login", json={"phone": "0933149999", "pin": "0000"},
                                 headers={"X-CSRFToken": csrf, "X-Forwarded-For": "198.19.99.1"})
                assert ghost == [401] * 10 and r_ghost.status_code == 429, "空號與已註冊號碼的鎖定行為不一致 (可被用來列舉會員)"

                clear_pin_lockout(tid, "0933140001")   # 櫃檯重設 PIN 時會呼叫
                r_ok = c.post("/phone_login", json={"phone": "0933140001", "pin": "2468"},
                              headers={"X-CSRFToken": csrf, "X-Forwarded-For": "198.18.100.1"})
                assert r_ok.status_code == 200, f"解除鎖定後以正確 PIN 登入失敗: {r_ok.status_code}"
            finally:
                clear_pin_lockout(tid, "0933140001")
                clear_pin_lockout(tid, "0933149999")
                with app.app_context():
                    db.session.execute(db.text("DELETE FROM user WHERE phone = '0933140001'"))
                    db.session.commit()
            return "輪換 10 個 IP 猜錯 PIN 後帳號鎖定 (429)、空號行為一致、清除鎖定後可登入"
        runner.check("14.3", "PIN 帳號層級鎖定：輪換來源 IP 也無法繞過，且不可列舉會員", t14_3)

        # 14.4 同一 IP 對大量不同手機噴灑猜測 -> IP 層級鎖定 (含門市共用 IP 的時間窗衰減設計)
        def t14_4():
            import app.routes.auth as auth_mod
            from app.routes.auth import clear_pin_lockout
            tid = _tid13()
            with app.app_context():
                u = User(tenant_id=tid, name="模組14噴灑會員", phone="0933140002", photo_path="m14b.jpg", points=0)
                u.set_pin("2468")
                db.session.add(u)
                db.session.commit()
            os.environ["DISABLE_RATE_LIMIT"] = "1"   # 只驗證 PIN 失敗鎖定，避開 15 次/分鐘的頻率限制
            spray_ip = "198.51.100.200"
            try:
                c = app.test_client()
                c.get("/?shop=default")
                csrf = get_client_csrf(c, "/register")
                codes = []
                for i in range(auth_mod._PIN_IP_MAX_FAILS):
                    r = c.post("/phone_login", json={"phone": f"0944{i:06d}", "pin": "0000"},
                               headers={"X-CSRFToken": csrf, "X-Forwarded-For": spray_ip})
                    codes.append(r.status_code)
                assert codes == [401] * auth_mod._PIN_IP_MAX_FAILS, f"噴灑前段應全為 401: {codes[:5]}..."
                r_locked = c.post("/phone_login", json={"phone": "0933140002", "pin": "2468"},
                                  headers={"X-CSRFToken": csrf, "X-Forwarded-For": spray_ip})
                assert r_locked.status_code == 429, f"IP 噴灑達上限後應整體鎖定 (429)，實際 {r_locked.status_code}"
                r_other_ip = c.post("/phone_login", json={"phone": "0933140002", "pin": "2468"},
                                    headers={"X-CSRFToken": csrf, "X-Forwarded-For": "198.51.100.201"})
                assert r_other_ip.status_code == 200, f"其他 IP 的正常登入不應受影響: {r_other_ip.status_code}"
            finally:
                os.environ["DISABLE_RATE_LIMIT"] = "0"
                with auth_mod._PIN_LOCK_MUTEX:
                    auth_mod._PIN_FAIL_STATE.pop((tid, "*ip*", spray_ip), None)
                clear_pin_lockout(tid, "0933140002")
                with app.app_context():
                    db.session.execute(db.text("DELETE FROM user WHERE phone = '0933140002'"))
                    db.session.commit()
            return f"同一 IP 噴灑 {auth_mod._PIN_IP_MAX_FAILS} 支手機後該 IP 鎖定，其他 IP 不受影響"
        runner.check("14.4", "PIN 噴灑式猜測防護：單一 IP 累計失敗達上限即鎖定該 IP", t14_4)

        # 14.5 整庫備份/還原僅限平台管理者 (預設店家負責人)，其他店家負責人 403 且介面不顯示
        def t14_5():
            cb = app.test_client()
            html_b = cb.get("/admin").get_data(as_text=True)
            csrf_b = extract_csrf(html_b)
            r_login = cb.post("/admin", data={"username": "owner_b2", "password": "Branch2Pass!2026", "csrf_token": csrf_b},
                              headers={"X-Forwarded-For": "198.51.100.51"})
            assert r_login.status_code == 302, f"他店負責人登入失敗: {r_login.status_code}"
            page_b = cb.get("/admin")
            assert page_b.status_code == 200, f"他店負責人無法開啟後台: {page_b.status_code}"
            page_b_html = page_b.get_data(as_text=True)
            csrf_b = extract_csrf(page_b_html)
            assert "pane-export-zip" not in page_b_html and "pane-restore-zip" not in page_b_html, "他店負責人的後台仍顯示整庫備份/還原介面"

            r_bak = cb.post("/admin/backup_system", data={"csrf_token": csrf_b}, headers={"X-CSRFToken": csrf_b})
            assert r_bak.status_code == 403, f"他店負責人下載整庫備份未被擋下: {r_bak.status_code}"
            r_rest = cb.post(
                "/admin/restore_backup",
                data={"csrf_token": csrf_b, "restore_db": "1", "backup_zip": (io.BytesIO(b"PK\x05\x06" + b"\x00" * 18), "x.zip")},
                content_type="multipart/form-data", headers={"X-CSRFToken": csrf_b},
            )
            assert r_rest.status_code == 403, f"他店負責人上傳還原包未被擋下: {r_rest.status_code}"

            csrf_a = get_client_csrf(admin_client, "/admin")
            page_a_html = admin_client.get("/admin").get_data(as_text=True)
            assert "pane-export-zip" in page_a_html and "pane-restore-zip" in page_a_html, "平台管理者看不到備份/還原介面"
            return "他店負責人：介面不顯示、備份 403、還原 403；平台管理者 (預設店家負責人) 可用"
        runner.check("14.5", "整庫備份/還原僅限平台管理者：其他店家負責人 403 且介面隱藏", t14_5)

        # 14.6 備份包保留菜單相片的店家子資料夾、排除暫存檔；還原前自動另存現有資料庫並可往返還原
        def t14_6():
            sub_dir = os.path.join(TEST_UPLOAD_MENU_DIR, "1")
            os.makedirs(sub_dir, exist_ok=True)
            menu_photo = os.path.join(sub_dir, "menu_test_14.jpg")
            member_photo = os.path.join(TEST_UPLOAD_MEMBER_DIR, "member_test14.jpg")
            temp_photo = os.path.join(TEST_UPLOAD_MEMBER_DIR, "temp_skip_14.jpg")
            for pth in (menu_photo, member_photo, temp_photo):
                with open(pth, "wb") as fh:
                    fh.write(make_test_jpeg_bytes(60, 60).getvalue())
            try:
                csrf = get_client_csrf(admin_client, "/admin")
                r_bak = admin_client.post("/admin/backup_system", data={"csrf_token": csrf}, headers={"X-CSRFToken": csrf})
                assert r_bak.status_code == 200 and r_bak.mimetype == "application/zip", f"備份失敗: {r_bak.status_code}"
                zip_bytes = r_bak.data
                with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
                    names = zf.namelist()
                    assert "menu.db" in names, "備份缺少 menu.db"
                    assert "static/menu/1/menu_test_14.jpg" in names, f"菜單相片未保留店家子資料夾: {[n for n in names if 'menu_test_14' in n]}"
                    assert "static/member/member_test14.jpg" in names, "備份缺少會員相片"
                    assert not any("temp_skip_14" in n for n in names), "備份不應包含人臉登入暫存檔"
                    snap = zf.read("menu.db")
                snap_path = os.path.join(TEST_DB_DIR, "snap_check.db")
                with open(snap_path, "wb") as fh:
                    fh.write(snap)
                import sqlite3 as _sq
                conn = _sq.connect(snap_path)
                try:
                    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "備份快照完整性檢查失敗"
                    assert conn.execute("SELECT COUNT(*) FROM tenant").fetchone()[0] >= 2, "備份快照缺少店家資料"
                finally:
                    conn.close()
                    os.remove(snap_path)

                # 刪掉相片後，用同一份備份還原，應能補回 (含子資料夾)，且還原前現有資料庫被另存
                os.remove(menu_photo)
                before_bak = {f for f in os.listdir(TEST_DB_DIR) if ".bak-" in f}
                csrf = get_client_csrf(admin_client, "/admin")
                r_rest = admin_client.post(
                    "/admin/restore_backup",
                    data={"csrf_token": csrf, "restore_db": "1", "restore_photos": "1", "restore_menu_photos": "1",
                          "overwrite_photos": "1", "backup_zip": (io.BytesIO(zip_bytes), "backup.zip")},
                    content_type="multipart/form-data", headers={"X-CSRFToken": csrf},
                )
                body = r_rest.get_data(as_text=True)
                assert r_rest.status_code == 200 and "還原完成" in body, f"還原失敗: {r_rest.status_code} {body[:200]}"
                assert os.path.exists(menu_photo), "還原後菜單相片未回到店家子資料夾"
                after_bak = {f for f in os.listdir(TEST_DB_DIR) if ".bak-" in f}
                assert len(after_bak - before_bak) == 1, f"還原前未自動另存現有資料庫: {after_bak - before_bak}"
                assert admin_client.get("/admin").status_code == 200, "還原後後台無法開啟"
            finally:
                for pth in (menu_photo, member_photo, temp_photo):
                    try:
                        os.remove(pth)
                    except OSError:
                        pass
            return "備份保留 static/menu/<店家>/、排除 temp_；還原前自動另存 .bak-*，往返還原成功"
        runner.check("14.6", "備份/還原往返：保留店家子資料夾、還原前自動另存、還原後系統可用", t14_6)

        # 14.7 惡意/損毀的備份包一律拒絕，且不得改動現有資料庫或寫出目錄之外
        def t14_7():
            def _restore(zip_bytes, **flags):
                csrf = get_client_csrf(admin_client, "/admin")
                data = {"csrf_token": csrf, "backup_zip": (io.BytesIO(zip_bytes), "evil.zip")}
                data.update(flags)
                return admin_client.post("/admin/restore_backup", data=data, content_type="multipart/form-data",
                                         headers={"X-CSRFToken": csrf})

            def _zip(entries):
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    for name, content in entries:
                        zf.writestr(name, content)
                return buf.getvalue()

            jpg = make_test_jpeg_bytes(50, 50).getvalue()
            before_bak = {f for f in os.listdir(TEST_DB_DIR) if ".bak-" in f}
            db_size_before = os.path.getsize(_db_path14())

            # (a) menu.db 不是 SQLite
            r = _restore(_zip([("menu.db", b"this is not a sqlite database")]), restore_db="1")
            assert r.status_code == 500 and "還原過程發生錯誤" in r.get_data(as_text=True), f"非 SQLite 檔案未被拒絕: {r.status_code}"
            # (b) 合法 SQLite 但不含本系統資料表
            import sqlite3 as _sq
            foreign_path = os.path.join(TEST_DB_DIR, "foreign.db")
            fc = _sq.connect(foreign_path)
            fc.execute("CREATE TABLE foo (id INTEGER)")
            fc.commit()
            fc.close()
            with open(foreign_path, "rb") as fh:
                foreign_bytes = fh.read()
            os.remove(foreign_path)
            r = _restore(_zip([("menu.db", foreign_bytes)]), restore_db="1")
            assert r.status_code == 500, f"不含系統資料表的資料庫未被拒絕: {r.status_code}"
            # (c) 相片：.html / 偽裝成 .jpg 的網頁 / 路徑穿越，皆不可落地到目錄之外或以危險副檔名落地
            evil_html = b"<html><script>alert(1)</script></html>"
            r = _restore(_zip([
                ("static/menu/evil.html", evil_html),
                ("static/menu/fake.jpg", evil_html),
                ("static/menu/../../escaped_14.jpg", jpg),
                ("static/member/member_ok14.jpg", jpg),
            ]), restore_menu_photos="1", restore_photos="1", overwrite_photos="1")
            assert r.status_code == 200, f"含危險檔案的相片還原回應異常: {r.status_code}"
            assert not os.path.exists(os.path.join(TEST_UPLOAD_MENU_DIR, "evil.html")), "還原寫出了 .html 檔案"
            assert not os.path.exists(os.path.join(TEST_UPLOAD_MENU_DIR, "fake.jpg")), "還原寫出了偽裝成圖片的網頁"
            assert not os.path.exists(os.path.join(os.path.dirname(TEST_UPLOAD_MENU_DIR), "escaped_14.jpg")), "路徑穿越寫出了目錄之外"
            assert os.path.exists(os.path.join(TEST_UPLOAD_MENU_DIR, "escaped_14.jpg")), "路徑穿越檔名應被安全攤平回菜單目錄"
            assert os.path.exists(os.path.join(TEST_UPLOAD_MEMBER_DIR, "member_ok14.jpg")), "合法會員相片未還原"
            for leftover in (os.path.join(TEST_UPLOAD_MENU_DIR, "escaped_14.jpg"), os.path.join(TEST_UPLOAD_MEMBER_DIR, "member_ok14.jpg")):
                os.remove(leftover)
            # (d) 單檔大小超限
            from app.config import Config as _Cfg
            old_limit = _Cfg.MAX_SINGLE_FILE_SIZE
            try:
                _Cfg.MAX_SINGLE_FILE_SIZE = 100
                r = _restore(_zip([("static/member/big14.jpg", jpg)]), restore_photos="1")
                assert r.status_code == 500, f"超過單檔上限的相片未被拒絕: {r.status_code}"
            finally:
                _Cfg.MAX_SINGLE_FILE_SIZE = old_limit

            after_bak = {f for f in os.listdir(TEST_DB_DIR) if ".bak-" in f}
            assert after_bak == before_bak, "驗證失敗的還原不應動到現有資料庫 (不應產生另存檔)"
            assert os.path.getsize(_db_path14()) == db_size_before or admin_client.get("/admin").status_code == 200, "驗證失敗後現有資料庫狀態異常"
            return "非 SQLite/無系統資料表/.html/偽裝圖片/路徑穿越/超限皆被拒絕或安全處理，現有資料庫未被動到"
        runner.check("14.7", "惡意或損毀的備份包被拒絕：不動現有資料庫、不寫出目錄之外、不落地危險檔案", t14_7)

        # 14.8 XSS：廚房/取餐/叫號畫面的動態內容必須跳脫 (原本只定義 escapeHtml 卻沒有使用)
        def t14_8():
            # 員工端畫面 (廚房/取餐/叫號)：所有使用者可控欄位都必須跳脫
            risky_staff = re.compile(
                r"\$\{\s*(?:o|it|item|order)\.(?:table_number|user_name|note|customization|item_name|name|order_type|created_at)\s*\}"
            )
            # 顧客端畫面：餐點名稱多半放在 innerText/confirm (本身安全)，這裡只檢查會被他人資料污染的欄位
            risky_customer = re.compile(
                r"\$\{\s*(?:o|it|item|order)\.(?:table_number|user_name|note|customization|item_name|order_type|created_at)\s*\}"
            )
            offenders, call_counts = [], {}
            for tpl in ("kitchen", "pickup", "counter", "customer"):
                with open(os.path.join(BASE_DIR, "templates", f"{tpl}.html"), encoding="utf-8") as fh:
                    src = fh.read()
                call_counts[tpl] = src.count("escapeHtml(") - 1   # 扣掉函式定義本身
                for m in (risky_customer if tpl == "customer" else risky_staff).finditer(src):
                    offenders.append(f"{tpl}.html: {m.group(0)}")
            assert not offenders, "仍有未跳脫就放進 innerHTML 的使用者可控欄位: " + "; ".join(offenders)
            assert all(n >= 3 for n in call_counts.values()), f"escapeHtml 實際被呼叫的次數過少: {call_counts}"

            from app.utils import js_alert_redirect
            html_out, _ = js_alert_redirect("惡意</script><img src=x onerror=alert(1)>")
            assert html_out.count("</script>") == 1 and "<img" not in html_out, f"js_alert_redirect 未跳脫 </script>: {html_out}"
            return f"廚房/取餐/叫號/顧客頁面 escapeHtml 呼叫次數={call_counts}；js_alert_redirect 已跳脫 </script>"
        runner.check("14.8", "XSS 防護：廚房/取餐/叫號畫面輸出必須跳脫，js_alert_redirect 不可被 </script> 截斷", t14_8)

        # 14.9 兌換憑證狀態轉換必須原子：核銷與退點對同一張憑證只能有一方成功
        def t14_9():
            from app.services.voucher_service import try_claim_voucher, try_refund_voucher
            tid = _tid13()
            with app.app_context():
                item = MenuItem(tenant_id=tid, name="模組14兌換品", category="主餐", price=100, stock=500, total_stock=500,
                                is_reward=True, reward_points=10, reward_discount_points=0)
                user = User(tenant_id=tid, name="模組14憑證會員", phone="0933140003", photo_path="m14v.jpg", points=1000)
                db.session.add_all([item, user])
                db.session.commit()
                item_id, user_id = item.id, user.id

            def _new_voucher():
                with app.app_context():
                    v = RewardRedemption(tenant_id=tid, user_id=user_id, menu_item_id=item_id, points_cost=10,
                                         is_used=False, is_refunded=False)
                    db.session.add(v)
                    db.session.commit()
                    return v.id

            try:
                # (a) 決定性：已核銷的憑證不可再退點、已退點的憑證不可再核銷、同一動作不可重複成功
                vid = _new_voucher()
                with app.app_context():
                    assert try_claim_voucher(tid, user_id, vid) is True
                    assert try_claim_voucher(tid, user_id, vid) is False, "憑證被重複核銷"
                    assert try_refund_voucher(tid, user_id, vid) is False, "已核銷的憑證仍可退點 (餐點拿了、點數也退了)"
                    db.session.commit()
                vid2 = _new_voucher()
                with app.app_context():
                    assert try_refund_voucher(tid, user_id, vid2) is True
                    assert try_refund_voucher(tid, user_id, vid2) is False, "憑證被重複退點"
                    assert try_claim_voucher(tid, user_id, vid2) is False, "已退點的憑證仍可核銷"
                    db.session.commit()

                # (b) 多執行緒競態：同一張憑證同時核銷與退點，恰好一方成功
                outcomes = []
                def _runner(kind, vid_, barrier, sink):
                    with app.app_context():
                        barrier.wait()
                        fn = try_claim_voucher if kind == "claim" else try_refund_voucher
                        ok_ = fn(tid, user_id, vid_)
                        db.session.commit()
                        sink.append((kind, ok_))
                for _ in range(24):
                    v_id = _new_voucher()
                    barrier = threading.Barrier(2)
                    sink = []
                    ths = [threading.Thread(target=_runner, args=(k, v_id, barrier, sink)) for k in ("claim", "refund")]
                    for th in ths:
                        th.start()
                    for th in ths:
                        th.join()
                    outcomes.append((v_id, sink))
                bad = [(v_id, s) for v_id, s in outcomes if sum(1 for _, ok_ in s if ok_) != 1]
                assert not bad, f"同一張憑證核銷/退點並行時未恰好一方成功: {bad[:3]}"
                with app.app_context():
                    rows = RewardRedemption.query.execution_options(skip_tenant_filter=True).filter(
                        RewardRedemption.id.in_([v for v, _ in outcomes])
                    ).all()
                    assert all(bool(r.is_used) != bool(r.is_refunded) for r in rows), "憑證狀態不一致 (同時已使用且已退點)"

                # (c) 端對端：同一會員同時「結帳使用憑證」與「退點」，點數與餐點必須守恆 (不可同時拿餐又拿回點數)
                def _login_client(ip):
                    cl = app.test_client()
                    cl.get("/?shop=default")
                    csr = get_client_csrf(cl, "/register")
                    with app.app_context():
                        u_ = db.session.get(User, user_id)
                        u_.set_pin("2468")
                        db.session.commit()
                    clear_rate_buckets()
                    r_ = cl.post("/phone_login", json={"phone": "0933140003", "pin": "2468"},
                                 headers={"X-CSRFToken": csr, "X-Forwarded-For": ip})
                    assert r_.status_code == 200, f"登入失敗: {r_.status_code}"
                    return cl, csr
                leaks = []
                for rnd in range(8):
                    clear_rate_buckets()
                    cl_a, csrf_a = _login_client(f"198.51.101.{rnd * 2 + 1}")
                    cl_b, csrf_b = _login_client(f"198.51.101.{rnd * 2 + 2}")
                    with app.app_context():
                        pts_before = db.session.get(User, user_id).points
                        orders_before = Order.query.execution_options(skip_tenant_filter=True).filter_by(user_id=user_id).count()
                    _new_voucher()
                    bar = threading.Barrier(2)
                    res = {}
                    def _do_order():
                        bar.wait()
                        res["order"] = cl_a.post("/submit_order", json={
                            "items": [{"id": item_id, "menu_item_id": item_id, "name": "🎁 [點數兌換] 模組14兌換品",
                                       "quantity": 1, "is_reward": True}],
                            "payment_method": "Cash", "order_type": "內用"},
                            headers={"X-CSRFToken": csrf_a, "X-Forwarded-For": f"198.51.102.{rnd * 2 + 1}"})
                    def _do_refund():
                        bar.wait()
                        res["refund"] = cl_b.post("/api/refund_reward_item", json={"item_id": item_id, "quantity": 1},
                                                  headers={"X-CSRFToken": csrf_b, "X-Forwarded-For": f"198.51.102.{rnd * 2 + 2}"})
                    ths = [threading.Thread(target=_do_order), threading.Thread(target=_do_refund)]
                    for th in ths:
                        th.start()
                    for th in ths:
                        th.join()
                    with app.app_context():
                        pts_after = db.session.get(User, user_id).points
                        orders_after = Order.query.execution_options(skip_tenant_filter=True).filter_by(user_id=user_id).count()
                    got_item = res["order"].status_code == 200
                    # 守恆：拿到餐點則點數不變 (憑證抵用，或退點後再以點數支付)；沒拿到餐點則退點成功 (+10) 或皆未發生
                    if got_item and pts_after != pts_before:
                        leaks.append((rnd, "拿到餐點但點數變動", pts_before, pts_after, res["order"].status_code, res["refund"].status_code))
                    if got_item and orders_after != orders_before + 1:
                        leaks.append((rnd, "訂單數異常", orders_before, orders_after))
                assert not leaks, f"憑證核銷與退點並行造成點數/餐點不守恆: {leaks}"
            finally:
                with app.app_context():
                    db.session.execute(db.text("DELETE FROM reward_redemption WHERE user_id = :u"), {"u": user_id})
                    db.session.execute(db.text('DELETE FROM order_item WHERE order_id IN (SELECT id FROM "order" WHERE user_id = :u)'), {"u": user_id})
                    db.session.execute(db.text('DELETE FROM "order" WHERE user_id = :u'), {"u": user_id})
                    db.session.execute(db.text("DELETE FROM user WHERE id = :u"), {"u": user_id})
                    db.session.execute(db.text("DELETE FROM menu_item WHERE id = :i"), {"i": item_id})
                    db.session.commit()
            return "核銷/退點皆為原子條件式更新：重複動作皆失敗、24 組並行恰好一方成功、結帳×退點 8 輪點數與餐點守恆"
        runner.check("14.9", "兌換憑證原子性：核銷與退點並行時，不可同時拿到餐點又拿回點數", t14_9)

        # ==========================================================================
        # 模組 15：中低風險修補回歸 — 結帳/取消、鎖與設定、後台表單、隱私、註冊與上傳、輸出安全
        # ==========================================================================
        runner.start_suite("模組 15：中低風險修補回歸 (結帳取消、冪等、後台表單、隱私、註冊上傳、安全標頭)")
        clear_rate_buckets()
        from flask import g as _flask_g
        from sqlalchemy.exc import IntegrityError as _IntegrityError

        def _admin_csrf15():
            return get_client_csrf(admin_client, "/admin")

        def _new_item15(name, price=100, stock=100, **kw):
            with app.app_context():
                it = MenuItem(tenant_id=_tid13(), name=name, category="主餐", price=price, stock=stock, total_stock=stock, **kw)
                db.session.add(it)
                db.session.commit()
                return it.id

        def _new_member15(name, phone, points=0):
            with app.app_context():
                u = User(tenant_id=_tid13(), name=name, phone=phone, photo_path=f"{phone}.jpg", points=points)
                db.session.add(u)
                db.session.commit()
                return u.id

        def _cleanup15(user_ids=(), item_ids=(), coupon_codes=(), phones=()):
            with app.app_context():
                T = db.text
                if phones:
                    for ph in phones:
                        row = db.session.execute(T("SELECT id FROM user WHERE phone = :p"), {"p": ph}).fetchone()
                        if row:
                            user_ids = tuple(user_ids) + (row[0],)
                for uid in user_ids:
                    db.session.execute(T('DELETE FROM order_item WHERE order_id IN (SELECT id FROM "order" WHERE user_id = :u)'), {"u": uid})
                    db.session.execute(T('DELETE FROM "order" WHERE user_id = :u'), {"u": uid})
                    db.session.execute(T("DELETE FROM user_coupon WHERE user_id = :u"), {"u": uid})
                    db.session.execute(T("DELETE FROM reward_redemption WHERE user_id = :u"), {"u": uid})
                    db.session.execute(T("DELETE FROM user WHERE id = :u"), {"u": uid})
                for iid in item_ids:
                    db.session.execute(T("DELETE FROM order_item WHERE menu_item_id = :i"), {"i": iid})
                    db.session.execute(T("DELETE FROM menu_item WHERE id = :i"), {"i": iid})
                for code in coupon_codes:
                    db.session.execute(T("DELETE FROM user_coupon WHERE code = :c"), {"c": code})
                    db.session.execute(T("DELETE FROM coupon WHERE code = :c"), {"c": code})
                db.session.commit()

        def _pos_checkout15(item_id, user_id=None, promo="", qty=1):
            csrf = _admin_csrf15()
            return admin_client.post("/api/pos/checkout", json={
                "items": [{"id": item_id, "menu_item_id": item_id, "name": "m15", "quantity": qty}],
                "payment_method": "Cash", "order_type": "內用", "promo_code": promo,
                "user_id": user_id, "cash_received": 0,
            }, headers={"X-CSRFToken": csrf})

        def _cancel15(order_id):
            csrf = _admin_csrf15()
            return admin_client.post(f"/api/orders/{order_id}/status", json={"status": "Cancelled"}, headers={"X-CSRFToken": csrf})

        def _owner_b_client15():
            cb = app.test_client()
            csrf_b = extract_csrf(cb.get("/admin").get_data(as_text=True))
            r = cb.post("/admin", data={"username": "owner_b2", "password": "Branch2Pass!2026", "csrf_token": csrf_b},
                        headers={"X-Forwarded-For": "198.51.100.77"})
            assert r.status_code == 302, f"他店負責人登入失敗: {r.status_code}"
            return cb

        # 15.1 結帳必須擋下被標示「售完」的餐點；取消訂單回補庫存時不可自動解除「手動售完」
        def t15_1():
            iid = _new_item15("模組15售完品", price=50, stock=5)
            try:
                with app.app_context():
                    db.session.get(MenuItem, iid).is_sold_out = True
                    db.session.commit()
                r = _pos_checkout15(iid)
                assert r.status_code == 400 and r.get_json().get("sold_out") is True, f"被標示售完的餐點仍可下單: {r.status_code} {r.get_json()}"
                with app.app_context():
                    assert db.session.get(MenuItem, iid).stock == 5, "被拒絕的訂單不應扣庫存"
                    db.session.get(MenuItem, iid).is_sold_out = False
                    db.session.commit()
                r2 = _pos_checkout15(iid)
                assert r2.status_code == 200, f"解除售完後應可下單: {r2.status_code}"
                oid = r2.get_json()["order_id"]
                with app.app_context():
                    it = db.session.get(MenuItem, iid)
                    assert it.stock == 4
                    it.is_sold_out = True      # 店員手動標示售完 (庫存仍大於 0)
                    db.session.commit()
                assert _cancel15(oid).status_code == 200
                with app.app_context():
                    it = db.session.get(MenuItem, iid)
                    assert it.stock == 5 and it.is_sold_out is True, f"取消訂單不應解除手動售完: stock={it.stock}, sold_out={it.is_sold_out}"
            finally:
                _cleanup15(item_ids=(iid,))
            return "手動售完的餐點下單回 400 且不扣庫存；取消訂單回補庫存但不解除手動售完"
        runner.check("15.1", "結帳擋下手動標示售完的餐點，取消回補不誤解除售完", t15_1)

        # 15.2 取消訂單只還原「本單實際核銷」的券；輸入了代碼但沒套用成功的訂單，取消時不可把別張已用券復活
        def t15_2():
            tid = _tid13()
            iid = _new_item15("模組15券品", price=100, stock=50)
            uid = _new_member15("模組15券會員", "0933150002")
            try:
                with app.app_context():
                    cp = Coupon(tenant_id=tid, code="M15C", title="M15券", discount_type="fixed", discount_value=10,
                                min_spend=0, reward_points=5, is_reward=True)
                    db.session.add(cp)
                    db.session.flush()
                    uc = UserCoupon(tenant_id=tid, user_id=uid, coupon_id=cp.id, code="M15C", is_used=False)
                    db.session.add(uc)
                    db.session.commit()
                    ucid = uc.id
                rA = _pos_checkout15(iid, uid, "M15C")
                rB = _pos_checkout15(iid, uid, "M15C")       # 已沒有可用的 M15C，不會套用
                assert rA.status_code == 200 and rB.status_code == 200
                oidA, oidB = rA.get_json()["order_id"], rB.get_json()["order_id"]
                with app.app_context():
                    oA, oB = db.session.get(Order, oidA), db.session.get(Order, oidB)
                    assert oA.coupon_code == "M15C" and oA.applied_user_coupon_id == ucid, "實際套用的訂單未記錄券 ID"
                    assert oB.coupon_code == "" and oB.applied_user_coupon_id is None, "沒套用成功的訂單不應記錄代碼"
                    assert db.session.get(UserCoupon, ucid).is_used is True
                assert _cancel15(oidB).status_code == 200
                with app.app_context():
                    assert db.session.get(UserCoupon, ucid).is_used is True, "取消『沒套用券』的訂單不應復活別張已用券 (券可被無限重用)"
                assert _cancel15(oidA).status_code == 200
                with app.app_context():
                    assert db.session.get(UserCoupon, ucid).is_used is False, "取消實際核銷該券的訂單後，券應被還原"
            finally:
                _cleanup15(user_ids=(uid,), item_ids=(iid,), coupon_codes=("M15C",))
            return "只還原本單核銷的那一張券；未套用成功的訂單取消不會讓已用券復活"
        runner.check("15.2", "取消訂單的優惠券還原：只還原實際核銷的券，杜絕券被無限重用", t15_2)

        # 15.3 取消訂單的點數追回改為原子更新；餘額不足時不再無聲吞掉，會記錄在稽核日誌
        def t15_3():
            iid = _new_item15("模組15點數品", price=1000, stock=50)
            uid = _new_member15("模組15點數會員", "0933150003")
            try:
                r = _pos_checkout15(iid, uid)
                assert r.status_code == 200
                earned = r.get_json()["points_earned"]
                assert earned >= 4, f"測試前置：本單應贈點，實際 {earned}"
                left = max(0, earned - 3)           # 模擬會員已把大部分贈點花掉
                with app.app_context():
                    db.session.execute(db.text("UPDATE user SET points = :p WHERE id = :u"), {"p": left, "u": uid})
                    db.session.commit()
                oid = r.get_json()["order_id"]
                assert _cancel15(oid).status_code == 200
                with app.app_context():
                    assert db.session.get(User, uid).points == 0, "追回贈點後餘額不應為負或殘留"
                    # 訂單編號在刪除測試資料後會被 SQLite 重用，取最新的一筆稽核才是這次取消產生的
                    audit = StaffAuditLog.query.execution_options(skip_tenant_filter=True).filter_by(
                        action_type="CANCEL_ORDER", target_order_id=oid).order_by(StaffAuditLog.id.desc()).first()
                    assert audit is not None and "無法追回" in (audit.details or ""), f"追不回的點數未記錄於稽核: {audit.details if audit else None}"
                    assert audit.points_delta == -left, f"稽核點數異動應為實際追回量 -{left}，實際 {audit.points_delta}"
            finally:
                _cleanup15(user_ids=(uid,), item_ids=(iid,))
            return "餘額不足時只追回實際可追回的點數，並把追不回的點數寫入稽核"
        runner.check("15.3", "取消訂單點數追回：原子更新、餘額不足不再無聲吞掉", t15_3)

        # 15.4 金額一致性：百分比折扣產生小數時，應付 + 折扣 = 小計 (四捨五入到整數元)
        def t15_4():
            tid = _tid13()
            iid = _new_item15("模組15金額品", price=105, stock=50)
            try:
                with app.app_context():
                    db.session.add(Coupon(tenant_id=tid, code="M15P", title="M15九折", discount_type="percent",
                                          discount_value=0.9, min_spend=0, reward_points=0, is_reward=False))
                    db.session.commit()
                r = _pos_checkout15(iid, None, "M15P")
                d = r.get_json()
                assert r.status_code == 200 and d["subtotal"] == 105
                assert d["total_price"] + d["discount_amount"] == d["subtotal"], f"小計≠應付+折扣: {d['subtotal']} vs {d['total_price']}+{d['discount_amount']}"
                assert d["total_price"] == 95, f"105 元 9 折 (折 10.5) 應四捨五入為 95，實際 {d['total_price']}"
            finally:
                _cleanup15(item_ids=(iid,), coupon_codes=("M15P",))
            return "105 元 9 折：應付 95、折扣 10，小計 = 應付 + 折扣"
        runner.check("15.4", "金額四捨五入與折扣總額一致 (小計 = 應付 + 折扣)", t15_4)

        # 15.5 送單冪等：還沒收到回應的重送只會建立一張訂單；成功之後刻意再點同樣的餐則是新訂單
        def t15_5():
            clear_rate_buckets()
            iid = _new_item15("模組15冪等品", price=20, stock=50)
            try:
                c1 = app.test_client()
                c1.get("/?shop=default")
                c1.get("/guest_login")
                csrf = get_client_csrf(c1, "/")
                ck = c1.get_cookie("session")
                c2 = app.test_client()
                c2.set_cookie("session", ck.value)       # 同一個工作階段 (模擬連按兩次/網路重送)
                payload = {"items": [{"id": iid, "menu_item_id": iid, "name": "m15", "quantity": 1}],
                           "payment_method": "Cash", "order_type": "內用"}
                res = {}
                bar = threading.Barrier(2)
                def _post(key, cl):
                    bar.wait()
                    res[key] = cl.post("/submit_order", json=payload, headers={"X-CSRFToken": csrf})
                ths = [threading.Thread(target=_post, args=("a", c1)), threading.Thread(target=_post, args=("b", c2))]
                for th in ths:
                    th.start()
                for th in ths:
                    th.join()
                assert res["a"].status_code == 200 and res["b"].status_code == 200, f"{res['a'].status_code}/{res['b'].status_code}"
                assert res["a"].get_json()["order_id"] == res["b"].get_json()["order_id"], "重送應回傳同一張訂單"
                with app.app_context():
                    n1 = OrderItem.query.execution_options(skip_tenant_filter=True).filter_by(menu_item_id=iid).count()
                    stock1 = db.session.get(MenuItem, iid).stock
                assert n1 == 1 and stock1 == 49, f"重送建立了多張訂單/多扣庫存: 明細={n1}, 庫存={stock1}"
                r3 = c1.post("/submit_order", json=payload, headers={"X-CSRFToken": csrf})
                assert r3.status_code == 200 and r3.get_json()["order_id"] != res["a"].get_json()["order_id"], "成功後刻意再點同樣的餐應是新訂單"
                with app.app_context():
                    assert db.session.get(MenuItem, iid).stock == 48
            finally:
                _cleanup15(item_ids=(iid,))
            return "同時重送 → 1 張訂單、庫存只扣 1；成功後再點同樣的餐 → 新訂單"
        runner.check("15.5", "送單冪等：連按/重送不重複下單，刻意再點同樣的餐不被誤擋", t15_5)

        # 15.6 公開叫號看板不得露出會員全名 (姓名遮罩)；訪客代號照常顯示
        def t15_6():
            uid = _new_member15("陳大文", "0933150006")
            tid = _tid13()
            try:
                with app.app_context():
                    db.session.add(Order(tenant_id=tid, user_id=uid, table_number="陳大文", total_price=10, status="Pending", pickup_number=77, created_at=datetime.now()))
                    db.session.add(Order(tenant_id=tid, user_id=None, table_number="訪客-1234", total_price=10, status="Pending", pickup_number=78, created_at=datetime.now()))
                    db.session.commit()
                anon = app.test_client()
                anon.get("/?shop=default")
                payload = anon.get("/api/counter_orders").get_json()
                names = [o["user_name"] for o in payload["preparing_orders"] + payload["ready_orders"]]
                assert "陳大文" not in names, "公開叫號看板仍露出會員全名"
                assert "陳○文" in names and "訪客-1234" in names, f"遮罩後的名稱或訪客代號未顯示: {names}"
            finally:
                with app.app_context():
                    db.session.execute(db.text('DELETE FROM "order" WHERE table_number IN (\'陳大文\', \'訪客-1234\')'))
                    db.session.commit()
                _cleanup15(user_ids=(uid,))
            return "公開 API 顯示『陳○文』，不露出『陳大文』；訪客-1234 原樣顯示"
        runner.check("15.6", "公開叫號看板會員姓名遮罩", t15_6)

        # 15.7 訪客的『我的訂單』以進場紀錄歸戶：訪客代號撞號也看不到別人的訂單
        def t15_7():
            tid = _tid13()
            gc = app.test_client()
            gc.get("/?shop=default")
            gc.get("/guest_login")
            with gc.session_transaction() as s:
                guest_name, log_id = s["user_name"], s["customer_log_id"]
            try:
                with app.app_context():
                    db.session.add(Order(tenant_id=tid, user_id=None, table_number=guest_name, customer_log_id=None, total_price=111, status="Pending", pickup_number=5))
                    db.session.add(Order(tenant_id=tid, user_id=None, table_number=guest_name, customer_log_id=log_id, total_price=222, status="Pending", pickup_number=6))
                    db.session.commit()
                totals = [o["total_price"] for o in gc.get("/my_orders").get_json()]
                assert totals == [222], f"訪客只應看到自己這次進場的訂單，實際: {totals}"
            finally:
                with app.app_context():
                    db.session.execute(db.text('DELETE FROM "order" WHERE table_number = :n'), {"n": guest_name})
                    db.session.commit()
            return "同代號但不同進場紀錄的訂單 (111 元) 不會出現在訪客的我的訂單"
        runner.check("15.7", "訪客我的訂單以進場紀錄歸戶，撞號不外洩他人訂單", t15_7)

        # 15.8 後台編輯不再用過期的表單值蓋掉庫存/點數 (只套用差額)
        def t15_8():
            uid = _new_member15("模組15編輯會員", "0933150008", points=100)
            iid = _new_item15("模組15編輯品", price=100, stock=50)
            try:
                csrf = _admin_csrf15()
                with app.app_context():
                    db.session.execute(db.text("UPDATE user SET points = 150 WHERE id = :u"), {"u": uid})
                    db.session.execute(db.text("UPDATE menu_item SET stock = 40 WHERE id = :i"), {"i": iid})
                    db.session.commit()
                # (a) 只改姓名/價格，表單裡的點數/庫存是開啟視窗時的舊值 -> 不可動到期間變動的數量
                r = admin_client.post(f"/admin/edit_user/{uid}", data={"name": "新名字", "phone": "0933150008", "points": "100",
                                      "loaded_points": "100", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
                assert r.status_code == 302, r.status_code
                r = admin_client.post(f"/admin/edit/{iid}", data={"name": "模組15編輯品", "price": "111", "stock": "50", "total_stock": "50",
                                      "loaded_stock": "50", "loaded_total_stock": "50", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
                assert r.status_code == 302, r.status_code
                with app.app_context():
                    assert db.session.get(User, uid).points == 150, f"只改姓名卻動到點數: {db.session.get(User, uid).points}"
                    it = db.session.get(MenuItem, iid)
                    assert it.stock == 40 and it.price == 111, f"只改價格卻動到庫存: stock={it.stock}, price={it.price}"
                # (b) 有改數量：以差額套用 (+20 點 / +10 庫存)
                admin_client.post(f"/admin/edit_user/{uid}", data={"name": "新名字", "phone": "0933150008", "points": "120",
                                  "loaded_points": "100", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
                admin_client.post(f"/admin/edit/{iid}", data={"name": "模組15編輯品", "price": "111", "stock": "60", "total_stock": "60",
                                  "loaded_stock": "50", "loaded_total_stock": "50", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
                with app.app_context():
                    assert db.session.get(User, uid).points == 170, f"點數差額套用錯誤: {db.session.get(User, uid).points}"
                    assert db.session.get(MenuItem, iid).stock == 50, f"庫存差額套用錯誤: {db.session.get(MenuItem, iid).stock}"
                # (c) 沒帶 loaded_*：維持舊行為 (以表單值為準)
                admin_client.post(f"/admin/edit_user/{uid}", data={"name": "新名字", "phone": "0933150008", "points": "10", "csrf_token": csrf},
                                  headers={"X-CSRFToken": csrf})
                with app.app_context():
                    assert db.session.get(User, uid).points == 10
            finally:
                _cleanup15(user_ids=(uid,), item_ids=(iid,))
            return "只改姓名/價格不動期間變動的點數/庫存；有改數量以差額套用；舊表單維持絕對值"
        runner.check("15.8", "後台編輯會員/餐點：以差額套用，不蓋掉編輯期間的點數與庫存變動", t15_8)

        # 15.9 後台警示頁面不可被會員姓名 (使用者可控) 中的 </script> 截斷
        def t15_9():
            evil = "</script><b>x"
            ua = _new_member15(evil, "0933150009")
            ub = _new_member15("正常會員", "0933150010")
            try:
                csrf = _admin_csrf15()
                r = admin_client.post(f"/admin/edit_user/{ub}", data={"name": "正常會員", "phone": "0933150009", "points": "0", "csrf_token": csrf},
                                      headers={"X-CSRFToken": csrf})
                body = r.get_data(as_text=True)
                assert r.status_code == 409, r.status_code
                assert "</script><b>x" not in body and body.count("</script>") == 1, f"警示頁面被姓名中的 </script> 截斷: {body[:200]}"
            finally:
                _cleanup15(user_ids=(ua, ub))
            return "重複電話警示頁面含惡意姓名時仍只有 1 個 </script>"
        runner.check("15.9", "後台警示頁面輸出跳脫 (姓名含 </script> 不可截斷腳本)", t15_9)

        # 15.10 Excel 匯出不可讓 "=" 開頭的姓名變成公式；匯入 0 點會員維持 0 點
        def t15_10():
            uid = _new_member15("=1+1+M15", "0933150101", points=3)
            try:
                csrf = _admin_csrf15()
                r = admin_client.post("/admin/export_excel", data={"tables": "User", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
                assert r.status_code == 200, r.status_code
                wb = openpyxl.load_workbook(io.BytesIO(r.data))
                ws = wb["會員資料(User)"]
                cells = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=1+1+M15")]
                assert cells, "匯出檔找不到測試會員姓名"
                assert all(c.data_type == "s" for c in cells), f"以 = 開頭的姓名被寫成公式: {[c.data_type for c in cells]}"
            finally:
                _cleanup15(user_ids=(uid,))
            # 匯入：點數欄位為 0 的新會員維持 0 點；整個檔案沒有點數欄才給預設 20 點
            wb2 = openpyxl.Workbook()
            ws2 = wb2.active
            ws2.title = "會員資料(User)"
            ws2.append(["姓名(Name)", "電話(Phone)", "紅利點數(Points)"])
            ws2.append(["M15零點", "0933150011", 0])
            ws2.append(["M15有點", "0933150012", 7])
            buf = io.BytesIO()
            wb2.save(buf)
            wb3 = openpyxl.Workbook()
            ws3 = wb3.active
            ws3.title = "會員資料(User)"
            ws3.append(["姓名(Name)", "電話(Phone)"])
            ws3.append(["M15無點數欄", "0933150013"])
            buf3 = io.BytesIO()
            wb3.save(buf3)
            try:
                csrf = _admin_csrf15()
                for data_buf, nm in ((buf, "a.xlsx"), (buf3, "b.xlsx")):
                    data_buf.seek(0)
                    ri = admin_client.post("/admin/import_smart", data={"file": (data_buf, nm), "csrf_token": csrf},
                                           content_type="multipart/form-data", headers={"X-CSRFToken": csrf})
                    assert ri.status_code == 200, f"匯入失敗: {ri.status_code} {ri.get_data(as_text=True)[:200]}"
                with app.app_context():
                    pts = {u.phone: u.points for u in User.query.execution_options(skip_tenant_filter=True).filter(
                        User.phone.in_(["0933150011", "0933150012", "0933150013"])).all()}
                assert pts == {"0933150011": 0, "0933150012": 7, "0933150013": 20}, f"匯入點數不符預期: {pts}"
            finally:
                _cleanup15(phones=("0933150011", "0933150012", "0933150013"))
            return "= 開頭姓名匯出為純文字；匯入 0 點維持 0、有點數照填、無點數欄才給 20"
        runner.check("15.10", "Excel 匯出防公式注入，匯入不再把 0 點會員補成 20 點", t15_10)

        # 15.11 後台表單遇到亂填的數字不再 500
        def t15_11():
            csrf = _admin_csrf15()
            r1 = admin_client.post("/admin/add", data={"name": "模組15壞價", "price": "abc", "stock": "x", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
            assert r1.status_code == 400, f"單價亂填應回 400，實際 {r1.status_code}"
            r2 = admin_client.post("/admin/add_coupon", data={"code": "M15X", "title": "T", "discount_type": "fixed", "discount_value": "10",
                                   "min_spend": "abc", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
            assert r2.status_code == 302, f"門檻亂填不應 500: {r2.status_code}"
            r3 = admin_client.post("/admin/add_combo", data={"name": "壞套餐", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
            assert r3.status_code < 500, f"缺主餐的套餐表單不應 500: {r3.status_code}"
            from app.utils import safe_int, safe_float
            assert safe_int(float("inf")) == 0 and safe_int("1e999") == 0 and safe_float("nan", 1.5) == 1.5
            _cleanup15(coupon_codes=("M15X",))
            return "單價亂填 400、門檻亂填不 500、缺主餐套餐不 500；safe_int 不再被 Infinity 擊倒"
        runner.check("15.11", "後台表單數字解析：亂填不 500 (含 Infinity)", t15_11)

        # 15.12 安全標頭與 CSP；Session Cookie 設定
        def t15_12():
            r = app.test_client().get("/")
            h = r.headers
            assert h.get("X-Content-Type-Options") == "nosniff" and h.get("X-Frame-Options") == "SAMEORIGIN", "缺少基本安全標頭"
            csp = h.get("Content-Security-Policy", "")
            assert "frame-ancestors 'self'" in csp and "object-src 'none'" in csp and "script-src" in csp, f"缺少 CSP: {csp[:120]}"
            assert "camera=(self)" in h.get("Permissions-Policy", ""), "相機權限政策未設定 (人臉註冊需要相機)"
            assert "Content-Security-Policy" not in app.test_client().get("/api/counter_orders").headers, "JSON 回應不需要 CSP"
            from app.config import Config as _C
            assert _C.CSP_MODE in ("enforce", "report-only", "off")
            assert app.config.get("SESSION_COOKIE_HTTPONLY") is True and app.config.get("SESSION_COOKIE_SAMESITE") == "Lax"
            return "HTML 回應帶 CSP/nosniff/X-Frame-Options/Permissions-Policy，JSON 不帶 CSP"
        runner.check("15.12", "安全標頭與內容安全政策 (CSP)", t15_12)

        # 15.13 SECRET_KEY 不再使用寫死的公開金鑰：自動產生並持久化
        def t15_13():
            import app.config as cfg_mod
            old_base, old_env = cfg_mod.BASE_DIR, os.environ.pop("SECRET_KEY", None)
            tmp = tempfile.mkdtemp(prefix="kiosk_secret_")
            try:
                cfg_mod.BASE_DIR = tmp
                k1 = cfg_mod._load_or_create_secret_key()
                k2 = cfg_mod._load_or_create_secret_key()
                assert k1 == k2 and len(k1) >= 64, "金鑰未持久化或長度不足"
                assert "kiosk-fallback-secret-key" not in k1, "仍使用寫死的公開金鑰"
                assert os.path.exists(os.path.join(tmp, "database", ".secret_key"))
                os.environ["SECRET_KEY"] = "from-env-value"
                assert cfg_mod._load_or_create_secret_key() == "from-env-value", ".env 的 SECRET_KEY 應優先"
            finally:
                cfg_mod.BASE_DIR = old_base
                os.environ.pop("SECRET_KEY", None)
                if old_env is not None:
                    os.environ["SECRET_KEY"] = old_env
                shutil.rmtree(tmp, ignore_errors=True)
            return "未設定時自動產生 64 字元隨機金鑰並持久化；.env 設定優先"
        runner.check("15.13", "SECRET_KEY 自動產生並持久化 (不再使用寫死金鑰)", t15_13)

        # 15.14 預設負責人帳號：驗證一般啟動、救援重設與缺少 .env 憑證時輪替
        def t15_14():
            from app.config import Config as _C
            old_config = (_C.ADMIN_USERNAME, _C.ADMIN_PASSWORD, _C.ADMIN_PASSWORD_HASH,
                          _C.ADMIN_CREDENTIALS_GENERATED, _C.RESET_ADMIN_PASSWORD_ON_START)
            with app.app_context():
                acc = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(username="admin_test").first()
                acc.set_password("Changed!Pass1234")
                db.session.commit()
            run_database_migrations(app)
            with app.app_context():
                acc = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(username="admin_test").first()
                assert acc.check_password("Changed!Pass1234"), "重啟 (遷移) 把負責人自行修改的密碼洗掉了"
            try:
                _C.RESET_ADMIN_PASSWORD_ON_START = True
                run_database_migrations(app)
                with app.app_context():
                    acc = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(username="admin_test").first()
                    assert acc.check_password("AdminPass!2026"), "救援開關開啟時應重設為 .env 的密碼"

                _C.RESET_ADMIN_PASSWORD_ON_START = False
                _C.ADMIN_USERNAME = "owner-generated-test"
                _C.ADMIN_PASSWORD = "Generated!Pass2026"
                _C.ADMIN_PASSWORD_HASH = generate_password_hash(_C.ADMIN_PASSWORD)
                _C.ADMIN_CREDENTIALS_GENERATED = True
                run_database_migrations(app)
                with app.app_context():
                    rotated = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(
                        username="owner-generated-test"
                    ).first()
                    assert rotated is not None and rotated.role == "owner" and rotated.is_active
                    assert rotated.check_password("Generated!Pass2026"), "產生的新密碼未寫入資料庫"
                    assert StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(
                        username="admin_test"
                    ).first() is None, "舊管理員帳號仍可使用"
            finally:
                with app.app_context():
                    rotated = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(
                        username="owner-generated-test"
                    ).first()
                    if rotated:
                        rotated.username = "admin_test"
                        rotated.set_password("AdminPass!2026")
                        db.session.commit()
                (_C.ADMIN_USERNAME, _C.ADMIN_PASSWORD, _C.ADMIN_PASSWORD_HASH,
                 _C.ADMIN_CREDENTIALS_GENERATED, _C.RESET_ADMIN_PASSWORD_ON_START) = old_config
            with app.app_context():
                acc = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(username="admin_test").first()
                assert acc.check_password("AdminPass!2026"), "測試清理未恢復管理員憑證"
            return "一般啟動保留密碼、救援開關可重設；缺少 .env 帳密時替換預設 owner 並使舊帳密失效"
        runner.check("15.14", "預設負責人密碼：重啟不被洗掉，救援開關才重設", t15_14)

        # 15.15 分散式鎖：Redis 中斷時降級為「依名稱區分」的本地鎖，結帳不受影響
        def t15_15():
            import app.extensions as ext
            import redis as _redis

            class _BadRedis:
                def lock(self, *a, **k):
                    raise _redis.exceptions.ConnectionError("down")
            iid = _new_item15("模組15鎖品", price=10, stock=5)
            old = ext.redis_client
            ext.redis_client = _BadRedis()
            try:
                with ext.acquire_distributed_lock("lock:m15:a", timeout=2, blocking_timeout=1):
                    with ext.acquire_distributed_lock("lock:m15:b", timeout=2, blocking_timeout=1):
                        pass    # 不同名稱互不阻塞 (舊版全域單一鎖會讓巢狀取鎖卡住)
                timed_out = False
                with ext.acquire_distributed_lock("lock:m15:c", timeout=2, blocking_timeout=0.2):
                    try:
                        with ext.acquire_distributed_lock("lock:m15:c", timeout=2, blocking_timeout=0.2):
                            pass
                    except TimeoutError:
                        timed_out = True
                assert timed_out, "同名鎖應互斥"
                r = _pos_checkout15(iid)
                assert r.status_code == 200, f"Redis 中斷時結帳應降級成功: {r.status_code} {r.get_data(as_text=True)[:150]}"
            finally:
                ext.redis_client = old
                _cleanup15(item_ids=(iid,))
            return "Redis 例外 → 本地鎖；不同名稱不互相阻塞、同名互斥；Redis 中斷時 POS 結帳 200"
        runner.check("15.15", "分散式鎖 Redis 中斷降級與依名稱區分的本地鎖", t15_15)

        # 15.16 SSE 連線被配額淘汰時會收到結束訊號 (釋放伺服器執行緒)
        def t15_16():
            from app.services.event_bus import order_event_bus
            with order_event_bus._lock:
                order_event_bus._subscribers.clear()
            qs = [order_event_bus.subscribe(1, client_ip="198.51.100.151", is_staff=False) for _ in range(13)]
            evicted = qs[:3]
            for q in evicted:
                assert q.get_nowait() is None, "被淘汰的連線沒有收到結束訊號，會永遠占用一條伺服器執行緒"
            with order_event_bus._lock:
                order_event_bus._subscribers.clear()
            return "單 IP 超過 10 條時，最舊的 3 條都收到結束訊號"
        runner.check("15.16", "SSE 被淘汰的連線收到結束訊號，不再占用執行緒", t15_16)

        # 15.17 會員照片保護：路徑正規化 (./、大小寫) 與跨店隔離
        def t15_17():
            tid = _tid13()
            static_member_dir = os.path.join(app.static_folder, "member")
            os.makedirs(static_member_dir, exist_ok=True)
            fname = "m15_guard_photo.jpg"
            fpath = os.path.join(static_member_dir, fname)
            with open(fpath, "wb") as fh:
                fh.write(make_test_jpeg_bytes().read())
            with app.app_context():
                u = User(tenant_id=tid, name="模組15照片會員", phone="0933150017", photo_path=fname, points=0)
                db.session.add(u)
                db.session.commit()
                uid = u.id
            try:
                anon = app.test_client()
                assert anon.get(f"/static/member/{fname}").status_code == 403
                assert anon.get(f"/static/./member/{fname}").status_code == 403, "用 ./ 繞過照片保護"
                assert anon.get(f"/static/MEMBER/{fname}").status_code == 403, "用大小寫繞過照片保護 (Windows 不分大小寫)"
                assert admin_client.get(f"/static/member/{fname}").status_code == 200, "同店員工應可讀取"
                cb = _owner_b_client15()
                assert cb.get(f"/static/member/{fname}").status_code == 403, "他店員工不可讀取本店會員照片"
            finally:
                if os.path.exists(fpath):
                    os.remove(fpath)
                _cleanup15(user_ids=(uid,))
            return "匿名 (含 ./、大小寫變體) 403、同店員工 200、他店員工 403"
        runner.check("15.17", "會員照片保護：路徑正規化與跨店隔離", t15_17)

        # 15.18 註冊：同一張臉不可用不同手機重複註冊
        def t15_18():
            clear_rate_buckets()
            prev = auth_route_mod.extract_feature
            vec_a = np.zeros(128, dtype=np.float32)
            vec_a[7] = 1.0
            vec_b = np.zeros(128, dtype=np.float32)
            vec_b[9] = 1.0
            cur = {"v": vec_a}
            auth_route_mod.extract_feature = lambda p: cur["v"].copy()
            try:
                c = app.test_client()
                c.get("/?shop=default")
                csrf = get_client_csrf(c, "/register")
                def _reg(phone):
                    return c.post("/register", data={"name": "模組15註冊", "phone": phone, "pin": "1234",
                                  "photo": (make_test_jpeg_bytes(200, 200), "p.jpg")},
                                  content_type="multipart/form-data", headers={"X-CSRFToken": csrf})
                assert _reg("0933150018").status_code == 302, "第一次註冊應成功"
                r2 = _reg("0933150019")
                assert r2.status_code == 409, f"同一張臉用第二支手機註冊應被擋 (409)，實際 {r2.status_code}"
                with app.app_context():
                    assert User.query.execution_options(skip_tenant_filter=True).filter_by(phone="0933150019").count() == 0
                cur["v"] = vec_b
                assert _reg("0933150020").status_code == 302, "不同的臉應可註冊"
            finally:
                auth_route_mod.extract_feature = prev
                _cleanup15(phones=("0933150018", "0933150019", "0933150020"))
            return "同臉第二支手機 409 且未建立會員；不同臉可註冊"
        runner.check("15.18", "註冊防重複人臉 (防註冊禮點數農場與同臉多帳號)", t15_18)

        # 15.19 上傳圖片限制：像素數與格式
        def t15_19():
            from werkzeug.datastructures import FileStorage
            from app.services.cv_service import save_and_fix_image, MAX_UPLOAD_IMAGE_PIXELS
            dest = os.path.join(TEST_DB_DIR, "img_limit_test.jpg")
            def _fs(img, fmt):
                b = io.BytesIO()
                img.save(b, format=fmt)
                b.seek(0)
                return FileStorage(stream=b, filename=f"x.{fmt.lower()}")
            side = int(MAX_UPLOAD_IMAGE_PIXELS ** 0.5) + 200
            assert save_and_fix_image(_fs(Image.new("RGB", (side, side)), "PNG"), dest) is False, "超大像素圖未被拒絕"
            assert save_and_fix_image(_fs(Image.new("RGB", (64, 64), (200, 100, 50)), "GIF"), dest) is False, "不支援的格式 (GIF) 未被拒絕"
            assert save_and_fix_image(_fs(Image.new("RGB", (320, 240), (200, 100, 50)), "JPEG"), dest) is True and os.path.exists(dest)
            os.remove(dest)
            return f"超過 {MAX_UPLOAD_IMAGE_PIXELS:,} 像素與 GIF 被拒絕；一般 JPEG 通過"
        runner.check("15.19", "上傳圖片像素數與格式限制 (防免登入端點被大圖耗盡記憶體)", t15_19)

        # 15.20 JSON 本文不合法/不是物件時回 JSON 錯誤，不再 415/500 HTML
        def t15_20():
            clear_rate_buckets()
            c = app.test_client()
            c.get("/?shop=default")
            csrf = get_client_csrf(c, "/register")
            r1 = c.post("/phone_login", data="not json", content_type="text/plain", headers={"X-CSRFToken": csrf, "X-Forwarded-For": "198.51.100.161"})
            r2 = c.post("/phone_login", json=[1, 2, 3], headers={"X-CSRFToken": csrf, "X-Forwarded-For": "198.51.100.162"})
            for r in (r1, r2):
                assert r.status_code == 400 and r.get_json() and r.get_json().get("success") is False, f"非法 JSON 應回 400 JSON: {r.status_code}"
            return "非 JSON 本文與陣列根節點都回 400 JSON"
        runner.check("15.20", "非法 JSON 請求回 400 JSON (不再 415/500)", t15_20)

        # 15.21 紅利設定每店唯一 (含併發首次建立)，歷史重複列在遷移時清除
        def t15_21():
            from app.routes.common import get_or_create_reward_setting
            with app.app_context():
                t = Tenant(name="M15設定店", slug="m15-rs", is_active=True)
                db.session.add(t)
                db.session.commit()
                tid2 = t.id
            errors = []
            def _worker():
                try:
                    with app.test_request_context("/"):
                        _flask_g.tenant_id = tid2
                        get_or_create_reward_setting(tid2)
                except Exception as e:  # noqa: BLE001
                    errors.append(repr(e))
            try:
                ths = [threading.Thread(target=_worker) for _ in range(6)]
                for th in ths:
                    th.start()
                for th in ths:
                    th.join()
                assert not errors, f"併發首次建立紅利設定出錯: {errors[:2]}"
                with app.app_context():
                    assert db.session.execute(db.text("SELECT COUNT(*) FROM reward_setting WHERE tenant_id = :t"), {"t": tid2}).scalar() == 1, "併發建立產生了重複的紅利設定"
                    try:
                        db.session.execute(db.text("INSERT INTO reward_setting (tenant_id, is_enabled, points_per_dollar, max_discount_per_order, spend_per_point) VALUES (:t,1,1,0,100)"), {"t": tid2})
                        db.session.commit()
                        raise AssertionError("唯一索引未生效")
                    except _IntegrityError:
                        db.session.rollback()
                    # 遷移的去重：移除索引、造出重複列，遷移後只剩一筆且索引重建
                    db.session.execute(db.text('DROP INDEX IF EXISTS "ux_reward_setting_tenant"'))
                    db.session.execute(db.text("INSERT INTO reward_setting (tenant_id, is_enabled, points_per_dollar, max_discount_per_order, spend_per_point) VALUES (:t,1,1,0,100)"), {"t": tid2})
                    db.session.commit()
                run_database_migrations(app)
                with app.app_context():
                    assert db.session.execute(db.text("SELECT COUNT(*) FROM reward_setting WHERE tenant_id = :t"), {"t": tid2}).scalar() == 1
                    assert db.session.execute(db.text("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='ux_reward_setting_tenant'")).scalar() == 1
            finally:
                with app.app_context():
                    db.session.execute(db.text("DELETE FROM reward_setting WHERE tenant_id = :t"), {"t": tid2})
                    db.session.execute(db.text("DELETE FROM tenant WHERE id = :t"), {"t": tid2})
                    db.session.commit()
            return "6 個執行緒同時首次建立只留 1 筆；唯一索引生效；遷移清除歷史重複列並重建索引"
        runner.check("15.21", "紅利設定每店唯一：併發首次建立與歷史重複列清除", t15_21)

        # 15.22 後台儀表板/即時訂單只載入「今天 + 待製作」；稽核新增的操作都有紀錄
        def t15_22():
            tid = _tid13()
            old_day = datetime.now() - timedelta(days=3)
            with app.app_context():
                db.session.add(Order(tenant_id=tid, user_id=None, table_number="M15舊單甲", total_price=11, status="Completed", pickup_number=1, created_at=old_day))
                db.session.add(Order(tenant_id=tid, user_id=None, table_number="M15舊單乙", total_price=22, status="Pending", pickup_number=2, created_at=old_day))
                db.session.commit()
            try:
                live = admin_client.get("/api/admin_live_orders")
                assert live.status_code == 200
                data = live.get_json()
                names = [o["user_name"] for o in data["orders"]]
                assert "M15舊單甲" not in names and "M15舊單乙" not in names, "即時訂單不應包含舊日期的訂單"
                assert data["analytics"]["stale_pending_count"] >= 1, "前幾日還沒結案的訂單應以 stale_pending_count 提示"
                assert admin_client.get("/admin").status_code == 200

                csrf = _admin_csrf15()
                # 優惠券/套餐/紅利設定/會員券切換/廚房一鍵出餐 都要有稽核紀錄
                admin_client.post("/admin/add_coupon", data={"code": "M15AUD", "title": "稽核測試", "discount_type": "fixed", "discount_value": "5", "csrf_token": csrf},
                                  headers={"X-CSRFToken": csrf})
                k_csrf = get_client_csrf(kitchen_client, "/kitchen")
                kitchen_client.post("/api/kitchen_complete_all", json={}, headers={"X-CSRFToken": k_csrf})
                with app.app_context():
                    kinds = {a.action_type for a in StaffAuditLog.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=tid).all()}
                assert "COUPON_CHANGE" in kinds, "新增優惠券沒有稽核紀錄"
                assert "KITCHEN_COMPLETE_ALL" in kinds, "廚房一鍵出餐沒有稽核紀錄"
            finally:
                with app.app_context():
                    db.session.execute(db.text('DELETE FROM "order" WHERE table_number LIKE \'M15舊單%\''))
                    db.session.commit()
                _cleanup15(coupon_codes=("M15AUD",))
            return "舊日期已出餐訂單不載入、舊的待製作訂單仍計入；優惠券異動與廚房一鍵出餐有稽核紀錄"
        runner.check("15.22", "儀表板只載入今日與待製作訂單；新增的稽核紀錄", t15_22)

        # 15.23 編輯會員的稽核只記真正變動的欄位：只改手機時，不寫「點數 A -> A」也不誤報重設 PIN
        def t15_23():
            uid = _new_member15("模組15稽核會員", "0933150023", points=40)
            try:
                csrf = _admin_csrf15()
                def _last_audit():
                    with app.app_context():
                        a = StaffAuditLog.query.execution_options(skip_tenant_filter=True).filter_by(
                            action_type="EDIT_USER", target_user_id=uid).order_by(StaffAuditLog.id.desc()).first()
                        return a.details if a else None
                r = admin_client.post(f"/admin/edit_user/{uid}", data={"name": "模組15稽核會員", "phone": "0933150024", "points": "40",
                                      "loaded_points": "40", "pin": "", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
                assert r.status_code == 302
                d = _last_audit()
                assert d and "手機 0933150023 -> 0933150024" in d, f"稽核沒有記錄手機異動: {d}"
                assert "點數" not in d and "PIN" not in d, f"沒變動的點數/PIN 不應出現在稽核: {d}"
                # 真的改點數與 PIN 時才記錄
                admin_client.post(f"/admin/edit_user/{uid}", data={"name": "模組15稽核會員", "phone": "0933150024", "points": "55",
                                  "loaded_points": "40", "pin": "2468", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
                d2 = _last_audit()
                assert "點數 40 -> 55" in d2 and "重設安全PIN碼" in d2, f"有變動的欄位應被記錄: {d2}"
                # 完全沒變動：不寫稽核
                with app.app_context():
                    n_before = StaffAuditLog.query.execution_options(skip_tenant_filter=True).filter_by(action_type="EDIT_USER", target_user_id=uid).count()
                admin_client.post(f"/admin/edit_user/{uid}", data={"name": "模組15稽核會員", "phone": "0933150024", "points": "55",
                                  "loaded_points": "55", "csrf_token": csrf}, headers={"X-CSRFToken": csrf})
                with app.app_context():
                    n_after = StaffAuditLog.query.execution_options(skip_tenant_filter=True).filter_by(action_type="EDIT_USER", target_user_id=uid).count()
                assert n_after == n_before, "完全沒有異動時不應新增稽核紀錄"
                # 表單層：PIN 欄位必須阻止瀏覽器自動帶入已儲存密碼
                with open(os.path.join(BASE_DIR, "templates", "admin.html"), encoding="utf-8") as fh:
                    html = fh.read()
                assert 'id="edit-user-pin"' in html and 'autocomplete="new-password"' in html.split('id="edit-user-pin"')[1].split(">")[0], "PIN 欄位未設定 autocomplete=new-password"
            finally:
                _cleanup15(user_ids=(uid,))
            return "只改手機：稽核寫『手機 A -> B』且不含點數/PIN；有變動才記；無異動不寫；PIN 欄位禁止瀏覽器自動帶入"
        runner.check("15.23", "編輯會員稽核只記真正變動的欄位，PIN 欄位不被瀏覽器自動填入", t15_23)

        # 15.24 營運分析欄位完整且口徑正確：舊日期的待製作單不灌水預估出餐；已完成數與平均出餐時間有值
        def t15_24():
            from app.services.order_service import build_order_analytics
            tid = _tid13()
            now = datetime.now()
            with app.app_context():
                olds = [Order(tenant_id=tid, table_number="M15分析舊單", total_price=10, status="Pending", pickup_number=1, created_at=now - timedelta(days=2)) for _ in range(5)]
                today_done = Order(tenant_id=tid, table_number="M15分析今單", total_price=100, status="Completed", pickup_number=2, created_at=now - timedelta(minutes=10), completed_at=now - timedelta(minutes=4))
                today_pts = Order(tenant_id=tid, table_number="M15分析今單", total_price=0, status="PickedUp", pickup_number=3, created_at=now - timedelta(minutes=8), completed_at=now - timedelta(minutes=6))
                today_pending = Order(tenant_id=tid, table_number="M15分析今單", total_price=50, status="Pending", pickup_number=4, created_at=now - timedelta(minutes=1))
                db.session.add_all(olds + [today_done, today_pending, today_pts])
                db.session.commit()
                ids = [o.id for o in olds + [today_done, today_pending, today_pts]]
                mine = Order.query.execution_options(skip_tenant_filter=True).filter(Order.id.in_(ids)).all()
                a = build_order_analytics(mine, limit=3)
            try:
                for key in ("completed_today", "avg_order_time", "stale_pending_count", "pending_count", "eta_minutes", "avg_order_value"):
                    assert key in a, f"分析結果缺少欄位 {key} (頁面會顯示 undefined/空白)"
                assert a["pending_count"] == 1 and a["eta_minutes"] == 4, f"只應計入今天的待製作單，且以實際平均 4.0 分/單估算 (1×4.0=4): {a}"
                assert a["stale_pending_count"] == 5, f"前幾日未結案訂單數錯誤: {a['stale_pending_count']}"
                assert a["completed_today"] == 2, f"已完成今日訂單應含已出餐與已取餐: {a['completed_today']}"
                assert a["avg_order_time"] == 4.0, f"平均出餐時間應為 (6+2)/2=4.0 分鐘，實際 {a['avg_order_time']}"
                assert a["avg_order_value"] == 75.0, f"平均客單價應排除 0 元的純點數單 (100+50)/2=75，實際 {a['avg_order_value']}"
                # 即時 API 也要帶出同樣欄位，頁面不按 F5 才會更新
                live = admin_client.get("/api/admin_live_orders").get_json()["analytics"]
                assert "completed_today" in live and "avg_order_time" in live and "stale_pending_count" in live
            finally:
                with app.app_context():
                    db.session.execute(db.text('DELETE FROM "order" WHERE table_number IN (\'M15分析舊單\', \'M15分析今單\')'))
                    db.session.commit()
            return "待製作/預估只算今天 (舊單另列 5 張)、已完成 2、平均出餐 4.0 分、客單價排除 0 元單；即時 API 同樣帶出"
        runner.check("15.24", "營運分析口徑與欄位：預估出餐不被舊單灌水、已完成數與平均出餐時間不再 undefined", t15_24)

        # 15.25 換日自動結案：前一日(含更早)還在製作中的訂單一律改為已出餐；今天的、已取餐的不動
        def t15_25():
            import app.services.order_service as osvc
            tid = _tid13()
            now = datetime.now()
            old = now - timedelta(days=1, hours=2)
            with app.app_context():
                t2 = Tenant(name="M15結案店", slug="m15-ac", is_active=True)
                db.session.add(t2)
                db.session.commit()
                tid2 = t2.id
                mk = lambda t, st, created, **kw: Order(tenant_id=t, table_number="M15自動結案", total_price=10, status=st, pickup_number=1, created_at=created, **kw)
                rows = {
                    "old_a": mk(tid, "Pending", old),
                    "old_other": mk(tid2, "Pending", now - timedelta(days=5)),
                    "today": mk(tid, "Pending", now - timedelta(minutes=1)),
                    "old_done": mk(tid, "Completed", old, completed_at=old + timedelta(minutes=5)),
                    "old_picked": mk(tid, "PickedUp", old, completed_at=old + timedelta(minutes=5)),
                    "old_cancel": mk(tid, "Cancelled", old),
                }
                db.session.add_all(rows.values())
                db.session.commit()
                ids = {k: v.id for k, v in rows.items()}
            try:
                with app.app_context():
                    closed = osvc.auto_close_stale_orders()
                    assert closed.get(tid) == 1 and closed.get(tid2) == 1, f"各店結案張數不符: {closed}"
                    got = {k: db.session.get(Order, i) for k, i in ids.items()}
                    assert got["old_a"].status == "Completed" and got["old_a"].completed_at is None, "舊的製作中訂單應改為已出餐，且不填假的出餐時間"
                    assert got["old_other"].status == "Completed", "其他店家的舊訂單也要結案"
                    assert got["today"].status == "Pending", "今天的待製作訂單不可被動到"
                    assert got["old_done"].status == "Completed" and got["old_picked"].status == "PickedUp" and got["old_cancel"].status == "Cancelled", "已出餐/已取餐/已取消的訂單不應被改動"
                    audits = db.session.execute(db.text(
                        "SELECT tenant_id, staff_username, details FROM staff_audit_log WHERE action_type = 'AUTO_CLOSE_STALE_ORDERS' AND tenant_id IN (:a, :b)"
                    ), {"a": tid, "b": tid2}).fetchall()
                    assert {r[0] for r in audits} == {tid, tid2} and all(r[1] == "system" for r in audits), f"每家有結案的店都應各有一筆系統稽核: {audits}"
                    # 再執行一次不會重複結案 (條件式更新)
                    assert osvc.auto_close_stale_orders() == {}, "已結案的訂單不應被重複處理"

                # 跨日不重啟：重設「今天已處理」旗標，下一個請求就會結案
                with app.app_context():
                    late = mk(tid, "Pending", old)
                    db.session.add(late)
                    db.session.commit()
                    late_id = late.id
                osvc._AUTO_CLOSE_STATE["date"] = None
                app.test_client().get("/api/counter_orders")
                with app.app_context():
                    assert db.session.get(Order, late_id).status == "Completed", "跨日後的第一個請求應觸發自動結案"
                assert osvc._AUTO_CLOSE_STATE["date"] == datetime.now().date()
            finally:
                with app.app_context():
                    db.session.execute(db.text('DELETE FROM "order" WHERE table_number = \'M15自動結案\''))
                    db.session.execute(db.text("DELETE FROM staff_audit_log WHERE action_type = 'AUTO_CLOSE_STALE_ORDERS' AND tenant_id IN (:a, :b)"), {"a": tid, "b": tid2})
                    db.session.execute(db.text("DELETE FROM tenant WHERE id = :t"), {"t": tid2})
                    db.session.commit()
            return "前一日(含更早)的製作中訂單全店改為已出餐且不填假出餐時間；今天/已取餐/已取消不動；各店寫系統稽核；跨日首個請求觸發"
        runner.check("15.25", "換日自動結案：未切換狀態的舊訂單一律改為完成", t15_25)

        # 15.26 預估出餐 = 待製作張數 × 今日實際平均出餐時間 (排除極端值)；沒有紀錄時用預設值
        def t15_26():
            from app.services.order_service import build_order_analytics, mean_without_outliers, DEFAULT_MINUTES_PER_ORDER
            m, n = mean_without_outliers([4, 4, 5, 5, 4, 60])
            assert n == 5 and round(m, 1) == 4.4, f"IQR 應排除 60 分鐘的離群值: {m}, {n}"
            m, n = mean_without_outliers([4, 5, 200])
            assert n == 2 and m == 4.5, f"超過上限的 200 分鐘應被排除: {m}, {n}"
            assert mean_without_outliers([]) == (0.0, 0)

            now = datetime.now()
            def _o(status, minutes_to_done=None, age=30):
                created = now - timedelta(minutes=age)
                done = created + timedelta(minutes=minutes_to_done) if minutes_to_done is not None else None
                return Order(total_price=10, status=status, pickup_number=1, created_at=created, completed_at=done)
            orders = [_o("Completed", d) for d in (4, 4, 5, 5, 4)]
            orders += [_o("Completed", 120)]                 # 忘了按出餐，隔兩小時才補按 -> 極端值
            orders += [_o("Completed", None)]                # 自動結案 (沒有出餐時間) -> 不納入
            orders += [_o("Pending", age=1) for _ in range(3)]
            a = build_order_analytics(orders)
            assert a["avg_order_time"] == 4.4 and a["eta_based_on_samples"] == 5, f"平均出餐時間應為排除極端值後的 4.4: {a}"
            assert a["pending_count"] == 3 and a["eta_minutes"] == 14, f"預估出餐應為 ceil(3×4.4)=14 分鐘，實際 {a['eta_minutes']}"
            assert a["eta_per_order_minutes"] == 4.4

            a2 = build_order_analytics([_o("Pending", age=1), _o("Pending", age=2)])
            assert a2["eta_based_on_samples"] == 0 and a2["eta_minutes"] == int(2 * DEFAULT_MINUTES_PER_ORDER), f"沒有出餐紀錄時應用預設值: {a2}"
            a3 = build_order_analytics([_o("Completed", 4)])
            assert a3["pending_count"] == 0 and a3["eta_minutes"] == 0, "沒有待製作訂單時預估出餐應為 0"
            return "排除 60/120/200 分鐘的極端值與自動結案單；3 張待製作 × 4.4 → 14 分鐘；無紀錄用預設 3 分/單"
        runner.check("15.26", "預估出餐依實際平均出餐時間並排除極端值", t15_26)

        # 15.27 熱門餐點排行要有銷售額 (價錢)；模板讀取的分析欄位必須都真的存在 (防止再出現 undefined/空白)
        def t15_27():
            from app.services.order_service import build_order_analytics
            now = datetime.now()
            def _order(items, status="Completed"):
                o = Order(total_price=0, status=status, pickup_number=1, created_at=now - timedelta(minutes=5), completed_at=now - timedelta(minutes=1))
                o.items = [OrderItem(item_name=n, price=p, quantity=q) for n, p, q in items]
                return o
            orders = [
                _order([("香檸氣泡飲", 80, 1), ("🎁 [點數兌換] 紅茶", 0, 1)]),
                _order([("香檸氣泡飲", 80, 2), ("雞排", 70, 1)]),
                _order([("雞排", 70, 1)], status="Cancelled"),     # 已取消不計入
            ]
            a = build_order_analytics(orders, limit=5)
            top = {t["name"]: t for t in a["top_items"]}
            assert top["香檸氣泡飲"]["quantity"] == 3 and top["香檸氣泡飲"]["revenue"] == 240, f"銷售額應為 80×3=240: {top['香檸氣泡飲']}"
            assert top["雞排"]["quantity"] == 1 and top["雞排"]["revenue"] == 70, f"已取消的訂單不應計入: {top['雞排']}"
            assert top["紅茶"]["revenue"] == 0, "點數兌換的免費品項銷售額應為 0"
            assert a["top_items"][0]["name"] == "香檸氣泡飲", "應依份數由多到少排序"
            assert len(build_order_analytics(orders, limit=1)["top_items"]) == 1

            # 模板讀取的 analytics.<欄位> 與 item.revenue 必須都由後端提供
            with open(os.path.join(BASE_DIR, "templates", "admin.html"), encoding="utf-8") as fh:
                html = fh.read()
            used = set(re.findall(r"analytics\.(\w+)", html))
            provided = set(a.keys()) | {"ai_advice"}
            assert used <= provided, f"模板讀取了後端沒有提供的分析欄位 (頁面會顯示 undefined/空白): {sorted(used - provided)}"
            assert "item.revenue" in html and all("revenue" in t for t in a["top_items"])

            # 即時 API 的熱門排行帶有銷售額，且尊重 ?limit=
            live = admin_client.get("/api/admin_live_orders?limit=2").get_json()["analytics"]
            assert len(live["top_items"]) <= 2 and all("revenue" in t for t in live["top_items"]), live["top_items"]
            page = admin_client.get("/admin").get_data(as_text=True)
            assert 'id="stat-top-items"' in page and "ai_advice" not in page.split("stat-ai-advice")[0][-10:], "排行榜需有可即時更新的容器"
            return "熱門排行：香檸氣泡飲 3 份 $240、雞排 $70、點數兌換品 $0；模板用到的分析欄位後端都有提供；即時 API 帶銷售額並尊重 limit"
        runner.check("15.27", "熱門餐點排行顯示銷售額，且模板讀取的分析欄位後端都有提供", t15_27)

        # ==========================================================================
        # 模組 16：舊版備份還原後的即時升級 (必須最後執行：會以舊結構資料庫取代目前資料庫)
        # ==========================================================================
        runner.start_suite("模組 16：舊版備份還原即時升級 (最後執行：會以舊結構資料庫取代目前資料庫)")
        # 16.1 舊版備份還原後立即自動升級
        def _legacy_restore_check(force_in_place):
            import sqlite3 as _sq
            import app.models.user as um
            legacy_vec = np.linspace(0.2, 0.8, 128, dtype=np.float32)
            legacy_path = os.path.join(TEST_DB_DIR, "legacy_make.db")
            if os.path.exists(legacy_path):
                os.remove(legacy_path)
            lc = _sq.connect(legacy_path)
            lc.execute("CREATE TABLE user (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, phone VARCHAR(50) NOT NULL, "
                       "photo_path VARCHAR(200) NOT NULL, feature BLOB, points INTEGER DEFAULT 0)")
            lc.execute("INSERT INTO user (name, phone, photo_path, feature, points) VALUES (?,?,?,?,?)",
                       ("舊庫甲", "0911000001", "a.jpg", pickle.dumps(legacy_vec, protocol=4), 40))
            lc.execute("INSERT INTO user (name, phone, photo_path, feature, points) VALUES (?,?,?,?,?)",
                       ("舊庫乙", "0911000002", "b.jpg", b"\x80\x04garbage-feature", 5))
            lc.commit()
            lc.close()
            with open(legacy_path, "rb") as fh:
                legacy_bytes = fh.read()
            os.remove(legacy_path)

            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as zf:
                zf.writestr("menu.db", legacy_bytes)
            # 前一個還原已換掉資料庫，這裡重新以預設管理員登入
            c_admin = app.test_client()
            page = c_admin.get("/admin")
            c_admin.post("/admin", data={"username": "admin_test", "password": "AdminPass!2026", "csrf_token": extract_csrf(page.get_data(as_text=True))},
                         headers={"X-Forwarded-For": "198.51.100.%d" % (60 if not force_in_place else 61)})
            csrf = get_client_csrf(c_admin, "/admin")
            real_replace = os.replace
            def _locked_replace(src_, dst_, *a, **k):
                # 模擬 Windows：資料庫檔案仍被其他連線開啟，無法被替換
                if str(os.fspath(src_)).endswith(".tmp") and "menu.db.restore-" in str(os.fspath(src_)):
                    raise PermissionError(5, "存取被拒", str(dst_))
                return real_replace(src_, dst_, *a, **k)
            if force_in_place:
                os.replace = _locked_replace
            try:
                r = c_admin.post(
                    "/admin/restore_backup",
                    data={"csrf_token": csrf, "restore_db": "1", "backup_zip": (io.BytesIO(buf.getvalue()), "legacy.zip")},
                    content_type="multipart/form-data", headers={"X-CSRFToken": csrf},
                )
            finally:
                os.replace = real_replace
            body = r.get_data(as_text=True)
            assert r.status_code == 200 and "還原完成" in body, f"舊版備份還原失敗: {r.status_code} {body[:300]}"

            conn = _sq.connect(_db_path14())
            try:
                cols = {row[1] for row in conn.execute("PRAGMA table_info(user)")}
                assert {"tenant_id", "pin_hash", "last_login_at"} <= cols, f"還原後未立即升級資料表結構: {cols}"
                feats = {row[0]: row[1] for row in conn.execute("SELECT phone, feature FROM user")}
                assert bytes(feats["0911000001"]).startswith(um.FEATURE_MAGIC_PREFIX), "舊版 Pickle 特徵未在還原後轉為安全格式"
                assert bytes(feats["0911000002"]) == b"\x80\x04garbage-feature", "無法解析的舊特徵被覆寫 (應保留原始資料)"
                assert conn.execute("SELECT COUNT(*) FROM tenant WHERE slug = 'default'").fetchone()[0] == 1, "還原後缺少預設店家"
                assert conn.execute("SELECT COUNT(*) FROM user WHERE tenant_id IS NULL").fetchone()[0] == 0, "還原後仍有會員未歸屬店家"
            finally:
                conn.close()
            leftovers = [f for f in os.listdir(TEST_DB_DIR) if f.endswith("-wal") and os.path.getsize(os.path.join(TEST_DB_DIR, f)) > 0]
            return ("(Windows 備援：原地寫入) " if force_in_place else "") + "舊結構備份還原後立即補 tenant_id/PIN 等欄位、Pickle 特徵轉安全格式、壞特徵原樣保留"

        def t14_10():
            return _legacy_restore_check(force_in_place=False)
        runner.check("16.1", "舊版備份還原後立即自動升級 (不必重啟)，壞特徵原樣保留", t14_10)

        def t14_11():
            return _legacy_restore_check(force_in_place=True)
        runner.check("16.2", "資料庫檔案被占用 (Windows WinError 5) 時改以原地寫入還原，結果與換檔一致", t14_11)
    finally:
        os.environ["DISABLE_RATE_LIMIT"] = "0"
        if _orig_extract_feature is not None:
            cv_service_mod.extract_feature = _orig_extract_feature
            auth_route_mod.extract_feature = _orig_extract_feature

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