"""
數位看板 (門市上方大螢幕，免登入)：菜單與售完標示、促銷標語輪播、取餐 / 候位叫號。

- 純前端呈現層：資料由 /api/signage 一次取得 (帶 ETag，沒變動時回 304)，
  並訂閱既有的 /api/orders_stream (SSE)：訂單、售完、庫存、後台菜單修改都會觸發重新抓取。
- 售完 / 供應時段 / 原料不足 / 分店上下架的判斷與點餐頁相同 (沿用 customer._availability_map)。
- 設定在後台「數位看板」區塊 (settings_service 的 signage)。
"""
import json
from datetime import date, datetime

from flask import Blueprint, abort, g, render_template, url_for

from app.extensions import limiter
from app.utils import json_etag_response
from app.models.enums import OrderStatus
from app.models.menu import MenuItem
from app.models.order import Order
from app.models.promotion import Promotion
from app.services import branch_service, i18n_service, intake_service, promotion_service, settings_service
from app.services.order_service import todays_orders_condition, todays_range
from app.sync import runtime as sync_runtime

signage_bp = Blueprint('signage', __name__)
MAX_READY = 12
MAX_CALLED = 6


def _config():
    cfg = settings_service.get('signage')
    return {
        'enabled': bool(cfg.get('enabled', True)),
        'headline': (cfg.get('headline') or '').strip() or (g.tenant.name if g.tenant else ''),
        'columns': max(1, min(4, int(cfg.get('columns') or 3))),
        'pageSeconds': max(3, int(cfg.get('page_seconds') or 10)),
        'showImages': bool(cfg.get('show_images', True)),
        'showEnglish': bool(cfg.get('show_english', True)),
        'hideSoldOut': bool(cfg.get('hide_sold_out')),
        'showPickup': bool(cfg.get('show_pickup', True)),
        'promoSeconds': max(3, int(cfg.get('promo_seconds') or 8)),
        'categories': [c.strip() for c in str(cfg.get('categories') or '').replace('，', ',').split(',') if c.strip()],
        'promoLines': [ln.strip() for ln in str(cfg.get('promo_lines') or '').splitlines() if ln.strip()],
    }


@signage_bp.route('/signage', endpoint='board')
def board():
    cfg = _config()
    if not cfg['enabled']:
        abort(404)
    return render_template('signage.html', tenant=g.tenant, config=cfg)


# ---------------------------------------------------------------------------
# 資料
# ---------------------------------------------------------------------------
def _promo_lines(cfg, now):
    """後台填的標語優先；空白時輪播進行中的門市促銷活動名稱"""
    if cfg['promoLines']:
        return cfg['promoLines']
    promos = Promotion.query.filter_by(is_active=True).order_by(Promotion.priority.desc(), Promotion.id.asc()).all()
    return [p.name for p in promos if promotion_service.is_active(p, 'kiosk', False, now)][:10]


def _on_sale(item, today):
    """販售期間以外 (尚未開賣 / 已停售) 的餐點不上看板；供應時段外的仍顯示並標示時段"""
    if item.sale_from and today < item.sale_from:
        return False
    return not (item.sale_until and today > item.sale_until)


def _menu(cfg, now):
    from app.routes.customer import _availability_map, _online_nodes, _pickup_node
    is_hub = sync_runtime.is_hub()
    pickup_node = _pickup_node(_online_nodes()) if is_hub else None
    branch_service.activate(g.tenant_id, branch_service.storefront_branch(pickup_node))
    today = now.date()
    items = [i for i in MenuItem.query.order_by(MenuItem.id.asc()).all()
             if not i.is_branch_hidden and _on_sale(i, today)]
    avail = _availability_map(items, is_hub, pickup_node)
    en_cats = i18n_service.category_labels('en') if cfg['showEnglish'] else {}

    grouped = {}
    for i in items:
        available, hint = avail[i.id]
        if cfg['hideSoldOut'] and not available:
            continue
        grouped.setdefault((i.category or '未分類').strip(), []).append({
            'id': i.id,
            'name': i.name,
            'name_en': (i.name_en or '') if cfg['showEnglish'] else '',
            'price': i.unit_price,
            'original_price': i.list_price if i.unit_price < i.list_price else 0,
            'available': available,
            'hint': hint,
            'is_new': bool(i.is_new),
            'is_popular': bool(i.is_manual_popular or i.is_recommended),
            'spicy_level': i.spicy_level or 0,
            'diet': i.diet or '',
            'image': url_for('media.menu_image', filename=i.image_path) if cfg['showImages'] and i.image_path else '',
        })
    if cfg['categories']:
        order = [c for c in cfg['categories'] if c in grouped]
    else:
        order = sorted(grouped)
    return [{'name': c, 'name_en': en_cats.get(c, ''), 'items': grouped[c]} for c in order]


def _pickup():
    start, end = todays_range()
    ready = (Order.query.filter(todays_orders_condition(start, end, include_future=False),
                                Order.status == OrderStatus.COMPLETED,
                                Order.payment_status.notin_(('pending', 'authorizing', 'expired')))
             .order_by(Order.id.desc()).limit(MAX_READY).all())
    from app.models.reservation import WaitlistEntry
    called = (WaitlistEntry.query.filter_by(business_date=date.today(), status='called')
              .order_by(WaitlistEntry.called_at.desc()).limit(MAX_CALLED).all())
    return {'ready': [{'id': o.id, 'number': o.display_pickup_number} for o in ready],
            'called': [{'id': w.id, 'label': w.label} for w in called]}


@signage_bp.route('/api/signage', endpoint='data')
@limiter.limit('120/minute')
def api_signage():
    cfg = _config()
    if not cfg['enabled']:
        abort(404)
    now = datetime.now()
    payload = {
        'success': True,
        'headline': cfg['headline'],
        'categories': _menu(cfg, now),
        'promos': _promo_lines(cfg, now),
        'pickup': _pickup() if cfg['showPickup'] else None,
        'intake': intake_service.customer_notice(g.tenant_id, now=now),
    }
    return json_etag_response(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')))
