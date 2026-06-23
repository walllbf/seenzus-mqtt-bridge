from __future__ import annotations

import pytest

from seenzus_bridge.ha_dispatcher import dispatch
from tests.helpers import FakeHass


@pytest.mark.asyncio
async def test_dispatch_get_config_returns_hass_config() -> None:
    hass = FakeHass()

    result = await dispatch(hass, "GET", "/api/config", None)

    assert result.status == 200
    assert result.data == hass.config.as_dict()
    assert result.touched_entities == []


@pytest.mark.asyncio
async def test_dispatch_get_state_returns_entity_and_touched_entity() -> None:
    hass = FakeHass()
    hass.states.set("light.demo", state="on", attributes={"brightness": 120})

    result = await dispatch(hass, "GET", "/api/states/light.demo", None)

    assert result.status == 200
    assert result.data["entity_id"] == "light.demo"
    assert result.touched_entities == ["light.demo"]


@pytest.mark.asyncio
async def test_dispatch_service_call_invokes_service_and_extracts_entities() -> None:
    hass = FakeHass()

    result = await dispatch(
        hass,
        "POST",
        "/api/services/light/turn_on",
        {"entity_id": ["light.a", "light.b"]},
    )

    assert result.status == 200
    assert hass.services.calls[0]["domain"] == "light"
    assert hass.services.calls[0]["service"] == "turn_on"
    assert result.touched_entities == ["light.a", "light.b"]


@pytest.mark.asyncio
async def test_dispatch_unsupported_route_returns_404() -> None:
    hass = FakeHass()

    result = await dispatch(hass, "DELETE", "/api/unknown", None)

    assert result.status == 404
    assert "Endpoint not supported" in result.data["message"]
