from __future__ import annotations

import asyncio

import pytest

from seenzus_bridge import BridgeCoordinator, _async_reload_entry
from seenzus_bridge.bridge_protocol import build_topics
from tests.helpers import AsyncFakeMQTTClient, FakeConfigEntry, FakeHass


def _reload_context(client):
    hass = FakeHass()
    entry = FakeConfigEntry(
        data={"mqtt_host": "broker.example.com", "topic_root": "seenzus/v2"},
        options={"bridge_id": "ha-new"},
    )
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_client = client
    coordinator._topics = build_topics("seenzus/v2", "ha-old")
    hass.data["seenzus_bridge"] = {entry.entry_id: coordinator}
    return hass, entry, coordinator


@pytest.mark.asyncio
async def test_async_reload_entry_clears_retained_presence_before_reloading(monkeypatch) -> None:
    hass, entry, coordinator = _reload_context(AsyncFakeMQTTClient())

    await _async_reload_entry(hass, entry)

    assert [item["topic"] for item in coordinator._mqtt_client.published] == [
        "seenzus/v2/bridge/ha-old/presence",
        "seenzus/v2/bridge/ha-old/catalog",
    ]
    assert hass.config_entries.reload_calls == [entry.entry_id]


@pytest.mark.asyncio
async def test_async_reload_entry_propagates_retained_cleanup_cancellation() -> None:
    class CancelledClient:
        async def publish(self, topic: str, payload: str, *, qos: int, retain: bool = False) -> None:
            raise asyncio.CancelledError

    hass, entry, _coordinator = _reload_context(CancelledClient())

    with pytest.raises(asyncio.CancelledError):
        await _async_reload_entry(hass, entry)

    assert hass.config_entries.reload_calls == []


@pytest.mark.asyncio
async def test_async_reload_entry_continues_when_retained_cleanup_times_out() -> None:
    class TimeoutClient:
        async def publish(self, topic: str, payload: str, *, qos: int, retain: bool = False) -> None:
            raise RuntimeError("Operation timed out")

    hass, entry, _coordinator = _reload_context(TimeoutClient())

    await _async_reload_entry(hass, entry)

    assert hass.config_entries.reload_calls == [entry.entry_id]
