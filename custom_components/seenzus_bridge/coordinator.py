"""seenzus Bridge MQTT coordinator: connection lifecycle, command dispatch, state mirror, presence.

Owns the long-lived MQTT loop (connect, subscribe, retry with backoff),
dispatches v2 commands to the HA HTTP-equivalent dispatcher, mirrors HA state
changes and the device catalog to MQTT, and publishes retained presence with a
periodic heartbeat.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.core import CoreState, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .bridge_protocol import (
    BridgeTopics,
    build_bridge_id,
    build_topics,
    retained_topics_to_clear_on_reload,
)
from .catalog import build_device_catalog_payload, utc_now_iso
from .const import (
    BRIDGE_VERSION,
    CONFIG_SOURCE_MANUAL,
    CONFIG_SOURCE_WEB_PAIR,
    CONF_BRIDGE_ID,
    CONF_CONFIG_SOURCE,
    CONF_ALLOW_DANGEROUS_SERVICES,
    CONF_ENABLE_STATE_EVENTS,
    CONF_ENABLE_TEMPLATE_API,
    CONF_EXPOSE_FULL_CONFIG,
    CONF_MQTT_HOST,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    CONF_PAIRING_API_BASE,
    CONF_PAIRING_BOUND_AT,
    CONF_PAIRING_MODE,
    CONF_PAIRING_SESSION_ID,
    CONF_SOURCE_ID,
    CONF_SOURCE_NAME,
    CONF_SOURCE_TYPE,
    CONF_TOPIC_ROOT,
    DEFAULT_ALLOW_DANGEROUS_SERVICES,
    DEFAULT_ENABLE_STATE_EVENTS,
    DEFAULT_ENABLE_TEMPLATE_API,
    DEFAULT_EXPOSE_FULL_CONFIG,
    DEFAULT_MQTT_PORT,
    DEFAULT_TOPIC_ROOT,
    PAIRING_MODE_MANUAL,
    PAIRING_MODE_SEAMLESS,
    PAIRING_STATUS_BOUND,
    PAIRING_STATUS_BRIDGE_READY,
    PAIRING_STATUS_BRIDGE_STARTING,
    PAIRING_STATUS_IDLE,
    PAIRING_STATUS_MQTT_AUTH_FAILED,
    PAIRING_STATUS_PAIRED,
    PAIRING_STATUS_WAITING_EXTERNAL_AUTH,
    normalize_pairing_mode,
)
from .entity_filters import looks_like_internal_bridge_entity_id, name_has_model_marker
from .ha_dispatcher import DispatchPolicy, dispatch

_LOGGER = logging.getLogger(__name__)

PRESENCE_HEARTBEAT_INTERVAL_SECONDS = 30


class BridgeCoordinator:
    """Manage MQTT bridge runtime and HA dispatch."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry

        self.status = "starting"
        self.mqtt_connected = False
        self.req_count = 0
        self.err_count = 0
        self.last_req: datetime | None = None
        self.last_error: str | None = None
        self.result_count = 0
        self.state_push_count = 0
        self.pairing_mode = PAIRING_MODE_MANUAL
        self.pairing_status = PAIRING_STATUS_IDLE
        self.pairing_session_id: str | None = None
        # pairing_expires_at / pairing_verification_code / pairing_last_diagnostic
        # are never written by production code, but sensor.py
        # BridgePairingStateSensor exposes them as pinned sensor attributes —
        # kept for attribute-shape stability, do not remove as dead code.
        self.pairing_expires_at: str | None = None
        self.pairing_verification_code: str | None = None
        self.pairing_last_error: str | None = None
        self.pairing_bound_at: str | None = None
        self.pairing_last_step: str | None = None
        self.pairing_last_api_base: str | None = None
        self.pairing_last_diagnostic: str | None = None
        self.config_source = CONFIG_SOURCE_MANUAL
        self.source_id: str | None = None
        self.source_type: str | None = None
        self.source_name: str | None = None

        self._listeners: list[Callable[[], None]] = []
        self._task: asyncio.Task | None = None
        self._state_unsub = None
        self._ha_started_unsub = None
        self._mqtt_client = None
        self._aiomqtt = None
        self._topics: BridgeTopics | None = None
        self._command_prefix = ""
        self._skip_offline_presence = False
        self._ha_started_event = asyncio.Event()
        self._initial_snapshot_done = False
        self._pending_state_events: dict[str, Event] = {}
        self._state_worker_task: asyncio.Task | None = None
        self._presence_heartbeat_task: asyncio.Task | None = None

    def register_update_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def _fire(self) -> None:
        for cb in self._listeners:
            cb()

    def _conf(self) -> dict[str, Any]:
        return {**self._entry.data, **self._entry.options}

    def _set_pairing_step(self, step: str, *, api_base: str | None = None) -> None:
        self.pairing_last_step = step
        if api_base:
            self.pairing_last_api_base = api_base

    def _resolve_pairing_mode(self) -> str:
        return normalize_pairing_mode(self._conf().get(CONF_PAIRING_MODE, ""))

    def _resolve_config_source(self) -> str:
        conf = self._conf()
        source = str(conf.get(CONF_CONFIG_SOURCE, "")).strip()
        if source:
            return source
        if self._resolve_pairing_mode() == PAIRING_MODE_SEAMLESS:
            return CONFIG_SOURCE_WEB_PAIR
        return CONFIG_SOURCE_MANUAL

    def _sync_source_metadata(self) -> None:
        conf = self._conf()
        self.source_id = str(conf.get(CONF_SOURCE_ID, "")).strip() or None
        self.source_type = str(conf.get(CONF_SOURCE_TYPE, "")).strip() or None
        self.source_name = str(conf.get(CONF_SOURCE_NAME, "")).strip() or None

    async def _async_import_aiomqtt(self):
        async_add_executor_job = getattr(self.hass, "async_add_executor_job", None)
        if async_add_executor_job is None:
            return importlib.import_module("aiomqtt")
        return await async_add_executor_job(importlib.import_module, "aiomqtt")

    def _mark_mqtt_error(self, message: str) -> None:
        self.status = "error"
        self.mqtt_connected = False
        self.last_error = message
        if self._resolve_pairing_mode() == PAIRING_MODE_SEAMLESS:
            lowered = message.lower()
            if "not authorized" in lowered or "code:135" in lowered:
                self.pairing_status = PAIRING_STATUS_MQTT_AUTH_FAILED
                self.pairing_last_error = message

    def _dispatch_policy(self) -> DispatchPolicy:
        conf = self._conf()
        return DispatchPolicy(
            allow_template=bool(conf.get(CONF_ENABLE_TEMPLATE_API, DEFAULT_ENABLE_TEMPLATE_API)),
            allow_dangerous_services=bool(
                conf.get(CONF_ALLOW_DANGEROUS_SERVICES, DEFAULT_ALLOW_DANGEROUS_SERVICES)
            ),
            expose_full_config=bool(conf.get(CONF_EXPOSE_FULL_CONFIG, DEFAULT_EXPOSE_FULL_CONFIG)),
        )

    def _resolve_topics(self) -> BridgeTopics:
        conf = self._conf()
        bridge_id = build_bridge_id(str(conf.get(CONF_BRIDGE_ID, "")), self._entry.entry_id)
        topic_root = str(conf.get(CONF_TOPIC_ROOT, DEFAULT_TOPIC_ROOT))
        return build_topics(topic_root, bridge_id)

    @callback
    def _is_own_entity(self, entity_id: str) -> bool:
        if looks_like_internal_bridge_entity_id(entity_id):
            return True

        registry = er.async_get(self.hass)
        if any(
            entry.entity_id == entity_id
            for entry in registry.entities.get_entries_for_config_entry_id(self._entry.entry_id)
        ):
            return True

        registry_entry = registry.async_get(entity_id)
        return bool(registry_entry and registry_entry.config_entry_id == self._entry.entry_id)

    @callback
    def _is_model_marked_entity(self, state: Any) -> bool:
        """True for entities whose friendly name carries a model marker ('*')."""
        attributes = getattr(state, "attributes", None) or {}
        return name_has_model_marker(attributes.get("friendly_name"))

    async def async_start(self) -> None:
        self._topics = self._resolve_topics()
        self._command_prefix = self._topics.command_sub[:-2]
        self.pairing_mode = self._resolve_pairing_mode()
        self.config_source = self._resolve_config_source()
        self._sync_source_metadata()
        if self.config_source == CONFIG_SOURCE_WEB_PAIR and self.pairing_status == PAIRING_STATUS_IDLE:
            self.pairing_status = PAIRING_STATUS_PAIRED

        self._aiomqtt = await self._async_import_aiomqtt()

        conf = self._conf()
        events_enabled = bool(conf.get(CONF_ENABLE_STATE_EVENTS, DEFAULT_ENABLE_STATE_EVENTS))
        if events_enabled:
            _LOGGER.info("State event publishing enabled (deferred until HA started)")
        else:
            _LOGGER.info("State event publishing disabled")

        if self.hass.state == CoreState.running:
            self._ha_started_event.set()
            if events_enabled:
                self._subscribe_state_events()
        else:
            self._ha_started_unsub = self.hass.bus.async_listen_once(
                EVENT_HOMEASSISTANT_STARTED, self._on_ha_started
            )

        self._task = self.hass.async_create_task(self._mqtt_loop())

    @callback
    def _on_ha_started(self, _event: Event) -> None:
        self._ha_started_event.set()
        self._ha_started_unsub = None
        if bool(self._conf().get(CONF_ENABLE_STATE_EVENTS, DEFAULT_ENABLE_STATE_EVENTS)):
            self._subscribe_state_events()

    @callback
    def _subscribe_state_events(self) -> None:
        if self._state_unsub is None:
            self._state_unsub = self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._on_state_changed)

    async def async_stop(self) -> None:
        if self._ha_started_unsub is not None:
            self._ha_started_unsub()
            self._ha_started_unsub = None

        if self._state_unsub is not None:
            self._state_unsub()
            self._state_unsub = None

        if (
            self._mqtt_client is not None
            and self._topics is not None
            and not self._skip_offline_presence
        ):
            await self._publish_presence("offline")

        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

        if self._state_worker_task and not self._state_worker_task.done():
            self._state_worker_task.cancel()
            try:
                await self._state_worker_task
            except asyncio.CancelledError:
                pass

        await self._stop_presence_heartbeat()

        self._task = None
        self._state_worker_task = None
        self._pending_state_events.clear()
        self._mqtt_client = None
        self._skip_offline_presence = False
        self.status = "stopped"
        self.mqtt_connected = False
        _LOGGER.info("seenzus Bridge stopped")
        self._fire()

    async def async_prepare_for_reload(self) -> None:
        if self._topics is None:
            return

        topics_to_clear = retained_topics_to_clear_on_reload(
            self._topics,
            self._resolve_topics(),
        )
        if not topics_to_clear:
            return

        self._skip_offline_presence = True
        if self._mqtt_client is None:
            return

        for topic in topics_to_clear:
            await self._mqtt_client.publish(topic, "", qos=1, retain=True)

    async def _mqtt_loop(self) -> None:
        """Retry shell: backoff/cleanup around each _connect_and_serve cycle."""
        aiomqtt = self._aiomqtt
        if aiomqtt is None:
            aiomqtt = await self._async_import_aiomqtt()
            self._aiomqtt = aiomqtt

        client_id = f"seenzus-bridge-{self._entry.entry_id[:8]}"

        while True:
            try:
                if not await self._connect_and_serve(aiomqtt, client_id):
                    _LOGGER.warning("MQTT host missing, retry in 10s")
                    await asyncio.sleep(10)
                    continue
            except aiomqtt.MqttError as err:
                await self._stop_presence_heartbeat()
                self._mark_mqtt_error(str(err))
                self._fire()
                _LOGGER.error("MQTT disconnected: %s, retry in 5s", err)
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                await self._stop_presence_heartbeat()
                self._mark_mqtt_error(str(err))
                self._fire()
                _LOGGER.exception("MQTT loop error: %s, retry in 10s", err)
                await asyncio.sleep(10)
            finally:
                await self._stop_presence_heartbeat()
                self._mqtt_client = None

    async def _connect_and_serve(self, aiomqtt: Any, client_id: str) -> bool:
        """Resolve config, connect, announce, snapshot once, pump messages.

        Returns False when the MQTT host is not configured yet so the retry
        shell can back off; connection errors propagate to the shell.
        """
        conf = self._conf()
        self.pairing_mode = self._resolve_pairing_mode()
        self.config_source = self._resolve_config_source()
        self._sync_source_metadata()
        host = str(conf.get(CONF_MQTT_HOST, "")).strip()
        if not host:
            self._mark_mqtt_error("mqtt_host_missing")
            if self.pairing_mode == PAIRING_MODE_SEAMLESS:
                self._set_pairing_step("waiting_external_auth")
                self.pairing_status = PAIRING_STATUS_WAITING_EXTERNAL_AUTH
                self.pairing_last_error = "mqtt_host_missing"
            self._fire()
            return False

        port = int(conf.get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT))
        username = str(conf.get(CONF_MQTT_USERNAME, "")).strip() or None
        password = str(conf.get(CONF_MQTT_PASSWORD, "")).strip() or None
        if self.pairing_mode == PAIRING_MODE_SEAMLESS:
            self._set_pairing_step("bridge_starting")
            if self.pairing_status in {PAIRING_STATUS_IDLE, PAIRING_STATUS_WAITING_EXTERNAL_AUTH}:
                self.pairing_status = PAIRING_STATUS_BRIDGE_STARTING
                self._fire()

        async with aiomqtt.Client(
            hostname=host,
            port=port,
            username=username,
            password=password,
            identifier=client_id,
        ) as client:
            self._mqtt_client = client

            if self._topics is None:
                self._topics = self._resolve_topics()
                self._command_prefix = self._topics.command_sub[:-2]

            await client.subscribe(self._topics.command_sub, qos=1)

            await self._publish_presence("online")
            self._start_presence_heartbeat()
            # Catalog + state snapshot both need HA fully started (entity registry
            # populated); defer on the started event (no sleep-poll).
            await self._ha_started_event.wait()
            # Full STATE snapshot: once per coordinator lifetime. State is a non-retained
            # on-change stream, so re-flooding it on every reconnect would risk a publish
            # storm on a flaky link (the loop reconnects every few seconds) — live values
            # recover via change events instead.
            if not self._initial_snapshot_done:
                await self._publish_all_states(client, source="startup_snapshot")
                self._initial_snapshot_done = True
                catalog_source = "startup_snapshot"
            else:
                catalog_source = "reconnect"
            # Device CATALOG: re-assert on EVERY (re)connect, mirroring presence. The
            # catalog is the durable topology truth every consumer depends on, yet the
            # broker's retained store can be wiped on broker restart — publishing it once
            # per lifetime left the backend with an empty catalog after any broker bounce
            # (live reads + control all dead) until a full plugin reload. Announcing it on
            # each connect lets consumers self-heal at reconnect time.
            await self._publish_device_catalog(client, source=catalog_source)
            await self._try_pairing()

            self.status = "active"
            self.mqtt_connected = True
            if self.pairing_mode == PAIRING_MODE_SEAMLESS and self.pairing_status != PAIRING_STATUS_BOUND:
                self._set_pairing_step("bridge_ready")
                self.pairing_status = PAIRING_STATUS_BRIDGE_READY
                self.pairing_last_error = None
            self._fire()
            _LOGGER.info(
                "MQTT connected %s:%s, v2 command topic: %s",
                host,
                port,
                self._topics.command_sub,
            )

            async for message in client.messages:
                topic = str(message.topic)
                raw = message.payload.decode(errors="replace")
                self.hass.async_create_task(self._handle_message(topic, raw, client))

        return True

    async def _try_pairing(self) -> None:
        self.pairing_mode = self._resolve_pairing_mode()
        self.config_source = self._resolve_config_source()
        self._sync_source_metadata()
        if self.pairing_mode != PAIRING_MODE_SEAMLESS:
            return

        conf = self._conf()
        api_base = str(conf.get(CONF_PAIRING_API_BASE, "")).strip()
        if self.config_source == CONFIG_SOURCE_WEB_PAIR:
            self._set_pairing_step("web_pair_ready", api_base=api_base or None)
            self.pairing_session_id = str(conf.get(CONF_PAIRING_SESSION_ID, "")).strip() or self.pairing_session_id
            self.pairing_bound_at = str(conf.get(CONF_PAIRING_BOUND_AT, "")).strip() or self.pairing_bound_at
            self.pairing_last_error = None
            self.pairing_status = PAIRING_STATUS_BOUND
        else:
            self._set_pairing_step("waiting_quick_pair", api_base=api_base or None)
            self.pairing_status = PAIRING_STATUS_WAITING_EXTERNAL_AUTH
        self._fire()

    async def _handle_message(self, topic: str, raw: str, client: Any) -> None:
        if self._topics and topic.startswith(f"{self._command_prefix}/"):
            msg_id = topic.removeprefix(f"{self._command_prefix}/").strip()
            _LOGGER.info("Received MQTT command topic=%s msgId=%s", topic, msg_id)
            await self._handle_v2_command(msg_id, raw, client)
        else:
            _LOGGER.debug("Ignore unsupported topic: %s", topic)

    async def _handle_v2_command(self, msg_id: str, raw: str, client: Any) -> None:
        if self._topics is None:
            return

        try:
            req = json.loads(raw) if raw else {}
            if not isinstance(req, dict):
                req = {}
        except json.JSONDecodeError:
            self.err_count += 1
            self.last_error = f"invalid_json:{raw[:120]}"
            self._fire()
            await self._publish_result(client, msg_id, success=False, status=400, error="invalid_json")
            return

        method = str(req.get("method", "GET"))
        path = str(req.get("path", "/"))
        body = req.get("body")
        body = body if isinstance(body, dict) else None
        effective_msg_id = str(req.get("msgId", req.get("correlationId", msg_id))) or msg_id

        self.req_count += 1
        self.last_req = datetime.now(timezone.utc)
        self._fire()

        try:
            if method.upper() == "GET" and path.rstrip("/") in {
                "/api/seenzus/device-catalog",
                "/api/seenzus/devices",
            }:
                catalog_payload = self._build_device_catalog_payload(
                    source="command",
                    correlation_id=effective_msg_id,
                )
                await self._publish_result(
                    client,
                    effective_msg_id,
                    success=True,
                    status=200,
                    data=catalog_payload,
                )
                # Reuse the already-built payload for the retained catalog topic
                # instead of rebuilding (which would mint a fresh eventId/ts).
                # Acceptable side effect: result.data and the retained catalog now
                # share one eventId/ts.
                await self._publish_catalog_payload(
                    client,
                    catalog_payload,
                    source="command",
                )
                return

            result = await dispatch(self.hass, method, path, body, self._dispatch_policy())
            await self._publish_result(
                client,
                effective_msg_id,
                success=result.status < 400,
                status=result.status,
                data=result.data,
            )
            if method.upper() == "GET" and path.rstrip("/") == "/api/states":
                await self._publish_all_states(client, source="full_snapshot", correlation_id=effective_msg_id)
            else:
                await self._publish_states_for_entities(client, result.touched_entities, correlation_id=effective_msg_id)
        except Exception as err:  # noqa: BLE001
            self.err_count += 1
            self.last_error = f"[{effective_msg_id}] {err}"
            self._fire()
            _LOGGER.exception("[%s] Command handling error: %s", effective_msg_id, err)
            await self._publish_result(client, effective_msg_id, success=False, status=500, error=str(err))

    async def _publish_result(self, client: Any, msg_id: str, *, success: bool, status: int, data: Any = None, error: str | None = None) -> None:
        if self._topics is None:
            return
        payload: dict[str, Any] = {
            "msgId": msg_id,
            "bridgeId": self._topics.bridge_id,
            "success": success,
            "status": status,
            "finishedAt": utc_now_iso(),
        }
        if error:
            payload["error"] = error
        else:
            payload["data"] = data

        try:
            await client.publish(
                f"{self._topics.result_prefix}/{msg_id}",
                json.dumps(payload, default=str),
                qos=1,
            )
        except Exception as err:  # noqa: BLE001
            # Count the failure here (once) instead of letting it escape the
            # fire-and-forget command task as an unhandled exception.
            self.err_count += 1
            self.last_error = f"result_publish_failed:{err}"
            self._fire()
            _LOGGER.exception("[%s] Result publish failed: %s", msg_id, err)
            return
        self.result_count += 1
        self._fire()

    async def _publish_states_for_entities(self, client: Any, entity_ids: list[str], *, correlation_id: str | None = None) -> None:
        dedup = list(dict.fromkeys(entity_ids))
        for entity_id in dedup:
            await self._publish_state_for_entity(client, entity_id, source="command", correlation_id=correlation_id)

    async def _publish_all_states(self, client: Any, *, source: str, correlation_id: str | None = None) -> None:
        snapshot_qos = 0 if source in {"startup_snapshot", "full_snapshot"} else 1
        states = self.hass.states.async_all()
        published = 0
        for state in states:
            entity_id = getattr(state, "entity_id", "")
            if not entity_id or self._is_own_entity(entity_id):
                continue
            if self._is_model_marked_entity(state):
                continue
            await self._publish_state_for_entity(
                client, entity_id, source=source, correlation_id=correlation_id, qos=snapshot_qos
            )
            published += 1
            if published % 50 == 0:
                await asyncio.sleep(0)
        _LOGGER.info("Published full HA state snapshot: %s entities", published)

    async def _publish_device_catalog(self, client: Any, *, source: str, correlation_id: str | None = None) -> None:
        if self._topics is None:
            return
        payload = self._build_device_catalog_payload(source=source, correlation_id=correlation_id)
        await self._publish_catalog_payload(client, payload, source=source)

    async def _publish_catalog_payload(self, client: Any, payload: dict[str, Any], *, source: str) -> None:
        if self._topics is None:
            return
        catalog_qos = 0 if source == "startup_snapshot" else 1
        await client.publish(
            self._topics.catalog_topic,
            json.dumps(payload, default=str),
            qos=catalog_qos,
            retain=True,
        )
        _LOGGER.info(
            "Published HA device catalog snapshot: %s devices, %s entities",
            len(payload["devices"]),
            payload["entityCount"],
        )

    def _build_device_catalog_payload(self, *, source: str, correlation_id: str | None = None) -> dict[str, Any]:
        if self._topics is None:
            return {}
        return build_device_catalog_payload(
            self.hass,
            bridge_id=self._topics.bridge_id,
            source=source,
            correlation_id=correlation_id,
            is_own_entity=self._is_own_entity,
        )

    def _build_state_payload(
        self,
        entity_id: str,
        state_obj: Any,
        *,
        source: str,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "eventId": str(uuid.uuid4()),
            "bridgeId": self._topics.bridge_id,
            "entityId": entity_id,
            "state": state_obj.state,
            "attributes": dict(state_obj.attributes),
            "ts": utc_now_iso(),
            "source": source,
        }
        if correlation_id:
            payload["correlationMsgId"] = correlation_id
        return payload

    async def _publish_state_for_entity(
        self,
        client: Any,
        entity_id: str,
        *,
        source: str,
        correlation_id: str | None = None,
        qos: int = 1,
    ) -> None:
        if self._topics is None:
            return
        if self._is_own_entity(entity_id):
            return
        state = self.hass.states.get(entity_id)
        if state is None:
            return
        if self._is_model_marked_entity(state):
            return
        topic_entity = entity_id.replace("/", "_")
        payload = self._build_state_payload(
            entity_id, state, source=source, correlation_id=correlation_id
        )
        await client.publish(
            f"{self._topics.state_prefix}/{topic_entity}",
            json.dumps(payload, default=str),
            qos=qos,
        )
        self.state_push_count += 1
        self._fire()

    @callback
    def _on_state_changed(self, event: Event) -> None:
        conf = self._conf()
        if not bool(conf.get(CONF_ENABLE_STATE_EVENTS, DEFAULT_ENABLE_STATE_EVENTS)):
            return
        new_state = event.data.get("new_state")
        entity_id = getattr(new_state, "entity_id", None) if new_state is not None else None
        if not entity_id or self._is_own_entity(entity_id):
            return
        if self._is_model_marked_entity(new_state):
            return
        self._pending_state_events[entity_id] = event
        if self._state_worker_task is None or self._state_worker_task.done():
            self._state_worker_task = self.hass.async_create_task(self._state_worker())

    async def _state_worker(self) -> None:
        while self._pending_state_events:
            entity_id, event = next(iter(self._pending_state_events.items()))
            self._pending_state_events.pop(entity_id, None)
            try:
                await self._publish_state_from_event(event)
            except Exception as err:  # noqa: BLE001
                # Log only — _publish_state_from_event already counts a
                # publish failure as state_publish_failed (one failure, one label).
                _LOGGER.exception("State worker error for %s: %s", entity_id, err)

    async def _publish_state_from_event(self, event: Event) -> None:
        if self._mqtt_client is None or self._topics is None:
            return
        new_state = event.data.get("new_state")
        if new_state is None or not getattr(new_state, "entity_id", None):
            return
        if self._is_own_entity(new_state.entity_id):
            return
        if self._is_model_marked_entity(new_state):
            return
        payload = self._build_state_payload(
            new_state.entity_id, new_state, source="ha_state_changed"
        )
        topic_entity = new_state.entity_id.replace("/", "_")
        try:
            await self._mqtt_client.publish(
                f"{self._topics.state_prefix}/{topic_entity}",
                json.dumps(payload, default=str),
                qos=1,
            )
            self.state_push_count += 1
            self._fire()
        except Exception as err:  # noqa: BLE001
            self.err_count += 1
            self.last_error = f"state_publish_failed:{err}"
            self._fire()

    async def _publish_presence(self, status: str) -> None:
        if self._mqtt_client is None or self._topics is None:
            return
        self._sync_source_metadata()
        payload = {
            "bridgeId": self._topics.bridge_id,
            "status": status,
            "mqttConnected": self.mqtt_connected,
            "pairingStatus": self.pairing_status,
            "configSource": self.config_source,
            "sourceId": self.source_id,
            "sourceType": self.source_type,
            "sourceName": self.source_name,
            "pairingLastError": self.pairing_last_error,
            "pairingSessionId": self.pairing_session_id,
            "ts": utc_now_iso(),
            "requestCount": self.req_count,
            "errorCount": self.err_count,
            "lastError": self.last_error,
            "version": BRIDGE_VERSION,
        }
        try:
            await self._mqtt_client.publish(
                self._topics.presence_topic,
                json.dumps(payload, default=str),
                qos=1,
                retain=True,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Presence publish failed: %s", err)

    def _start_presence_heartbeat(self) -> None:
        if self._presence_heartbeat_task is not None and not self._presence_heartbeat_task.done():
            return
        self._presence_heartbeat_task = self.hass.async_create_task(self._presence_heartbeat())

    async def _stop_presence_heartbeat(self) -> None:
        if self._presence_heartbeat_task is None or self._presence_heartbeat_task.done():
            self._presence_heartbeat_task = None
            return
        self._presence_heartbeat_task.cancel()
        try:
            await self._presence_heartbeat_task
        except asyncio.CancelledError:
            pass
        finally:
            self._presence_heartbeat_task = None

    async def _presence_heartbeat(self) -> None:
        while True:
            await asyncio.sleep(PRESENCE_HEARTBEAT_INTERVAL_SECONDS)
            await self._publish_presence("online")
