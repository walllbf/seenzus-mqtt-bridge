"""Large MQTT replies must preserve data without disconnecting the bridge."""
from __future__ import annotations

from datetime import datetime, timezone
import json

import paho.mqtt.client as paho
import pytest

from homeassistant.helpers import service as service_helper

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
FINISHED_AT = "2026-10-04T02:00:00+00:00"


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
    monkeypatch.setattr(coordinator_module, "utc_now_iso", lambda: FINISHED_AT)
    instance = BridgeCoordinator(hass, entry)
    instance._topics = build_topics("seenzus/v2", "ha-demo")
    return instance


def _paho_packet(topic: str, payload: str, qos: int) -> bytes:
    """Use the installed Paho serializer as an independent wire-size oracle."""
    client = paho.Client(client_id="packet-size-oracle", protocol=paho.MQTTv311)
    packets = []

    def capture(_command, packet, _mid, _qos, _info):
        packets.append(bytes(packet))
        return paho.MQTT_ERR_SUCCESS

    client._packet_queue = capture
    client._sock = object()
    try:
        client._send_publish(1, topic.encode("utf-8"), payload.encode("utf-8"), qos=qos)
    finally:
        client._sock = None
    assert len(packets) == 1
    return packets[0]


@pytest.mark.parametrize("qos", [0, 1])
@pytest.mark.parametrize("remaining_length", [127, 128, 16383, 16384])
def test_packet_size_matches_paho_at_remaining_length_boundaries(qos, remaining_length):
    topic = "bridge/客厅/state"
    payload = "x" * (remaining_length - 2 - len(topic.encode("utf-8")) - (2 if qos else 0))
    assert coordinator_module._mqtt_publish_packet_size(topic, payload, qos) == len(
        _paho_packet(topic, payload, qos)
    )


def test_compact_json_preserves_unicode_and_default_serialization():
    original = {
        "name": "客厅温度 🌡",
        "values": [1, 2.5, True, None],
        "quoted": 'a "value" with\na newline',
        "time": datetime(2026, 10, 4, tzinfo=timezone.utc),
    }
    payload = coordinator_module._mqtt_json(original)
    assert json.loads(payload) == json.loads(json.dumps(original, default=str))
    assert "客厅温度" in payload
    assert len(payload.encode("utf-8")) < len(json.dumps(original, default=str).encode("utf-8"))


def test_compact_json_keeps_lone_surrogates_encodable_without_losing_data():
    original = {"name": "客厅", "device_value": "before\ud800after"}
    payload = coordinator_module._mqtt_json(original)
    assert json.loads(payload.encode("utf-8")) == original


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix,qos,retain", [("catalog", 0, True), ("state/light.demo", 1, False)])
async def test_publish_checks_full_packet_boundary_before_client(coordinator, suffix, qos, retain):
    assert coordinator.mqtt_max_packet_size == PACKET_LIMIT
    topic = f"seenzus/v2/bridge/ha-demo/{suffix}"
    candidate = "x" * PACKET_LIMIT
    payload = candidate[:PACKET_LIMIT - (len(_paho_packet(topic, candidate, qos)) - PACKET_LIMIT)]
    assert len(_paho_packet(topic, payload, qos)) == PACKET_LIMIT
    client = AsyncFakeMQTTClient()

    await coordinator._publish(client, topic, payload, qos=qos, retain=retain)
    with pytest.raises(coordinator_module._MqttPublishFailure) as raised:
        await coordinator._publish(client, topic, payload + "x", qos=qos, retain=retain)

    assert isinstance(raised.value, coordinator_module._MqttPacketTooLarge)
    assert client.published == [{"topic": topic, "payload": payload, "qos": qos, "retain": retain}]


