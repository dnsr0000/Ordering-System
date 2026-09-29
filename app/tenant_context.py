from flask import abort, current_app, g, has_request_context, request, session
from sqlalchemy import event
from sqlalchemy.orm import Session, with_loader_criteria

from app.extensions import db
from app.models.tenant import StaffAccount, Tenant, TenantScoped


@event.listens_for(Session, 'do_orm_execute')
def apply_tenant_scope(execute_state):
    if execute_state.execution_options.get('skip_tenant_filter'):
        return
    if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
        return

    tenant_id = getattr(g, 'tenant_id', None) if has_request_context() else None
    if tenant_id is None:
        # Fail closed during requests without a resolved tenant. CLI/migration
        # operations run outside request context and remain unscoped.
        if has_request_context():
            tenant_id = -1
        if tenant_id is None:
            return

    if execute_state.is_select:
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                TenantScoped,
                lambda model: model.tenant_id == tenant_id,
                include_aliases=True,
                track_closure_variables=True,
            )
        )
        return

    entity = execute_state.statement.entity_description.get('entity')
    if entity is not None and isinstance(entity, type) and issubclass(entity, TenantScoped):
        execute_state.statement = execute_state.statement.where(entity.tenant_id == tenant_id)


@event.listens_for(Session, 'before_flush')
def enforce_tenant_on_flush(session_obj, flush_context, instances):
    if not has_request_context():
        return

    tenant_id = getattr(g, 'tenant_id', None)
    for obj in session_obj.new:
        if isinstance(obj, TenantScoped):
            if tenant_id is None:
                raise RuntimeError('Tenant context is required to create tenant-owned data.')
            obj.tenant_id = tenant_id

    for obj in session_obj.dirty.union(session_obj.deleted):
        if isinstance(obj, TenantScoped) and tenant_id is not None and obj.tenant_id != tenant_id:
            raise RuntimeError('Cross-tenant data modification was blocked.')


def _unscoped_tenant_by_id(tenant_id):
    return Tenant.query.execution_options(skip_tenant_filter=True).filter_by(id=tenant_id).first()


def _default_tenant():
    return Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug='default').first()


def _staff_by_id(staff_id):
    return StaffAccount.query.execution_options(skip_tenant_filter=True).filter_by(id=staff_id).first()


def install_tenant_context(app):
    @app.before_request
    def resolve_request_tenant():
        if request.endpoint == 'static':
            return None

        # Tenant registration is deliberately public; it creates a new tenant
        # and its first owner account in one transaction.
        if request.path.rstrip('/') == '/tenant/register':
            g.tenant_id = None
            g.tenant = None
            g.staff_account = None
            return None

        staff_id = session.get('staff_account_id')
        if staff_id:
            staff = _staff_by_id(staff_id)
            tenant = _unscoped_tenant_by_id(staff.tenant_id) if staff and staff.is_active else None
            if staff and tenant and tenant.is_active:
                g.staff_account = staff
                g.tenant = tenant
                g.tenant_id = tenant.id
                session['tenant_id'] = tenant.id
                session['admin_logged_in'] = True
                return None
            for key in ('staff_account_id', 'tenant_id', 'admin_logged_in', 'admin_role'):
                session.pop(key, None)

        if request.endpoint == 'customer.customer_index' and request.args.get('shop'):
            tenant = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(
                slug=request.args['shop'].strip().lower()
            ).first()
            if not tenant or not tenant.is_active:
                abort(404)
            session['tenant_id'] = tenant.id
            g.tenant = tenant
            g.tenant_id = tenant.id
            g.staff_account = None
            return None

        # Public storefront links use /shop/<slug>; the selected shop is stored
        # in the signed session cookie and reused by the existing endpoints.
        path_parts = request.path.strip('/').split('/')
        if len(path_parts) >= 2 and path_parts[0] == 'shop':
            tenant = Tenant.query.execution_options(skip_tenant_filter=True).filter_by(slug=path_parts[1]).first()
            if not tenant or not tenant.is_active:
                abort(404)
            session['tenant_id'] = tenant.id
            g.tenant = tenant
            g.tenant_id = tenant.id
            g.staff_account = None
            return None

        tenant_id = session.get('tenant_id')
        tenant = _unscoped_tenant_by_id(tenant_id) if tenant_id else None
        if not tenant or not tenant.is_active:
            tenant = _default_tenant()
        if not tenant:
            current_app.logger.error('No active default tenant exists; run database migration.')
            abort(503)

        g.tenant = tenant
        g.tenant_id = tenant.id
        g.staff_account = None
        if not staff_id:
            session['tenant_id'] = tenant.id
        return None
