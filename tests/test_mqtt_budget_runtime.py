"""Configured wire budgets apply to real publication and bootstrap paths."""
from __future__ import annotations

import asyncio
import json

import pytest

from seenzus_bridge.coordinator import _MqttPacketTooLarge
from seenzus_bridge.sensor import BridgeStatusSensor
from tests.helpers import AsyncFakeMQTTClient, FakeMqttError
from tests.test_mqtt_loop_behavior import (
    HAPPY_ENTRY_DATA,
    CATALOG_TOPIC,
    _install_recording_sleep,
    _make_coordinator,
)
from tests.test_mqtt_payload_behavior import _paho_packet


KEY = "mqtt_max_packet_size_kib"


@pytest.mark.asyncio
@pytest.mark.parametrize("topic_root", ["x" * 950, "温" * 316], ids=["ascii", "utf8"])
async def test_lower_budget_retracts_previous_online_on_long_topic(monkeypatch, topic_root):
    coordinator, fake = _make_coordinator(monkeypatch, data={
        **HAPPY_ENTRY_DATA, KEY: 2, "topic_root": topic_root,
    })
    coordinator._topics = coordinator._resolve_topics()
    previous = AsyncFakeMQTTClient()
    await coordinator._publish_presence("online", client=previous, required=True)
    assert json.loads(previous.published[-1]["payload"])["status"] == "online"

    coordinator._entry.options[KEY] = 1
    coordinator._on_ha_started(None)
    await asyncio.wait_for(coordinator._mqtt_loop(), timeout=1)

    assert len(fake.clients) == 1
    offline = fake.clients[0].published[-1]
    assert offline["topic"] == previous.published[-1]["topic"]
    assert offline["retain"] is True
    assert json.loads(offline["payload"])["status"] == "offline"
    assert len(_paho_packet(offline["topic"], offline["payload"], 1)) <= 1024
    assert coordinator.status == "error"
    assert not coordinator.mqtt_connected


@pytest.mark.asyncio
async def test_impossible_presence_budget_stops_before_connecting(monkeypatch):
    coordinator, fake = _make_coordinator(monkeypatch, data={
        **HAPPY_ENTRY_DATA, KEY: 1, "topic_root": "x" * 990,
    })
    coordinator._on_ha_started(None)
    await asyncio.wait_for(coordinator._mqtt_loop(), timeout=1)
    assert fake.clients == []
    assert coordinator.status == "error"
    assert "presence" in coordinator.last_error


@pytest.mark.asyncio
async def test_configured_two_mib_preserves_result_above_default_budget(monkeypatch):
    coordinator, _ = _make_coordinator(monkeypatch, data={**HAPPY_ENTRY_DATA, KEY: 1024})
    coordinator._entry.options[KEY] = 2048
    coordinator._topics = coordinator._resolve_topics()
    data = {"full": "x" * 1_120_000}
    client = AsyncFakeMQTTClient()

    assert await coordinator._publish_result(client, "large", success=True, status=200, data=data)

    result = json.loads(client.published[0]["payload"])
    assert result["success"] is True
    assert result["data"] == data
    assert len(_paho_packet(client.published[0]["topic"], client.published[0]["payload"], 1)) < 2 * 1024**2


@pytest.mark.asyncio
async def test_small_configured_budget_reports_its_actual_limit(monkeypatch):
    coordinator, _ = _make_coordinator(monkeypatch, data={**HAPPY_ENTRY_DATA, KEY: 4})
    coordinator._topics = coordinator._resolve_topics()
    client = AsyncFakeMQTTClient()

    assert await coordinator._publish_result(client, "large", success=True, status=200, data="x" * 5000)

    result = json.loads(client.published[0]["payload"])
    assert result["success"] is False
    assert result["status"] == 413
    assert result["maxPacketSize"] == 4096
    assert result["packetSize"] > 5000
    assert "limit=4096" in coordinator.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize("qos", [0, 1])
