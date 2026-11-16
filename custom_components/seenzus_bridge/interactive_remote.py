"""ADR0153 foreground remote admission. Volatile, bounded, never replayed.

No World authorization is inferred here. Cloud checks World grants per admission;
this module checks only current HA grants, reviewed integration identity and time.
"""
from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from homeassistant import loader
from homeassistant.helpers import entity_registry as er

from .device_access import DeviceAccess

PATH = "/api/seenzus/interactive-remote"
PRESS_MS, SESSION_MS, DEDUPE_MS = 1500, 30000, 60000
MAX_WAITING, MAX_DEVICES, MAX_SESSIONS, MAX_RECORDS, MAX_RESULTS = 6, 64, 128, 256, 64
PROFILES = {
    "tv_remote": ("1.0.3", "remote:tv_remote:3", {
        "up": "up", "down": "down", "left": "left", "right": "right", "ok": "ok",
        "home": "home", "back": "back", "menu": "menu", "volumeUp": "volup",
        "volumeDown": "voldown", "mute": "mute", "playPause": "playpause",
    }),
    "dangbei_local": ("0.4.3", "remote:dangbei_local:2", {
        "up": "up", "down": "down", "left": "left", "right": "right", "ok": "ok",
        "home": "home", "back": "back", "menu": "menu", "volumeUp": "volume_up", "volumeDown": "volume_down",
    }),
}


def _ms() -> int:
    return int(time.time() * 1000)


def _token(value: Any) -> bool:
    return isinstance(value, str) and 16 <= len(value) <= 128 and all(c.isascii() and (c.isalnum() or c in "_-") for c in value)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class RemoteDenied(Exception):
    """An explicit, non-retryable rejection of this sequence."""


@dataclass
class _Fence:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


@asynccontextmanager
async def _physical_fence(hass: Any, device_id: str):
    # Shared across bridge entries/reloads. An old in-flight HA call must finish
    # before a new coordinator may execute on the same physical HA Device.
    fences = hass.data.setdefault("seenzus_interactive_remote_fences_v1", {})
    fence = fences.get(device_id)
    if fence is None:
        if len(fences) >= MAX_DEVICES:
            raise RemoteDenied("device_capacity")
        fence = fences[device_id] = _Fence()
    if fence.users >= MAX_WAITING + 1:
        raise RemoteDenied("device_capacity")
    fence.users += 1
    try:
        async with fence.lock:
            yield
    finally:
        fence.users -= 1
        if fence.users == 0:
            fences.pop(device_id, None)


