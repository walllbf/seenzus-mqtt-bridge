"""One oversized entity must not prevent unrelated states from being delivered."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging

import pytest

import seenzus_bridge.coordinator as coordinator_module
from seenzus_bridge import BridgeCoordinator, dr, er
from seenzus_bridge.bridge_protocol import build_topics
from tests.helpers import (
    AsyncFakeMQTTClient,
    FakeConfigEntry,
    FakeDeviceRegistry,
    FakeEntityRegistry,
    FakeHass,
)


PACKET_LIMIT = 1024 * 1024
START = datetime(2026, 10, 4, tzinfo=timezone.utc)
ENTITY_IDS = ["sensor.normal_a", "sensor.large_b", "sensor.normal_c"]


@pytest.fixture
def coordinator(monkeypatch):
    hass = FakeHass()
    entry = FakeConfigEntry(data={
        "mqtt_host": "broker.example.com",
        "topic_root": "seenzus/v2",
        "bridge_id": "ha-demo",
    })
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    monkeypatch.setattr(dr, "async_get", lambda _hass: FakeDeviceRegistry())
    instance = BridgeCoordinator(hass, entry)
    instance._topics = build_topics("seenzus/v2", "ha-demo")
    return instance


def _states(coordinator, *, oversized: bool):
    for index, entity_id in enumerate(ENTITY_IDS):
        attributes = {"friendly_name": entity_id, "nested": {"enabled": True}}
        if oversized and index == 1:
            attributes["device_data"] = "x" * PACKET_LIMIT
        coordinator.hass.states.set(entity_id, state="on", attributes=attributes)
        state = coordinator.hass.states.get(entity_id)
        state.last_changed = START + timedelta(seconds=index + 1)
        state.last_updated = state.last_changed
    return coordinator.hass.states.async_all()


def _state_payloads(client):
    return [
        json.loads(item["payload"])
        for item in client.published
        if "/state/" in item["topic"]
    ]


def _assert_good_states_delivered(coordinator, client, source):
    payloads = _state_payloads(client)
    assert [payload["entityId"] for payload in payloads] == [ENTITY_IDS[0], ENTITY_IDS[2]]
    assert coordinator.state_push_count == 2
    for payload in payloads:
        assert payload["source"] == source
        assert payload["attributes"] == coordinator.hass.states.get(payload["entityId"]).attributes


def _assert_oversize_diagnosed(coordinator, caplog, *, errors=1):
    assert coordinator.err_count == errors
    assert coordinator._dropped_state_events == 1
    assert ENTITY_IDS[1] in (coordinator.last_error or "")
    assert "packet=" in coordinator.last_error
    assert f"limit={PACKET_LIMIT}" in coordinator.last_error
    assert any(
        record.levelno >= logging.WARNING
        and ENTITY_IDS[1] in record.getMessage()
        and "packet=" in record.getMessage()
        and f"limit={PACKET_LIMIT}" in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["startup_snapshot", "full_snapshot"])
async def test_snapshot_skips_only_oversized_entity(coordinator, caplog, source):
    _states(coordinator, oversized=True)
    client = AsyncFakeMQTTClient()
    caplog.set_level(logging.INFO, logger=coordinator_module.__name__)

    await coordinator._publish_all_states(client, source=source, correlation_id="snapshot-1")

    _assert_good_states_delivered(coordinator, client, source)
    _assert_oversize_diagnosed(coordinator, caplog)
    assert source in coordinator.last_error
    assert all(payload["correlationMsgId"] == "snapshot-1" for payload in _state_payloads(client))
    assert "Published HA state snapshot: 2 entities; skipped 1 oversized entities" in caplog.text


@pytest.mark.asyncio
async def test_history_skips_only_oversized_entity(coordinator, monkeypatch, caplog):
    states = _states(coordinator, oversized=True)

    async def history(_start, _end):
        return list(reversed(states))

    monkeypatch.setattr(coordinator, "_async_fetch_history_states", history)
    client = AsyncFakeMQTTClient()
    caplog.set_level(logging.INFO, logger=coordinator_module.__name__)

    await coordinator._run_history_replay(START, START + timedelta(minutes=1), client=client)

    _assert_good_states_delivered(coordinator, client, "history_replay")
    _assert_oversize_diagnosed(coordinator, caplog)
    assert "history_replay" in coordinator.last_error
    assert [payload["ts"] for payload in _state_payloads(client)] == [
        (START + timedelta(seconds=1)).isoformat(),
        (START + timedelta(seconds=3)).isoformat(),
    ]
    assert (
        f"Replayed 2 HA recorder state change(s) from {START.isoformat()}; skipped 1 oversized states"
        in caplog.text
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["full_snapshot", "history_replay"])
async def test_all_oversized_batch_still_yields_to_other_tasks(coordinator, monkeypatch, source):
    # A smaller wire budget keeps this scheduling test cheap; the one-MiB
    # production boundary is exercised by the mixed-batch tests above.
    monkeypatch.setattr(coordinator_module, "MAX_MQTT_PACKET_SIZE", 4096)
    count = coordinator_module.SNAPSHOT_BATCH_SIZE + 1
    for index in range(count):
        coordinator.hass.states.set(
            f"sensor.large_{index}", attributes={"device_data": "x" * 4096}
        )

    async def history(_start, _end):
        return coordinator.hass.states.async_all()

    monkeypatch.setattr(coordinator, "_async_fetch_history_states", history)
    errors_when_scheduled_work_runs = []
    asyncio.get_running_loop().call_soon(
        lambda: errors_when_scheduled_work_runs.append(coordinator.err_count)
    )
    client = AsyncFakeMQTTClient()

    if source == "history_replay":
        await coordinator._run_history_replay(START, START + timedelta(minutes=1), client=client)
    else:
        await coordinator._publish_all_states(client, source=source)
    await asyncio.sleep(0)

    assert len(errors_when_scheduled_work_runs) == 1
    assert 0 < errors_when_scheduled_work_runs[0] < count
    assert coordinator.err_count == count
    assert coordinator._dropped_state_events == count
    assert coordinator.state_push_count == 0
    assert client.published == []


@pytest.mark.asyncio
async def test_all_states_command_returns_413_then_delivers_other_entities(coordinator, caplog):
    _states(coordinator, oversized=True)
    client = AsyncFakeMQTTClient()

    await coordinator._handle_v2_command(
        "topic-id",
        json.dumps({"msgId": "request-id", "method": "GET", "path": "/api/states"}),
        client,
    )

    results = [item for item in client.published if "/result/" in item["topic"]]
    assert len(results) == 1
    response = json.loads(results[0]["payload"])
    assert response["msgId"] == "request-id"
    assert response["status"] == 413
    assert response["success"] is False
    assert response["error"] == "response_too_large"
    assert "data" not in response
    _assert_good_states_delivered(coordinator, client, "full_snapshot")
    _assert_oversize_diagnosed(coordinator, caplog, errors=2)
    assert all(payload["correlationMsgId"] == "request-id" for payload in _state_payloads(client))


class _FailSecondStateClient(AsyncFakeMQTTClient):
    def __init__(self, failure):
        super().__init__()
        self.failure = failure
        self.attempted_entities = []

    async def publish(self, topic, payload, *, qos, retain=False):
        entity_id = json.loads(payload)["entityId"]
        self.attempted_entities.append(entity_id)
        if entity_id == ENTITY_IDS[1]:
            raise self.failure
        await super().publish(topic, payload, qos=qos, retain=retain)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["startup_snapshot", "full_snapshot"])
async def test_snapshot_transport_failure_still_stops_batch(coordinator, source):
    _states(coordinator, oversized=False)
    failure = RuntimeError("connection lost")
    client = _FailSecondStateClient(failure)

    with pytest.raises(coordinator_module._MqttPublishFailure) as raised:
        await coordinator._publish_all_states(client, source=source)

    assert raised.value.__cause__ is failure
    assert client.attempted_entities == ENTITY_IDS[:2]
    assert [payload["entityId"] for payload in _state_payloads(client)] == ENTITY_IDS[:1]


@pytest.mark.asyncio
async def test_history_transport_failure_still_stops_batch(coordinator, monkeypatch, caplog):
    states = _states(coordinator, oversized=False)

    async def history(_start, _end):
        return states

    monkeypatch.setattr(coordinator, "_async_fetch_history_states", history)
    client = _FailSecondStateClient(RuntimeError("connection lost"))

    await coordinator._run_history_replay(START, START + timedelta(minutes=1), client=client)

    assert client.attempted_entities == ENTITY_IDS[:2]
    assert [payload["entityId"] for payload in _state_payloads(client)] == ENTITY_IDS[:1]
    assert "Recorder history replay unavailable or interrupted: connection lost" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["startup_snapshot", "full_snapshot", "history_replay"])
async def test_batch_cancellation_still_propagates(coordinator, monkeypatch, source):
    states = _states(coordinator, oversized=False)

    async def history(_start, _end):
        return states

    monkeypatch.setattr(coordinator, "_async_fetch_history_states", history)
    cancellation = asyncio.CancelledError("shutdown")
    client = _FailSecondStateClient(cancellation)

    with pytest.raises(asyncio.CancelledError) as raised:
        if source == "history_replay":
            await coordinator._run_history_replay(START, START + timedelta(minutes=1), client=client)
        else:
            await coordinator._publish_all_states(client, source=source)

    assert raised.value is cancellation
    assert client.attempted_entities == ENTITY_IDS[:2]
    assert [payload["entityId"] for payload in _state_payloads(client)] == ENTITY_IDS[:1]
