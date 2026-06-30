from __future__ import annotations

import json

import pytest

from seenzus_bridge import BridgeCoordinator, er
from seenzus_bridge.bridge_protocol import build_topics
from tests.helpers import AsyncFakeMQTTClient, FakeConfigEntry, FakeEntityRegistry, FakeHass


@pytest.fixture
def command_coordinator(monkeypatch):
    hass = FakeHass()
    entry = FakeConfigEntry(data={"mqtt_host": "broker.example.com", "topic_root": "seenzus/v2"})
    registry = FakeEntityRegistry()
    monkeypatch.setattr(er, "async_get", lambda _hass: registry)
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._topics = build_topics("seenzus/v2", "ha-demo")
    return coordinator


class _FailingPublishClient(AsyncFakeMQTTClient):
    """Client whose publish always raises (broker gone mid-flight)."""

    async def publish(self, topic: str, payload: str, *, qos: int, retain: bool = False) -> None:
        raise RuntimeError("broker gone")


@pytest.mark.asyncio
async def test_handle_v2_command_invalid_json_returns_400_result(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()

    await command_coordinator._handle_v2_command("msg-1", "{broken", client)

    payload = json.loads(client.published[0]["payload"])
    assert client.published[0]["topic"] == "seenzus/v2/bridge/ha-demo/result/msg-1"
    assert payload["success"] is False
    assert payload["status"] == 400
    assert payload["error"] == "invalid_json"


@pytest.mark.asyncio
async def test_handle_v2_command_publishes_result_and_followup_state(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()
    command_coordinator.hass.states.set("light.demo", state="on", attributes={"brightness": 99})
    raw = json.dumps(
        {
            "msgId": "msg-2",
            "method": "GET",
            "path": "/api/states/light.demo",
        }
    )

    await command_coordinator._handle_v2_command("msg-2", raw, client)

    assert client.published[0]["topic"] == "seenzus/v2/bridge/ha-demo/result/msg-2"
    assert client.published[1]["topic"] == "seenzus/v2/bridge/ha-demo/state/light.demo"
    state_payload = json.loads(client.published[1]["payload"])
    assert state_payload["correlationMsgId"] == "msg-2"
    assert state_payload["entityId"] == "light.demo"


@pytest.mark.asyncio
async def test_msgid_precedence_payload_msgid_wins_over_correlation_and_topic(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()
    command_coordinator.hass.states.set("light.demo", state="on")
    raw = json.dumps(
        {
            "msgId": "payload-id",
            "correlationId": "corr-id",
            "method": "GET",
            "path": "/api/states/light.demo",
        }
    )

    await command_coordinator._handle_v2_command("topic-id", raw, client)

    # Result topic uses the EFFECTIVE msgId (invariant 4), not the topic segment.
    assert client.published[0]["topic"] == "seenzus/v2/bridge/ha-demo/result/payload-id"
    assert json.loads(client.published[0]["payload"])["msgId"] == "payload-id"
    state_payload = json.loads(client.published[1]["payload"])
    assert state_payload["correlationMsgId"] == "payload-id"


@pytest.mark.asyncio
async def test_msgid_precedence_correlation_id_wins_over_topic_segment(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()
    command_coordinator.hass.states.set("light.demo", state="on")
    raw = json.dumps(
        {
            "correlationId": "corr-id",
            "method": "GET",
            "path": "/api/states/light.demo",
        }
    )

    await command_coordinator._handle_v2_command("topic-id", raw, client)

    assert client.published[0]["topic"] == "seenzus/v2/bridge/ha-demo/result/corr-id"
    assert json.loads(client.published[0]["payload"])["msgId"] == "corr-id"


@pytest.mark.asyncio
async def test_msgid_falls_back_to_topic_segment_when_payload_has_no_ids(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()
    command_coordinator.hass.states.set("light.demo", state="on")
    raw = json.dumps({"method": "GET", "path": "/api/states/light.demo"})

    await command_coordinator._handle_v2_command("topic-id", raw, client)

    assert client.published[0]["topic"] == "seenzus/v2/bridge/ha-demo/result/topic-id"
    assert json.loads(client.published[0]["payload"])["msgId"] == "topic-id"


@pytest.mark.asyncio
async def test_full_snapshot_states_use_qos0_while_result_uses_qos1(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()
    command_coordinator.hass.states.set("light.living_room", state="on")
    raw = json.dumps({"msgId": "snap-qos-1", "method": "GET", "path": "/api/states"})

    await command_coordinator._handle_v2_command("snap-qos-1", raw, client)

    result_messages = [item for item in client.published if "/result/" in item["topic"]]
    assert [item["qos"] for item in result_messages] == [1]
    state_messages = [item for item in client.published if "/state/" in item["topic"]]
    assert state_messages, "full snapshot should publish at least one state"
    assert all(item["qos"] == 0 for item in state_messages)
    assert all(
        json.loads(item["payload"])["source"] == "full_snapshot" for item in state_messages
    )


@pytest.mark.asyncio
async def test_publish_result_failure_counts_error_once_and_does_not_raise(command_coordinator) -> None:
    client = _FailingPublishClient()

    await command_coordinator._publish_result(
        client, "msg-fail", success=True, status=200, data={"ok": True}
    )

    assert command_coordinator.err_count == 1
    assert command_coordinator.result_count == 0
    assert command_coordinator.last_error.startswith("result_publish_failed:")


@pytest.mark.asyncio
async def test_last_req_is_timezone_aware_after_command(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()
    command_coordinator.hass.states.set("light.demo", state="on")
    raw = json.dumps({"msgId": "tz-1", "method": "GET", "path": "/api/states/light.demo"})

    await command_coordinator._handle_v2_command("tz-1", raw, client)

    assert command_coordinator.last_req is not None
    assert command_coordinator.last_req.tzinfo is not None
