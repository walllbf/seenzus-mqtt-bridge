from __future__ import annotations

import pytest

from seenzus_bridge import BridgeCoordinator, er
from seenzus_bridge.bridge_protocol import build_topics
from tests.helpers import FakeConfigEntry, FakeEntityRegistry, FakeHass


@pytest.mark.asyncio
async def test_manual_pairing_does_not_call_backend_pairing(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(
        data={
            "pairing_mode": "manual",
            "mqtt_host": "broker.example.com",
        }
    )
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._topics = build_topics("savant/v2", "ha-demo")

    await coordinator._try_pairing()

    assert coordinator.pairing_status == "idle"
    assert coordinator.pairing_last_step is None


@pytest.mark.asyncio
async def test_try_pairing_marks_bound_for_web_pair_source(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(
        data={
            "pairing_mode": "seamless",
            "config_source": "web_pair",
            "mqtt_host": "broker.example.com",
            "pairing_api_base": "https://api.seenzus.xxx",
            "pairing_session_id": "wps_abc123",
            "pairing_bound_at": "2026-04-20T12:01:22Z",
        }
    )
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._topics = build_topics("savant/v2", "ha-web-bridge")

    await coordinator._try_pairing()

    assert coordinator.pairing_status == "bound"
    assert coordinator.pairing_session_id == "wps_abc123"
    assert coordinator.pairing_bound_at == "2026-04-20T12:01:22Z"
    assert coordinator.pairing_last_step == "web_pair_ready"
    assert coordinator.pairing_last_api_base == "https://api.seenzus.xxx"


@pytest.mark.asyncio
async def test_try_pairing_waits_when_seamless_config_is_not_web_pair(monkeypatch) -> None:
    hass = FakeHass()
    entry = FakeConfigEntry(
        data={
            "pairing_mode": "seamless",
            "config_source": "pending_quick_pair",
        }
    )
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._topics = build_topics("savant/v2", "ha-demo")

    await coordinator._try_pairing()

    assert coordinator.pairing_status == "waiting_external_auth"
    assert coordinator.pairing_last_step == "waiting_quick_pair"
