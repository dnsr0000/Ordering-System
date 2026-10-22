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
    from app.sync.hub import sync_api

    for bp in (customer_bp, auth_bp, rewards_bp, kitchen_bp, pickup_bp, admin_bp, pos_bp, orders_bp, media_bp,
               health_bp, sync_admin_bp, sync_api, payment_bp,
               privacy_bp, tables_bp):
        app.register_blueprint(bp)
