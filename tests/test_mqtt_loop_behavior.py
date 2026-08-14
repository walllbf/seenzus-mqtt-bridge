"""Characterization net for BridgeCoordinator._mqtt_loop (invariants 4 + 8).

These tests drive the real _mqtt_loop() as a task with the aiomqtt module
replaced through the pre-existing coordinator._aiomqtt seam. They pin the
behavior the Stage 8/9 loop split must preserve: error backoff, the connect
sequence, snapshot-once-per-coordinator-lifetime, HA-started gating (driven
ONLY through the _on_ha_started callback so the B5 Event rewrite passes the
same tests), and message routing to a published result.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from homeassistant.core import CoreState
from homeassistant.helpers import service as service_helper

import seenzus_bridge
from seenzus_bridge import (
    BridgeCoordinator,
    PRESENCE_HEARTBEAT_INTERVAL_SECONDS,
    dr,
    er,
)
from tests.helpers import (
    FakeAiomqttModule,
    FakeConfigEntry,
    FakeDeviceRegistry,
    FakeEntityRegistry,
    FakeHass,
    FakeMqttError,
    FakeMqttMessage,
)

pytestmark = pytest.mark.timeout(10)

PRESENCE_TOPIC = "seenzus/v2/bridge/ha-demo/presence"
CATALOG_TOPIC = "seenzus/v2/bridge/ha-demo/catalog"
COMMAND_SUB = "seenzus/v2/bridge/ha-demo/command/+"

HAPPY_ENTRY_DATA = {
    "mqtt_host": "broker.example.com",
    "topic_root": "seenzus/v2",
    "bridge_id": "ha-demo",
    "pairing_mode": "manual",
    "enable_state_events": False,
}


def _make_coordinator(
    monkeypatch,
    *,
    data: dict,
    cycles: list[dict] | None = None,
    entity_registry: FakeEntityRegistry | None = None,
    device_registry: FakeDeviceRegistry | None = None,
):
    hass = FakeHass()
    entry = FakeConfigEntry(data=data)
    entity_registry = entity_registry or FakeEntityRegistry()
    device_registry = device_registry or FakeDeviceRegistry()
    monkeypatch.setattr(er, "async_get", lambda _hass: entity_registry)
    monkeypatch.setattr(dr, "async_get", lambda _hass: device_registry)
    coordinator = BridgeCoordinator(hass, entry)
    fake_aiomqtt = FakeAiomqttModule(cycles)
    coordinator._aiomqtt = fake_aiomqtt
    return coordinator, fake_aiomqtt


def _install_recording_sleep(monkeypatch, *, cancel_on: float | None = None):
    """Patch module-level asyncio.sleep with a recorder.

    Heartbeat-length sleeps park forever (cancellable) so the presence
    heartbeat cannot flood the published list; shorter sleeps yield once so
    concurrent tasks make progress. `cancel_on` turns one recorded delay into
    a CancelledError, which _mqtt_loop treats as a clean shutdown.
    """
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(delay, *args, **kwargs):
        sleeps.append(delay)
        if cancel_on is not None and delay == cancel_on:
            raise asyncio.CancelledError
        if delay >= PRESENCE_HEARTBEAT_INTERVAL_SECONDS:
            await asyncio.Event().wait()
            return
        await real_sleep(0)

    monkeypatch.setattr(seenzus_bridge.asyncio, "sleep", _fake_sleep)
    return sleeps, real_sleep


async def _shutdown_loop(coordinator, task: asyncio.Task) -> None:
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    for scheduled in coordinator.hass.scheduled_tasks:
        if not scheduled.done():
            scheduled.cancel()
            try:
                await scheduled
            except asyncio.CancelledError:
                pass


@pytest.mark.asyncio
async def test_loop_missing_host_marks_error_and_waits_for_external_auth(monkeypatch) -> None:
    coordinator, fake = _make_coordinator(
        monkeypatch,
        data={
            "pairing_mode": "seamless",
            "topic_root": "seenzus/v2",
            "bridge_id": "ha-demo",
        },
    )
    sleeps, _real_sleep = _install_recording_sleep(monkeypatch, cancel_on=10)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert coordinator.status == "error"
    assert coordinator.mqtt_connected is False
    assert coordinator.last_error == "mqtt_host_missing"
    assert coordinator.pairing_status == "waiting_external_auth"
    assert coordinator.pairing_last_error == "mqtt_host_missing"
    assert coordinator.pairing_last_step == "waiting_external_auth"
    assert sleeps == [10]
    assert fake.clients == []


@pytest.mark.asyncio
async def test_loop_happy_connect_subscribes_then_presence_snapshot_catalog(monkeypatch) -> None:
    coordinator, fake = _make_coordinator(
        monkeypatch, data=dict(HAPPY_ENTRY_DATA), cycles=[{"end": "block"}]
    )
    coordinator.hass.states.set(
        "light.living_room", state="on", attributes={"friendly_name": "Living Room"}
    )
    _sleeps, real_sleep = _install_recording_sleep(monkeypatch)
    coordinator._on_ha_started(None)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
    try:
        for _ in range(5):
            await real_sleep(0)

        client = fake.clients[0]
        assert client.connect_kwargs["hostname"] == "broker.example.com"
        assert client.connect_kwargs["port"] == 1883
        assert client.connect_kwargs["identifier"] == "seenzus-bridge-01kpcrmg"
        # 裸 TCP entry（无 scheme）绝不附加 wss 支持引入的传输参数（issue #14）。
        assert not {"transport", "websocket_path", "tls_context"} & set(client.connect_kwargs)
        assert client.subscriptions == [{"topic": COMMAND_SUB, "qos": 1}]

        presence = client.published[0]
        assert presence["topic"] == PRESENCE_TOPIC
        assert presence["qos"] == 1
        assert presence["retain"] is True
        assert json.loads(presence["payload"])["status"] == "online"

        states = [item for item in client.published if "/state/" in item["topic"]]
        assert [item["topic"] for item in states] == [
            "seenzus/v2/bridge/ha-demo/state/light.living_room"
        ]
        assert states[0]["qos"] == 0
        assert json.loads(states[0]["payload"])["source"] == "startup_snapshot"

        catalogs = [item for item in client.published if item["topic"] == CATALOG_TOPIC]
        assert len(catalogs) == 1
        assert catalogs[0]["retain"] is True
        assert catalogs[0]["qos"] == 0
        assert json.loads(catalogs[0]["payload"])["source"] == "startup_snapshot"

        assert coordinator.status == "active"
        assert coordinator.mqtt_connected is True
    finally:
        await _shutdown_loop(coordinator, task)


@pytest.mark.asyncio
async def test_loop_catalog_keeps_every_entity_attached_to_an_unfamiliar_device(monkeypatch) -> None:
    entity_registry = FakeEntityRegistry()
    entity_registry.add("sensor.reef_water_temperature", device_id="reef-master-x1")
    entity_registry.add("reef_controller.feeding_motor", device_id="reef-master-x1", name="Aqara T1*")
    device_registry = FakeDeviceRegistry()
    device_registry.add("reef-master-x1", name="ReefMaster X1", model="X1")
    coordinator, fake = _make_coordinator(
        monkeypatch,
        data=dict(HAPPY_ENTRY_DATA),
        cycles=[{"end": "block"}],
        entity_registry=entity_registry,
        device_registry=device_registry,
    )
    coordinator.hass.states.set(
        "sensor.reef_water_temperature",
        state="25.2",
        attributes={"device_class": "temperature", "unit_of_measurement": "°C"},
    )
    coordinator.hass.states.set("reef_controller.feeding_motor", state="idle")
    _sleeps, real_sleep = _install_recording_sleep(monkeypatch)
    coordinator._on_ha_started(None)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
    try:
        for _ in range(5):
            await real_sleep(0)

        catalog_message = next(
            item for item in fake.clients[0].published if item["topic"] == CATALOG_TOPIC
        )
        payload = json.loads(catalog_message["payload"])
        device = payload["devices"][0]
        assert {
            "deviceId": device["deviceId"],
            "name": device["name"],
            "entityCount": device["entityCount"],
            "entities": [
                {
                    "entityId": entity["entityId"],
                    "domain": entity["domain"],
                    "state": entity["state"],
                    **({"deviceClass": entity["deviceClass"]} if "deviceClass" in entity else {}),
                    **({"unit": entity["unit"]} if "unit" in entity else {}),
                }
                for entity in device["entities"]
            ],
        } == {
            "deviceId": "reef-master-x1",
            "name": "ReefMaster X1",
            "entityCount": 2,
            "entities": [
                {
                    "entityId": "sensor.reef_water_temperature",
                    "domain": "sensor",
                    "state": "25.2",
                    "deviceClass": "temperature",
                    "unit": "°C",
                },
                {
                    "entityId": "reef_controller.feeding_motor",
                    "domain": "reef_controller",
                    "state": "idle",
                },
            ],
        }
    finally:
        await _shutdown_loop(coordinator, task)


@pytest.mark.asyncio
async def test_loop_catalog_keeps_standalone_entities_from_official_platforms(monkeypatch) -> None:
    coordinator, fake = _make_coordinator(
        monkeypatch,
        data=dict(HAPPY_ENTRY_DATA),
        cycles=[{"end": "block"}],
    )
    coordinator.hass.states.set(
        "update.router_firmware",
        state="off",
        attributes={"friendly_name": "路由器固件"},
    )
    coordinator.hass.states.set("input_text.guest_note", state="unknown")
    coordinator.hass.states.set("input_datetime.dinner", state="2026-08-08 19:00:00")
    coordinator.hass.states.set("automation.good_night", state="on")
    coordinator.hass.states.set("script.feed_fish", state="off")
    _sleeps, real_sleep = _install_recording_sleep(monkeypatch)
    coordinator._on_ha_started(None)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
    try:
        for _ in range(5):
            await real_sleep(0)

        catalog_message = next(
            item for item in fake.clients[0].published if item["topic"] == CATALOG_TOPIC
        )
        payload = json.loads(catalog_message["payload"])
        assert next(
            device for device in payload["devices"] if device["deviceId"] == "input_text.guest_note"
        )["entities"][0]["available"] is True
        assert [
            {
                "deviceId": device["deviceId"],
                "primaryDomain": device["primaryDomain"],
                "entities": [entity["entityId"] for entity in device["entities"]],
            }
            for device in payload["devices"]
        ] == [
            {
                "deviceId": "input_datetime.dinner",
                "primaryDomain": "input_datetime",
                "entities": ["input_datetime.dinner"],
            },
            {
                "deviceId": "input_text.guest_note",
                "primaryDomain": "input_text",
                "entities": ["input_text.guest_note"],
            },
            {
                "deviceId": "update.router_firmware",
                "primaryDomain": "update",
                "entities": ["update.router_firmware"],
            },
        ]
    finally:
        await _shutdown_loop(coordinator, task)


@pytest.mark.asyncio
async def test_loop_wss_entry_connects_with_websockets_transport(monkeypatch) -> None:
    # wss entry（issue #14）：连接层给 aiomqtt.Client 附加 websockets transport、
    # 握手路径与 TLS 上下文；裸 TCP entry 不带这三个键（见 happy 测试的
    # connect_kwargs 与 test_coordinator_behavior 的 _transport_connect_kwargs 组）。
    import ssl

    coordinator, fake = _make_coordinator(
        monkeypatch,
        data={
            **HAPPY_ENTRY_DATA,
            "mqtt_host": "edge.seenzus.ai",
            "mqtt_port": 443,
            "mqtt_scheme": "wss",
            "mqtt_ws_path": "/mqtt",
        },
        cycles=[{"end": "block"}],
    )
    _sleeps, real_sleep = _install_recording_sleep(monkeypatch)
    coordinator._on_ha_started(None)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
    try:
        for _ in range(5):
            await real_sleep(0)

        client = fake.clients[0]
        assert client.connect_kwargs["hostname"] == "edge.seenzus.ai"
        assert client.connect_kwargs["port"] == 443
        assert client.connect_kwargs["transport"] == "websockets"
        assert client.connect_kwargs["websocket_path"] == "/mqtt"
        assert isinstance(client.connect_kwargs["tls_context"], ssl.SSLContext)
        # presence 上报生效传输方式，供后端/运维确认桥已切到 wss。
        presence_payload = json.loads(client.published[0]["payload"])
        assert presence_payload["transport"] == "wss"
        assert presence_payload["wsPath"] == "/mqtt"
        assert coordinator.mqtt_connected is True
    finally:
        await _shutdown_loop(coordinator, task)


@pytest.mark.asyncio
async def test_loop_publishes_startup_snapshot_once_across_reconnect_cycles(monkeypatch) -> None:
    coordinator, fake = _make_coordinator(
        monkeypatch,
        data=dict(HAPPY_ENTRY_DATA),
        cycles=[
            {"end": FakeMqttError("[code:7] connection lost")},
            {"end": asyncio.CancelledError},
        ],
    )
    coordinator.hass.states.set("light.living_room", state="on")
    sleeps, _real_sleep = _install_recording_sleep(monkeypatch)
    coordinator._on_ha_started(None)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)

    assert len(fake.clients) == 2
    first_cycle, second_cycle = fake.clients
    assert [item["topic"] for item in first_cycle.published] == [
        PRESENCE_TOPIC,
        "seenzus/v2/bridge/ha-demo/state/light.living_room",
        CATALOG_TOPIC,
    ]
    # Reconnect re-asserts presence AND the retained catalog (durable topology truth —
    # self-heals an empty broker after a restart), but NOT the full state snapshot
    # (once per coordinator lifetime; state recovers via on-change events).
    assert [item["topic"] for item in second_cycle.published] == [
        PRESENCE_TOPIC,
        CATALOG_TOPIC,
    ]
    # The reconnect catalog is tagged source="reconnect" and sent at qos 1 (reliable).
    reconnect_catalog = next(
        item for item in second_cycle.published if item["topic"] == CATALOG_TOPIC
    )
    assert reconnect_catalog["retain"] is True
    assert reconnect_catalog["qos"] == 1
    assert json.loads(reconnect_catalog["payload"])["source"] == "reconnect"
    assert coordinator._initial_snapshot_done is True
    assert coordinator._mqtt_client is None
    # A recovered connection must not keep presenting the previous iterator
    # disconnect as its current Last error.
    assert coordinator.last_error is None
    # MqttError retry backoff is 5s (heartbeat sleeps filtered out).
    assert [delay for delay in sleeps if delay in (5, 10)] == [5]


@pytest.mark.asyncio
async def test_loop_defers_startup_snapshot_until_ha_started(monkeypatch) -> None:
    coordinator, fake = _make_coordinator(
        monkeypatch, data=dict(HAPPY_ENTRY_DATA), cycles=[{"end": "block"}]
    )
    coordinator.hass.state = CoreState.not_running
    coordinator.hass.states.set("light.living_room", state="on")
    _sleeps, real_sleep = _install_recording_sleep(monkeypatch)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
    try:
        for _ in range(5):
            await real_sleep(0)

        client = fake.clients[0]
        assert client.subscriptions == [{"topic": COMMAND_SUB, "qos": 1}]
        # Connected and announced, but no snapshot before HA has started.
        assert [item["topic"] for item in client.published] == [PRESENCE_TOPIC]
        assert coordinator._initial_snapshot_done is False

        coordinator._on_ha_started(None)
        for _ in range(10):
            await real_sleep(0)

        topics = [item["topic"] for item in client.published]
        assert "seenzus/v2/bridge/ha-demo/state/light.living_room" in topics
        assert CATALOG_TOPIC in topics
        assert coordinator._initial_snapshot_done is True
        assert coordinator.status == "active"
    finally:
        await _shutdown_loop(coordinator, task)


@pytest.mark.asyncio
async def test_loop_returns_the_connected_instances_runtime_action_catalog(monkeypatch) -> None:
    descriptions = {
        "fan": {
            "set_percentage": {
                "target": {"entity": {"domain": "fan", "supported_features": [1]}},
                "fields": {"percentage": {"selector": {"number": {"min": 0, "max": 100}}}},
            },
        },
    }

    async def fake_descriptions(_hass):
        return descriptions

    monkeypatch.setattr(service_helper, "async_get_all_descriptions", fake_descriptions)
    message = FakeMqttMessage(
        "seenzus/v2/bridge/ha-demo/command/actions-1",
        json.dumps({"method": "GET", "path": "/api/services"}),
    )
    coordinator, fake = _make_coordinator(
        monkeypatch,
        data=dict(HAPPY_ENTRY_DATA),
        cycles=[{"messages": [message], "end": "block"}],
    )
    _sleeps, real_sleep = _install_recording_sleep(monkeypatch)
    coordinator._on_ha_started(None)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
    try:
        for _ in range(10):
            await real_sleep(0)

        result_message = next(
            item for item in fake.clients[0].published if item["topic"].endswith("/result/actions-1")
        )
        result = json.loads(result_message["payload"])
        assert {
            "success": result["success"],
            "status": result["status"],
            "data": result["data"],
        } == {"success": True, "status": 200, "data": descriptions}
    finally:
        await _shutdown_loop(coordinator, task)


@pytest.mark.asyncio
async def test_loop_routes_command_message_to_published_result(monkeypatch) -> None:
    command_payload = json.dumps({"method": "GET", "path": "/api/states/light.demo"})
    message = FakeMqttMessage(
        "seenzus/v2/bridge/ha-demo/command/cmd-route-1", command_payload
    )
    coordinator, fake = _make_coordinator(
        monkeypatch,
        data=dict(HAPPY_ENTRY_DATA),
        cycles=[{"messages": [message], "end": "block"}],
    )
    coordinator.hass.states.set("light.demo", state="on", attributes={"brightness": 42})
    _sleeps, real_sleep = _install_recording_sleep(monkeypatch)
    coordinator._on_ha_started(None)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
    try:
        for _ in range(10):
            await real_sleep(0)

        client = fake.clients[0]
        results = [item for item in client.published if "/result/" in item["topic"]]
        assert [item["topic"] for item in results] == [
            "seenzus/v2/bridge/ha-demo/result/cmd-route-1"
        ]
        result_payload = json.loads(results[0]["payload"])
        assert result_payload["msgId"] == "cmd-route-1"
        assert result_payload["success"] is True
        assert result_payload["status"] == 200
        assert coordinator.req_count == 1

        followups = [
            json.loads(item["payload"])
            for item in client.published
            if item["topic"] == "seenzus/v2/bridge/ha-demo/state/light.demo"
        ]
        assert any(
            payload.get("correlationMsgId") == "cmd-route-1"
            and payload["source"] == "command"
            for payload in followups
        )
    finally:
        await _shutdown_loop(coordinator, task)
