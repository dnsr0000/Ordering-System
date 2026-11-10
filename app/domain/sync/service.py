"""Sync domain service for hub/node orchestration and retry policies."""

from __future__ import annotations

from typing import Any

from app.core.errors import RetryableSyncError, SyncConflictError


class SyncDomainService:
    """Coordinates sync operations without depending directly on route logic."""

    def audit_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "node_id": payload.get("node_id"),
            "status": "received",
            "version": payload.get("version", 0),
        }

    def detect_conflict(self, local_version: int, remote_version: int) -> None:
        if local_version != remote_version:
            raise SyncConflictError("本地與遠端資料版本不一致，請先同步後再更新")

    def request_retry(self, reason: str) -> None:
        raise RetryableSyncError(reason)


__all__ = ["SyncDomainService"]
