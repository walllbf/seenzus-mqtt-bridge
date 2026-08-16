"""Persistent side-effect fences for commands carrying an operation key."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol


OPERATION_STATUSES = {"claimed", "dispatched", "completed", "unknown"}


@dataclass
class OperationRecord:
    fingerprint: str | None
    status: str
    result: dict[str, Any] | None = None
    updated_at: str | None = None


class OperationStore(Protocol):
    async def claim(self, key: str, fingerprint: str) -> tuple[str, dict[str, Any] | None]: ...
    async def mark_dispatched(self, key: str, fingerprint: str) -> bool: ...
    async def complete(self, key: str, fingerprint: str, result: dict[str, Any]) -> bool: ...


class PersistentOperationStore:
    """HA .storage-backed ledger; operation keys are retained as durable tombstones."""

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
                if not isinstance(key, str):
                    continue
                if not isinstance(value, dict):
                    rows[key] = OperationRecord(None, "unknown")
                    continue
                fingerprint = value.get("fingerprint")
                status = value.get("status")
                result = value.get("result")
                updated_at = value.get("updated_at")
                if (
                    not isinstance(fingerprint, str)
                    or status not in OPERATION_STATUSES - {"unknown"}
                    or (result is not None and not isinstance(result, dict))
                ):
                    # An unparseable persisted key may have crossed the device
                    # boundary. Preserve it as unknown rather than redispatch.
                    rows[key] = OperationRecord(None, "unknown")
                    continue
                rows[key] = OperationRecord(fingerprint, status, result, updated_at if isinstance(updated_at, str) else None)
            self._rows = rows
        return self._rows

    async def _save(self) -> None:
        if self._store is None:
            raise RuntimeError("operation store was not initialized")
        await self._store.async_save({key: vars(value) for key, value in (self._rows or {}).items()})

    async def claim(self, key: str, fingerprint: str) -> tuple[str, dict[str, Any] | None]:
        async with self._lock:
            rows = await self._load()
            row = rows.get(key)
            if row is None:
                rows[key] = OperationRecord(fingerprint, "claimed", updated_at=datetime.now(timezone.utc).isoformat())
                self._owned_claims.add(key)
                try:
                    await self._save()
                except Exception:
                    # The write may have reached disk even if it raised. Forget
                    # local ownership so the next attempt safely treats a
                    # persisted pre-dispatch claim as recoverable.
                    self._owned_claims.discard(key)
                    raise
                return "claimed", None
            if row.status == "unknown":
                return "unknown", None
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
                try:
                    await self._save()
                except Exception:
                    self._owned_claims.discard(key)
                    raise
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
