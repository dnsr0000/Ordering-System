"""Tenant domain service for tenant routing, role context and permission checks."""

from __future__ import annotations

from typing import Any

from app.core.errors import TenantNotFoundError, ValidationError


class TenantDomainService:
    """Business-facing logic for resolving tenant identity and access rules."""

    def resolve_context(self, tenant_id: Any) -> dict[str, Any]:
        if tenant_id in (None, "", 0):
            raise TenantNotFoundError("找不到店家上下文")
        return {"tenant_id": tenant_id, "role": "manager"}

    def validate_access(self, role: str, required_role: str) -> None:
        if role != required_role:
            raise ValidationError(f"此操作需要 {required_role} 權限，當前角色為 {role}")


__all__ = ["TenantDomainService"]
