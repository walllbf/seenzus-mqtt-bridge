"""ADR0153 source contract tests. WRITTEN ONLY; NOT EXECUTED in this task.

Automatic approval rejected execution of external code; the main session then
narrowed this task to static edits. Execution, import and compilation are outside
that boundary. A separately authorized upstream environment must run this suite.
"""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from seenzus_bridge import interactive_remote as remote
from seenzus_bridge.device_access import SAFE_SERVICES


@pytest_asyncio.fixture(params=["tv_remote", "dangbei_local"])
async def source(monkeypatch, request):
    profile = request.param
    clock = SimpleNamespace(wall=1800000000000, mono=1000.0)
    monkeypatch.setattr(remote, "_ms", lambda: clock.wall)
    monkeypatch.setattr(remote.time, "monotonic", lambda: clock.mono)
    entity = SimpleNamespace(entity_id="remote.renamed", domain="remote", id="stable-registry-id", device_id="physical",
                             platform=profile, config_entry_id="reviewed", hidden_by=None, disabled_by=None)
    switch = SimpleNamespace(entity_id="switch.arbitrary", domain="switch", id="switch-registry-id", device_id="physical", platform=profile)
    registry = SimpleNamespace(entities={entity.entity_id: entity, switch.entity_id: switch},
                               async_get=lambda entity_id: {entity.entity_id: entity, switch.entity_id: switch}.get(entity_id))
    monkeypatch.setattr(remote.er, "async_get", lambda _hass: registry)
    monkeypatch.setattr(remote.loader, "async_get_integration", AsyncMock(return_value=SimpleNamespace(version=remote.PROFILES[profile][0])))
    conf = {"device_scope": "all"}
    hass = SimpleNamespace(data={}, services=SimpleNamespace(has_service=lambda *_: True),
                           config_entries=SimpleNamespace(async_get_entry=lambda _: SimpleNamespace(domain=profile)),
                           states=SimpleNamespace(get=lambda _: SimpleNamespace(state="on")))
    replies, calls, tasks = [], [], set()
    gate = asyncio.Event()

    async def emit(client, msg_id, data, _entity_id=None):
        replies.append((msg_id, copy.deepcopy(data)))

    async def dispatch(service, data):
        calls.append((service, data))
        await gate.wait()
        return SimpleNamespace(status=200)

    def create_task(coro):
        task = asyncio.create_task(coro)
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        return task

    engine = remote.InteractiveRemotes(hass, lambda: conf, dispatch, emit, create_task)
    client = object()
    engine.reset(client)
    binding = {"principal": "a" * 64, "deviceId": "canonical", "entityId": entity.entity_id, "capabilityVersion": remote.PROFILES[profile][1]}

    async def opened():
        await engine.handle({**binding, "kind": "open", "nonce": "nonce_synthetic_1234", "sentAt": clock.wall}, client, "open")
        return {key: replies[-1][1][key] for key in ("sessionId", "generation", "epoch", "expiresAt")}

    def press(handle, sequence, action="up", **patch):
        command = remote.PROFILES[profile][2].get(action)
        return {**binding, "kind": "press", "session": handle, "pressId": f"press_synthetic_{sequence:04}", "sequence": sequence,
                "action": action, "service": "remote.turn_off" if action == "powerOff" else "remote.turn_on" if action == "powerOn" else "remote.send_command",
                "data": {"command": command} if command else {}, "issuedAt": clock.wall, "expiresAt": clock.wall + 1500, **patch}

    async def drain():
        for _ in range(20):
            await asyncio.sleep(0)

    yield SimpleNamespace(engine=engine, client=client, binding=binding, opened=opened, press=press, replies=replies,
                          calls=calls, gate=gate, drain=drain, clock=clock, conf=conf, entity=entity, hass=hass, tasks=tasks, profile=profile)
    engine.reset()
    gate.set()
    if tasks:
        await asyncio.gather(*tuple(tasks), return_exceptions=True)