@pytest.mark.asyncio
async def test_publish_counts_utf8_bytes_not_python_characters(coordinator):
    client = AsyncFakeMQTTClient()
    payload = "中" * (PACKET_LIMIT // 3 + 1)
    assert len(payload) < PACKET_LIMIT < len(payload.encode("utf-8"))
    with pytest.raises(coordinator_module._MqttPublishFailure):
        await coordinator._publish(client, "seenzus/v2/bridge/ha-demo/state/温度", payload, qos=1)
    assert client.published == []


@pytest.mark.asyncio
async def test_large_verbose_service_result_compacts_without_changing_data(coordinator, monkeypatch):
    descriptions = {"light": {"turn_on": {
        "description": "中文说明" * 45_000,
        "fields": {"brightness": {"description": "完整保留", "selector": {"number": {"min": 0, "max": 255}}}},
    }}}

    async def descriptions_for_instance(_hass):
        return descriptions

    monkeypatch.setattr(service_helper, "async_get_all_descriptions", descriptions_for_instance)
    client = AsyncFakeMQTTClient()
    await coordinator._handle_v2_command(
        "services-large", json.dumps({"method": "GET", "path": "/api/services"}), client,
    )

    assert len(client.published) == 1
    publication = client.published[0]
    response = json.loads(publication["payload"])
    assert response["success"] is True
    assert response["status"] == 200
    assert response["data"] == descriptions
    # The old JSON format crosses the same 1 MiB boundary as the production failure.
    assert len(_paho_packet(publication["topic"], json.dumps(response), 1)) > PACKET_LIMIT
    assert len(_paho_packet(publication["topic"], publication["payload"], 1)) < PACKET_LIMIT
    assert coordinator.result_count == 1


@pytest.mark.asyncio
async def test_oversized_result_returns_explicit_413_without_sending_large_packet(coordinator):
    client = AsyncFakeMQTTClient()
    data = {"unbounded": "x" * PACKET_LIMIT}
    delivered = await coordinator._publish_result(client, "oversized", success=True, status=200, data=data)

    assert delivered is True
    assert len(client.published) == 1
    publication = client.published[0]
    response = json.loads(publication["payload"])
    assert response["msgId"] == "oversized"
    assert response["bridgeId"] == "ha-demo"
    assert response["success"] is False
    assert response["status"] == 413
    assert response["error"] == "response_too_large"
    assert "data" not in response
    assert response["maxPacketSize"] == PACKET_LIMIT
    original_response = {
        "msgId": "oversized", "bridgeId": "ha-demo", "success": True,
        "status": 200, "finishedAt": FINISHED_AT, "data": data,
    }
    expected_size = len(_paho_packet(
        publication["topic"], json.dumps(original_response, separators=(",", ":")), 1,
    ))
    assert response["packetSize"] == expected_size > PACKET_LIMIT
    assert len(_paho_packet(publication["topic"], publication["payload"], 1)) <= PACKET_LIMIT


@pytest.mark.asyncio
async def test_oversized_all_states_result_still_streams_every_complete_entity(coordinator):
    expected = {}
    for index in range(12):
        entity_id = f"light.room_{index}"
        attributes = {"friendly_name": f"房间 {index}", "device_data": "x" * 90_000}
        coordinator.hass.states.set(entity_id, state="on", attributes=attributes)
        expected[entity_id] = attributes
    client = AsyncFakeMQTTClient()
    await coordinator._handle_v2_command(
        "topic-request", json.dumps({"msgId": "original-request", "method": "GET", "path": "/api/states"}), client,
    )

    result = json.loads(client.published[0]["payload"])
    assert client.published[0]["topic"].endswith("/result/original-request")
    assert result["success"] is False
    assert result["status"] == 413
    assert result["error"] == "response_too_large"
    state_publications = [item for item in client.published if "/state/" in item["topic"]]
    assert len(state_publications) == len(expected)
    states = [json.loads(item["payload"]) for item in state_publications]
    assert {state["entityId"] for state in states} == set(expected)
    for state in states:
        assert state["source"] == "full_snapshot"
        assert state["correlationMsgId"] == "original-request"
        assert state["state"] == "on"
        assert state["attributes"] == expected[state["entityId"]]
    for publication in client.published:
        assert len(_paho_packet(publication["topic"], publication["payload"], publication["qos"])) <= PACKET_LIMIT

