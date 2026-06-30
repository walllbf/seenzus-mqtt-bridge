from __future__ import annotations

import asyncio
import json

import pytest

import seenzus_bridge
from seenzus_bridge import BridgeCoordinator, BRIDGE_VERSION, er
from seenzus_bridge.bridge_protocol import build_topics
from tests.helpers import AsyncFakeMQTTClient, FakeConfigEntry, FakeEntityRegistry, FakeHass, make_state_changed_event


@pytest.fixture
def runtime_coordinator(monkeypatch):
    hass = FakeHass()
    entry = FakeConfigEntry(
        data={
            "mqtt_host": "broker.example.com",
            "topic_root": "seenzus/v2",
            "enable_state_events": True,
        }
    )
    registry = FakeEntityRegistry()
    monkeypatch.setattr(er, "async_get", lambda _hass: registry)
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_loop = _async_noop  # type: ignore[method-assign]
    return coordinator


async def _async_noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_async_start_registers_state_listener_when_enabled(runtime_coordinator) -> None:
    await runtime_coordinator.async_start()

    assert runtime_coordinator._state_unsub is not None
    assert runtime_coordinator.hass.bus.listen_calls[0]["event_type"] == "state_changed"


@pytest.mark.asyncio
async def test_async_start_skips_state_listener_when_disabled(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(
        data={
            "mqtt_host": "broker.example.com",
            "topic_root": "seenzus/v2",
            "enable_state_events": False,
        }
    )
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_loop = _async_noop  # type: ignore[method-assign]

    await coordinator.async_start()

    assert coordinator._state_unsub is None
    assert hass.bus.listen_calls == []


@pytest.mark.asyncio
async def test_on_state_changed_ignores_events_when_state_push_disabled(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(
        data={"mqtt_host": "broker.example.com", "enable_state_events": False}
    )
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("seenzus/v2", "ha-demo")

    coordinator._on_state_changed(make_state_changed_event("light.demo"))

    assert hass.scheduled_tasks == []


@pytest.mark.asyncio
async def test_publish_presence_includes_expected_payload(runtime_coordinator) -> None:
    runtime_coordinator._mqtt_client = AsyncFakeMQTTClient()
    runtime_coordinator._topics = build_topics("seenzus/v2", "ha-demo")

    await runtime_coordinator._publish_presence("online")

    assert runtime_coordinator._mqtt_client.published[0]["retain"] is True
    payload = json.loads(runtime_coordinator._mqtt_client.published[0]["payload"])
    assert payload["bridgeId"] == "ha-demo"
    assert payload["status"] == "online"
    assert payload["version"] == BRIDGE_VERSION


@pytest.mark.asyncio
async def test_presence_heartbeat_publishes_every_default_interval(monkeypatch, runtime_coordinator) -> None:
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    runtime_coordinator._mqtt_client = AsyncFakeMQTTClient()
    runtime_coordinator._topics = build_topics("seenzus/v2", "ha-demo")
    monkeypatch.setattr(seenzus_bridge.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await runtime_coordinator._presence_heartbeat()

    assert sleeps == [30, 30]
    assert runtime_coordinator._mqtt_client.published[0]["topic"] == "seenzus/v2/bridge/ha-demo/presence"