@pytest.mark.asyncio
async def test_slow_ha_does_not_block_admission_but_preserves_physical_order(source):
    s = source
    handle = await s.opened()
    for seq, action in enumerate(("down", "left", "ok"), 1):
        await s.engine.handle(s.press(handle, seq, action), s.client, str(seq))
    await s.drain()
    assert [reply[1]["phase"] for reply in s.replies[1:]] == ["admitted"] * 3
    assert [data["command"] for _, data in s.calls] == ["down"]
    s.gate.set()
    await s.drain()
    assert [data["command"] for _, data in s.calls] == ["down", "left", "ok"]


@pytest.mark.asyncio
async def test_cross_cloud_duplicate_returns_old_admission_without_second_execution(source):
    s = source
    handle = await s.opened()
    body = s.press(handle, 1)
    await s.engine.handle(body, s.client, "cloud-a")
    await s.engine.handle(body, s.client, "cloud-b")
    s.gate.set()
    await s.drain()
    assert len(s.calls) == 1
    assert ("cloud-b", {"version": 1, **handle, "pressId": body["pressId"], "sequence": 1, "phase": "admitted"}) in s.replies


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["gap", "expiry", "capacity", "source_auth", "clock_jump"])
async def test_invalid_sequence_discards_waiting_ok(source, cause):
    s = source
    handle = await s.opened()
    await s.engine.handle(s.press(handle, 1, "down"), s.client, "first")
    await s.drain()
    await s.engine.handle(s.press(handle, 2, "left"), s.client, "waiting")
    seq = 4 if cause == "gap" else 3
    patch = {"expiresAt": s.clock.wall - 1} if cause == "expiry" else {}
    if cause == "capacity":
        for index in range(3, 8):
            await s.engine.handle(s.press(handle, index, "down"), s.client, str(index))
        seq = 8
    if cause == "source_auth":
        s.conf["device_scope"] = "selected"
    if cause == "clock_jump":
        s.clock.wall += 2000
    await s.engine.handle(s.press(handle, seq, "ok", **patch), s.client, "last")
    s.gate.set()
    await s.drain()
    assert len(s.calls) == 1
    assert any(reply.get("phase") == "rejected" for _, reply in s.replies)


@pytest.mark.asyncio
async def test_waiting_deadline_is_original_not_refreshed_on_execution(source):
    s = source
    handle = await s.opened()
    await s.engine.handle(s.press(handle, 1, "down"), s.client, "first")
    await s.drain()
    await s.engine.handle(s.press(handle, 2, "ok"), s.client, "second")
    s.clock.wall += 1600
    s.clock.mono += 1.6
    s.gate.set()
    await s.drain()
    assert len(s.calls) == 1


@pytest.mark.asyncio
async def test_explicit_ha_execution_rejection_discards_the_waiting_tail(source):
    s = source
    handle = await s.opened()
    async def reject(service, data):
        s.calls.append((service, data))
        await s.gate.wait()
        return SimpleNamespace(status=403)
    s.engine.dispatch = reject
    await s.engine.handle(s.press(handle, 1, "down"), s.client, "first")
    await s.drain()
    await s.engine.handle(s.press(handle, 2, "ok"), s.client, "second")
    s.gate.set()
    await s.drain()
    assert len(s.calls) == 1
    assert any(reply.get("phase") == "rejected" for _, reply in s.replies)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["close", "disconnect", "replace"])
async def test_lifecycle_drops_waiting_but_keeps_running_fence(source, reason):
    s = source
    handle = await s.opened()
    await s.engine.handle(s.press(handle, 1, "down"), s.client, "first")
    await s.drain()
    await s.engine.handle(s.press(handle, 2, "ok"), s.client, "second")
    if reason == "close":
        await s.engine.handle({**s.binding, "kind": "close", "session": handle}, s.client, "close")
    elif reason == "disconnect":
        s.engine.reset()
    else:
        await s.opened()
    assert s.hass.data["seenzus_interactive_remote_fences_v1"]["physical"].lock.locked()
    s.gate.set()
    await s.drain()
    assert len(s.calls) == 1
    assert not s.hass.data["seenzus_interactive_remote_fences_v1"]


