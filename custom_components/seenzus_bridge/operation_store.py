"""Persistent side-effect fences for commands carrying an operation key."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol


OPERATION_STATUSES = {"claimed", "dispatched", "completed"}
COMPLETED_RETENTION = timedelta(days=30)
MAX_COMPLETED_RECORDS = 1000


@dataclass
class OperationRecord:
    fingerprint: str
    status: str
    result: dict[str, Any] | None = None
    updated_at: str | None = None


class OperationStore(Protocol):
    async def claim(self, key: str, fingerprint: str) -> tuple[str, dict[str, Any] | None]: ...
    async def mark_dispatched(self, key: str, fingerprint: str) -> bool: ...
    async def complete(self, key: str, fingerprint: str, result: dict[str, Any]) -> bool: ...


class PersistentOperationStore:
    """HA .storage-backed ledger; dispatched rows are never reclaimed automatically."""

    def __init__(self, hass: Any, entry_id: str, storage: Any = None) -> None:
        self._hass = hass
        self._entry_id = entry_id
        self._store = storage
        self._rows: dict[str, OperationRecord] | None = None
        self._lock = asyncio.Lock()
        # Keys claimed by this live coordinator are still in the pre-dispatch
        # window. Claims loaded from storage belong to a previous process and
        # may be safely recovered because dispatch has not been fenced yet.
        self._owned_claims: set[str] = set()

    async def _load(self) -> dict[str, OperationRecord]:
        if self._rows is None:
            if self._store is None:
                from homeassistant.helpers.storage import Store
                self._store = Store(self._hass, 1, f"seenzus_bridge.operations.{self._entry_id}")
            raw = await self._store.async_load() or {}
            rows: dict[str, OperationRecord] = {}
            for key, value in raw.items():
                if not isinstance(key, str) or not isinstance(value, dict):
                    continue
                fingerprint = value.get("fingerprint")
                status = value.get("status")
                if not isinstance(fingerprint, str) or status not in OPERATION_STATUSES:
                    continue
                result = value.get("result")
                if result is not None and not isinstance(result, dict):
                    continue
                updated_at = value.get("updated_at")
                rows[key] = OperationRecord(fingerprint, status, result, updated_at if isinstance(updated_at, str) else None)
            self._rows = rows
            self._prune_completed(datetime.now(timezone.utc))
        return self._rows

    def _prune_completed(self, now: datetime) -> None:
        rows = self._rows or {}
        completed = []
        for key, row in rows.items():
            if row.status != "completed":
                continue
            try:
                age = now - datetime.fromisoformat(row.updated_at) if row.updated_at else timedelta.max
            except (TypeError, ValueError):
                age = timedelta.max
            completed.append((key, age, row.updated_at or ""))
        for key, age, _ in completed:
            if age > COMPLETED_RETENTION:
                rows.pop(key, None)
        remaining = sorted(
            ((key, row.updated_at or "") for key, row in rows.items() if row.status == "completed"),
            key=lambda item: item[1], reverse=True,
        )
        for key, _ in remaining[MAX_COMPLETED_RECORDS:]:
            rows.pop(key, None)

    async def _save(self) -> None:
        if self._store is None:
            raise RuntimeError("operation store was not initialized")
        self._prune_completed(datetime.now(timezone.utc))
        await self._store.async_save({key: vars(value) for key, value in (self._rows or {}).items()})

    async def claim(self, key: str, fingerprint: str) -> tuple[str, dict[str, Any] | None]:
        async with self._lock:
            rows = await self._load()
            row = rows.get(key)
            if row is None:
                rows[key] = OperationRecord(fingerprint, "claimed", updated_at=datetime.now(timezone.utc).isoformat())
                self._owned_claims.add(key)
                await self._save()
                return "claimed", None
            if row.fingerprint != fingerprint:
                return "conflict", None
            if row.status == "completed":
                return "completed", row.result
            if row.status == "dispatched":
                return "unknown", None
            if row.status == "claimed" and key not in self._owned_claims:
                # A claimed row loaded from an earlier process cannot have
                # reached mark_dispatched, so it is safe to take over.
                self._owned_claims.add(key)
                row.updated_at = datetime.now(timezone.utc).isoformat()
                await self._save()
                return "claimed", None
            return "pending", None

    async def mark_dispatched(self, key: str, fingerprint: str) -> bool:
        async with self._lock:
            row = (await self._load()).get(key)
            if row is None or row.fingerprint != fingerprint or row.status != "claimed":
                return False
            row.status = "dispatched"
            row.updated_at = datetime.now(timezone.utc).isoformat()
            self._owned_claims.discard(key)
            await self._save()
            return True

    async def complete(self, key: str, fingerprint: str, result: dict[str, Any]) -> bool:
        async with self._lock:
            row = (await self._load()).get(key)
            if row is None or row.fingerprint != fingerprint or row.status != "dispatched":
                return False
            row.status = "completed"
            row.result = result
            row.updated_at = datetime.now(timezone.utc).isoformat()
            self._owned_claims.discard(key)
            await self._save()
            return True
