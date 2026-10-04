"""Optional read protocol at the MQTT command/publication boundary."""
from __future__ import annotations

import json
import asyncio
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from seenzus_bridge import BridgeCoordinator, er
from seenzus_bridge.bridge_protocol import build_topics
from seenzus_bridge import coordinator as coordinator_module
from seenzus_bridge.ha_dispatcher import service_helper
from tests.helpers import AsyncFakeMQTTClient, FakeConfigEntry, FakeEntityRegistry, FakeHass


@pytest.fixture
def coordinator(monkeypatch):
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    instance = BridgeCoordinator(FakeHass(), FakeConfigEntry())
    instance._topics = build_topics("seenzus/v2", "ha-demo")
    instance._command_prefix = "seenzus/v2/bridge/ha-demo/command"
    return instance


async def request(coordinator, client, path, msg_id="read-1"):
    await coordinator._handle_message(
        f"seenzus/v2/bridge/ha-demo/command/{msg_id}",
        json.dumps({"msgId": msg_id, "method": "GET", "path": path}), client,
    )


def results(client):
    return [json.loads(item["payload"]) for item in client.published if "/result/" in item["topic"]]


@pytest.mark.asyncio
async def test_service_index_returns_all_names_without_service_descriptions(coordinator):
    coordinator.hass.services.async_services = lambda: {
        "light": {"turn_on": object(), "turn_off": object()},
        "homeassistant": {"restart": object()},
    }
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/seenzus/services/index")
    assert len(results(client)) == 1
    result = results(client)[0]
    assert result["success"] is True
    assert result["status"] == 200
    assert result["data"] == {
        "version": 1, "isComplete": True, "count": 3,
        "services": ["homeassistant.restart", "light.turn_off", "light.turn_on"],
    }


@pytest.mark.asyncio
async def test_snapshot_stream_keeps_full_states_without_aggregate_result(coordinator):
    coordinator.hass.states.set("sensor.a", state="unknown", attributes={"完整属性": [1, {"中文": "内容"}]})
    state = coordinator.hass.states.get("sensor.a")
    state.last_updated = datetime(2026, 10, 5, tzinfo=timezone.utc)
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/seenzus/states/snapshot")
    replies = results(client)
    assert [reply["status"] for reply in replies] == [202, 200]
    assert replies[0]["data"]["phase"] == "accepted"
    assert replies[0]["data"]["expectedCount"] == 1
    assert replies[1]["data"] == {
        **replies[0]["data"], "phase": "finished", "outcome": "complete",
        "sentCount": 1, "omittedCount": 0, "oversizedCount": 0,
    }
    states = [json.loads(item["payload"]) for item in client.published if "/state/" in item["topic"]]
    assert len(states) == 1
    assert states[0]["attributes"] == {"完整属性": [1, {"中文": "内容"}]}
    assert states[0]["state"] == "unknown"
    assert states[0]["available"] is True
    assert states[0]["ts"] == "2026-10-05T00:00:00+00:00"
    assert states[0]["source"] == "full_snapshot"
    assert states[0]["correlationMsgId"] == "read-1"
    assert states[0]["snapshotVersion"] == 1
    assert all("完整属性" not in json.dumps(reply, ensure_ascii=False) for reply in replies)


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", [{}, {"light": {"turn_on": object()}, "sensor": {}}])
async def test_service_index_equals_full_description_keys(coordinator, monkeypatch, registry):
    coordinator.hass.services.async_services = lambda: registry
    async def descriptions(_hass):
        return {domain: {name: {"description": "说明", "fields": {"x": {}}} for name in services}
                for domain, services in registry.items()}
    monkeypatch.setattr(service_helper, "async_get_all_descriptions", descriptions)
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/services", "old")
    await request(coordinator, client, "/api/seenzus/services/index", "new")
    full, index = [item["data"] for item in results(client)]
    expected = sorted(f"{domain}.{name}" for domain, services in full.items() for name in services)
    assert index == {"version": 1, "isComplete": True, "count": len(expected), "services": expected}


@pytest.mark.asyncio
@pytest.mark.parametrize("registry", [{"Bad Domain": {}}, {"light": {"bad.key": object()}}])
async def test_invalid_service_keys_reject_the_entire_index(coordinator, registry):
    coordinator.hass.services.async_services = lambda: registry
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/seenzus/services/index")
    assert results(client)[0]["success"] is False
    assert results(client)[0]["status"] == 500