@pytest.mark.asyncio
async def test_old_switch_entry_is_serialized_without_claiming_function_equivalence(source):
    s = source
    handle = await s.opened()
    await s.engine.handle(s.press(handle, 1, "down"), s.client, "first")
    await s.drain()
    legacy = AsyncMock(return_value=SimpleNamespace(status=200))
    task = asyncio.create_task(s.engine.dispatch_legacy("POST", "/api/services/switch/turn_on", {"entity_id": "switch.arbitrary"}, legacy))
    await s.drain()
    legacy.assert_not_called()
    s.gate.set()
    await task
    legacy.assert_awaited_once()


@pytest.mark.asyncio
async def test_selected_authorization_is_not_expanded_and_old_connection_handle_fails(source):
    s = source
    handle = await s.opened()
    s.engine.reset(s.client)
    await s.engine.handle(s.press(handle, 1), s.client, "old-epoch")
    assert s.replies[-1][1]["phase"] == "rejected"
    s.conf["device_scope"] = "selected"
    assert s.engine.declaration()["authorized"] is False
    with pytest.raises(remote.RemoteDenied):
        await s.opened()
    assert "remote" not in SAFE_SERVICES and "button" not in SAFE_SERVICES
    assert not s.calls


@pytest.mark.asyncio
async def test_unknown_integration_version_and_clock_fail_closed(source, monkeypatch):
    s = source
    monkeypatch.setattr(remote.loader, "async_get_integration", AsyncMock(return_value=SimpleNamespace(version="9.9.9")))
    with pytest.raises(remote.RemoteDenied, match="integration_version_unknown"):
        await s.opened()
    with pytest.raises(remote.RemoteDenied, match="clock_unverified"):
        await s.engine.handle({**s.binding, "kind": "open", "nonce": "nonce_synthetic_1234", "sentAt": s.clock.wall - 1000}, s.client, "stale")
    assert not s.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["unavailable", "removed", "grant_changed"])
async def test_delivered_owner_close_drops_waiting_even_when_remote_is_no_longer_usable(source, condition):
    s = source
    handle = await s.opened()
    await s.engine.handle(s.press(handle, 1, "down"), s.client, "first")
    await s.drain()
    await s.engine.handle(s.press(handle, 2, "ok"), s.client, "waiting")
    original = s.hass.states.get
    if condition == "grant_changed":
        s.conf["device_scope"] = "selected"
    else:
        s.hass.states.get = lambda _: None if condition == "removed" else SimpleNamespace(state="unavailable")
    await s.engine.handle({**s.binding, "kind": "close", "session": handle}, s.client, "close")
    assert s.replies[-1][1] == {"closed": True}
    assert not s.engine.queues.get("physical")
    assert s.hass.data["seenzus_interactive_remote_fences_v1"]["physical"].lock.locked()
    s.hass.states.get = original
    s.conf["device_scope"] = "all"
    s.gate.set()
    await s.drain()
    assert len(s.calls) == 1


@pytest.mark.asyncio
async def test_other_principal_cannot_close_an_owned_session(source):
    s = source
    handle = await s.opened()
    await s.engine.handle(s.press(handle, 1), s.client, "first")
    await s.drain()
    await s.engine.handle(s.press(handle, 2, "ok"), s.client, "waiting")
    with pytest.raises(remote.RemoteDenied):
        await s.engine.handle({**s.binding, "principal": "b" * 64, "kind": "close", "session": handle}, s.client, "foreign")
    assert len(s.engine.queues["physical"]) == 1
    s.gate.set()
    await s.drain()
    assert len(s.calls) == 2


