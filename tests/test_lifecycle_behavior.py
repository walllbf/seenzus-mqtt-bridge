from __future__ import annotations

import asyncio
import inspect
import time
from types import MappingProxyType
from unittest.mock import AsyncMock, Mock

import pytest

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryError

import seenzus_bridge
from seenzus_bridge import BridgeCoordinator
from seenzus_bridge.bridge_protocol import build_topics
from seenzus_bridge.config_flow import SavanAIBridgeOptionsFlow
from tests.helpers import (
    AsyncFakeMQTTClient,
    FakeAiomqttModule,
    FakeConfigEntry,
    FakeHass,
    make_state_changed_event,
)


def _make_core_config_entry() -> ConfigEntry:
    """Create a real Core ConfigEntry across the supported HA test matrix."""
    values = {
        "created_at": None,
        "data": {},
        "disabled_by": None,
        "discovery_keys": MappingProxyType({}),
        "domain": "seenzus_bridge",
        "entry_id": "01kpcrmg59ph",
        "minor_version": 1,
        "modified_at": None,
        "options": {},
        "pref_disable_new_entities": None,
        "pref_disable_polling": None,
        "source": "user",
        "subentries_data": MappingProxyType({}),
        "title": "seenzus MQTT Bridge",
        "unique_id": None,
        "version": 1,
    }
    parameters = inspect.signature(ConfigEntry).parameters
    return ConfigEntry(**{key: value for key, value in values.items() if key in parameters})


@pytest.mark.asyncio
@pytest.mark.parametrize("already_loaded", [False, True])
@pytest.mark.parametrize("method", ["manual", "quick_pair"])
async def test_real_ha_options_save_reloads_once_after_tls_correction(
    tmp_path, monkeypatch, already_loaded, method
) -> None:
    """Core must save before reload, even when blocked setup installed no listener."""
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    entry = _make_core_config_entry()
    hass.config_entries._entries[entry.entry_id] = entry
    monkeypatch.setattr(hass.config_entries, "_async_schedule_save", Mock())
    hass.config_entries.async_update_entry(entry, data={
        "mqtt_host": "broker.example.com",
        "mqtt_scheme": "mqtts" if already_loaded else "mqtt",
        "mqtt_port": 8883 if already_loaded else 1883,
    })
    coordinator = Mock(
        async_start=AsyncMock(),
        async_prepare_for_reload=AsyncMock(),
    )
    monkeypatch.setattr(seenzus_bridge, "BridgeCoordinator", Mock(return_value=coordinator))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock())
    try:
        if already_loaded:
            await seenzus_bridge.async_setup_entry(hass, entry)
            assert len(entry.update_listeners) == 1
        else:
            with pytest.raises(ConfigEntryError):
                await seenzus_bridge.async_setup_entry(hass, entry)
            assert not entry.update_listeners
            coordinator.async_start.assert_not_awaited()

        async def reload_saved_entry(entry_id):
            assert entry_id == entry.entry_id
            assert entry.options["mqtt_scheme"] == "wss"
            assert entry.options["mqtt_port"] == 443
            if already_loaded:
                coordinator.async_prepare_for_reload.assert_awaited_once()
            else:
                # Exercise the setup guard again with the options Core just saved.
                assert await seenzus_bridge.async_setup_entry(hass, entry)
            return True

        reload_entry = AsyncMock(side_effect=reload_saved_entry)
        monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
        flow = SavanAIBridgeOptionsFlow(entry)
        flow.hass = hass
        flow.handler = entry.entry_id
        corrected = {
            "mqtt_host": "broker.example.com", "mqtt_scheme": "wss", "mqtt_port": 443,
        }
        if method == "manual":
            result = await flow.async_step_manual({"mqtt_settings": corrected})
        else:
            result = flow._finish_quick_pair(corrected)
        # Use Core's actual finish/save/notification machinery, not a flow stub.
        await hass.config_entries.options.async_finish_flow(flow, result)
        await hass.async_block_till_done()
        reload_entry.assert_awaited_once_with(entry.entry_id)
        coordinator.async_start.assert_awaited_once()
        assert entry.update_listeners == [seenzus_bridge._async_reload_entry]
        if not already_loaded:
            coordinator.async_prepare_for_reload.assert_not_awaited()

        # Saving identical options must not cause another reload.
        await hass.config_entries.options.async_finish_flow(flow, result)
        await hass.async_block_till_done()
        reload_entry.assert_awaited_once()
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_real_ha_task_wait_ignores_mqtt_and_heartbeat_background_loops(tmp_path) -> None:
    hass = HomeAssistant(str(tmp_path))
    entry = _make_core_config_entry()
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._aiomqtt = FakeAiomqttModule()

    await coordinator.async_start()
    coordinator._start_presence_heartbeat()

    await asyncio.wait_for(hass.async_block_till_done(), timeout=0.2)
    assert coordinator._task is not None and not coordinator._task.done()
    assert coordinator._presence_heartbeat_task is not None
    assert not coordinator._presence_heartbeat_task.done()

    await coordinator.async_stop()
    await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_start_owns_mqtt_loop_as_config_entry_background_work() -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(data={})
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._aiomqtt = FakeAiomqttModule()

    await coordinator.async_start()

    assert entry.background_task_names == ["seenzus MQTT runtime"]
    assert entry.foreground_task_names == []

    await coordinator.async_stop()


