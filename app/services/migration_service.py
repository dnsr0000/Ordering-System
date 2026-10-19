from collections import defaultdict
from datetime import datetime
from werkzeug.security import generate_password_hash
from app.extensions import db
from app.models.order import Order
from app.models.menu import ComboOption, ModifierOption
from app.models.user import (
    RewardSetting, FEATURE_MAGIC_PREFIX,
    decode_feature_bytes_safely, encode_feature_array
)
from app.models.tenant import Tenant, StaffAccount
from app.config import Config


def _run_migrations_inner():
    default_tenant = None
    try:
        # ======================================================================
        # 0. 建立預設店家並回填舊單店資料
        # ======================================================================
        default_tenant = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug='default').first()
        if not default_tenant:
            default_tenant = Tenant(name='預設店家', slug='default', is_active=True)
            db.session.add(default_tenant)
            db.session.commit()

        tenant_tables = (
            'user', 'admin_log', 'staff_audit_log', 'customer_log', 'reward_setting',
            'reward_redemption', 'menu_item', 'combo_option', 'modifier_option',
            'order', 'order_item', 'coupon', 'user_coupon'
        )
        for table_name in tenant_tables:
            columns = db.session.execute(db.text(f'PRAGMA table_info("{table_name}")')).fetchall()
            if not columns:
                continue
            column_names = {column[1] for column in columns}
            if 'tenant_id' not in column_names:
                db.session.execute(db.text(f'ALTER TABLE "{table_name}" ADD COLUMN tenant_id INTEGER'))
            db.session.execute(
                db.text(f'UPDATE "{table_name}" SET tenant_id = :tenant_id WHERE tenant_id IS NULL'),
                {'tenant_id': default_tenant.id}
            )
            safe_table_name = table_name.replace('"', '')
            db.session.execute(db.text(
                f'CREATE INDEX IF NOT EXISTS "ix_{safe_table_name}_tenant_id" '
                f'ON "{safe_table_name}" (tenant_id)'
            ))
        db.session.commit()

        # 建立或同步 .env 設定之預設管理員帳號與密碼 (#1)
        bootstrap_account = StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(
            username=Config.ADMIN_USERNAME
        ).first()
        pwd_hash = getattr(Config, 'ADMIN_PASSWORD_HASH', None) or generate_password_hash(Config.ADMIN_PASSWORD)
        if not bootstrap_account:
            db.session.add(StaffAccount(
                tenant_id=default_tenant.id,
                username=Config.ADMIN_USERNAME,
                password_hash=pwd_hash,
                role='owner',
                is_active=True
            ))
            db.session.commit()
        else:
            if not bootstrap_account.check_password(Config.ADMIN_PASSWORD) or not bootstrap_account.is_active:
                bootstrap_account.password_hash = pwd_hash
                bootstrap_account.is_active = True
                db.session.commit()

        # ======================================================================
        # 0.5 確保 staff_audit_log 與 reward_redemption 資料表及所有欄位完整存在
        # ======================================================================
        db.session.execute(db.text("""
            CREATE TABLE IF NOT EXISTS staff_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL DEFAULT 1,
                staff_account_id INTEGER,
                staff_username VARCHAR(100) NOT NULL DEFAULT '',
                staff_role VARCHAR(30) NOT NULL DEFAULT 'staff',
                action_type VARCHAR(60) NOT NULL DEFAULT '',
                target_order_id INTEGER,
                target_user_id INTEGER,
                points_delta INTEGER DEFAULT 0,
                details TEXT DEFAULT '',
                ip_address VARCHAR(50) DEFAULT '',
                created_at DATETIME
            )
        """))
        audit_info = db.session.execute(db.text("PRAGMA table_info(staff_audit_log)")).fetchall()
        audit_cols = {col[1] for col in audit_info}
        for col_name, col_def in [
            ("tenant_id", "INTEGER DEFAULT 1"),
            ("staff_account_id", "INTEGER"),
            ("staff_username", "VARCHAR(100) DEFAULT ''"),
            ("staff_role", "VARCHAR(30) DEFAULT 'staff'"),
            ("action_type", "VARCHAR(60) DEFAULT ''"),
            ("target_order_id", "INTEGER"),
            ("target_user_id", "INTEGER"),
            ("points_delta", "INTEGER DEFAULT 0"),
            ("details", "TEXT DEFAULT ''"),
            ("ip_address", "VARCHAR(50) DEFAULT ''"),
            ("created_at", "DATETIME"),
        ]:
            if col_name not in audit_cols:
                db.session.execute(db.text(f"ALTER TABLE staff_audit_log ADD COLUMN {col_name} {col_def}"))

        db.session.execute(db.text("""
            CREATE TABLE IF NOT EXISTS reward_redemption (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id INTEGER NOT NULL DEFAULT 1,
                user_id INTEGER NOT NULL,
                menu_item_id INTEGER NOT NULL,
                points_cost INTEGER NOT NULL DEFAULT 0,
                is_used BOOLEAN NOT NULL DEFAULT 0,
                is_refunded BOOLEAN NOT NULL DEFAULT 0,
                order_id INTEGER,
                created_at DATETIME
            )
        """))
        red_info = db.session.execute(db.text("PRAGMA table_info(reward_redemption)")).fetchall()
        red_cols = {col[1] for col in red_info}
        for col_name, col_def in [
            ("tenant_id", "INTEGER DEFAULT 1"),
            ("user_id", "INTEGER DEFAULT 0"),
            ("menu_item_id", "INTEGER DEFAULT 0"),
            ("points_cost", "INTEGER DEFAULT 0"),
            ("is_used", "BOOLEAN DEFAULT 0"),
            ("is_refunded", "BOOLEAN DEFAULT 0"),
            ("order_id", "INTEGER"),
            ("created_at", "DATETIME"),
        ]:
            if col_name not in red_cols:
                db.session.execute(db.text(f"ALTER TABLE reward_redemption ADD COLUMN {col_name} {col_def}"))
        db.session.commit()

        # ======================================================================
        # 1. 檢查 User 資料表、舊版 Pickle 特徵轉型與重複電話合併
        # ======================================================================
        user_info = db.session.execute(db.text("PRAGMA table_info(user)")).fetchall()
        user_cols = [col[1] for col in user_info]

        if 'pin_hash' not in user_cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN pin_hash VARCHAR(255)"))
        if 'feature' not in user_cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN feature BLOB"))
        if 'points' not in user_cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN points INTEGER DEFAULT 0"))
        if 'last_login_at' not in user_cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN last_login_at DATETIME"))
        if 'last_logout_at' not in user_cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN last_logout_at DATETIME"))
        db.session.commit()

        # 1a. 自動將舊版 Pickle 人臉特徵轉型為安全二進位格式 NPF32v1:
        raw_feat_rows = db.session.execute(
            db.text("SELECT id, feature FROM user WHERE feature IS NOT NULL")
        ).fetchall()
        for uid, raw_blob in raw_feat_rows:
            if not raw_blob:
                continue
            b_data = bytes(raw_blob) if not isinstance(raw_blob, bytes) else raw_blob
            if not b_data.startswith(FEATURE_MAGIC_PREFIX):
                arr = decode_feature_bytes_safely(b_data)
                safe_blob = encode_feature_array(arr) if arr is not None else None
                db.session.execute(
                    db.text("UPDATE user SET feature = :feat WHERE id = :id"),
                    {"feat": safe_blob, "id": uid}
                )
        db.session.commit()

        # 1b. 自動合併同店重複電話號碼會員 (保留最高點數帳號，並將歷史訂單與優惠券歸戶)
        dup_groups = db.session.execute(db.text("""
            SELECT tenant_id, phone, COUNT(*) as cnt
            FROM user
            GROUP BY tenant_id, phone
            HAVING COUNT(*) > 1
        """)).fetchall()

        for tid, phone_val, _ in dup_groups:
            rows = db.session.execute(
                db.text("SELECT id, points FROM user WHERE tenant_id = :tid AND phone = :phone ORDER BY COALESCE(points, 0) DESC, id ASC"),
                {"tid": tid, "phone": phone_val}
            ).fetchall()
            if len(rows) > 1:
                keep_id = rows[0][0]
                max_pts = max(int(r[1] or 0) for r in rows)
                db.session.execute(
                    db.text("UPDATE user SET points = :pts WHERE id = :id"),
                    {"pts": max_pts, "id": keep_id}
                )
                for dup_id, _ in rows[1:]:
                    db.session.execute(
                        db.text('UPDATE "order" SET user_id = :keep_id WHERE user_id = :dup_id'),
                        {"keep_id": keep_id, "dup_id": dup_id}
                    )
                    db.session.execute(
                        db.text("UPDATE user_coupon SET user_id = :keep_id WHERE user_id = :dup_id"),
                        {"keep_id": keep_id, "dup_id": dup_id}
                    )
                    db.session.execute(
                        db.text("DELETE FROM user WHERE id = :dup_id"),
                        {"dup_id": dup_id}
                    )
        db.session.execute(
            db.text("CREATE UNIQUE INDEX IF NOT EXISTS ux_user_tenant_phone ON user (tenant_id, phone)")
        )
        db.session.commit()

        # ======================================================================
        # 2. 檢查 MenuItem 資料表
        # ======================================================================
        menu_info = db.session.execute(db.text("PRAGMA table_info(menu_item)")).fetchall()
        menu_cols = [col[1] for col in menu_info]

        if 'modifiers' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN modifiers VARCHAR(50) DEFAULT 'none'"))
        if 'description' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN description VARCHAR(200) DEFAULT ''"))
        if 'image_path' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN image_path VARCHAR(200) DEFAULT ''"))
        if 'is_recommended' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_recommended BOOLEAN DEFAULT 0"))
        if 'is_manual_popular' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_manual_popular BOOLEAN DEFAULT 0"))
        if 'is_new' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_new BOOLEAN DEFAULT 0"))
        if 'is_reward' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_reward BOOLEAN DEFAULT 0"))
        if 'reward_points' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN reward_points INTEGER DEFAULT 0"))
        if 'reward_discount_points' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN reward_discount_points INTEGER DEFAULT 0"))
        if 'is_discount' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_discount BOOLEAN DEFAULT 0"))
        if 'discount_price' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN discount_price INTEGER DEFAULT 0"))
        if 'is_sold_out' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN is_sold_out BOOLEAN DEFAULT 0"))
        if 'stock' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN stock INTEGER DEFAULT 50"))
        if 'total_stock' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN total_stock INTEGER DEFAULT 50"))
        if 'can_be_add_on' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN can_be_add_on BOOLEAN DEFAULT 0"))
        if 'add_on_price' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN add_on_price INTEGER DEFAULT 0"))
        if 'addon_trigger_type' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN addon_trigger_type VARCHAR(20) DEFAULT 'any'"))
        if 'addon_trigger_target' not in menu_cols:
            db.session.execute(db.text("ALTER TABLE menu_item ADD COLUMN addon_trigger_target VARCHAR(100) DEFAULT ''"))

        # ======================================================================
        # 3. 檢查 Order 資料表與回填舊訂單實收金額
        # ======================================================================
        order_info = db.session.execute(db.text('PRAGMA table_info("order")')).fetchall()
        order_cols = [col[1] for col in order_info]

        if 'user_id' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN user_id INTEGER'))
        if 'points_used' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN points_used INTEGER DEFAULT 0'))
        if 'points_earned' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN points_earned INTEGER DEFAULT 0'))
        if 'discount_amount' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN discount_amount INTEGER DEFAULT 0'))
        if 'payment_method' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN payment_method VARCHAR(50) DEFAULT "Cash"'))
        if 'order_type' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN order_type VARCHAR(50) DEFAULT "內用"'))
        if 'order_source' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN order_source VARCHAR(30) DEFAULT "Kiosk"'))
        if 'cashier_username' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN cashier_username VARCHAR(100) DEFAULT ""'))
        if 'cash_received' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN cash_received INTEGER DEFAULT 0'))
        if 'cash_change' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN cash_change INTEGER DEFAULT 0'))
        if 'need_cutlery' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN need_cutlery BOOLEAN DEFAULT 1'))
        if 'note' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN note VARCHAR(200) DEFAULT ""'))
        if 'status' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN status VARCHAR(50) DEFAULT "Pending"'))
        if 'completed_at' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN completed_at DATETIME'))
        if 'pickup_number' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN pickup_number INTEGER DEFAULT 1'))
        if 'customer_log_id' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN customer_log_id INTEGER'))
        if 'coupon_code' not in order_cols:
            db.session.execute(db.text('ALTER TABLE "order" ADD COLUMN coupon_code VARCHAR(50) DEFAULT ""'))

        # 回填歷史訂單的 order_source 與 cash_received，避免舊訂單匯出實收顯示為 $0
        db.session.execute(db.text("""
            UPDATE "order"
            SET order_source = COALESCE(NULLIF(order_source, ''), 'Kiosk'),
                cash_received = total_price
            WHERE (cash_received IS NULL OR cash_received = 0)
              AND (order_source IS NULL OR order_source = '' OR order_source = 'Kiosk')
              AND total_price > 0
        """))

        # ======================================================================
        # 4. 檢查 OrderItem 資料表
        # ======================================================================
        order_item_info = db.session.execute(db.text("PRAGMA table_info(order_item)")).fetchall()
        order_item_cols = [col[1] for col in order_item_info]

        if 'menu_item_id' not in order_item_cols:
            db.session.execute(db.text("ALTER TABLE order_item ADD COLUMN menu_item_id INTEGER"))
        if 'combo_option_id' not in order_item_cols:
            db.session.execute(db.text("ALTER TABLE order_item ADD COLUMN combo_option_id INTEGER"))
        if 'customization' not in order_item_cols:
            db.session.execute(db.text("ALTER TABLE order_item ADD COLUMN customization VARCHAR(200) DEFAULT ''"))

        # ======================================================================
        # 5. 檢查 ComboOption 資料表
        # ======================================================================
        combo_info = db.session.execute(db.text("PRAGMA table_info(combo_option)")).fetchall()
        if combo_info:
            combo_cols = [col[1] for col in combo_info]
            if 'item1_id' not in combo_cols:
                db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item1_id INTEGER REFERENCES menu_item(id)"))
            if 'item2_id' not in combo_cols:
                db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item2_id INTEGER REFERENCES menu_item(id)"))
            if 'item3_id' not in combo_cols:
                db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item3_id INTEGER REFERENCES menu_item(id)"))
            if 'item1_customizable' not in combo_cols:
                db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item1_customizable BOOLEAN DEFAULT 1"))
            if 'item2_customizable' not in combo_cols:
                db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item2_customizable BOOLEAN DEFAULT 1"))
            if 'item3_customizable' not in combo_cols:
                db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN item3_customizable BOOLEAN DEFAULT 1"))
            if 'can_addon' not in combo_cols:
                db.session.execute(db.text("ALTER TABLE combo_option ADD COLUMN can_addon BOOLEAN DEFAULT 1"))

        db.session.commit()

        # ======================================================================
        # 6. 自動校正歷史訂單每日循環取餐流水號 (1~999)
        # ======================================================================
        all_orders = Order.query.execution_options(skip_tenant_filter=True).order_by(Order.created_at.asc(), Order.id.asc()).all()
        orders_by_date = defaultdict(list)
        for o in all_orders:
            d_key = (o.tenant_id, o.created_at.date() if o.created_at else datetime.now().date())
            orders_by_date[d_key].append(o)

        needs_commit = False
        for d_key, d_orders in orders_by_date.items():
            p_nums = [o.pickup_number for o in d_orders]
            if len(d_orders) > 1 and len(set(p_nums)) <= 1:
                cur_no = 1
                for o in d_orders:
                    o.pickup_number = cur_no
                    cur_no = (cur_no % 999) + 1
                needs_commit = True
            elif len(d_orders) == 1 and d_orders[0].pickup_number is None:
                d_orders[0].pickup_number = 1
                needs_commit = True

        if needs_commit:
            db.session.commit()

        # 自動校正無客製化品項卻被標記為可客製的套餐設定
        combos = ComboOption.query.execution_options(skip_tenant_filter=True).all()
        for c in combos:
            if c.item1 and (not c.item1.modifiers or c.item1.modifiers == 'none'):
                c.item1_customizable = False
            if c.item2 and (not c.item2.modifiers or c.item2.modifiers == 'none'):
                c.item2_customizable = False
            if c.item3 and (not c.item3.modifiers or c.item3.modifiers == 'none'):
                c.item3_customizable = False
        db.session.commit()

        # ======================================================================
        # 7. 檢查 Coupon 資料表與複合唯一索引
        # ======================================================================
        coupon_info = db.session.execute(db.text("PRAGMA table_info(coupon)")).fetchall()
        coupon_cols = [col[1] for col in coupon_info]
        if 'per_user_limit' not in coupon_cols:
            db.session.execute(db.text("ALTER TABLE coupon ADD COLUMN per_user_limit INTEGER DEFAULT 0"))
            db.session.commit()

        db.session.execute(
            db.text("CREATE UNIQUE INDEX IF NOT EXISTS uq_coupon_tenant_code ON coupon (tenant_id, code)")
        )
        db.session.commit()

        # ======================================================================
        # 8. 檢查並初始化 ModifierOption 與 RewardSetting 資料表
        # ======================================================================
        if ModifierOption.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=default_tenant.id).count() == 0:
            default_addons = [
                ModifierOption(tenant_id=default_tenant.id, category='addons', name='加荷包蛋', price=10, is_active=True),
                ModifierOption(tenant_id=default_tenant.id, category='addons', name='加起司片', price=15, is_active=True),
            ]
            db.session.add_all(default_addons)
            db.session.commit()

        setting = RewardSetting.query.execution_options(skip_tenant_filter=True).filter_by(tenant_id=default_tenant.id).first()
        if not setting:
            setting = RewardSetting(
                tenant_id=default_tenant.id,
                is_enabled=True,
                points_per_dollar=1,
                max_discount_per_order=0,
                spend_per_point=100
            )
            db.session.add(setting)
            db.session.commit()

    except Exception as e:
        db.session.rollback()
        print(f"[!] 資料庫自動遷移與校正過程發生異常: {e}")


def run_database_migrations(app_instance=None):
    if app_instance is not None:
        with app_instance.app_context():
            _run_migrations_inner()
    else:
        _run_migrations_inner()
