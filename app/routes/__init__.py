def register_blueprints(app):
    from app.routes.admin import admin_bp
    from app.routes.auth import auth_bp
    from app.routes.customer import customer_bp
    from app.routes.health import health_bp
    from app.routes.kitchen import kitchen_bp
    from app.routes.media import media_bp
    from app.routes.orders import orders_bp
    from app.routes.payment import payment_bp
    from app.routes.pickup import pickup_bp
    from app.routes.pos import pos_bp
    from app.routes.privacy import privacy_bp
    from app.routes.rewards import rewards_bp
    from app.routes.sync_admin import sync_admin_bp
    from app.routes.tables import tables_bp
    from app.routes.settings_admin import settings_bp
    from app.routes.security import security_bp
    from app.routes.public_extras import public_bp
    from app.routes.finance import invoices_bp, refunds_bp, shifts_bp
    from app.routes.printing import printing_bp
    from app.routes.menu_pro import menu_pro_bp
    from app.routes.inventory import inventory_bp
    from app.routes.members_pro import members_pro_bp
    from app.routes.promotions import promotions_bp
    from app.routes.delivery import delivery_bp
    from app.routes.reservations import reservations_bp
    from app.routes.member_auth import member_auth_bp
    from app.routes.reports import reports_bp
    from app.routes.timeclock import timeclock_bp
    from app.routes.system import system_bp
    from app.routes.feedback import feedback_bp
    from app.routes.gift_cards import gift_cards_bp
    from app.routes.purchasing import purchasing_bp
    from app.routes.live_dashboard import live_bp
    from app.routes.dispatch import dispatch_bp
    from app.routes.passes import passes_bp
    from app.routes.table_pay import table_pay_bp
    from app.routes.branches import branches_bp
    from app.routes.marketing import marketing_bp
    from app.routes.integrations import archive_bp, devices_bp, integrations_bp
    from app.routes.api_v1 import api_v1
    from app.routes.pnl import pnl_bp
    from app.routes.group_orders import group_bp
    from app.sync.hub import sync_api

    for bp in (customer_bp, auth_bp, rewards_bp, kitchen_bp, pickup_bp, admin_bp, pos_bp, orders_bp, media_bp,
               health_bp, sync_admin_bp, sync_api, payment_bp,
               privacy_bp, tables_bp, settings_bp, public_bp, invoices_bp, refunds_bp, shifts_bp, printing_bp,
               menu_pro_bp, inventory_bp, members_pro_bp, promotions_bp, delivery_bp, reservations_bp,
               member_auth_bp, reports_bp, timeclock_bp, system_bp, security_bp, feedback_bp,
               gift_cards_bp, purchasing_bp, live_bp, dispatch_bp, passes_bp, table_pay_bp,
               branches_bp, marketing_bp, integrations_bp, devices_bp, api_v1,
               archive_bp, pnl_bp, group_bp):
        app.register_blueprint(bp)
