"""Transport failures must retire work instead of publishing more errors."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

import seenzus_bridge.coordinator as coordinator_module
from seenzus_bridge import BridgeCoordinator, dr, er
from seenzus_bridge.bridge_protocol import build_topics
from tests.helpers import FakeConfigEntry, FakeDeviceRegistry, FakeEntityRegistry, FakeHass


@pytest.fixture
def coordinator(monkeypatch):
    hass = FakeHass()
    hass.states.set("light.demo", state="on")
    entry = FakeConfigEntry(data={"mqtt_host": "broker.example.com", "topic_root": "seenzus/v2"})
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    monkeypatch.setattr(dr, "async_get", lambda _hass: FakeDeviceRegistry())
    instance = BridgeCoordinator(hass, entry)
    instance._topics = build_topics("seenzus/v2", "ha-demo")
    return instance


class FailingTransport:
    def __init__(self, fail_at):
        self.fail_at = fail_at
        self.calls = []

    async def publish(self, topic, payload, **kwargs):
        self.calls.append((topic, json.loads(payload), kwargs))
        if len(self.calls) >= self.fail_at:
            raise RuntimeError("Operation timed out")


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/states", "/api/seenzus/device-catalog"])
async def test_failed_result_stops_followup_publications(coordinator, path):
    transport = FailingTransport(fail_at=1)
    await coordinator._handle_v2_command("request-1", json.dumps({"method": "GET", "path": path}), transport)
    assert len(transport.calls) == 1
    assert coordinator.err_count == 1
    assert coordinator.result_count == 0


@pytest.mark.asyncio
async def test_failed_snapshot_does_not_publish_another_error_result(coordinator):
    transport = FailingTransport(fail_at=2)
    await coordinator._handle_v2_command("request-2", json.dumps({"method": "GET", "path": "/api/states"}), transport)
    assert len(transport.calls) == 2
    assert coordinator.err_count == 1
    assert coordinator.result_count == 1
    assert transport.calls[0][1]["status"] == 200
    assert transport.calls[1][2]["qos"] == 0


@pytest.mark.asyncio
async def test_publish_cancellation_is_not_converted_to_transport_failure(coordinator):
    class CancelledTransport:
        async def publish(self, *args, **kwargs):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await coordinator._publish_result(CancelledTransport(), "cancelled", success=True, status=200)
    assert coordinator.err_count == 0


@pytest.mark.asyncio
async def test_old_connection_commands_and_history_stop_before_backoff(coordinator, monkeypatch):
    started = asyncio.Event()

    async def blocked_handler():
        started.set()
        await asyncio.Event().wait()

    old_task = asyncio.create_task(blocked_handler())
    coordinator._command_tasks.add(old_task)
    old_task.add_done_callback(coordinator._command_tasks.discard)
    await started.wait()
    history_task = asyncio.create_task(blocked_handler())
    coordinator._history_replay_task = history_task
    coordinator._mqtt_client = object()

    class Disconnected(Exception):
        pass

    coordinator._aiomqtt = SimpleNamespace(MqttError=Disconnected)

    async def disconnect(*args):
        raise Disconnected("Disconnected during message iteration")

    async def backoff(delay):
        assert delay == 5
        assert coordinator._mqtt_client is None
        assert old_task.cancelled()
        assert history_task.cancelled()
        assert not coordinator._command_tasks
        assert coordinator._history_replay_task is None
        raise asyncio.CancelledError

    monkeypatch.setattr(coordinator, "_connect_and_serve", disconnect)
    # Avoid changing asyncio.sleep globally while cancellation/cleanup runs.
    monkeypatch.setattr(coordinator_module, "asyncio", SimpleNamespace(**{**vars(asyncio), "sleep": backoff}))
    try:
        with pytest.raises(asyncio.CancelledError):
            await coordinator._mqtt_loop()
    finally:
        for task in (old_task, history_task):
            task.cancel()
        await asyncio.gather(old_task, history_task, return_exceptions=True)