@pytest.mark.asyncio
async def test_status_preserves_known_rejection_after_entity_becomes_unavailable(source):
    s = source
    handle = await s.opened()
    await s.engine.handle(s.press(handle, 1), s.client, "first")
    await s.drain()
    waiting = s.press(handle, 2, "ok")
    await s.engine.handle(waiting, s.client, "waiting")
    await s.engine.handle({**s.binding, "kind": "close", "session": handle}, s.client, "close")
    s.hass.states.get = lambda _: SimpleNamespace(state="unavailable")
    await s.engine.handle({**s.binding, "kind": "status", "session": handle, "pressId": waiting["pressId"], "sequence": 2}, s.client, "status")
    assert s.replies[-1][1]["phase"] == "rejected"
    with pytest.raises(remote.RemoteDenied):
        await s.engine.handle({**s.binding, "kind": "status", "session": handle, "pressId": waiting["pressId"], "sequence": 1}, s.client, "wrong-sequence")
    s.gate.set()
    await s.drain()


@pytest.mark.asyncio
async def test_real_coordinator_message_route_propagates_non_replay_transport_eligibility(monkeypatch):
    import json
    from seenzus_bridge import BridgeCoordinator
    from seenzus_bridge.bridge_protocol import build_topics
    from tests.helpers import FakeHass, FakeConfigEntry, AsyncFakeMQTTClient
    coordinator = BridgeCoordinator(FakeHass(), FakeConfigEntry())
    coordinator._topics = build_topics("synthetic", "local")
    coordinator._command_prefix = coordinator._topics.command_sub[:-2]
    coordinator.mqtt_connected = True
    receive = AsyncMock()
    coordinator._interactive_remotes = SimpleNamespace(handle=receive)
    client = AsyncFakeMQTTClient()
    raw = json.dumps({"msgId": "request", "method": "POST", "path": remote.PATH, "body": {"kind": "open"}})
    await coordinator._handle_message(f"{coordinator._command_prefix}/request", raw, client, interactive_eligible=True)
    receive.assert_awaited_once_with({"kind": "open"}, client, "request")
    receive.reset_mock()
    await coordinator._handle_message(f"{coordinator._command_prefix}/request", raw, client)
    receive.assert_not_awaited()
    assert json.loads(client.published[-1]["payload"])["error"] == "remote_not_ready"


@pytest.mark.asyncio
async def test_reviewed_profile_volume_mapping_and_unsupported_dangbei_key_fail_closed(source):
    s = source
    handle = await s.opened()
    await s.engine.handle(s.press(handle, 1, "volumeUp"), s.client, "volume")
    await s.drain()
    assert s.calls == [("remote.send_command", {"entity_id": "remote.renamed", "command": "volume_up" if s.profile == "dangbei_local" else "volup"})]
    if s.profile == "dangbei_local":
        await s.engine.handle(s.press(handle, 2, "mute"), s.client, "unsupported")
        assert s.replies[-1][1]["phase"] == "rejected"
        assert s.replies[-1][1]["reason"] == "unknown_action"
    s.gate.set()
    await s.drain()
    assert len(s.calls) == 1


@pytest.mark.asyncio
async def test_explicit_power_off_replaces_waiting_navigation_without_crossing_running_action(source):
    s = source
    old = await s.opened()
    await s.engine.handle(s.press(old, 1, "down"), s.client, "running")
    await s.drain()
    await s.engine.handle(s.press(old, 2, "ok"), s.client, "waiting")
    fresh = await s.opened()
    await s.engine.handle(s.press(fresh, 1, "powerOff", pressId="power_off_synthetic_1234"), s.client, "power")
    await s.drain()
    assert len(s.calls) == 1
    # A late rejection for the old queue can follow the new admission. Correlate
    # this explicit power request rather than inspecting a different key's reply.
    assert [reply["phase"] for msg_id, reply in s.replies if msg_id == "power"] == ["admitted"]
    s.gate.set()
    await s.drain()
    assert s.calls == [("remote.send_command", {"entity_id": "remote.renamed", "command": "down"}),
                       ("remote.turn_off", {"entity_id": "remote.renamed"})]
    assert any(reply.get("pressId") == "press_synthetic_0002" and reply.get("phase") == "rejected" for _, reply in s.replies)