async def test_configured_budget_checks_complete_wire_boundary(monkeypatch, qos):
    coordinator, _ = _make_coordinator(monkeypatch, data={**HAPPY_ENTRY_DATA, KEY: 4})
    topic = "seenzus/温度/state"
    candidate = "x" * 4096
    payload = candidate[:4096 - (len(_paho_packet(topic, candidate, qos)) - 4096)]
    client = AsyncFakeMQTTClient()
    await coordinator._publish(client, topic, payload, qos=qos)
    with pytest.raises(_MqttPacketTooLarge, match="packet=4097 limit=4096"):
        await coordinator._publish(client, topic, payload + "x", qos=qos)
    assert len(client.published) == 1


@pytest.mark.parametrize("value,expected", [(None, 1024**2), (2048, 2 * 1024**2), (False, None)])
def test_diagnostics_show_configured_budget_without_crashing_on_invalid_value(monkeypatch, value, expected):
    data = dict(HAPPY_ENTRY_DATA)
    if value is not None:
        data[KEY] = value
    coordinator, _ = _make_coordinator(monkeypatch, data=data)
    sensor = BridgeStatusSensor(coordinator, coordinator._entry)
    assert sensor.extra_state_attributes["mqtt_max_packet_size"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("budget_kib", [None, 4])
async def test_oversized_catalog_stops_reconnecting_and_marks_offline(monkeypatch, budget_kib):
    data = dict(HAPPY_ENTRY_DATA)
    if budget_kib is not None:
        data[KEY] = budget_kib
    limit = (budget_kib or 1024) * 1024
    coordinator, fake = _make_coordinator(
        monkeypatch, data=data, cycles=[{"end": "block"}],
    )
    coordinator._on_ha_started(None)
    monkeypatch.setattr(coordinator, "_build_device_catalog_payload", lambda **_kwargs: {
        "devices": [], "entityCount": 0, "large": "x" * (limit + 100),
    })
    sleeps, _ = _install_recording_sleep(monkeypatch, cancel_on=5)

    await asyncio.wait_for(coordinator._mqtt_loop(), timeout=1)

    assert len(fake.clients) == 1
    assert 5 not in sleeps
    assert coordinator.status == "error"
    assert coordinator.mqtt_connected is False
    assert coordinator.err_count == 1
    assert "mqtt_packet_too_large" in coordinator.last_error
    assert f"limit={limit}" in coordinator.last_error
    assert not any(item["topic"] == CATALOG_TOPIC for item in fake.clients[0].published)
    presence = [json.loads(item["payload"]) for item in fake.clients[0].published if item["topic"].endswith("/presence")]
    assert presence[-1]["status"] == "offline"
    assert presence[-1]["lastError"] == coordinator.last_error
    assert not coordinator._initial_snapshot_attempted


@pytest.mark.asyncio
async def test_invalid_budget_stops_before_opening_connection(monkeypatch):
    coordinator, fake = _make_coordinator(monkeypatch, data={**HAPPY_ENTRY_DATA, KEY: -1})
    coordinator._on_ha_started(None)

    await asyncio.wait_for(coordinator._mqtt_loop(), timeout=1)

    assert fake.clients == []
    assert coordinator.status == "error"
    assert KEY in coordinator.last_error
    assert coordinator.mqtt_connected is False


@pytest.mark.asyncio
async def test_catalog_failure_retracts_online_when_offline_diagnostics_exceed_budget(monkeypatch):
    coordinator, fake = _make_coordinator(
        monkeypatch, data={**HAPPY_ENTRY_DATA, KEY: 1, "source_name": "x" * 500},
    )
    coordinator._on_ha_started(None)
    monkeypatch.setattr(coordinator, "_build_device_catalog_payload", lambda **_kwargs: {
        "devices": [], "entityCount": 0, "large": "x" * 1500,
    })

    await asyncio.wait_for(coordinator._mqtt_loop(), timeout=1)

    assert len(fake.clients) == 1
    publications = fake.clients[0].published
    assert [json.loads(item["payload"])["status"] for item in publications] == ["online", "offline"]
    for item in publications:
        assert item["retain"] is True
        assert len(_paho_packet(item["topic"], item["payload"], item["qos"])) <= 1024
    assert json.loads(publications[-1]["payload"])["bridgeId"] == "ha-demo"
    assert coordinator.status == "error"
    assert "mqtt_packet_too_large" in coordinator.last_error


@pytest.mark.asyncio
async def test_oversized_initial_presence_cannot_claim_ready(monkeypatch):
    coordinator, fake = _make_coordinator(
        monkeypatch, data={**HAPPY_ENTRY_DATA, KEY: 1, "source_name": "x" * 550},
    )
    coordinator._on_ha_started(None)

    await asyncio.wait_for(coordinator._mqtt_loop(), timeout=1)

    assert len(fake.clients) == 1
    assert [json.loads(item["payload"])["status"] for item in fake.clients[0].published] == ["offline"]
    assert coordinator.status == "error"
    assert not coordinator.mqtt_connected
    assert not coordinator._initial_snapshot_attempted
    assert "mqtt_packet_too_large" in coordinator.last_error


@pytest.mark.asyncio
async def test_initial_presence_transport_failure_retries_without_claiming_ready(monkeypatch):
    coordinator, fake = _make_coordinator(monkeypatch, data=dict(HAPPY_ENTRY_DATA))
    coordinator._on_ha_started(None)
    sleeps, _ = _install_recording_sleep(monkeypatch, cancel_on=5)

    async def fail_publish(*_args, **_kwargs):
        raise FakeMqttError("connection lost before online announcement")

    monkeypatch.setattr(AsyncFakeMQTTClient, "publish", fail_publish)

    with pytest.raises(asyncio.CancelledError):
        await coordinator._mqtt_loop()

    assert len(fake.clients) == 1
    assert fake.clients[0].published == []
    assert sleeps == [5]
    assert coordinator.status == "error"
    assert not coordinator.mqtt_connected
    assert not coordinator._initial_snapshot_attempted
    assert "connection lost before online announcement" in coordinator.last_error


@pytest.mark.asyncio
@pytest.mark.parametrize("source_name_length", [0, 480, 500])
async def test_catalog_failure_retries_failed_offline_retraction_before_stopping(monkeypatch, source_name_length):
    coordinator, fake = _make_coordinator(monkeypatch, data={
        **HAPPY_ENTRY_DATA, KEY: 1, "source_name": "x" * source_name_length,
    })
    coordinator._on_ha_started(None)
    monkeypatch.setattr(coordinator, "_build_device_catalog_payload", lambda **_kwargs: {
        "devices": [], "entityCount": 0, "large": "x" * 1500,
    })
    sleeps, _ = _install_recording_sleep(monkeypatch, cancel_on=10)
    original_publish = AsyncFakeMQTTClient.publish
    offline_attempts = 0

    async def fail_first_offline(client, topic, payload, **kwargs):
        nonlocal offline_attempts
        if json.loads(payload).get("status") == "offline":
            offline_attempts += 1
            if offline_attempts == 1:
                raise FakeMqttError("connection lost before offline retraction")
        await original_publish(client, topic, payload, **kwargs)

    monkeypatch.setattr(AsyncFakeMQTTClient, "publish", fail_first_offline)

    await asyncio.wait_for(coordinator._mqtt_loop(), timeout=1)

    assert offline_attempts == 2
    assert len(fake.clients) == 2
    assert sleeps == [5]
    assert json.loads(fake.clients[-1].published[-1]["payload"])["status"] == "offline"
    for client in fake.clients:
        for item in client.published:
            assert item["retain"] is True
            assert len(_paho_packet(item["topic"], item["payload"], item["qos"])) <= 1024
    assert coordinator.status == "error"
    assert not coordinator.mqtt_connected
    assert not coordinator._initial_snapshot_attempted
    assert "mqtt_packet_too_large" in coordinator.last_error
