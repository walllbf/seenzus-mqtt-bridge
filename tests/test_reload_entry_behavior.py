from __future__ import annotations

import pytest

from seenzus_bridge import BridgeCoordinator, _async_reload_entry
from seenzus_bridge.bridge_protocol import build_topics
from tests.helpers import AsyncFakeMQTTClient, FakeConfigEntry, FakeHass


@pytest.mark.asyncio
async def test_async_reload_entry_clears_retained_presence_before_reloading(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(
        data={"mqtt_host": "broker.example.com", "topic_root": "savant/v2"},
        options={"bridge_id": "ha-new"},
    )
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._mqtt_client = AsyncFakeMQTTClient()
    coordinator._topics = build_topics("savant/v2", "ha-old")
    hass.data["seenzus_bridge"] = {entry.entry_id: coordinator}

    await _async_reload_entry(hass, entry)

    assert coordinator._mqtt_client.published[0]["topic"] == "savant/v2/bridge/ha-old/presence"
    assert hass.config_entries.reload_calls == [entry.entry_id]
