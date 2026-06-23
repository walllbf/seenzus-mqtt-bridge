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

PRESENCE_TOPIC = "savant/v2/bridge/ha-demo/presence"
CATALOG_TOPIC = "savant/v2/bridge/ha-demo/catalog"
COMMAND_SUB = "savant/v2/bridge/ha-demo/command/+"

HAPPY_ENTRY_DATA = {
    "mqtt_host": "broker.example.com",
    "topic_root": "savant/v2",
    "bridge_id": "ha-demo",
    "pairing_mode": "manual",
    "enable_state_events": False,
}


def _make_coordinator(monkeypatch, *, data: dict, cycles: list[dict] | None = None):
    hass = FakeHass()
    entry = FakeConfigEntry(data=data)
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    monkeypatch.setattr(dr, "async_get", lambda _hass: FakeDeviceRegistry())
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
            "topic_root": "savant/v2",
            "bridge_id": "ha-demo",
        },
    )
    sleeps, _real_sleep = _install_recording_sleep(monkeypatch, cancel_on=10)

    task = asyncio.get_running_loop().create_task(coordinator._mqtt_loop())
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
        assert client.subscriptions == [{"topic": COMMAND_SUB, "qos": 1}]

        presence = client.published[0]
        assert presence["topic"] == PRESENCE_TOPIC
        assert presence["qos"] == 1
        assert presence["retain"] is True
        assert json.loads(presence["payload"])["status"] == "online"

        states = [item for item in client.published if "/state/" in item["topic"]]
        assert [item["topic"] for item in states] == [
            "savant/v2/bridge/ha-demo/state/light.living_room"
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
    await asyncio.wait_for(task, timeout=5)

    assert len(fake.clients) == 2
    first_cycle, second_cycle = fake.clients
    assert [item["topic"] for item in first_cycle.published] == [
        PRESENCE_TOPIC,
        "savant/v2/bridge/ha-demo/state/light.living_room",
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
        assert "savant/v2/bridge/ha-demo/state/light.living_room" in topics
        assert CATALOG_TOPIC in topics
        assert coordinator._initial_snapshot_done is True
        assert coordinator.status == "active"
    finally:
        await _shutdown_loop(coordinator, task)


@pytest.mark.asyncio
async def test_loop_routes_command_message_to_published_result(monkeypatch) -> None:
    command_payload = json.dumps({"method": "GET", "path": "/api/states/light.demo"})
    message = FakeMqttMessage(
        "savant/v2/bridge/ha-demo/command/cmd-route-1", command_payload
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
            "savant/v2/bridge/ha-demo/result/cmd-route-1"
        ]
        result_payload = json.loads(results[0]["payload"])
        assert result_payload["msgId"] == "cmd-route-1"
        assert result_payload["success"] is True
        assert result_payload["status"] == 200
        assert coordinator.req_count == 1

        followups = [
            json.loads(item["payload"])
            for item in client.published
            if item["topic"] == "savant/v2/bridge/ha-demo/state/light.demo"
        ]
        assert any(
            payload.get("correlationMsgId") == "cmd-route-1"
            and payload["source"] == "command"
            for payload in followups
        )
    finally:
        await _shutdown_loop(coordinator, task)