@pytest.mark.asyncio
async def test_execution_rechecks_source_grant_without_another_incoming_request(source):
    s = source
    handle = await s.opened()
    await s.engine.handle(s.press(handle, 1, "down"), s.client, "running")
    await s.drain()
    await s.engine.handle(s.press(handle, 2, "ok"), s.client, "waiting")
    s.conf["device_scope"] = "selected"
    s.gate.set()
    await s.drain()
    assert len(s.calls) == 1
    assert any(reply.get("pressId") == "press_synthetic_0002" and reply.get("phase") == "rejected" for _, reply in s.replies)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["dangbei_local"], indirect=True)
@pytest.mark.parametrize("state", ["off", "unknown", "unavailable"])
async def test_dangbei_connection_state_does_not_grant_an_open_session(source, state):
    s = source
    s.hass.states.get = lambda _: SimpleNamespace(state=state)
    with pytest.raises(remote.RemoteDenied, match="remote_unavailable"):
        await s.opened()
    assert not s.calls


@pytest.mark.asyncio
async def test_coordinator_routes_reviewed_profile_to_real_engine_dispatch_and_result(source):
    import json
    from seenzus_bridge import BridgeCoordinator
    from seenzus_bridge.bridge_protocol import build_topics
    from tests.helpers import FakeHass, FakeConfigEntry, AsyncFakeMQTTClient

    s = source
    hass = FakeHass()
    hass.states.set(s.entity.entity_id, state="on")
    coordinator = BridgeCoordinator(hass, FakeConfigEntry(data={"device_scope": "all"}))
    coordinator._topics = build_topics("synthetic", "local")
    coordinator._command_prefix = coordinator._topics.command_sub[:-2]
    coordinator.mqtt_connected = True
    client = AsyncFakeMQTTClient()
    coordinator._mqtt_client = client
    coordinator._interactive_remotes = s.engine
    s.engine.reset(client)
    s.engine.dispatch = coordinator._dispatch_interactive_remote
    s.engine.emit = coordinator._emit_interactive_remote
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(coordinator, "_publish_states_for_entities", AsyncMock())
    calls = []

    async def service(domain, action, data, *, blocking):
        calls.append((domain, action, data, blocking))
        await s.gate.wait()

    monkeypatch.setattr(hass.services, "async_call", service)

    async def route(msg_id, body, eligible=True):
        raw = json.dumps({"msgId": msg_id, "method": "POST", "path": remote.PATH, "body": body})
        await coordinator._handle_message(f"{coordinator._command_prefix}/{msg_id}", raw, client, interactive_eligible=eligible)

    try:
        await route("open", {**s.binding, "kind": "open", "nonce": "nonce_synthetic_1234", "sentAt": s.clock.wall})
        opened = json.loads(client.published[-1]["payload"])
        assert opened["success"] is True
        handle = {key: opened["data"][key] for key in ("sessionId", "generation", "epoch", "expiresAt")}
        await route("first", s.press(handle, 1, "volumeUp"))
        await s.drain()
        await route("second", s.press(handle, 2, "ok"))
        await s.drain()
        results = [json.loads(p["payload"]) for p in client.published]
        assert [r["data"].get("phase") for r in results[1:]] == ["admitted", "admitted"]
        assert calls == [("remote", "send_command", {"entity_id": "remote.renamed", "command": "volume_up" if s.profile == "dangbei_local" else "volup"}, True)]
        s.gate.set()
        await s.drain()
        assert [call[2]["command"] for call in calls] == ["volume_up" if s.profile == "dangbei_local" else "volup", "ok"]
        results = [json.loads(p["payload"]) for p in client.published]
        assert [r["data"].get("phase") for r in results].count("executed") == 2
        # The same online engine is not reachable through retained/QoS1 eligibility.
        await route("ineligible", s.press(handle, 3, "up"), eligible=False)
        assert json.loads(client.published[-1]["payload"])["error"] == "remote_not_ready"
        assert len(calls) == 2
    finally:
        s.gate.set()
        await s.drain()
        monkeypatch.undo()
