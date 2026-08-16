"""Persistent side-effect fences for commands carrying an operation key."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass
class OperationRecord:
    fingerprint: str
    status: str
    result: dict[str, Any] | None = None


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

    async def _load(self) -> dict[str, OperationRecord]:
        if self._rows is None:
            if self._store is None:
                from homeassistant.helpers.storage import Store
                self._store = Store(self._hass, 1, f"seenzus_bridge.operations.{self._entry_id}")
            raw = await self._store.async_load() or {}
            self._rows = {
                key: OperationRecord(**value)
                for key, value in raw.items()
                if isinstance(value, dict) and isinstance(value.get("fingerprint"), str)
            }
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
                rows[key] = OperationRecord(fingerprint, "claimed")
                await self._save()
                return "claimed", None
            if row.fingerprint != fingerprint:
                return "conflict", None
            if row.status == "completed":
                return "completed", row.result
            if row.status == "dispatched":
                return "unknown", None
            return "pending", None

    async def mark_dispatched(self, key: str, fingerprint: str) -> bool:
        async with self._lock:
            row = (await self._load()).get(key)
            if row is None or row.fingerprint != fingerprint or row.status != "claimed":
                return False
            row.status = "dispatched"
            await self._save()
            return True

    async def complete(self, key: str, fingerprint: str, result: dict[str, Any]) -> bool:
        async with self._lock:
            row = (await self._load()).get(key)
            if row is None or row.fingerprint != fingerprint or row.status != "dispatched":
                return False
            row.status = "completed"
            row.result = result
            await self._save()
            return True
