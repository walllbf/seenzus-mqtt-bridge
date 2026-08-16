from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from seenzus_bridge import BridgeCoordinator, er
from seenzus_bridge.coordinator import MAX_INFLIGHT_COMMANDS
from seenzus_bridge.operation_store import PersistentOperationStore
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


class _MemoryStorage:
    def __init__(self, payload=None) -> None:
        self.payload = payload

    async def async_load(self):
        return self.payload

    async def async_save(self, value):
        self.payload = value


class _FailOnceStorage(_MemoryStorage):
    async def async_save(self, value):
        self.payload = value
        if not hasattr(self, "failed"):
            self.failed = True
            raise RuntimeError("simulated storage acknowledgement failure")


def _persistent_store(storage=None):
    storage = storage or _MemoryStorage()
    return PersistentOperationStore(object(), "entry", storage), storage


def _operation_fingerprint(method, path, body):
    canonical = json.dumps({"method": method.upper(), "path": path, "body": body}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class _FailingPublishClient(AsyncFakeMQTTClient):
    """Client whose publish always raises (broker gone mid-flight)."""

    async def publish(self, topic: str, payload: str, *, qos: int, retain: bool = False) -> None:
        raise RuntimeError("broker gone")


@pytest.mark.asyncio
async def test_operation_key_replays_without_calling_home_assistant_twice(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()
    command_coordinator._operation_store, _ = _persistent_store()
    raw = json.dumps({"msgId": "msg-first", "operationKey": "opaque-op-a", "method": "POST", "path": "/api/services/light/turn_on", "body": {"entity_id": "light.demo"}})
    await command_coordinator._handle_v2_command("msg-first", raw, client)
    retry = json.dumps({"msgId": "msg-retry", "operationKey": "opaque-op-a", "method": "POST", "path": "/api/services/light/turn_on", "body": {"entity_id": "light.demo"}})
    await command_coordinator._handle_v2_command("msg-retry", retry, client)
    assert len(command_coordinator.hass.services.calls) == 1
    replay = json.loads([item for item in client.published if item["topic"].endswith("/msg-retry")][0]["payload"])
    assert replay["success"] is True


@pytest.mark.asyncio
async def test_dispatched_operation_is_frozen_as_unknown_after_restart(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()
    fingerprint = _operation_fingerprint("POST", "/api/services/light/turn_on", {"entity_id": "light.demo"})
    storage = _MemoryStorage({"opaque-op-a": {"fingerprint": fingerprint, "status": "dispatched", "result": None}})
    command_coordinator._operation_store, _ = _persistent_store(storage)
    raw = json.dumps({"msgId": "msg-retry", "operationKey": "opaque-op-a", "method": "POST", "path": "/api/services/light/turn_on", "body": {"entity_id": "light.demo"}})
    await command_coordinator._handle_v2_command("msg-retry", raw, client)
    assert command_coordinator.hass.services.calls == []
    result = json.loads(client.published[0]["payload"])
    assert result["error"] == "control_outcome_unknown"


@pytest.mark.asyncio
async def test_persistent_operation_store_replays_after_store_reconstruction() -> None:
    first, storage = _persistent_store()
    assert await first.claim("opaque-op", "fingerprint") == ("claimed", None)
    assert await first.mark_dispatched("opaque-op", "fingerprint") is True
    assert await first.complete("opaque-op", "fingerprint", {"success": True, "status": 200}) is True
    second, _ = _persistent_store(storage)
    assert await second.claim("opaque-op", "fingerprint") == ("completed", {"success": True, "status": 200})


@pytest.mark.asyncio
async def test_pre_dispatch_claim_is_recovered_after_store_reconstruction() -> None:
    first, storage = _persistent_store()
    assert await first.claim("opaque-op", "fingerprint") == ("claimed", None)
    assert await first.claim("opaque-op", "fingerprint") == ("pending", None)
    restarted, _ = _persistent_store(storage)
    assert await restarted.claim("opaque-op", "fingerprint") == ("claimed", None)
    assert await restarted.mark_dispatched("opaque-op", "fingerprint") is True


@pytest.mark.asyncio
async def test_failed_claim_save_does_not_leave_local_claim_stuck() -> None:
    store, _ = _persistent_store(_FailOnceStorage())
    with pytest.raises(RuntimeError, match="storage acknowledgement failure"):
        await store.claim("opaque-op", "fingerprint")
    assert await store.claim("opaque-op", "fingerprint") == ("claimed", None)


@pytest.mark.asyncio
async def test_operation_store_rejects_fingerprint_conflict() -> None:
    store, _ = _persistent_store()
    assert await store.claim("opaque-op", "fingerprint-a") == ("claimed", None)
    assert await store.claim("opaque-op", "fingerprint-b") == ("conflict", None)


@pytest.mark.asyncio
async def test_operation_store_freezes_malformed_rows_as_unknown() -> None:
    storage = _MemoryStorage({
        "missing-status": {"fingerprint": "fingerprint"},
        "bad-result": {"fingerprint": "fingerprint", "status": "completed", "result": "invalid"},
    })
    store, _ = _persistent_store(storage)
    assert await store.claim("missing-status", "fingerprint") == ("unknown", None)
    assert await store.claim("bad-result", "fingerprint") == ("unknown", None)
    assert await store.claim("new-key", "fingerprint") == ("claimed", None)
    assert storage.payload["missing-status"]["status"] == "unknown"
    assert storage.payload["bad-result"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_completed_operation_remains_a_tombstone_after_retention_window() -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=365)).isoformat()
    storage = _MemoryStorage({
        "old-completed": {"fingerprint": "a", "status": "completed", "result": {"success": True}, "updated_at": old},
        "old-unknown": {"fingerprint": "b", "status": "dispatched", "result": None, "updated_at": old},
    })
    store, _ = _persistent_store(storage)
    assert await store.claim("old-completed", "a") == ("completed", {"success": True})
    assert await store.claim("old-unknown", "b") == ("unknown", None)
    assert set(storage.payload) == {"old-completed", "old-unknown"}


@pytest.mark.asyncio
async def test_concurrent_same_key_dispatches_home_assistant_once(command_coordinator) -> None:
    client = AsyncFakeMQTTClient()
    command_coordinator._operation_store, _ = _persistent_store()
    raw = json.dumps({"operationKey": "opaque-op-a", "method": "POST", "path": "/api/services/light/turn_on", "body": {"entity_id": "light.demo"}})
    await asyncio.gather(
        command_coordinator._handle_v2_command("msg-first", raw, client),
        command_coordinator._handle_v2_command("msg-retry", raw, client),
    )
    assert len(command_coordinator.hass.services.calls) == 1


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


@pytest.mark.asyncio
async def test_command_scheduling_has_a_hard_inflight_limit(command_coordinator, monkeypatch) -> None:
    client = AsyncFakeMQTTClient()
    release = asyncio.Event()

    async def blocked_handler(_topic, _raw, _client) -> None:
        await release.wait()

    monkeypatch.setattr(command_coordinator, "_handle_message", blocked_handler)

    accepted = [
        command_coordinator._schedule_message(f"topic/{index}", "{}", client)
        for index in range(MAX_INFLIGHT_COMMANDS + 1)
    ]

    assert accepted == ([True] * MAX_INFLIGHT_COMMANDS) + [False]
    assert len(command_coordinator._command_tasks) == MAX_INFLIGHT_COMMANDS
    assert command_coordinator.last_error == "command_overload"

    release.set()
    await asyncio.gather(*tuple(command_coordinator._command_tasks))
    await asyncio.sleep(0)
    assert command_coordinator._command_tasks == set()