@pytest.mark.asyncio
async def test_oversized_service_index_is_an_explicit_failure(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator_module, "MAX_MQTT_PACKET_SIZE", 1024)
    coordinator.hass.services.async_services = lambda: {"light": {f"service_{i}": object() for i in range(200)}}
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/seenzus/services/index")
    assert results(client)[0]["status"] == 413
    assert results(client)[0]["error"] == "response_too_large"
    assert "data" not in results(client)[0]


@pytest.mark.asyncio
async def test_empty_snapshot_has_explicit_empty_scope_and_completion(coordinator):
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/seenzus/states/snapshot")
    assert [item["status"] for item in results(client)] == [202, 200]
    assert results(client)[0]["data"] == {
        "version": 1, "scope": "publishable_states", "phase": "accepted", "expectedCount": 0,
        "entitiesSha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    }
    assert results(client)[1]["data"]["outcome"] == "complete"


@pytest.mark.asyncio
async def test_oversized_entity_is_reported_missing_while_remaining_states_continue(coordinator, monkeypatch, caplog):
    monkeypatch.setattr(coordinator_module, "MAX_MQTT_PACKET_SIZE", 1024)
    for entity in ["sensor.a", "sensor.large", "sensor.c"]:
        coordinator.hass.states.set(entity, attributes={"value": "private-data" * (200 if entity == "sensor.large" else 1)})
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/seenzus/states/snapshot")
    assert results(client)[0]["data"]["expectedCount"] == 3
    end = results(client)[1]
    assert end["success"] is False and end["status"] == 413
    assert end["data"]["outcome"] == "partial"
    assert (end["data"]["sentCount"], end["data"]["omittedCount"], end["data"]["oversizedCount"]) == (2, 1, 1)
    assert [json.loads(item["payload"])["entityId"] for item in client.published if "/state/" in item["topic"]] == ["sensor.a", "sensor.c"]
    assert "private-data" not in caplog.text
    assert all(coordinator_module._mqtt_publish_packet_size(item["topic"], item["payload"], item["qos"]) <= 1024 for item in client.published)


@pytest.mark.asyncio
async def test_snapshot_freezes_scope_and_state_before_async_publication(coordinator):
    coordinator.hass.states.set("sensor.a", state="on", attributes={"中文": "原始"})
    class ChangingClient(AsyncFakeMQTTClient):
        async def publish(self, *args, **kwargs):
            await super().publish(*args, **kwargs)
            if len(self.published) == 1:
                coordinator.hass.states.set("sensor.a", state="off", attributes={"中文": "后续"})
                coordinator.hass.states.set("sensor.b")
    client = ChangingClient()
    await request(coordinator, client, "/api/seenzus/states/snapshot")
    assert results(client)[0]["data"]["expectedCount"] == 1
    assert results(client)[0]["data"]["entitiesSha256"] == hashlib.sha256(b"sensor.a\n").hexdigest()
    state = json.loads(client.published[1]["payload"])
    assert state["state"] == "on" and state["attributes"] == {"中文": "原始"}


@pytest.mark.asyncio
async def test_duplicate_and_overlapping_requests_do_not_start_parallel_streams(coordinator):
    coordinator.hass.states.set("sensor.a")
    entered, resume = asyncio.Event(), asyncio.Event()
    class PausedClient(AsyncFakeMQTTClient):
        async def publish(self, topic, payload, **kwargs):
            if "/state/" in topic:
                entered.set()
                await resume.wait()
            await super().publish(topic, payload, **kwargs)
    client = PausedClient()
    first = asyncio.create_task(request(coordinator, client, "/api/seenzus/states/snapshot", "first"))
    await entered.wait()
    await request(coordinator, client, "/api/seenzus/states/snapshot", "first")
    await request(coordinator, client, "/api/seenzus/states/snapshot", "second")
    await request(coordinator, client, "/api/states", "legacy")
    assert [(r["msgId"], r["status"]) for r in results(client)] == [("first", 202), ("second", 409), ("legacy", 409)]
    resume.set()
    await first
    await request(coordinator, client, "/api/seenzus/states/snapshot", "first")
    assert len([item for item in client.published if "/state/" in item["topic"]]) == 1
    assert results(client)[-1]["data"]["outcome"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["transport", "timeout", "cancel"])
async def test_interrupted_snapshot_never_claims_completion_and_releases_single_flight(coordinator, monkeypatch, failure):
    monkeypatch.setattr(coordinator_module, "SNAPSHOT_STREAM_TIMEOUT_SECONDS", 0.01)
    coordinator.hass.states.set("sensor.a")
    class InterruptedClient(AsyncFakeMQTTClient):
        async def publish(self, topic, payload, **kwargs):
            if "/state/" in topic:
                if failure == "transport":
                    raise RuntimeError("private payload must not be logged")
                if failure == "cancel":
                    raise asyncio.CancelledError
                await asyncio.Event().wait()
            await super().publish(topic, payload, **kwargs)
    client = InterruptedClient()
    if failure == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await request(coordinator, client, "/api/seenzus/states/snapshot")
        assert len(results(client)) == 1
    else:
        await request(coordinator, client, "/api/seenzus/states/snapshot")
        assert results(client)[-1]["data"]["outcome"] == "failed"
        assert results(client)[-1]["data"]["omittedCount"] == 1
    next_client = AsyncFakeMQTTClient()
    await request(coordinator, next_client, "/api/seenzus/states/snapshot", "next")
    assert results(next_client)[-1]["data"]["outcome"] == "complete"


@pytest.mark.asyncio
async def test_legacy_request_retains_full_aggregate_and_state_semantics(coordinator):
    coordinator.hass.states.set("sensor.a", attributes={"完整": [1, 2]})
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/states")
    assert results(client)[0]["data"] == [{"entity_id": "sensor.a", "state": "on", "attributes": {"完整": [1, 2]}}]
    assert "snapshotVersion" not in json.loads(client.published[-1]["payload"])


@pytest.mark.asyncio
async def test_snapshot_matches_the_cross_repository_wire_fixture(coordinator):
    fixture = json.loads((Path(__file__).parent / "fixtures/compact_reads_v1.json").read_text(encoding="utf-8"))
    for message in fixture["messages"]:
        payload = message["payload"]
        if "/state/" in message["topic"]:
            coordinator.hass.states.set(payload["entityId"], state=payload["state"], attributes=payload["attributes"])
            state = coordinator.hass.states.get(payload["entityId"])
            state.last_updated = state.last_changed = datetime.fromisoformat(payload["ts"])
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/seenzus/states/snapshot", fixture["requestId"])
    actual = []
    for message in client.published:
        payload = json.loads(message["payload"])
        payload.pop("finishedAt", None)
        actual.append({**message, "payload": payload})
    assert actual == fixture["messages"]


@pytest.mark.asyncio
async def test_snapshot_filters_the_same_scope_as_legacy_state_publications(coordinator):
    coordinator.hass.states.set("sensor.a")
    coordinator.hass.states.set("sensor.seenzus_mqtt_bridge_status")
    coordinator.hass.states.set("sensor.model", attributes={"friendly_name": "Temperature T123*"})
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/states", "old")
    await request(coordinator, client, "/api/seenzus/states/snapshot", "new")
    states = [json.loads(item["payload"]) for item in client.published if "/state/" in item["topic"]]
    assert [state["entityId"] for state in states if state["correlationMsgId"] == "old"] == ["sensor.a"]
    assert [state["entityId"] for state in states if state["correlationMsgId"] == "new"] == ["sensor.a"]
    assert results(client)[-1]["data"]["expectedCount"] == 1


@pytest.mark.asyncio
async def test_scope_limit_is_explicit_without_silently_truncating(coordinator, monkeypatch):
    monkeypatch.setattr(coordinator_module, "MAX_SNAPSHOT_ENTITIES", 1)
    coordinator.hass.states.set("sensor.a")
    coordinator.hass.states.set("sensor.b")
    client = AsyncFakeMQTTClient()
    await request(coordinator, client, "/api/seenzus/states/snapshot")
    assert len(client.published) == 1
    assert results(client)[0]["status"] == 413
    assert results(client)[0]["error"] == "snapshot_scope_too_large"
