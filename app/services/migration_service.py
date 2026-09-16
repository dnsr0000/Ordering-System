from collections import defaultdict
from datetime import datetime
from app.extensions import db
from app.models.order import Order
from app.models.menu import ComboOption, ModifierOption
from app.models.user import RewardSetting

def run_database_migrations():
    """
    資料庫結構自動遷移與相容性檢查：
    1. 檢測各資料表缺少之欄位並自動執行 ALTER TABLE
    2. 自動校正歷史訂單每日循環取餐流水號 (1~999)
    """
    try:
        # ======================================================================
        # 1. 檢查 User 資料表
        # ======================================================================
        user_info = db.session.execute(db.text("PRAGMA table_info(user)")).fetchall()
        user_cols = [col[1] for col in user_info]

        if 'feature' not in user_cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN feature BLOB"))
        if 'points' not in user_cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN points INTEGER DEFAULT 0"))
        if 'last_login_at' not in user_cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN last_login_at DATETIME"))
        if 'last_logout_at' not in user_cols:
            db.session.execute(db.text("ALTER TABLE user ADD COLUMN last_logout_at DATETIME"))

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
        # 3. 檢查 Order 資料表 (處理 SQLite 保留字 "order")
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

        # ======================================================================
        # 4. 檢查 OrderItem 資料表
        # ======================================================================
        order_item_info = db.session.execute(db.text("PRAGMA table_info(order_item)")).fetchall()
        order_item_cols = [col[1] for col in order_item_info]

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
        all_orders = Order.query.order_by(Order.created_at.asc(), Order.id.asc()).all()
        orders_by_date = defaultdict(list)
        for o in all_orders:
            d_key = o.created_at.date() if o.created_at else datetime.now().date()
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
            print("✅ 歷史訂單取餐流水號校正完成！")

        # 自動校正無客製化品項卻被標記為可客製的套餐設定
        combos = ComboOption.query.all()
        for c in combos:
            if c.item1 and (not c.item1.modifiers or c.item1.modifiers == 'none'):
                c.item1_customizable = False
            if c.item2 and (not c.item2.modifiers or c.item2.modifiers == 'none'):
                c.item2_customizable = False
            if c.item3 and (not c.item3.modifiers or c.item3.modifiers == 'none'):
                c.item3_customizable = False
        db.session.commit()

        # ======================================================================
        # 7. 檢查 Coupon 資料表
        # ======================================================================
        coupon_info = db.session.execute(db.text("PRAGMA table_info(coupon)")).fetchall()
        coupon_cols = [col[1] for col in coupon_info]
        if 'per_user_limit' not in coupon_cols:
            db.session.execute(db.text("ALTER TABLE coupon ADD COLUMN per_user_limit INTEGER DEFAULT 0"))
            db.session.commit()

        # ======================================================================
        # 8. 檢查並初始化 ModifierOption 資料表
        # ======================================================================
        db.session.execute(db.text("""
            CREATE TABLE IF NOT EXISTS modifier_option (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category VARCHAR(50) DEFAULT 'addons',
                name VARCHAR(50) NOT NULL,
                price INTEGER NOT NULL DEFAULT 0,
                is_active BOOLEAN DEFAULT 1
            );
        """))
        db.session.commit()

        if ModifierOption.query.count() == 0:
            default_addons = [
                ModifierOption(category='addons', name='加荷包蛋', price=10, is_active=True),
                ModifierOption(category='addons', name='加起司片', price=15, is_active=True),
            ]
            db.session.add_all(default_addons)
            db.session.commit()
            
    except Exception as e:
        db.session.rollback()
        print(f"[!] 資料庫自動遷移與校正過程發生異常: {e}")

    setting = RewardSetting.query.first()
    if not setting:
        setting = RewardSetting(
            is_enabled=True,
            points_per_dollar=1,
            max_discount_per_order=0,
            spend_per_point=100
        )
        db.session.add(setting)
        db.session.commit()