@pytest.mark.asyncio
async def test_mqtt_runtime_preserves_task_cancellation_semantics() -> None:
    coordinator = BridgeCoordinator(FakeHass(), FakeConfigEntry(data={}))
    coordinator._aiomqtt = FakeAiomqttModule()
    task = asyncio.create_task(coordinator._mqtt_loop())
    await asyncio.sleep(0)

    assert coordinator.last_error == "mqtt_host_missing"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()
    assert coordinator.last_error == "mqtt_host_missing"


@pytest.mark.asyncio
async def test_connected_heartbeat_is_config_entry_background_work() -> None:
    hass = FakeHass()
    entry = FakeConfigEntry()
    coordinator = BridgeCoordinator(hass, entry)

    coordinator._start_presence_heartbeat()
    await asyncio.sleep(0)

    assert entry.background_task_names == ["seenzus presence heartbeat"]
    assert entry.foreground_task_names == []

    await coordinator._stop_presence_heartbeat()


@pytest.mark.asyncio
async def test_finite_runtime_work_is_owned_as_config_entry_foreground_work(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry()
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._topics = build_topics("seenzus/v2", "ha-demo")

    monkeypatch.setattr(coordinator, "_is_own_entity", lambda _entity_id: False)
    monkeypatch.setattr(coordinator, "_is_model_marked_standalone_entity", lambda _state: False)
    coordinator._schedule_message("unsupported/topic", "{}", object())
    coordinator._on_state_changed(make_state_changed_event("light.demo"))
    await asyncio.sleep(0)

    # State changes may arrive while MQTT is still bootstrapping. They stay
    # coalesced and do not race the retained catalog/presence handshake.
    assert entry.foreground_task_names == ["seenzus command handler"]
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator.mqtt_connected = True
    coordinator._start_state_worker_if_ready()
    await asyncio.sleep(0)

    assert entry.foreground_task_names == [
        "seenzus command handler",
        "seenzus state publisher",
    ]

    await coordinator.async_stop()


def test_authentication_failure_notifies_only_while_ha_is_running(monkeypatch) -> None:
    notifications: list[dict] = []
    monkeypatch.setattr(
        persistent_notification,
        "async_create",
        lambda _hass, message, title=None, notification_id=None: notifications.append(
            {"message": message, "title": title, "notification_id": notification_id}
        ),
    )
    running = BridgeCoordinator(
        FakeHass(), FakeConfigEntry(data={"pairing_mode": "manual"})
    )
    stopping_hass = FakeHass()
    stopping_hass.state = CoreState.stopping
    stopping = BridgeCoordinator(
        stopping_hass, FakeConfigEntry(data={"pairing_mode": "seamless"})
    )

    running._mark_mqtt_error("[code:135] Not authorized")
    stopping._mark_mqtt_error("[code:135] Not authorized")

    assert notifications == [
        {
            "message": "MQTT authentication failed. Check or renew the seenzus pairing credentials.",
            "title": "seenzus MQTT Bridge authentication failed",
            "notification_id": f"seenzus_bridge_mqtt_auth_{running._entry.entry_id}",
        }
    ]


@pytest.mark.asyncio
async def test_shutdown_cleanup_returns_within_shutdown_budget_when_presence_never_returns(
    monkeypatch,
) -> None:
    class BlockingClient:
        async def publish(self, *_args, **_kwargs) -> None:
            await asyncio.Event().wait()

    hass = FakeHass()
    hass.state = CoreState.stopping
    coordinator = BridgeCoordinator(hass, FakeConfigEntry())
    coordinator._mqtt_client = BlockingClient()
    coordinator._topics = build_topics("seenzus/v2", "ha-demo")
    monkeypatch.setattr(seenzus_bridge.coordinator, "SHUTDOWN_CLEANUP_TIMEOUT_SECONDS", 0.02)

    started = time.monotonic()
    await coordinator.async_stop()
    elapsed = time.monotonic() - started

    assert elapsed < 0.2
    assert coordinator.status == "stopped"
    assert coordinator.last_cleanup_diagnostic is not None
    assert "shutdown" in coordinator.last_cleanup_diagnostic
    assert "0.02" in coordinator.last_cleanup_diagnostic


@pytest.mark.asyncio
async def test_reload_prepare_and_stop_share_one_total_cleanup_budget(monkeypatch) -> None:
    class BlockingClient:
        async def publish(self, *_args, **_kwargs) -> None:
            await asyncio.Event().wait()

    hass = FakeHass()
    entry = FakeConfigEntry(options={"bridge_id": "ha-new"})
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_client = BlockingClient()
    coordinator._topics = build_topics("seenzus/v2", "ha-old")
    monkeypatch.setattr(seenzus_bridge.coordinator, "RELOAD_CLEANUP_TIMEOUT_SECONDS", 0.05)

    started = time.monotonic()
    await coordinator.async_prepare_for_reload()
    coordinator._skip_offline_presence = False
    await coordinator.async_stop()
    elapsed = time.monotonic() - started

    assert elapsed < 0.08


@pytest.mark.asyncio
async def test_manual_reload_retained_cleanup_is_bounded_and_continues(monkeypatch) -> None:
    class BlockingClient:
        async def publish(self, *_args, **_kwargs) -> None:
            await asyncio.Event().wait()

    hass = FakeHass()
    entry = FakeConfigEntry(options={"bridge_id": "ha-new"})
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_client = BlockingClient()
    coordinator._topics = build_topics("seenzus/v2", "ha-old")
    hass.data["seenzus_bridge"] = {entry.entry_id: coordinator}
    monkeypatch.setattr(seenzus_bridge.coordinator, "RELOAD_CLEANUP_TIMEOUT_SECONDS", 0.02)

    await seenzus_bridge._async_reload_entry(hass, entry)

    assert hass.config_entries.reload_calls == [entry.entry_id]
    assert coordinator.last_cleanup_diagnostic is not None
    assert "retained MQTT cleanup" in coordinator.last_cleanup_diagnostic


@pytest.mark.asyncio
async def test_programming_error_ends_runtime_instead_of_retrying(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(data={"mqtt_host": "broker.example.com", "mqtt_scheme": "mqtts"})
    coordinator = BridgeCoordinator(hass, entry)
    fake_aiomqtt = FakeAiomqttModule(
        [{"connect_error": RuntimeError("broken dependency")}]
    )

    async def import_fake_aiomqtt():
        return fake_aiomqtt

    monkeypatch.setattr(coordinator, "_async_import_aiomqtt", import_fake_aiomqtt)
    await coordinator.async_start()
    assert coordinator._task is not None
    await asyncio.wait({coordinator._task}, timeout=1)

    assert coordinator._task.done()
    with pytest.raises(RuntimeError, match="broken dependency"):
        coordinator._task.result()
    assert coordinator.last_error == "broken dependency"


@pytest.mark.asyncio
async def test_fatal_start_error_is_not_converted_to_retryable_entry_error(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry()

    async def fail_import(self):
        raise ImportError("incompatible aiomqtt")

    monkeypatch.setattr(BridgeCoordinator, "_async_import_aiomqtt", fail_import)

    with pytest.raises(ImportError, match="incompatible aiomqtt"):
        await seenzus_bridge.async_setup_entry(hass, entry)
