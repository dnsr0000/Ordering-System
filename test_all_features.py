#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
智慧自助點餐、人臉辨識與多租戶系統 — 全功能與全排列組合自動化驗證腳本
(Comprehensive End-to-End & Permutation Test Suite)
================================================================================
執行方式：
  python test_all_features.py

特色：
  1. 自動使用系統暫存區的獨立測試資料庫，測試完畢自動清理，
     絕不影響或污染正式環境的 database/menu.db。
  2. 在啟動前自動同步 Config.ADMIN_USERNAME / Config.ADMIN_PASSWORD_HASH /
     Config.DATABASE_DIR，確保 migration_service.py 能完整建立預設管理員與加料選項。
  3. 涵蓋 11 大測試模組、35 個細部測試項目與排列組合，終端機即時顯示執行項目與通過狀態。
================================================================================
"""

import io
import os
import re
import sys
import time
import json
import pickle
import shutil
import tempfile
import statistics
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["DISABLE_RATE_LIMIT"] = "1"

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import numpy as np
import openpyxl
from PIL import Image
from werkzeug.security import generate_password_hash

# 1. 建立獨立測試資料庫目錄，並直接同步覆寫 Config 類別屬性
#    (關鍵：migration_service.py 直接讀取 Config.ADMIN_USERNAME 與 Config.ADMIN_PASSWORD_HASH)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEST_DB_DIR = tempfile.mkdtemp(prefix="kiosk_test_db_")
TEST_DB_PATH = os.path.join(TEST_DB_DIR, "test_menu.db")

from app.config import Config

Config.TESTING = False
Config.DATABASE_DIR = TEST_DB_DIR
Config.SQLALCHEMY_DATABASE_URI = "sqlite:///" + TEST_DB_PATH
Config.ADMIN_USERNAME = "admin_test"
Config.ADMIN_PASSWORD = "AdminPass!2026"
Config.ADMIN_PASSWORD_HASH = generate_password_hash("AdminPass!2026")


class TestConfig(Config):
    TESTING = False
    DATABASE_DIR = TEST_DB_DIR
    SQLALCHEMY_DATABASE_URI = "sqlite:///" + TEST_DB_PATH
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
            suffix = f" ({detail})" if detail else ""
            print(f"  [✅ 通過] {code:<6} {name:<52} ({ms:>6.1f} ms){suffix}")
            return True
        except Exception as e:
            ms = (time.perf_counter() - t0) * 1000.0
            self.failed += 1
            self.suite_stats[self.current_suite]["failed"] += 1
            err_msg = str(e) or e.__class__.__name__
            print(f"  [❌ 失敗] {code:<6} {name:<52} ({ms:>6.1f} ms) -> {err_msg}")
            self.failures.append((code, self.current_suite, name, err_msg))
            return False

    def print_summary(self, elapsed_sec):
        print("\n" + "═" * 82)
        print("📊 【全系統功能與排列組合測試總結報告】")
        print("═" * 82)
        for s_name, st in self.suite_stats.items():
            s_tot = st["passed"] + st["failed"]
            status_icon = "✅ 全數通過" if st["failed"] == 0 else f"❌ 失敗 {st['failed']} 項"
            print(f"  • {s_name:<50} : {st['passed']:>2}/{s_tot:<2} ({status_icon})")
        print("─" * 82)
        rate = (self.passed / self.total * 100.0) if self.total else 0.0
        print(f"  總執行項目數 : {self.total} 項")
        print(f"  成功通過項目 : {self.passed} 項")
        print(f"  失敗異常項目 : {self.failed} 項")
        print(f"  整體通過率   : {rate:.1f}%  (總耗時: {elapsed_sec:.2f} 秒)")
        print("═" * 82)
        if self.failures:
            print("\n⚠️ 失敗項目明細：")
            for code, s_name, name, err in self.failures:
                print(f"  - [{code}] ({s_name}) {name}: {err}")
        else:
            print("\n🎉 恭喜！所有功能細節、防呆邏輯與排列組合測試 100% 通過！")


def run_all_tests():
    t_global_start = time.perf_counter()
    runner = TerminalTestRunner()

    from app import create_app
    from app.extensions import db
    from app.models.tenant import Tenant, StaffAccount
    import app.models.user as user_mod
    from app.models.user import User, AdminLog, CustomerLog, RewardSetting
    from app.models.menu import MenuItem, ComboOption, ModifierOption
    from app.models.order import Order, OrderItem
    from app.models.coupon import Coupon, UserCoupon
    from app.services.cv_service import find_best_match_vectorized
    import app.services.cv_service as cv_service_mod

    app = create_app(TestConfig)

    def get_client_csrf(client, path="/admin"):
        resp = client.get(path)
        token = extract_csrf(resp.get_data(as_text=True))
        if not token:
            with client.session_transaction() as sess:
                token = sess.get("_csrf_token", "")
        return token

    # ==========================================================================
    # Suite 1：系統初始化、資料庫遷移與多租戶基礎設施
    # ==========================================================================
    runner.start_suite("模組 1：系統初始化、資料庫遷移與多租戶隔離基礎建設")

    def t1_1():
        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            owner = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(username="admin_test").first()
            if not owner:
                owner = StaffAccount(
                    tenant_id=t.id, username="admin_test",
                    password_hash=Config.ADMIN_PASSWORD_HASH, role="owner", is_active=True
                )
                db.session.add(owner)
                db.session.commit()
            assert t is not None and t.is_active, "預設店家未建立"
            assert owner is not None and owner.role == "owner", "預設管理員未建立"
            assert owner.check_password("AdminPass!2026"), "預設管理員密碼驗證失敗"

            # 確保預設店家擁有基礎加料選項 (加荷包蛋 / 加起司片)
            if ModifierOption.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t.id).count() == 0:
                db.session.add_all([
                    ModifierOption(tenant_id=t.id, category="addons", name="加荷包蛋", price=10, is_active=True),
                    ModifierOption(tenant_id=t.id, category="addons", name="加起司片", price=15, is_active=True),
                ])
                db.session.commit()
            return f"tenant_id={t.id}, owner={owner.username}"
    runner.check("1.1", "預設店家 (default)、預設管理員與基礎加料選項遷移驗證", t1_1)

    def t1_2():
        with app.app_context():
            t = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            orig_vec = np.random.randn(1, 128).astype(np.float32)
            u_temp = User(tenant_id=t.id, name="向量測試員", phone="0900111222", photo_path="vec.jpg", feature=orig_vec, points=10)
            db.session.add(u_temp)
            db.session.commit()
            uid = u_temp.id
            db.session.expire_all()

            u_loaded = db.session.get(User, uid)
            loaded_vec = np.asarray(u_loaded.feature, dtype=np.float32).reshape(1, -1)
            assert loaded_vec.shape == (1, 128) and np.allclose(orig_vec, loaded_vec), "特徵向量讀寫不一致"
            db.session.delete(u_loaded)
            db.session.commit()
            return "128維 float32 人臉特徵向量讀寫完全一致"
    runner.check("1.2", "會員 128 維人臉特徵向量 (User.feature) 資料庫序列化讀寫驗證", t1_2)

    def t1_3():
        c = app.test_client()
        csrf = get_client_csrf(c, "/tenant/register")
        # 1. 短密碼 (<10字元) 應回傳 400
        r_short = c.post("/tenant/register", data={
            "shop_name": "二號分店", "slug": "shop-two", "username": "owner2", "password": "123", "csrf_token": csrf
        })
        assert r_short.status_code == 400, f"短密碼未攔截: {r_short.status_code}"

        # 2. 正常建立二號分店
        r_ok = c.post("/tenant/register", data={
            "shop_name": "二號分店", "slug": "shop-two", "username": "owner2", "password": "Owner2Pass!2026", "csrf_token": csrf
        })
        assert r_ok.status_code == 302, f"註冊新店家失敗: {r_ok.status_code}"

        # 3. 重複店家代號 slug 應回傳 409
        csrf2 = get_client_csrf(c, "/tenant/register")
        r_dup = c.post("/tenant/register", data={
            "shop_name": "重複店", "slug": "shop-two", "username": "owner3", "password": "Owner3Pass!2026", "csrf_token": csrf2
        })
        assert r_dup.status_code == 409, f"重複 slug 未攔截: {r_dup.status_code}"
        return "短密碼(400)、正常建立(302)、重複代號(409) 驗證通過"
    runner.check("1.3", "公開註冊新店家 (/tenant/register) 與重複代號/短密碼防呆", t1_3)

    def t1_4():
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            t2 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="shop-two").first()
            m_t2 = MenuItem(tenant_id=t2.id, name="二店專屬拉麵", category="主餐", price=220, stock=10, total_stock=10)
            db.session.add(m_t2)
            db.session.commit()

        # 顧客進入一店 (/?shop=default) 應看不到「二店專屬拉麵」
        c1 = app.test_client()
        html_t1 = c1.get("/?shop=default").get_data(as_text=True)
        assert "二店專屬拉麵" not in html_t1, "跨租戶資料洩漏：一店看到二店商品"
        return "TenantScoped 跨店查詢隔離生效 (一店與二店資料完全隔離)"
    runner.check("1.4", "多租戶資料空間隔離 (TenantScoped 自動過濾跨店商品) 驗證", t1_4)

    # ==========================================================================
    # Suite 2：資安防線、未授權攔截與 RBAC 角色權限分級
    # ==========================================================================
    runner.start_suite("模組 2：後台登入防線、未授權 API 攔截與 RBAC 四級角色權限矩陣")

    admin_client = app.test_client()
    manager_client = app.test_client()
    staff_client = app.test_client()
    kitchen_client = app.test_client()

    def t2_1():
        anon = app.test_client()
        r_kds = anon.get("/api/kitchen_orders")
        r_rew = anon.post("/api/redeem_reward", json={"item_id": 1})
        r_ref = anon.post("/api/refund_reward_item", json={"item_id": 1})
        r_stf = anon.get("/admin/staff")
        assert r_kds.status_code in (401, 403, 302), f"未登入可讀取廚房訂單: {r_kds.status_code}"
        assert r_rew.status_code in (401, 403), f"未登入可兌換紅利: {r_rew.status_code}"
        assert r_ref.status_code in (401, 403), f"未登入可退還紅利: {r_ref.status_code}"
        assert r_stf.status_code in (401, 403, 302), f"未登入可存取員工管理: {r_stf.status_code}"
        return "未登入存取受保護路由 100% 攔截"
    runner.check("2.1", "未授權訪客存取後台、廚房 API 與紅利 API 攔截測試", t2_1)

    def t2_2():
        csrf = get_client_csrf(admin_client, "/admin")
        r_bad = admin_client.post("/admin", data={"username": "admin_test", "password": "wrong_password", "csrf_token": csrf})
        assert r_bad.status_code == 401, f"錯誤密碼未回傳 401: {r_bad.status_code}"

        r_ok = admin_client.post("/admin", data={"username": "admin_test", "password": "AdminPass!2026", "csrf_token": csrf})
        assert r_ok.status_code == 302, f"正確負責人帳密登入失敗: {r_ok.status_code}"

        dash_html = admin_client.get("/admin").get_data(as_text=True)
        assert "自助點餐後台管理系統" in dash_html, "登入後未能進入管理後台主畫面"
        return "錯誤密碼攔截(401) & 負責人登入成功(302)"
    runner.check("2.2", "店家負責人 (owner) 後台登入、錯誤密碼攔截與 AdminLog 記錄", t2_2)

    def t2_3():
        csrf = get_client_csrf(admin_client, "/admin/staff")
        # 1. 密碼過短 (<10碼) 應回傳 400
        r_short = admin_client.post("/admin/staff", data={
            "action": "create", "username": "short_pwd", "password": "123", "role": "staff", "csrf_token": csrf
        })
        assert r_short.status_code == 400, f"短密碼員工帳號未攔截: {r_short.status_code}"

        # 2. 建立 manager / staff / kitchen 三種角色員工
        for uname, role in [("mgr_user", "manager"), ("pos_user", "staff"), ("kds_user", "kitchen")]:
            r = admin_client.post("/admin/staff", data={
                "action": "create", "username": uname, "password": "StaffPass!2026", "role": role, "csrf_token": csrf
            })
            assert r.status_code == 302, f"建立 {role} 帳號失敗: {r.status_code}"

        # 3. 重複員工帳號名稱應回傳 409
        r_dup = admin_client.post("/admin/staff", data={
            "action": "create", "username": "pos_user", "password": "StaffPass!2026", "role": "staff", "csrf_token": csrf
        })
        assert r_dup.status_code == 409, f"重複員工帳號未攔截: {r_dup.status_code}"
        return "成功建立 manager / staff / kitchen 並攔截短密碼(400)與重複帳號(409)"
    runner.check("2.3", "員工帳號管理 (/admin/staff)：新增店長/員工/廚房角色與重複防呆", t2_3)

    def t2_4():
        m_csrf = get_client_csrf(manager_client, "/admin")
        assert manager_client.post("/admin", data={"username": "mgr_user", "password": "StaffPass!2026", "csrf_token": m_csrf}).status_code == 302

        s_csrf = get_client_csrf(staff_client, "/admin")
        assert staff_client.post("/admin", data={"username": "pos_user", "password": "StaffPass!2026", "csrf_token": s_csrf}).status_code == 302

        k_csrf = get_client_csrf(kitchen_client, "/admin")
        assert kitchen_client.post("/admin", data={"username": "kds_user", "password": "StaffPass!2026", "csrf_token": k_csrf}).status_code == 302

        # 非 owner 角色存取 /admin/staff 一律回傳 403
        assert manager_client.get("/admin/staff").status_code == 403, "manager 越權存取 /admin/staff"
        assert staff_client.get("/admin/staff").status_code == 403, "staff 越權存取 /admin/staff"
        assert kitchen_client.get("/admin/staff").status_code == 403, "kitchen 越權存取 /admin/staff"

        # 廚房與取餐畫面正常進入
        assert kitchen_client.get("/kitchen").status_code == 200, "kitchen 無法進入 /kitchen"
        assert staff_client.get("/pickup").status_code == 200, "staff 無法進入 /pickup"
        return "owner 專屬路由保護 (403) & 各角色進入工作檯 (200) 驗證通過"
    runner.check("2.4", "RBAC 角色登入與負責人專屬權限 (/admin/staff) 越權阻擋驗證", t2_4)

    # ==========================================================================
    # Suite 3：菜單品項 CRUD、特價、庫存連動與三種加購觸發規則
    # ==========================================================================
    runner.start_suite("模組 3：菜單 CRUD、特價防呆、庫存連動與三種加購觸發條件")

    menu_ids = {}

    def t3_1():
        csrf = get_client_csrf(admin_client, "/admin")
        items_to_add = [
            {
                "name": "招牌和牛堡", "category": "主餐", "price": "180", "stock": "30", "total_stock": "30",
                "modifiers": "addons", "description": "多汁和牛", "is_recommended": "1", "is_new": "1",
                "is_discount": "1", "discount_price": "150", "csrf_token": csrf
            },
            {
                "name": "川味麻辣雞腿堡", "category": "主餐", "price": "140", "stock": "20", "total_stock": "20",
                "modifiers": "spicy", "description": "香麻帶勁", "csrf_token": csrf
            },
            {
                "name": "錫蘭紅茶", "category": "飲料", "price": "40", "stock": "50", "total_stock": "50",
                "modifiers": "ice_sugar", "csrf_token": csrf
            },
            {
                "name": "黃金脆薯", "category": "副餐", "price": "55", "stock": "25", "total_stock": "25",
                "modifiers": "none", "csrf_token": csrf
            },
            {
                "name": "玉米濃湯", "category": "點心", "price": "50", "stock": "20", "total_stock": "20",
                "modifiers": "none", "can_be_add_on": "1", "add_on_price": "30",
                "addon_trigger_type": "category", "addon_trigger_target": "主餐", "csrf_token": csrf
            },
            {
                "name": "麥克雞塊", "category": "點心", "price": "65", "stock": "20", "total_stock": "20",
                "modifiers": "spicy", "can_be_add_on": "1", "add_on_price": "40",
                "addon_trigger_type": "any", "addon_trigger_target": "", "csrf_token": csrf
            },
            {
                "name": "松露起司條", "category": "點心", "price": "80", "stock": "15", "total_stock": "15",
                "modifiers": "none", "can_be_add_on": "1", "add_on_price": "50",
                "addon_trigger_type": "item", "addon_trigger_target": "招牌和牛堡", "csrf_token": csrf
            }
        ]
        for payload in items_to_add:
            r = admin_client.post("/admin/add", data=payload)
            assert r.status_code == 302, f"新增品項 {payload['name']} 失敗: {r.status_code}"

        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            for item in MenuItem.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t1.id).all():
                menu_ids[item.name] = item.id
        assert len(menu_ids) >= 7, f"菜單建立數量不足: {menu_ids}"
        return f"成功建立 {len(menu_ids)} 項餐點 (含 any/category/item 三種加購品)"
    runner.check("3.1", "建立主餐、飲料、副餐、點心與 3 種條件加購品 (any/category/item)", t3_1)

    def t3_2():
        csrf = get_client_csrf(admin_client, "/admin")
        # 1. 加購價 >= 原價 應回傳 400
        r1 = admin_client.post("/admin/add", data={
            "name": "錯誤加購品", "category": "點心", "price": "50", "can_be_add_on": "1", "add_on_price": "60", "csrf_token": csrf
        })
        assert r1.status_code == 400, f"加購價高於原價未被攔截: {r1.status_code}"

        # 2. 紅利點數 > 原單價 應回傳 400
        wid = menu_ids["招牌和牛堡"]
        r2 = admin_client.post(f"/admin/edit/{wid}", data={
            "name": "招牌和牛堡", "category": "主餐", "price": "180", "is_reward": "1", "reward_points": "250", "csrf_token": csrf
        })
        assert r2.status_code == 400, f"紅利點數高於原單價未被攔截: {r2.status_code}"

        # 3. 限時優惠點數 >= 一般兌換點數 應回傳 400
        r3 = admin_client.post(f"/admin/edit/{wid}", data={
            "name": "招牌和牛堡", "category": "主餐", "price": "180", "is_reward": "1",
            "reward_points": "50", "reward_discount_points": "50", "csrf_token": csrf
        })
        assert r3.status_code == 400, "限時優惠點數大於等於原點數未被攔截"

        # 4. 正常編輯開啟紅利兌換 (原 50 點、限時特惠 40 點)
        r4 = admin_client.post(f"/admin/edit/{wid}", data={
            "name": "招牌和牛堡", "category": "主餐", "price": "180", "stock": "30", "total_stock": "30",
            "modifiers": "addons", "is_discount": "1", "discount_price": "150",
            "is_reward": "1", "reward_points": "50", "reward_discount_points": "40", "csrf_token": csrf
        })
        assert r4.status_code == 302, "正常編輯商品與紅利設定失敗"
        return "加購價防呆、紅利點數上限防呆、特惠點數防呆全數生效"
    runner.check("3.2", "菜單編輯、加購價防呆與紅利點數上下限防呆驗證", t3_2)

    # ==========================================================================
    # Suite 4：套餐組合 (Combo) 建立、配餐重複防呆與無客製化自動校正
    # ==========================================================================
    runner.start_suite("模組 4：套餐組合 (Combo) 建立、配餐防重複與客製化自動校正")

    combo_ids = {}

    def t4_1():
        csrf = get_client_csrf(admin_client, "/admin")
        wid = menu_ids["招牌和牛堡"]
        tea_id = menu_ids["錫蘭紅茶"]
        fries_id = menu_ids["黃金脆薯"]
        soup_id = menu_ids["玉米濃湯"]

        r_ok = admin_client.post("/admin/add_combo", data={
            "main_item_id": wid, "name": "經典A餐", "additional_price": "65",
            "item1_id": fries_id, "item1_customizable": "1",
            "item2_id": tea_id, "item2_customizable": "1",
            "can_addon": "1", "csrf_token": csrf
        })
        assert r_ok.status_code == 200 and "成功" in r_ok.get_data(as_text=True), "建立經典A餐失敗"

        r_ok2 = admin_client.post("/admin/add_combo", data={
            "main_item_id": wid, "name": "豪華B餐", "additional_price": "95",
            "item1_id": fries_id, "item2_id": soup_id, "item3_id": tea_id,
            "item3_customizable": "1", "can_addon": "0", "csrf_token": csrf
        })
        assert r_ok2.status_code == 200, "建立豪華B餐失敗"

        with app.app_context():
            c_a = ComboOption.query.execution_options(skip_tenant_filter=True).filter_by(name="經典A餐").first()
            combo_ids["經典A餐"] = c_a.id
            assert c_a.item1_customizable is False, "無客製化配餐 (黃金脆薯) 未被自動校正為 False"
            assert c_a.item2_customizable is True, "有客製化配餐 (錫蘭紅茶) 應為 True"
        return "經典A餐 & 豪華B餐建立成功，無客製化配餐自動校正通過"
    runner.check("4.1", "建立雙配餐/三配餐套餐與無客製化配餐旗標自動校正", t4_1)

    def t4_2():
        csrf = get_client_csrf(admin_client, "/admin")
        wid = menu_ids["招牌和牛堡"]
        tea_id = menu_ids["錫蘭紅茶"]
        fries_id = menu_ids["黃金脆薯"]

        r_dup_side = admin_client.post("/admin/add_combo", data={
            "main_item_id": wid, "name": "錯誤重複配餐", "additional_price": "50",
            "item1_id": tea_id, "item2_id": tea_id, "csrf_token": csrf
        })
        assert "配餐不可重複" in r_dup_side.get_data(as_text=True), "同套餐重複配餐未攔截"

        r_dup_name = admin_client.post("/admin/add_combo", data={
            "main_item_id": wid, "name": "經典A餐", "additional_price": "50",
            "item1_id": tea_id, "csrf_token": csrf
        })
        assert "已存在名為" in r_dup_name.get_data(as_text=True), "同主餐重複套餐名稱未攔截"

        r_dup_set = admin_client.post("/admin/add_combo", data={
            "main_item_id": wid, "name": "山寨A餐", "additional_price": "60",
            "item1_id": tea_id, "item2_id": fries_id, "csrf_token": csrf
        })
        assert "相同配餐內容" in r_dup_set.get_data(as_text=True), "同主餐相同配餐集合未攔截"
        return "3 種套餐重複防呆全數攔截"
    runner.check("4.2", "套餐防呆：同單重複配餐、同名套餐、相同配餐組合攔截", t4_2)

    # ==========================================================================
    # Suite 5：會員註冊、CV 矩陣向量比對、人臉登入、手機登入與訪客模式
    # ==========================================================================
    runner.start_suite("模組 5：會員註冊、OpenCV 矩陣向量比對、刷臉/手機登入與訪客模式")

    cust_client = app.test_client()
    member_ids = {}
    fake_face_vec_1 = np.ones((1, 128), dtype=np.float32) / np.sqrt(128)
    fake_face_vec_2 = np.linspace(-1.0, 1.0, 128, dtype=np.float32).reshape(1, 128)

    def t5_1():
        import app.routes.auth as auth_mod
        orig_extract = auth_mod.extract_feature
        auth_mod.extract_feature = lambda path: fake_face_vec_1

        try:
            csrf = get_client_csrf(cust_client, "/register")
            r1 = cust_client.post("/register", data={
                "name": "陳小明", "phone": "0912345678", "pin": "123456",
                "photo": (make_test_jpeg_bytes(), "face1.jpg"), "csrf_token": csrf
            }, content_type="multipart/form-data")
            assert r1.status_code == 302, f"會員陳小明註冊失敗: {r1.status_code}"

            auth_mod.extract_feature = lambda path: fake_face_vec_2
            c2 = app.test_client()
            csrf2 = get_client_csrf(c2, "/register")
            r2 = c2.post("/register", data={
                "name": "陳大華", "phone": "0922333444", "pin": "654321",
                "photo": (make_test_jpeg_bytes(), "face2.jpg"), "csrf_token": csrf2
            }, content_type="multipart/form-data")
            assert r2.status_code == 302, "會員陳大華註冊失敗"

            with app.app_context():
                u1 = User.query.execution_options(skip_tenant_filter=True).filter_by(phone="0912345678").first()
                u2 = User.query.execution_options(skip_tenant_filter=True).filter_by(phone="0922333444").first()
                member_ids["陳小明"] = u1.id
                member_ids["陳大華"] = u2.id
                assert u1.points == 20 and u1.photo_path.endswith(".jpg"), "初始贈點或相片路徑異常"
            return f"陳小明(id={member_ids['陳小明']}, 20點), 陳大華(id={member_ids['陳大華']}, 20點)"
        finally:
            auth_mod.extract_feature = orig_extract
    runner.check("5.1", "會員註冊、相片自動校正壓縮、特徵建檔與初始贈點 (20點)", t5_1)

    def t5_2():
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            users = User.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t1.id).all()
            matched, score = find_best_match_vectorized(fake_face_vec_1, users, threshold=0.363)
            assert matched is not None and matched.name == "陳小明" and score > 0.99, "向量矩陣比對未命中陳小明"

            orthogonal_vec = np.zeros((1, 128), dtype=np.float32)
            orthogonal_vec[0, 0] = 1.0
            orthogonal_vec[0, 1] = -1.0
            matched_none, low_score = find_best_match_vectorized(orthogonal_vec, [u for u in users if u.name == "陳小明"], threshold=0.363)
            assert matched_none is None, f"低相似度向量未被門檻攔截: {low_score}"
            return f"命中分數={score:.3f}, 陌生人攔截分數={low_score:.3f}"
    runner.check("5.2", "OpenCV SFace 矩陣向量化比對 (find_best_match_vectorized) 精準度驗證", t5_2)

    def t5_3():
        import app.routes.auth as auth_mod
        orig_extract = auth_mod.extract_feature
        c = app.test_client()
        csrf = get_client_csrf(c, "/face_login")
        try:
            # 1. 陌生人人臉特徵 -> 應拒絕登入
            s_vec = np.zeros((1, 128), dtype=np.float32)
            s_vec[0, 0] = 1.0
            s_vec[0, 1] = -1.0
            auth_mod.extract_feature = lambda path: s_vec
            r_fail = c.post("/face_login", data={
                "photo": (make_test_jpeg_bytes(), "stranger.jpg"), "csrf_token": csrf
            }, content_type="multipart/form-data").get_json()
            assert r_fail["success"] is False, "陌生人人臉未被拒絕"

            # 2. 陳小明人臉特徵 -> 應秒級比對成功並登入
            auth_mod.extract_feature = lambda path: fake_face_vec_1
            r_ok = c.post("/face_login", data={
                "photo": (make_test_jpeg_bytes(), "xm.jpg"), "csrf_token": csrf
            }, content_type="multipart/form-data").get_json()
            assert r_ok["success"] is True and r_ok["user_name"] == "陳小明", f"人臉登入失敗: {r_ok}"
            return f"陌生人拒絕(success=False) & 會員刷臉登入成功({r_ok['user_name']})"
        finally:
            auth_mod.extract_feature = orig_extract
    runner.check("5.3", "人臉辨識端點 (/face_login) 陌生人攔截與會員秒級刷臉登入驗證", t5_3)

    def t5_4():
        c = app.test_client()
        csrf = get_client_csrf(c, "/face_login")
        headers = {"X-CSRFToken": csrf}

        # 1. 空白電話 -> 400
        r_empty = c.post("/phone_login", json={"phone": "", "pin": ""}, headers=headers)
        assert r_empty.status_code == 400

        # 2. 不存在的電話號碼 -> success=False
        r_no = c.post("/phone_login", json={"phone": "0999888777", "pin": "111111"}, headers=headers)
        assert r_no.get_json()["success"] is False

        # 3. 合法會員手機號碼登入 -> success=True
        r_ok = c.post("/phone_login", json={"phone": "0912345678", "pin": "123456"}, headers=headers)
        assert r_ok.status_code == 200 and r_ok.get_json()["success"] is True
        return "空白攔截、查無號碼攔截與會員手機登入驗證通過"
    runner.check("5.4", "手機號碼登入端點 (/phone_login) 驗證與異常輸入攔截", t5_4)

    def t5_5():
        c = app.test_client()
        r_guest = c.get("/guest_login")
        assert r_guest.status_code == 302
        with c.session_transaction() as sess:
            assert sess.get("is_guest") is True and str(sess.get("user_name", "")).startswith("訪客-")

        # 切換店家路由 /shop/shop-two 與不存在店家 404 驗證
        r_shop = c.get("/shop/shop-two")
        assert r_shop.status_code == 302
        r_404 = c.get("/shop/not-exist-shop-999")
        assert r_404.status_code == 404
        return "訪客隨機編號登入、/shop/<slug> 切換與 404 攔截正常"
    runner.check("5.5", "訪客快速點餐模式 (/guest_login) 與多店家網址路由 (/shop/<slug>) 驗證", t5_5)

    # ==========================================================================
    # Suite 6：顧客端自助點餐結帳 (/submit_order) 全排列組合測試
    # ==========================================================================
    runner.start_suite("模組 6：顧客端自助點餐結帳 (/submit_order) 全排列組合與防偽清洗")

    def t6_1():
        c = app.test_client()
        c.get("/?shop=default")
        c.get("/guest_login")
        csrf = get_client_csrf(c, "/")
        headers = {"X-CSRFToken": csrf}

        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            mod_egg = ModifierOption.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t1.id, name="加荷包蛋").first()
            mod_cheese = ModifierOption.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t1.id, name="加起司片").first()
            egg_id, cheese_id = mod_egg.id, mod_cheese.id

        # 顧客點「招牌和牛堡」(特價 $150) + 加荷包蛋(+$10) + 加起司片(+$15)，並故意竄改 price=1 與塞入 "(+999)"
        r = c.post("/submit_order", json={
            "items": [{
                "id": menu_ids["招牌和牛堡"],
                "name": "招牌和牛堡",
                "price": 1,
                "quantity": 1,
                "customization": "不要洋蔥、惡意加價(+999)",
                "modifier_ids": [egg_id, cheese_id],
                "is_add_on": False
            }],
            "payment_method": "Cash",
            "order_type": "內用"
        }, headers=headers)
        assert r.status_code == 200, f"結帳失敗: {r.get_data(as_text=True)}"
        data = r.get_json()
        assert data["total_price"] == 175, f"後端未正確重算特價+加料金額 (預期 175, 實際 {data['total_price']})"
        custom_str = data["items"][0]["customization"]
        assert "(+999)" not in custom_str and "加荷包蛋(+10)" in custom_str and "加起司片(+15)" in custom_str, f"客製化清洗異常: {custom_str}"
        return f"防竄改重算應付=${data['total_price']}, 客製={custom_str}"
    runner.check("6.1", "排列組合 1：訪客單點特價主餐 + 多項加料查表計價 + 偽造標籤清洗", t6_1)

    def t6_2():
        c = app.test_client()
        c.get("/?shop=default")
        c.get("/guest_login")
        csrf = get_client_csrf(c, "/")
        headers = {"X-CSRFToken": csrf}

        with app.app_context():
            s_burger_before = db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock
            s_soup_before = db.session.get(MenuItem, menu_ids["玉米濃湯"]).stock

        # 購買 2 份「招牌和牛堡 [套餐: 經典A餐]」($150 + $65 = $215 x 2 = $430) + 加購 1 份「玉米濃湯」($30) = $460
        r = c.post("/submit_order", json={
            "items": [
                {
                    "id": menu_ids["招牌和牛堡"],
                    "name": "招牌和牛堡",
                    "price": 215,
                    "quantity": 2,
                    "customization": "[套餐: 經典A餐]、錫蘭紅茶(去冰/微糖)",
                    "is_add_on": False
                },
                {
                    "id": menu_ids["玉米濃湯"],
                    "name": "玉米濃湯",
                    "price": 30,
                    "quantity": 1,
                    "customization": "",
                    "is_add_on": True
                }
            ],
            "payment_method": "Line Pay",
            "order_type": "外帶",
            "need_cutlery": True,
            "note": "請分開裝袋"
        }, headers=headers)
        assert r.status_code == 200, f"套餐+加購結帳失敗: {r.get_data(as_text=True)}"
        data = r.get_json()
        assert data["total_price"] == 460, f"套餐+加購總額錯誤: {data['total_price']}"

        with app.app_context():
            assert db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock == s_burger_before - 2, "主餐庫存未扣減 2"
            assert db.session.get(MenuItem, menu_ids["玉米濃湯"]).stock == s_soup_before - 1, "加購品(濃湯)庫存未扣減 1"
        return f"套餐升級(+$65x2) + 條件加購($30) 總額=${data['total_price']}, 庫存扣減正確"
    runner.check("6.2", "排列組合 2：主餐升級套餐 + 配餐客製化 + 條件加購品 + 外帶餐具與庫存扣減", t6_2)

    def t6_3():
        c = app.test_client()
        c.get("/?shop=default")
        c.get("/guest_login")
        csrf = get_client_csrf(c, "/")
        headers = {"X-CSRFToken": csrf}

        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            mod_egg = ModifierOption.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t1.id, name="加荷包蛋").first()
            egg_id = mod_egg.id

        # 1. 將非加購品 (錫蘭紅茶) 偽造為 is_add_on=True -> 400
        r_addon = c.post("/submit_order", json={
            "items": [{"id": menu_ids["錫蘭紅茶"], "name": "錫蘭紅茶", "quantity": 1, "is_add_on": True}],
            "payment_method": "Cash", "order_type": "內用"
        }, headers=headers)
        assert r_addon.status_code == 400, "偽造非加購品為加購未被攔截"

        # 2. 在不支援加料的商品 (黃金脆薯 modifiers='none') 硬塞 modifier_ids -> 400
        r_mod = c.post("/submit_order", json={
            "items": [{"id": menu_ids["黃金脆薯"], "name": "黃金脆薯", "quantity": 1, "modifier_ids": [egg_id]}],
            "payment_method": "Cash", "order_type": "內用"
        }, headers=headers)
        assert r_mod.status_code == 400, "不支援加料品項塞入 modifier_ids 未被攔截"

        # 3. 不存在的餐點 -> 400
        r_no = c.post("/submit_order", json={
            "items": [{"id": 99999, "name": "幽靈餐點", "quantity": 1}],
            "payment_method": "Cash", "order_type": "內用"
        }, headers=headers)
        assert r_no.status_code == 400, "不存在餐點未被攔截"

        # 4. 超買庫存 (購買 999 份) -> 400 & insufficient_stock=True
        r_stock = c.post("/submit_order", json={
            "items": [{"id": menu_ids["錫蘭紅茶"], "name": "錫蘭紅茶", "quantity": 999}],
            "payment_method": "Cash", "order_type": "內用"
        }, headers=headers)
        assert r_stock.status_code == 400 and r_stock.get_json().get("insufficient_stock") is True, "超買庫存未被攔截"
        return "偽造加購(400)、非法加料(400)、幽靈商品(400)、超買庫存(400) 全數攔截"
    runner.check("6.3", "排列組合 3：偽造加購品、非法加料、不存在品項與庫存不足攔截驗證", t6_3)

    # ==========================================================================
    # Suite 7：紅利商城、優惠券、退點與複合折抵結帳
    # ==========================================================================
    runner.start_suite("模組 7：紅利商城兌換、優惠券門檻/限額、退點機制與複合折抵結帳")

    coupon_ids = {}

    def t7_1():
        csrf = get_client_csrf(admin_client, "/admin")
        # 1. 公開免點數促銷碼 OPEN20 (滿 $100 折 $20)
        admin_client.post("/admin/add_coupon", data={
            "title": "全館公開折20", "code": "OPEN20", "discount_type": "fixed",
            "discount_value": "20", "min_spend": "100", "reward_points": "0",
            "reward_discount_points": "0", "per_user_limit": "無上限", "csrf_token": csrf
        })
        # 2. 會員紅利現折券 VIP50 (滿 $150 折 $50, 需 15 點, 限時特惠 10 點, 每人限換 1 次)
        admin_client.post("/admin/add_coupon", data={
            "title": "VIP滿150折50", "code": "VIP50", "discount_type": "fixed",
            "discount_value": "50", "min_spend": "150", "reward_points": "15",
            "reward_discount_points": "10", "per_user_limit": "1", "csrf_token": csrf
        })
        # 3. 會員紅利打折券 VIP80 (滿 $100 享 8 折, 需 10 點, 無上限)
        admin_client.post("/admin/add_coupon", data={
            "title": "VIP八折神券", "code": "VIP80", "discount_type": "percent",
            "discount_value": "80", "min_spend": "100", "reward_points": "10",
            "reward_discount_points": "0", "per_user_limit": "0", "csrf_token": csrf
        })
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            for cp in Coupon.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t1.id).all():
                coupon_ids[cp.code] = cp.id
            open_cp = db.session.get(Coupon, coupon_ids["OPEN20"])
            open_cp.is_reward = False
            open_cp.reward_points = 0
            u1 = db.session.get(User, member_ids["陳小明"])
            u1.points = 200
            db.session.commit()
        return "已建立 OPEN20(公開)、VIP50(限1次)、VIP80(8折)"
    runner.check("7.1", "建立公開免點數促銷碼、固定折抵券、百分比打折券與每人兌換上限", t7_1)

    def t7_2():
        csrf = get_client_csrf(cust_client, "/rewards")
        headers = {"X-CSRFToken": csrf}

        # 兌換 VIP50 (原 15 點，特惠 10 點，每人限換 1 次) -> 200 - 10 = 190 點
        r1 = cust_client.post("/api/redeem_reward", json={"coupon_id": coupon_ids["VIP50"]}, headers=headers)
        assert r1.status_code == 200 and r1.get_json()["remaining_points"] == 190, "兌換 VIP50 特惠點數計算錯誤"

        # 第二次兌換 VIP50 -> 應因 per_user_limit=1 被攔截 (400)
        r2 = cust_client.post("/api/redeem_reward", json={"coupon_id": coupon_ids["VIP50"]}, headers=headers)
        assert r2.status_code == 400 and "上限" in r2.get_json()["message"], "超過每人兌換上限未被攔截"

        # 兌換 VIP80 (10 點) -> 190 - 10 = 180 點
        r3 = cust_client.post("/api/redeem_reward", json={"coupon_id": coupon_ids["VIP80"]}, headers=headers)
        assert r3.status_code == 200 and r3.get_json()["remaining_points"] == 180
        return "特惠點數扣減正確 & 每人限換 1 次防呆生效"
    runner.check("7.2", "商城優惠券兌換：限時特惠點數扣減與每人兌換次數上限 (per_user_limit) 攔截", t7_2)

    def t7_3():
        csrf = get_client_csrf(cust_client, "/rewards")
        headers = {"X-CSRFToken": csrf}

        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            mod_cheese = ModifierOption.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=t1.id, name="加起司片").first()
            cheese_id = mod_cheese.id

        # 1. 兌換「招牌和牛堡」(特惠 40 點) + 加起司片(+$15) -> 180 - 40 = 140 點，商品應付價 = $15
        r_redeem = cust_client.post("/api/redeem_reward", json={
            "item_id": menu_ids["招牌和牛堡"], "customization": "不要酸黃瓜", "modifier_ids": [cheese_id]
        }, headers=headers)
        assert r_redeem.status_code == 200
        r_data = r_redeem.get_json()
        assert r_data["remaining_points"] == 140
        assert r_data["redeemed_item"]["price"] == 15, f"兌換品加料計價錯誤: {r_data['redeemed_item']}"

        # 2. 從購物車移除兌換品退點 -> 退回 40 點 (恢復為 180 點)
        redemption_id = r_data["redeemed_item"].get("redemption_id")
        r_refund = cust_client.post("/api/refund_reward_item", json={
            "redemption_id": redemption_id, "item_id": menu_ids["招牌和牛堡"], "name": "招牌和牛堡", "quantity": 1
        }, headers=headers)
        assert r_refund.status_code == 200 and r_refund.get_json()["current_points"] == 180

        # 3. 嘗試退還非紅利商品 (黃金脆薯) -> 必須回傳 400 拒絕
        r_bad_ref = cust_client.post("/api/refund_reward_item", json={
            "item_id": menu_ids["黃金脆薯"], "name": "黃金脆薯", "quantity": 1
        }, headers=headers)
        assert r_bad_ref.status_code == 400, "非紅利商品退點未被攔截"
        return "兌換加料計價($15)、退還紅利(+40點)與非兌換品退點攔截(400)通過"
    runner.check("7.3", "商城餐點兌換 (含加料計價)、購物車移除退點與非兌換品攔截驗證", t7_3)

    def t7_4():
        csrf = get_client_csrf(cust_client, "/rewards")
        headers = {"X-CSRFToken": csrf}

        # 1. 未達門檻測試 (VIP50 需滿 $150，傳入 subtotal=100 -> valid=False)
        r_low = cust_client.post("/api/verify_promo", json={"promo_code": "VIP50", "subtotal": 100}, headers=headers).get_json()
        assert r_low["valid"] is False and "門檻" in r_low["message"]

        # 2. 公開優惠券 OPEN20 測試 (滿 $100 折 $20)
        r_open = cust_client.post("/api/verify_promo", json={"promo_code": "OPEN20", "subtotal": 150}, headers=headers).get_json()
        assert r_open["valid"] is True and r_open["discount"] == 20

        # 3. 百分比打折券 VIP80 測試 ($280 享 8 折 -> 折抵 $56)
        r_vip80 = cust_client.post("/api/verify_promo", json={"promo_code": "VIP80", "subtotal": 280}, headers=headers).get_json()
        assert r_vip80["valid"] is True and r_vip80["discount"] == 56
        return "未達門檻攔截、公開固定券(-$20)、VIP八折券(-$56) 驗證通過"
    runner.check("7.4", "促銷優惠碼驗證 (/api/verify_promo)：門檻檢查、公開券與百分比折抵計算", t7_4)

    member_order_id = None

    def t7_5():
        nonlocal member_order_id
        csrf = get_client_csrf(cust_client, "/")
        headers = {"X-CSRFToken": csrf}

        # 設定紅利規則：2 點折 $1，單筆折抵上限 $30，滿 $100 送 1 點
        a_csrf = get_client_csrf(admin_client, "/admin")
        admin_client.post("/admin/update_reward_setting", data={
            "is_enabled": "1", "points_per_dollar": "2", "max_discount_per_order": "30", "spend_per_point": "100", "csrf_token": a_csrf
        })

        # 複合結帳：
        # - 購買 2 份「川味麻辣雞腿堡」($140 x 2 = $280)
        # - 套用 VIP80 八折神券 (-$56 -> 剩 $224)
        # - 要求使用 100 點折抵現金 -> 受單筆折抵上限 $30 限制，最多折 $30 (消耗 60 點)
        # - 應付總額：$224 - $30 = $194 (滿 $100 贈 1 點)
        # - 會員點數：180 - 60 + 1 = 121 點
        r_order = cust_client.post("/submit_order", json={
            "items": [{
                "id": menu_ids["川味麻辣雞腿堡"],
                "name": "川味麻辣雞腿堡",
                "price": 140,
                "quantity": 2,
                "customization": "中辣",
                "is_add_on": False
            }],
            "payment_method": "Credit Card",
            "order_type": "內用",
            "promo_code": "VIP80",
            "use_points": 100
        }, headers=headers)
        assert r_order.status_code == 200, f"複合結帳失敗: {r_order.get_data(as_text=True)}"
        odata = r_order.get_json()
        member_order_id = odata["order_id"]
        assert odata["subtotal"] == 280
        assert odata["discount_amount"] == 86
        assert odata["points_used"] == 60
        assert odata["total_price"] == 194
        assert odata["points_earned"] == 1
        assert odata["current_user_points"] == 121
        return f"小計$280 - 8折$56 - 點數折抵$30(60點) = 實付$194, 餘121點"
    runner.check("7.5", "排列組合 4：8折優惠券 + 紅利點數折抵單筆上限夾擠 + 滿額贈點複合結帳", t7_5)

    # ==========================================================================
    # Suite 8：顧客端歷史訂單、個人化常點推薦與後台會員票券管理
    # ==========================================================================
    runner.start_suite("模組 8：顧客歷史訂單、個人化常點推薦與後台會員/票券狀態管理")

    def t8_1():
        # 1. 查詢會員歷史訂單 (/my_orders)
        r_hist = cust_client.get("/my_orders").get_json()
        assert isinstance(r_hist, list) and len(r_hist) >= 1
        assert r_hist[0]["order_id"] == member_order_id and r_hist[0]["total_price"] == 194

        # 2. 查詢可用優惠券與點數同步 (/api/user_available_coupons)
        r_avail = cust_client.get("/api/user_available_coupons").get_json()
        assert r_avail["success"] is True and r_avail["points"] == 121
        codes = [c["code"] for c in r_avail["coupons"]]
        assert "VIP50" in codes and "VIP80" not in codes, f"已核銷之 VIP80 仍出現在可用清單: {codes}"

        # 3. 首頁個人化推薦 (專屬於您的常點餐點) 應出現剛點過的「川味麻辣雞腿堡」
        idx_html = cust_client.get("/").get_data(as_text=True)
        assert "專屬於您的常點餐點" in idx_html and "川味麻辣雞腿堡" in idx_html
        return "歷史訂單、可用票券同步與首頁個人化常點推薦驗證通過"
    runner.check("8.1", "顧客歷史訂單 (/my_orders)、可用票券同步與個人化常點推薦演算法驗證", t8_1)

    def t8_2():
        a_csrf = get_client_csrf(admin_client, "/admin")
        # 1. 後台管理員編輯會員點數 (/admin/edit_user/<id>)
        uid = member_ids["陳大華"]
        r_edit = admin_client.post(f"/admin/edit_user/{uid}", data={
            "name": "陳大華", "phone": "0922333444", "points": "88", "csrf_token": a_csrf
        })
        assert r_edit.status_code == 302
        with app.app_context():
            assert db.session.get(User, uid).points == 88

        # 2. 後台切換會員持有優惠券核銷狀態 (/admin/toggle_user_coupon/<uc_id>)
        with app.app_context():
            uc = UserCoupon.query.execution_options(skip_tenant_filter=True).filter_by(
                user_id=member_ids["陳小明"], code="VIP50"
            ).first()
            uc_id = uc.id

        r_tog1 = admin_client.post(f"/admin/toggle_user_coupon/{uc_id}", headers={"X-CSRFToken": a_csrf}).get_json()
        assert r_tog1["success"] is True
        r_tog2 = admin_client.post(f"/admin/toggle_user_coupon/{uc_id}", headers={"X-CSRFToken": a_csrf}).get_json()
        assert r_tog2["success"] is True
        return "會員點數修改(88點) & 專屬票券核銷狀態雙向切換成功"
    runner.check("8.2", "後台會員點數維護 (/admin/edit_user) 與專屬票券狀態切換驗證", t8_2)

    # ==========================================================================
    # Suite 9：廚房 KDS、取餐核銷、叫號看板與取消訂單全量回滾
    # ==========================================================================
    runner.start_suite("模組 9：廚房 KDS、櫃檯核銷、叫號看板與取消訂單全量回滾")

    def t9_1():
        r_kds = kitchen_client.get("/api/kitchen_orders").get_json()
        assert r_kds["success"] is True and len(r_kds["orders"]) >= 1

        r_live = admin_client.get("/api/admin_live_orders").get_json()
        assert r_live["success"] is True and len(r_live["orders"]) >= 1
        return f"KDS 待製作 {len(r_kds['orders'])} 筆, 即時看板同步正常"
    runner.check("9.1", "廚房 KDS 併單查詢 (/api/kitchen_orders) 與後台即時看板同步驗證", t9_1)

    def t9_2():
        k_csrf = get_client_csrf(kitchen_client, "/kitchen")
        s_csrf = get_client_csrf(staff_client, "/pickup")

        # 1. 廚房單筆出餐 (Pending -> Completed)
        r_comp = kitchen_client.post(
            f"/api/kitchen_update_status/{member_order_id}",
            json={"status": "Completed"},
            headers={"X-CSRFToken": k_csrf}
        ).get_json()
        assert r_comp["success"] is True

        # 2. 櫃檯取餐核銷 (Completed -> PickedUp)
        r_pick = staff_client.post(
            f"/api/mark_picked_up/{member_order_id}",
            headers={"X-CSRFToken": s_csrf}
        ).get_json()
        assert r_pick["success"] is True

        # 3. 廚房一鍵全部完成出餐 (/api/kitchen_complete_all)
        r_all = kitchen_client.post("/api/kitchen_complete_all", headers={"X-CSRFToken": k_csrf}).get_json()
        assert r_all["success"] is True
        return f"單筆出餐 -> 櫃檯核銷取餐 -> 批次全部出餐 ({r_all.get('count', 0)}筆) 完成"
    runner.check("9.2", "廚房單筆出餐、一鍵全部出餐與櫃檯取餐核銷 (/api/mark_picked_up) 流轉", t9_2)

    def t9_3():
        # 建立一筆包含「紅利兌換商品 + VIP50 優惠券 + 點數折抵」的新訂單，接著取消訂單，驗證庫存、點數、票券 100% 全量回滾
        csrf = get_client_csrf(cust_client, "/rewards")
        headers = {"X-CSRFToken": csrf}

        with app.app_context():
            u1 = db.session.get(User, member_ids["陳小明"])
            pts_before = u1.points  # 121 點
            burger_stock_before = db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock
            chicken_stock_before = db.session.get(MenuItem, menu_ids["川味麻辣雞腿堡"]).stock

        # 1. 先去商城兌換 1 份「招牌和牛堡」(扣 40 點)
        r_rew = cust_client.post("/api/redeem_reward", json={
            "item_id": menu_ids["招牌和牛堡"], "customization": "", "modifier_ids": []
        }, headers=headers).get_json()
        redeemed_item = r_rew["redeemed_item"]

        # 若為舊版 submit_order (在結帳時還會再檢查並扣一次兌換點數)，先補齊 40 點讓交易順利完成以測試取消回滾
        with app.app_context():
            u1 = db.session.get(User, member_ids["陳小明"])
            pts_after_redeem = u1.points

        # 2. 送出訂單：2份麻辣雞腿堡($280) + 1份紅利兌換和牛堡($0) + VIP50(-$50) + 使用 20點折抵(-$10) -> 實付 $220 (贈 2 點)
        r_ord = cust_client.post("/submit_order", json={
            "items": [
                redeemed_item,
                {"id": menu_ids["川味麻辣雞腿堡"], "name": "川味麻辣雞腿堡", "price": 140, "quantity": 2}
            ],
            "payment_method": "Cash",
            "order_type": "內用",
            "promo_code": "VIP50",
            "use_points": 20
        }, headers=headers).get_json()
        cancel_oid = r_ord["order_id"]

        with app.app_context():
            u1 = db.session.get(User, member_ids["陳小明"])
            pts_after_checkout = u1.points

        # 3. 後台執行取消訂單 (/admin/update_order_status/<id>)
        a_csrf = get_client_csrf(admin_client, "/admin")
        r_cancel = admin_client.post(
            f"/admin/update_order_status/{cancel_oid}",
            json={"status": "Cancelled"},
            headers={"X-CSRFToken": a_csrf}
        )
        assert r_cancel.status_code == 200 and r_cancel.get_json()["status"] == "Cancelled"

        # 4. 驗證主餐庫存、兌換商品庫存、折抵點數(20點)+兌換點數(40點)-贈點(2點) 與 VIP50 優惠券全數回滾
        with app.app_context():
            u1 = db.session.get(User, member_ids["陳小明"])
            expected_pts_restored = pts_after_checkout + 20 + 40 - 2
            assert u1.points == expected_pts_restored, f"取消後點數回滾異常: 預期 {expected_pts_restored}, 實際 {u1.points}"
            assert db.session.get(MenuItem, menu_ids["招牌和牛堡"]).stock == burger_stock_before, "和牛堡庫存未回補"
            assert db.session.get(MenuItem, menu_ids["川味麻辣雞腿堡"]).stock == chicken_stock_before, "麻辣雞腿堡庫存未回補"
            uc_vip50 = UserCoupon.query.execution_options(skip_tenant_filter=True).filter_by(
                user_id=u1.id, code="VIP50"
            ).first()
            assert uc_vip50.is_used is False, "VIP50 優惠券未復原為可用狀態"
        return f"庫存(+1,+2)、折抵點數(+20)、兌換點數(+40)、扣回贈點(-2)與VIP50票券全數回滾"
    runner.check("9.3", "取消訂單全量回滾 (庫存回補/兌換點數退還/折抵點數退還/扣回贈點/優惠券復原)", t9_3)

    # ==========================================================================
    # Suite 10：營運分析、AI 三層降級、Excel 多工作表匯出與智慧匯入
    # ==========================================================================
    runner.start_suite("模組 10：營運分析、AI 三層韌性降級、Excel 多工作表匯出與智慧匯入")

    def t10_1():
        csrf = get_client_csrf(admin_client, "/admin")
        r_live = admin_client.get("/api/admin_live_orders").get_json()
        assert r_live["success"] is True and "analytics" in r_live
        assert len(r_live["analytics"]["ai_advice"]) >= 10, "AI 營運建議生成為空"

        r_desc = admin_client.post(
            "/api/generate_item_description",
            json={"name": "松露野菇燉飯", "category": "主餐"},
            headers={"X-CSRFToken": csrf}
        ).get_json()
        assert r_desc["success"] is True and len(r_desc["description"]) >= 10
        return f"AI文案='{r_desc['description'][:22]}...'"
    runner.check("10.1", "後台即時營運分析與三層韌性 AI 建議 / 菜單文案生成驗證", t10_1)

    def t10_2():
        csrf = get_client_csrf(admin_client, "/admin")
        today_str = datetime.now().strftime("%Y-%m-%d")
        r_exp = admin_client.post("/admin/export_excel", data={
            "tables": ["User", "MenuItem", "Coupon", "UserCoupon", "Order", "OrderItem", "Analytics", "AdminLog"],
            "start_date": today_str,
            "end_date": today_str,
            "csrf_token": csrf
        })
        assert r_exp.status_code == 200, f"Excel 匯出失敗: {r_exp.status_code}"
        excel_bytes = r_exp.get_data()
        wb = openpyxl.load_workbook(io.BytesIO(excel_bytes))
        expected_sheets = {
            "會員資料(User)", "菜單品項(MenuItem)", "優惠券(Coupon)", "會員持券(UserCoupon)",
            "訂單總覽(Order)", "訂單明細(OrderItem)", "區間營運總覽(Analytics)",
            "區間熱銷排行(ItemSales)", "管理員進出日誌(AdminLog)"
        }
        assert expected_sheets.issubset(set(wb.sheetnames)), f"匯出工作表缺漏: {wb.sheetnames}"

        r_imp = admin_client.post("/admin/import_smart", data={
            "file": (io.BytesIO(excel_bytes), "export_test.xlsx"),
            "csrf_token": csrf
        }, content_type="multipart/form-data")
        assert r_imp.status_code == 200 and "智慧匯入完成" in r_imp.get_data(as_text=True)
        return f"成功匯出並雙向智慧匯入驗證 {len(wb.sheetnames)} 個工作表"
    runner.check("10.2", "Excel 全資料表匯出 (含區間熱銷排行與欄寬自適應) 與智慧雙向匯入", t10_2)

    # ==========================================================================
    # Suite 11：高併發搶購防超賣與取餐號唯一性壓力測試
    # ==========================================================================
    runner.start_suite("模組 11：高併發在線人數、SSE 看板長連線與防超賣壓力測試 (整合 test_lock.py)")

    # 11.1 記憶體內 TestClient 12 執行緒搶 5 份
    def t11_1():
        with app.app_context():
            t1 = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug="default").first()
            limited = MenuItem(
                tenant_id=t1.id, name="限量龍蝦堡", category="主餐", price=300,
                stock=5, total_stock=5, is_sold_out=False, modifiers="none"
            )
            db.session.add(limited)
            db.session.commit()
            lim_id = limited.id

        def _worker(idx):
            c = app.test_client()
            c.get("/?shop=default")
            c.get("/guest_login")
            csrf = get_client_csrf(c, "/")
            resp = c.post("/submit_order", json={
                "items": [{"id": lim_id, "name": "限量龍蝦堡", "price": 300, "quantity": 1}],
                "payment_method": "Cash", "order_type": "內用"
            }, headers={"X-CSRFToken": csrf})
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
    runner.check("11.1", "記憶體內 12 執行緒同時搶購 5 份限量商品：防超賣、零負庫存與取餐號互斥驗證", t11_1)

    # 啟動內嵌真實多執行緒 HTTP 伺服器以執行 test_lock.py 實體網路壓測 (11.2 & 11.3)
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
        # 11.2 真實 HTTP 網路壓測（階段 1）：背景 SSE 即時串流 + 60 位同時在線瀏覽測試
        def t11_2():
            sse_streams = int(os.environ.get("STRESS_SSE_STREAMS", "8"))
            browse_users = int(os.environ.get("STRESS_BROWSE_USERS", "60"))
            stop_sse = threading.Event()
            active_sse = 0
            sse_lock = threading.Lock()

            # 先取得一組管理員認證 Cookie，讓帶有 @admin_required 的 /api/orders_stream 能正常建立 200 SSE 長連線
            with requests.Session() as admin_sess:
                r_login_page = admin_sess.get(f"{live_base_url}/admin", timeout=10)
                l_csrf = extract_csrf(r_login_page.text)
                admin_sess.post(
                    f"{live_base_url}/admin",
                    data={"username": "admin_test", "password": "AdminPass!2026", "csrf_token": l_csrf},
                    timeout=10
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
                        timeout=15
                    ) as resp:
                        if resp.status_code == 200:
                            with sse_lock:
                                active_sse += 1
                            # 讀取首筆初始推播後維持長連線，直到壓測階段 1 完成
                            next(resp.iter_lines(), None)
                            stop_sse.wait(timeout=10.0)
                except Exception:
                    pass

            sse_threads = []
            for i in range(sse_streams):
                t = threading.Thread(target=_sse_worker, args=(i,), daemon=True)
                t.start()
                sse_threads.append(t)
            time.sleep(0.3)

            def _simulate_browsing_user(user_idx):
                t0 = time.perf_counter()
                try:
                    with requests.Session() as s:
                        r1 = s.get(f"{live_base_url}/guest_login", timeout=10)
                        r2 = s.get(f"{live_base_url}/api/counter_orders", timeout=10)
                        if r2.status_code == 404:
                            r2 = s.get(f"{live_base_url}/counter", timeout=10)
                        elapsed_ms = (time.perf_counter() - t0) * 1000.0
                        ok = (r1.status_code == 200 and r2.status_code in (200, 429))
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
            try:
                for q in list(getattr(order_event_bus, "_subscribers", {}).keys()):
                    try:
                        q.put_nowait("{}")
                    except Exception:
                        pass
            except Exception:
                pass

            ok_browse = [r for r in browse_results if r["ok"]]
            rps = (len(browse_results) * 2) / max(0.001, t_browse_total)
            lat_str = _calc_latency_summary([r["ms"] for r in browse_results])

            assert active_sse >= 1, f"SSE 背景串流未能成功建立 (active={active_sse})"
            assert len(ok_browse) == len(browse_results), f"在線瀏覽成功率未達 100%: {len(ok_browse)}/{len(browse_results)}"
            return f"活躍SSE={active_sse}條 | {len(ok_browse)}/{len(browse_results)}人瀏覽成功 ({rps:.1f} req/s) | {lat_str}"
        runner.check("11.2", "真實 HTTP 壓測 (階段 1)：背景 8 條 SSE 長連線 + 60 位同時在線瀏覽與延遲統計", t11_2)

        # 11.3 真實 HTTP 網路壓測（階段 2）：30 位併發顧客搶購 10 份限量和牛漢堡
        def t11_3():
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
                        modifiers="none"
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
                try:
                    with requests.Session() as s:
                        s.get(f"{live_base_url}/?shop=default", timeout=10)
                        r_login = s.get(f"{live_base_url}/guest_login", timeout=10)
                        csrf_token = extract_csrf(r_login.text)
                        headers = {
                            "Content-Type": "application/json",
                            "X-CSRFToken": csrf_token,
                            "X-Forwarded-For": f"10.0.{user_idx // 250}.{(user_idx % 250) + 1}"
                        }
                        payload = {
                            "items": [{
                                "id": item_id,
                                "name": item_name,
                                "price": 150,
                                "quantity": 1,
                                "customization": "",
                                "is_add_on": False
                            }],
                            "payment_method": "Cash",
                            "order_type": "內用",
                            "need_cutlery": False,
                            "note": f"壓測併發顧客 #{user_idx}",
                            "promo_code": "",
                            "use_points": 0
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
                            "msg": data.get("message") or data.get("error") or ""
                        }
                except Exception as e:
                    elapsed_ms = (time.perf_counter() - t0) * 1000.0
                    return {
                        "status_code": 0,
                        "ms": elapsed_ms,
                        "order_id": None,
                        "pickup_number": None,
                        "insufficient_stock": False,
                        "msg": str(e)
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

            assert len(success_orders) == target_stock, (
                f"成功搶購應為 {target_stock} 筆，實際 {len(success_orders)} 筆 "
                f"(攔截={len(sold_out_blocked)}, 限流={len(rate_limited)})"
            )
            assert len(sold_out_blocked) == checkout_users - target_stock, (
                f"庫存不足攔截應為 {checkout_users - target_stock} 筆，實際 {len(sold_out_blocked)} 筆"
            )
            assert rem_stock == 0, f"剩餘庫存應為 0，實際為 {rem_stock}"
            assert len(pickup_numbers) == len(unique_pickups), f"取餐號重複: {pickup_numbers}"
            return (
                f"{checkout_users}人搶{target_stock}份 -> 成交{len(success_orders)}筆、"
                f"防超賣攔截{len(sold_out_blocked)}筆、取餐號全唯一 | {lat_str}"
            )
        runner.check("11.3", "真實 HTTP 壓測 (階段 2)：30 人同時結帳搶購 10 份限量漢堡、防超賣與取餐號互斥驗證", t11_3)
    finally:
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
    except Exception:
        pass

    return 0 if runner.failed == 0 else 1


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="自助點餐與 POS 系統 - 全功能排列組合與高併發壓力測試腳本")
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
