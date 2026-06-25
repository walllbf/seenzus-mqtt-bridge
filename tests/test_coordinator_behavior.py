from __future__ import annotations

import json

import pytest

from seenzus_bridge import BridgeCoordinator
from seenzus_bridge.bridge_protocol import build_topics
from seenzus_bridge import dr
from seenzus_bridge import er
from tests.helpers import (
    AsyncFakeMQTTClient,
    FakeConfigEntry,
    FakeDeviceRegistry,
    FakeEntityRegistry,
    FakeHass,
    make_state_changed_event,
)


@pytest.fixture
def coordinator(monkeypatch):
    hass = FakeHass()
    entry = FakeConfigEntry(
        data={
            "mqtt_host": "broker.example.com",
            "topic_root": "savant/v2",
            "enable_state_events": True,
        }
    )
    registry = FakeEntityRegistry()
    device_registry = FakeDeviceRegistry()
    monkeypatch.setattr(er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(dr, "async_get", lambda _hass: device_registry)
    return BridgeCoordinator(hass, entry)


def test_fire_notifies_listeners_without_async_add_job(coordinator) -> None:
    calls: list[str] = []

    def _listener() -> None:
        calls.append("updated")

    coordinator.register_update_listener(_listener)
    coordinator.hass.async_add_job = None

    coordinator._fire()

    assert calls == ["updated"]


def test_mqtt_auth_error_sets_pairing_status_for_web_pair_config() -> None:
    coordinator = BridgeCoordinator(
        FakeHass(),
        FakeConfigEntry(
            data={
                "pairing_mode": "seamless",
                "config_source": "web_pair",
            }
        ),
    )

    coordinator._mark_mqtt_error("[code:135] Not authorized")

    assert coordinator.status == "error"
    assert coordinator.mqtt_connected is False
    assert coordinator.pairing_status == "mqtt_auth_failed"
    assert coordinator.pairing_last_error == "[code:135] Not authorized"


@pytest.mark.asyncio
async def test_presence_includes_mqtt_and_pairing_diagnostics() -> None:
    coordinator = BridgeCoordinator(
        FakeHass(),
        FakeConfigEntry(
            data={
                "pairing_mode": "seamless",
                "config_source": "web_pair",
                "pairing_session_id": "wps_1",
                "source_id": "ha-bridge-ha-demo",
                "source_type": "haos_bridge",
                "source_name": "HA Bridge",
            }
        ),
    )
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")
    coordinator.mqtt_connected = True
    coordinator.pairing_mode = "seamless"
    coordinator.config_source = "web_pair"
    coordinator.pairing_status = "bridge_ready"
    coordinator.pairing_last_error = None
    coordinator.pairing_session_id = "wps_1"

    await coordinator._publish_presence("online")

    assert coordinator._mqtt_client.published[0]["topic"] == "savant/v2/bridge/ha-demo/presence"
    assert coordinator._mqtt_client.published[0]["retain"] is True
    payload = json.loads(coordinator._mqtt_client.published[0]["payload"])
    assert payload["mqttConnected"] is True
    assert payload["pairingStatus"] == "bridge_ready"
    assert payload["configSource"] == "web_pair"
    assert payload["sourceId"] == "ha-bridge-ha-demo"
    assert payload["sourceType"] == "haos_bridge"
    assert payload["sourceName"] == "HA Bridge"
    assert payload["pairingLastError"] is None
    assert payload["pairingSessionId"] == "wps_1"


@pytest.mark.asyncio
async def test_publish_state_from_event_ignores_bridge_internal_entity(coordinator) -> None:
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")

    await coordinator._publish_state_from_event(
        make_state_changed_event("sensor.seenzus_mqtt_bridge_zhuang_tai_tui_song_ci_shu")
    )

    assert coordinator._mqtt_client.published == []


@pytest.mark.asyncio
async def test_state_publish_failure_counts_one_error_with_state_publish_failed_label(coordinator) -> None:
    class _FailingPublishClient(AsyncFakeMQTTClient):
        async def publish(self, topic: str, payload: str, *, qos: int, retain: bool = False) -> None:
            raise RuntimeError("broker gone")

    coordinator._mqtt_client = _FailingPublishClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")
    event = make_state_changed_event("light.demo", state="on")
    coordinator._pending_state_events["light.demo"] = event

    await coordinator._state_worker()

    # One failure, one label: counted once by _publish_state_from_event,
    # the worker's outer catch is log-only.
    assert coordinator.err_count == 1
    assert coordinator.last_error.startswith("state_publish_failed:")
    assert coordinator.state_push_count == 0


@pytest.mark.asyncio
async def test_publish_state_from_event_publishes_regular_entity_state(coordinator) -> None:
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")

    await coordinator._publish_state_from_event(
        make_state_changed_event(
            "fan.dmaker_cn_740506461_p5c_s_2_fan",
            state="on",
            attributes={"percentage": 75},
        )
    )

    assert coordinator._mqtt_client.published[0]["topic"] == (
        "savant/v2/bridge/ha-demo/state/fan.dmaker_cn_740506461_p5c_s_2_fan"
    )
    payload = json.loads(coordinator._mqtt_client.published[0]["payload"])
    assert payload["entityId"] == "fan.dmaker_cn_740506461_p5c_s_2_fan"
    assert payload["state"] == "on"


@pytest.mark.asyncio
async def test_publish_state_from_event_ignores_model_marked_entity(coordinator) -> None:
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")

    await coordinator._publish_state_from_event(
        make_state_changed_event(
            "sensor.aqara_model",
            state="on",
            attributes={"friendly_name": "Aqara T1*"},
        )
    )

    assert coordinator._mqtt_client.published == []


@pytest.mark.asyncio
async def test_get_states_command_skips_model_marked_entity(coordinator) -> None:
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")
    coordinator._command_prefix = coordinator._topics.command_sub[:-2]
    coordinator.hass.states.set(
        "light.living_room", state="on", attributes={"friendly_name": "Living Room"}
    )
    coordinator.hass.states.set(
        "sensor.aqara_model", state="on", attributes={"friendly_name": "Aqara T1*"}
    )

    await coordinator._handle_v2_command(
        "snapshot-marked",
        json.dumps({"msgId": "snapshot-marked", "method": "GET", "path": "/api/states"}),
        coordinator._mqtt_client,
    )

    state_topics = [
        item["topic"]
        for item in coordinator._mqtt_client.published
        if "/state/" in item["topic"]
    ]
    assert state_topics == ["savant/v2/bridge/ha-demo/state/light.living_room"]


@pytest.mark.asyncio
async def test_get_states_command_publishes_full_state_snapshot(coordinator) -> None:
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")
    coordinator._command_prefix = coordinator._topics.command_sub[:-2]
    coordinator.hass.states.set("light.living_room", state="on", attributes={"friendly_name": "Living Room"})
    coordinator.hass.states.set("sensor.seenzus_mqtt_bridge_zhuang_tai_tui_song_ci_shu", state="1")

    await coordinator._handle_v2_command(
        "snapshot-1",
        json.dumps({"msgId": "snapshot-1", "method": "GET", "path": "/api/states"}),
        coordinator._mqtt_client,
    )

    state_messages = [
        item for item in coordinator._mqtt_client.published
        if "/state/" in item["topic"]
    ]
    assert [item["topic"] for item in state_messages] == [
        "savant/v2/bridge/ha-demo/state/light.living_room"
    ]
    payload = json.loads(state_messages[0]["payload"])
    assert payload["entityId"] == "light.living_room"
    assert payload["state"] == "on"
    assert payload["source"] == "full_snapshot"


@pytest.mark.asyncio
async def test_publish_device_catalog_groups_entities_under_devices(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(data={"mqtt_host": "broker.example.com"})
    entity_registry = FakeEntityRegistry()
    device_registry = FakeDeviceRegistry()
    entity_registry.add(
        "light.kitchen",
        device_id="device-kitchen",
        name="Kitchen Light",
    )
    entity_registry.add(
        "sensor.kitchen_power",
        device_id="device-kitchen",
        name="Kitchen Power",
    )
    device_registry.add(
        "device-kitchen",
        name="Kitchen Lamp",
        manufacturer="Acme",
        model="L1",
        area_id="kitchen",
    )
    monkeypatch.setattr(er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(dr, "async_get", lambda _hass: device_registry)
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")
    hass.states.set("light.kitchen", state="on", attributes={"friendly_name": "Kitchen"})
    hass.states.set("sensor.kitchen_power", state="5", attributes={"device_class": "power", "unit_of_measurement": "W"})

    await coordinator._publish_device_catalog(coordinator._mqtt_client, source="test")

    assert coordinator._mqtt_client.published[0]["topic"] == "savant/v2/bridge/ha-demo/catalog"
    assert coordinator._mqtt_client.published[0]["retain"] is True
    payload = json.loads(coordinator._mqtt_client.published[0]["payload"])
    assert payload["bridgeId"] == "ha-demo"
    assert payload["deviceCount"] == 1
    assert payload["entityCount"] == 2
    assert payload["devices"][0]["deviceId"] == "device-kitchen"
    assert payload["devices"][0]["name"] == "Kitchen Lamp"
    assert [entity["entityId"] for entity in payload["devices"][0]["entities"]] == [
        "light.kitchen",
        "sensor.kitchen_power",
    ]


@pytest.mark.asyncio
async def test_device_catalog_keeps_ha_device_domain_entities(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(data={"mqtt_host": "broker.example.com"})
    entity_registry = FakeEntityRegistry()
    device_registry = FakeDeviceRegistry()
    entity_registry.add("light.kitchen", device_id="device-kitchen")
    entity_registry.add("sensor.kitchen_power", device_id="device-kitchen")
    entity_registry.add("sensor.router_uptime", device_id="device-router")
    entity_registry.add("update.router_firmware", device_id="device-router")
    device_registry.add("device-kitchen", name="Kitchen Lamp")
    device_registry.add("device-router", name="Router")
    monkeypatch.setattr(er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(dr, "async_get", lambda _hass: device_registry)
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")
    hass.states.set("light.kitchen", state="on")
    hass.states.set("sensor.kitchen_power", state="5", attributes={"device_class": "power"})
    hass.states.set("sensor.router_uptime", state="123", attributes={"device_class": "duration"})
    hass.states.set("update.router_firmware", state="off")

    await coordinator._publish_device_catalog(coordinator._mqtt_client, source="test")

    payload = json.loads(coordinator._mqtt_client.published[0]["payload"])
    assert payload["deviceCount"] == 2
    assert payload["entityCount"] == 3
    assert payload["devices"][0]["deviceId"] == "device-kitchen"
    assert [entity["entityId"] for entity in payload["devices"][0]["entities"]] == [
        "light.kitchen",
        "sensor.kitchen_power",
    ]
    assert payload["devices"][1]["deviceId"] == "device-router"
    assert [entity["entityId"] for entity in payload["devices"][1]["entities"]] == [
        "sensor.router_uptime",
    ]


@pytest.mark.asyncio
async def test_device_catalog_excludes_model_marked_entities(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(data={"mqtt_host": "broker.example.com"})
    entity_registry = FakeEntityRegistry()
    device_registry = FakeDeviceRegistry()
    entity_registry.add("light.kitchen", device_id="device-kitchen", name="Kitchen Light")
    device_registry.add("device-kitchen", name="Kitchen Lamp")
    monkeypatch.setattr(er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(dr, "async_get", lambda _hass: device_registry)
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")
    hass.states.set("light.kitchen", state="on", attributes={"friendly_name": "Kitchen"})
    hass.states.set(
        "sensor.aqara_model", state="on", attributes={"friendly_name": "Aqara T1*"}
    )

    await coordinator._publish_device_catalog(coordinator._mqtt_client, source="test")

    payload = json.loads(coordinator._mqtt_client.published[0]["payload"])
    assert payload["entityCount"] == 1
    reported_entities = [
        entity["entityId"]
        for device in payload["devices"]
        for entity in device["entities"]
    ]
    assert reported_entities == ["light.kitchen"]


@pytest.mark.asyncio
async def test_device_catalog_command_publishes_catalog_snapshot(coordinator, monkeypatch) -> None:
    entity_registry = FakeEntityRegistry()
    device_registry = FakeDeviceRegistry()
    entity_registry.add("switch.freezer_indicator", device_id="device-freezer")
    device_registry.add("device-freezer", name="Freezer")
    monkeypatch.setattr(er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(dr, "async_get", lambda _hass: device_registry)
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-demo")
    coordinator._command_prefix = coordinator._topics.command_sub[:-2]
    coordinator.hass.states.set("switch.freezer_indicator", state="on")

    await coordinator._handle_v2_command(
        "catalog-1",
        json.dumps({"msgId": "catalog-1", "method": "GET", "path": "/api/seenzus/device-catalog"}),
        coordinator._mqtt_client,
    )

    catalog_messages = [
        item for item in coordinator._mqtt_client.published
        if item["topic"] == "savant/v2/bridge/ha-demo/catalog"
    ]
    assert len(catalog_messages) == 1
    payload = json.loads(catalog_messages[0]["payload"])
    assert payload["correlationMsgId"] == "catalog-1"
    assert payload["devices"][0]["entities"][0]["entityId"] == "switch.freezer_indicator"


@pytest.mark.asyncio
async def test_prepare_for_reload_clears_old_retained_presence_when_bridge_changes(coordinator) -> None:
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-old")
    coordinator._entry.options = {"bridge_id": "ha-new"}

    await coordinator.async_prepare_for_reload()

    assert coordinator._mqtt_client.published[0]["topic"] == "savant/v2/bridge/ha-old/presence"
    assert coordinator._mqtt_client.published[0]["payload"] == ""
    assert coordinator._mqtt_client.published[0]["retain"] is True