class InteractiveRemotes:
    def __init__(self, hass: Any, conf: Any, dispatch: Any, emit: Any, create_task: Any) -> None:
        self.hass, self.conf, self.dispatch, self.emit, self.create_task = hass, conf, dispatch, emit, create_task
        self.epoch = secrets.token_urlsafe(24)
        self.client: Any = None
        self.sessions: dict[str, dict] = {}
        self.records: dict[str, dict] = {}
        self.queues: dict[str, deque] = {}
        self.workers: dict[str, asyncio.Task] = {}
        self.results: set[asyncio.Task] = set()
        self.sweep_timer: asyncio.TimerHandle | None = None

    def declaration(self) -> dict:
        return {"version": 1, "profiles": [profile[1] for profile in PROFILES.values()],
                "authorized": not DeviceAccess(self.hass, self.conf()).restricted}

    def reset(self, client: Any = None) -> None:
        # Never cancel running HA calls. Their shared physical fence survives reload.
        self.epoch = secrets.token_urlsafe(24)
        self.client = client
        for session in self.sessions.values():
            self._invalidate(session, "connection_changed")
        self.sessions.clear()
        self.records.clear()
        self.queues.clear()
        if self.sweep_timer:
            self.sweep_timer.cancel()
            self.sweep_timer = None

    def _background_emit(self, item: dict) -> None:
        if item["client"] is not self.client or len(self.results) >= MAX_RESULTS:
            return
        async def send():
            try:
                async with asyncio.timeout(2):
                    await self.emit(item["client"], item["msgId"], item["receipt"],
                                    item["session"]["entityId"] if item["receipt"]["phase"] == "executed" else None)
            except Exception:  # Delivery uncertainty is represented by bounded cloud observation.
                pass
        task = self.create_task(send())
        self.results.add(task)
        task.add_done_callback(self.results.discard)

    def _invalidate(self, session: dict, reason: str) -> None:
        session["valid"] = False
        queue = self.queues.get(session["physical"], ())
        for item in tuple(queue):
            if item["session"] is session and not item.get("running"):
                queue.remove(item)
                item["receipt"].update(phase="rejected", reason=reason)
                self._background_emit(item)

    def _sweep(self) -> None:
        now = time.monotonic()
        for key, session in tuple(self.sessions.items()):
            if now >= session["monoExpiry"]:
                self._invalidate(session, "session_expired")
            if now >= session["retainUntil"]:
                self.sessions.pop(key, None)
        for key, item in tuple(self.records.items()):
            if item["receipt"]["phase"] == "admitted" and not item.get("running") and now >= item["deadline"]:
                self._invalidate(item["session"], "press_expired")
            if now >= item["retainUntil"] and not item.get("running"):
                self.records.pop(key, None)

    def _arm_sweep(self) -> None:
        if self.sweep_timer is not None:
            return
        def tick():
            self.sweep_timer = None
            self._sweep()
            if self.client is not None and (self.sessions or self.records):
                self._arm_sweep()
        self.sweep_timer = asyncio.get_running_loop().call_later(0.25, tick)

    def _identity(self, body: dict) -> tuple[Any, str]:
        entity_id = body.get("entityId")
        if not isinstance(entity_id, str) or not entity_id.startswith("remote."):
            raise RemoteDenied("invalid_remote")
        entry = er.async_get(self.hass).async_get(entity_id)
        platform = getattr(entry, "platform", None)
        config = self.hass.config_entries.async_get_entry(getattr(entry, "config_entry_id", None)) if entry else None
        if (not entry or not getattr(entry, "id", None) or not getattr(entry, "device_id", None)
                or platform not in PROFILES or getattr(config, "domain", None) != platform
                or getattr(entry, "disabled_by", None) or getattr(entry, "hidden_by", None)
                or body.get("capabilityVersion") != PROFILES[platform][1]):
            raise RemoteDenied("profile_changed")
        access = DeviceAccess(self.hass, self.conf())
        # Do not add remote/button to SAFE_SERVICES or expand a selected grant.
        if access.restricted or not access.allows(entity_id):
            raise RemoteDenied("source_authorization")
        state = self.hass.states.get(entity_id)
        if state is None or state.state in {"unknown", "unavailable"} or platform == "dangbei_local" and state.state != "on":
            raise RemoteDenied("remote_unavailable")
        return entry, access.fingerprint()

    def _owner(self, body: dict) -> dict:
        """Session ownership only: cleanup never requires a currently usable HA entity."""
        handle = body.get("session")
        session = self.sessions.get(handle.get("sessionId")) if isinstance(handle, dict) else None
        if (not session or handle != session["handle"] or session["handle"]["epoch"] != self.epoch
                or any(body.get(key) != session[key] for key in ("principal", "deviceId", "entityId", "capabilityVersion"))):
            raise RemoteDenied("session_changed")
        return session

    def _bound(self, body: dict) -> dict:
        session = self._owner(body)
        if (not session["valid"] or time.monotonic() >= session["monoExpiry"]
                or abs(_ms() - (session["wallStart"] + (time.monotonic() - session["monoStart"]) * 1000)) > 100):
            self._invalidate(session, "session_expired")
            raise RemoteDenied("session_expired")
        entry, fingerprint = self._identity(body)
        if entry.device_id != session["physical"] or entry.id != session["registryId"] or fingerprint != session["access"]:
            self._invalidate(session, "source_authorization_changed")
            raise RemoteDenied("source_authorization_changed")
        return session

    def _command(self, session: dict, action: str) -> tuple[str, dict]:
        commands = PROFILES[session["platform"]][2]
        if action in ("powerOn", "powerOff"):
            service = "remote.turn_on" if action == "powerOn" else "remote.turn_off"
            data = {}
        elif action in commands:
            service, data = "remote.send_command", {"command": commands[action]}
        else:
            raise RemoteDenied("unknown_action")
        domain, name = service.split(".")
        if not self.hass.services.has_service(domain, name):
            raise RemoteDenied("service_unavailable")
        return service, data

    async def handle(self, body: dict, client: Any, msg_id: str) -> None:
        if client is not self.client:
            raise RemoteDenied("connection_changed")
        self._sweep()
        kind = body.get("kind")
        if kind == "open":
            entry, access = self._identity(body)
            if (not _token(body.get("nonce")) or not isinstance(body.get("principal"), str) or len(body["principal"]) != 64
                    or not isinstance(body.get("deviceId"), str) or not _integer(body.get("sentAt"))
                    or not -100 <= _ms() - body["sentAt"] <= 600):
                raise RemoteDenied("clock_unverified")
            integration = await loader.async_get_integration(self.hass, entry.platform)
            # Version pins are evidence, not an assumption about every installed copy.
            if str(integration.version) != PROFILES[entry.platform][0]:
                raise RemoteDenied("integration_version_unknown")
            if client is not self.client:
                raise RemoteDenied("connection_changed")
            entry, access = self._identity(body)
            if len(self.sessions) >= MAX_SESSIONS:
                raise RemoteDenied("session_capacity")
            for old in self.sessions.values():
                if old["physical"] == entry.device_id:
                    self._invalidate(old, "session_replaced")
            now, mono = _ms(), time.monotonic()
            handle = {"sessionId": secrets.token_urlsafe(24), "generation": secrets.token_urlsafe(24),
                      "epoch": self.epoch, "expiresAt": now + SESSION_MS}
            session = {key: body[key] for key in ("principal", "deviceId", "entityId", "capabilityVersion")}
            session.update(handle=handle, valid=True, next=1, physical=entry.device_id, registryId=entry.id,
                           platform=entry.platform, access=access, monoExpiry=mono + SESSION_MS / 1000,
                           retainUntil=mono + DEDUPE_MS / 1000, wallStart=now, monoStart=mono)
            self.sessions[handle["sessionId"]] = session
            self._arm_sweep()
            await self.emit(client, msg_id, {"version": 1, **handle, "nonce": body["nonce"],
                                           "sourceNow": now, "capabilityVersion": body["capabilityVersion"]})
            return
        try:
            # Cleanup and receipt reads have no new HA side effect. Their stored
            # owner binding survives temporary entity unavailability/revocation.
            session = self._owner(body) if kind in {"close", "status"} else self._bound(body)
        except RemoteDenied as error:
            if kind == "press" and isinstance(body.get("session"), dict):
                await self.emit(client, msg_id, {"version": 1, **body["session"], "pressId": body.get("pressId"),
                                               "sequence": body.get("sequence"), "phase": "rejected", "reason": str(error)})
                return
            raise
        if kind == "close":
            self._invalidate(session, "session_closed")
            await self.emit(client, msg_id, {"closed": True})
            return
        press_id = body.get("pressId")
        if not _token(press_id):
            raise RemoteDenied("invalid_press")
        previous = self.records.get(press_id)
        if kind == "status":
            if not previous or previous["session"] is not session or previous["body"]["sequence"] != body.get("sequence"):
                raise RemoteDenied("unknown_press")
            await self.emit(client, msg_id, previous["receipt"])
            return
        if kind != "press":
            raise RemoteDenied("unknown_request")
        sequence = body.get("sequence")
        receipt = {"version": 1, **session["handle"], "pressId": press_id, "sequence": sequence, "phase": "rejected"}
        try:
            if previous:
                if previous["session"] is not session or previous["body"]["sequence"] != sequence or previous["body"]["action"] != body.get("action"):
                    raise RemoteDenied("duplicate_conflict")
                await self.emit(client, msg_id, {**receipt, "phase": "admitted"})
                if previous["receipt"]["phase"] != "admitted":
                    await self.emit(client, msg_id, previous["receipt"])
                return
            if not _integer(sequence) or sequence != session["next"] or sequence > 256:
                raise RemoteDenied("sequence_gap")
            now = _ms()
            issued, expiry = body.get("issuedAt"), body.get("expiresAt")
            if (not _integer(issued) or not _integer(expiry) or issued > now + 100
                    or not 0 < expiry - issued <= PRESS_MS or now + 100 >= expiry):
                raise RemoteDenied("press_expired")
            if not isinstance(body.get("action"), str):
                raise RemoteDenied("unknown_action")
            service, data = self._command(session, body["action"])
            if body.get("service") != service or (body.get("data") or {}) != data:
                raise RemoteDenied("command_mismatch")
            queue = self.queues.setdefault(session["physical"], deque())
            if body["action"] == "powerOff":
                for item in tuple(queue):
                    self._invalidate(item["session"], "power_off")
                # A power-off request is a new explicit sequence; browser normally opens a new session.
                if not session["valid"]:
                    raise RemoteDenied("new_session_required")
            if len(queue) >= MAX_WAITING or len(self.records) >= MAX_RECORDS or len(self.workers) >= MAX_DEVICES and session["physical"] not in self.workers:
                raise RemoteDenied("remote_capacity")
            item = {"session": session, "body": dict(body), "client": client, "msgId": msg_id,
                    "receipt": {**receipt, "phase": "admitted"}, "deadline": time.monotonic() + (expiry - now - 100) / 1000,
                    "retainUntil": time.monotonic() + DEDUPE_MS / 1000, "ready": False}
            self.records[press_id] = item
            queue.append(item)
            session["next"] += 1
            try:
                await self.emit(client, msg_id, item["receipt"])
            finally:
                item["ready"] = True
                self._start_worker(session["physical"])
        except RemoteDenied as error:
            self._invalidate(session, str(error))
            await self.emit(client, msg_id, {**receipt, "reason": str(error)})

    def _start_worker(self, physical: str) -> None:
        if physical in self.workers:
            return
        task = self.create_task(self._run(physical))
        self.workers[physical] = task
        def done(_task):
            self.workers.pop(physical, None)
            if self.queues.get(physical) and self.queues[physical][0]["ready"]:
                self._start_worker(physical)
        task.add_done_callback(done)

    async def _run(self, physical: str) -> None:
        queue = self.queues.get(physical)
        while queue:
            if not queue[0]["ready"]:
                return
            item = queue.popleft()
            try:
                async with _physical_fence(self.hass, physical):
                    self._bound(item["body"])
                    if time.monotonic() >= item["deadline"] or _ms() + 100 >= item["body"]["expiresAt"]:
                        raise RemoteDenied("press_expired")
                    service, data = self._command(item["session"], item["body"]["action"])
                    item["running"] = True
                    task = self.create_task(self.dispatch(service, {"entity_id": item["session"]["entityId"], **data}))
                    try:
                        result = await asyncio.shield(task)
                    except asyncio.CancelledError:
                        await task
                        raise
                    item["receipt"].update(phase="executed" if result.status < 400 else "rejected")
                    if result.status >= 400:
                        self._invalidate(item["session"], "execution_rejected")
            except asyncio.CancelledError:
                item["receipt"].update(phase="unknown", reason="execution_interrupted")
                self._invalidate(item["session"], "execution_interrupted")
                raise
            except Exception as error:
                item["receipt"].update(phase="rejected" if isinstance(error, RemoteDenied) else "unknown",
                                       reason=str(error) if isinstance(error, RemoteDenied) else "execution_unknown")
                self._invalidate(item["session"], "sequence_failed")
            finally:
                item["running"] = False
                self._background_emit(item)
        if self.queues.get(physical) is queue:
            self.queues.pop(physical, None)

    async def dispatch_legacy(self, method: str, path: str, body: dict | None, run: Any):
        """Existing power/other explicit targets cannot bypass a running remote action.

        Registry Device identity provides serialization only; it asserts no TV switch equivalence.
        """
        raw = (body or {}).get("entity_id")
        targets = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
        registry = er.async_get(self.hass)
        physical = {getattr(registry.async_get(entity), "device_id", None) for entity in targets if isinstance(entity, str)}
        governed = sorted(device for device in physical if device and any(
            entry.device_id == device and entry.domain == "remote" and entry.platform in PROFILES
            for entry in registry.entities.values())) if method.upper() == "POST" and path.startswith("/api/services/") else []
        if not governed:
            return await run()
        if path == "/api/services/remote/turn_off":
            for session in self.sessions.values():
                if session["physical"] in governed:
                    self._invalidate(session, "power_off")
        async def locked(index=0):
            if index < len(governed):
                async with _physical_fence(self.hass, governed[index]):
                    return await locked(index + 1)
            task = self.create_task(run())
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Cancellation of an RPC observer must not release an active HA execution fence.
                await task
                raise
        return await locked()
