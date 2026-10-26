"""
多租戶隔離：
1. 每個請求先決定「目前店家」(g.tenant / g.tenant_id)：
   已登入員工 → 員工所屬店家；/shop/<slug> 或 ?shop=<slug> → 指定店家；否則沿用 session 或預設店家。
2. ORM 查詢 (SELECT / UPDATE / DELETE) 自動加上 tenant_id 條件；新增資料自動填入目前店家，
   跨店修改直接拒絕。請求中找不到店家時一律 fail-closed (查不到任何資料)。
3. 系統工作 (換日結案、遷移) 需要跨店操作時，使用 tenant_bypass()。
"""
from contextlib import contextmanager

from flask import abort, current_app, g, has_request_context, request, session
from flask_login import current_user
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from app.models.mixins import TenantScoped
from app.models.tenant import Tenant


def current_tenant_id():
    """目前請求的店家 ID；不在請求中或尚未決定店家時拋出例外 (不可猜成 1 號店)"""
    tid = getattr(g, 'tenant_id', None) if has_request_context() else None
    if tid is None:
        raise RuntimeError('缺少店家資訊 (tenant)')
    return tid


@contextmanager
def tenant_bypass():
    """暫時關閉租戶自動過濾 (僅供系統工作使用)"""
    prev = g.get('_tenant_bypass', False)
    g._tenant_bypass = True
    try:
        yield
    finally:
        g._tenant_bypass = prev


def _scope_tenant_id():
    """回傳要套用的店家 ID；None 代表不套用 (非請求情境或 bypass)"""
    if not has_request_context() or g.get('_tenant_bypass'):
        return None
    tid = getattr(g, 'tenant_id', None)
    return -1 if tid is None else tid


@event.listens_for(Session, 'do_orm_execute')
def _apply_tenant_scope(state):
    if state.execution_options.get('skip_tenant_filter'):
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    tid = _scope_tenant_id()
    if tid is None:
        return
    if state.is_select:
        state.statement = state.statement.options(with_loader_criteria(
            TenantScoped, lambda cls: cls.tenant_id == tid, include_aliases=True, track_closure_variables=True,
        ))
        return
    try:
        entity = state.statement.entity_description.get('entity')
    except (AttributeError, KeyError):  # 以 Table 建立的 update() / delete() 沒有 ORM 實體
        entity = None
    if isinstance(entity, type) and issubclass(entity, TenantScoped):
        state.statement = state.statement.where(entity.tenant_id == tid)


@event.listens_for(Session, 'before_flush')
def _enforce_tenant_on_flush(session_obj, flush_context, instances):
    tid = _scope_tenant_id()
    if tid is None:
        return
    for obj in session_obj.new:
        if isinstance(obj, TenantScoped):
            if tid == -1:
                raise RuntimeError('新增店家資料時必須先確定店家 (tenant)')
            if obj.tenant_id is None:
                obj.tenant_id = tid
            elif obj.tenant_id != tid:
                raise RuntimeError('Cross-tenant data creation was blocked.')
    for obj in session_obj.dirty | session_obj.deleted:
        if isinstance(obj, TenantScoped) and obj.tenant_id != tid:
            raise RuntimeError('Cross-tenant data modification was blocked.')


def _tenant_by(**filters):
    return Tenant.query.filter_by(**filters).first()


def _use_tenant(tenant, remember=True):
    g.tenant = tenant
    g.tenant_id = tenant.id
    if remember:
        session['tenant_id'] = tenant.id


def install_tenant_context(app):
    @app.before_request
    def resolve_request_tenant():
        g.tenant = g.tenant_id = None
        g.staff_account = None
        # 不需要店家的請求：靜態檔、健康檢查、分店同步 API (同步 API 依分店權杖自行決定店家)
        # 綠界的伺服器通知與跨站 POST 回傳也不解析店家：不可寫 session (會蓋掉顧客原本的登入 Cookie)
        if request.endpoint in ('static', 'health.healthz', 'payment.ecpay_notify', 'payment.ecpay_result',
                                'table_pay.notify', 'table_pay.result', 'reservations.deposit_notify',
                                'reservations.deposit_result') \
                or request.blueprint in ('sync_api', 'api_v1') \
                or request.path.rstrip('/') == '/tenant/register':
            return None

        if current_user.is_authenticated:
            g.staff_account = current_user
            _use_tenant(current_user.tenant)
            return None

        slug = None
        if request.path.startswith('/shop/'):
            slug = request.path.split('/')[2]
        elif request.endpoint == 'customer.customer_index' and request.args.get('shop'):
            slug = request.args['shop']
        if slug:
            tenant = _tenant_by(slug=slug.strip().lower())
            if not tenant or not tenant.is_active:
                abort(404)
            _use_tenant(tenant)
            return None

        tenant = _tenant_by(id=session['tenant_id']) if session.get('tenant_id') else None
        if not tenant or not tenant.is_active:
            tenant = _tenant_by(slug='default')
        if not tenant:
            current_app.logger.error('找不到預設店家 (default)，請確認資料庫遷移是否完成。')
            abort(503)
        _use_tenant(tenant)
        return None
