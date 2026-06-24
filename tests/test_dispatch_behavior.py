from __future__ import annotations

import pytest

from seenzus_bridge.ha_dispatcher import DispatchPolicy, dispatch
from tests.helpers import FakeConfig, FakeHass


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


@pytest.mark.asyncio
async def test_dispatch_config_redacts_location_by_default() -> None:
    hass = FakeHass()
    hass.config = FakeConfig(
        {
            "location_name": "Home",
            "latitude": 31.23,
            "longitude": 121.47,
            "internal_url": "http://10.0.0.5:8123",
            "external_url": "https://example.duckdns.org",
            "time_zone": "Asia/Shanghai",
        }
    )

    result = await dispatch(hass, "GET", "/api/config", None)

    assert result.status == 200
    assert "latitude" not in result.data
    assert "longitude" not in result.data
    assert "internal_url" not in result.data
    assert "external_url" not in result.data
    # Non-sensitive fields survive.
    assert result.data["time_zone"] == "Asia/Shanghai"


@pytest.mark.asyncio
async def test_dispatch_config_full_when_policy_allows() -> None:
    hass = FakeHass()
    hass.config = FakeConfig({"latitude": 31.23, "time_zone": "Asia/Shanghai"})

    result = await dispatch(
        hass, "GET", "/api/config", None, DispatchPolicy(expose_full_config=True)
    )

    assert result.status == 200
    assert result.data["latitude"] == 31.23


@pytest.mark.asyncio
async def test_dispatch_dangerous_service_blocked_by_default() -> None:
    hass = FakeHass()

    result = await dispatch(hass, "POST", "/api/services/hassio/host_reboot", {})

    assert result.status == 403
    assert hass.services.calls == []
    assert "security policy" in result.data["message"]


@pytest.mark.asyncio
async def test_dispatch_dangerous_service_allowed_with_policy() -> None:
    hass = FakeHass()

    result = await dispatch(
        hass,
        "POST",
        "/api/services/hassio/host_reboot",
        {},
        DispatchPolicy(allow_dangerous_services=True),
    )

    assert result.status == 200
    assert hass.services.calls[0]["domain"] == "hassio"


@pytest.mark.asyncio
async def test_dispatch_homeassistant_restart_blocked_but_turn_on_allowed() -> None:
    hass = FakeHass()

    blocked = await dispatch(hass, "POST", "/api/services/homeassistant/restart", {})
    assert blocked.status == 403
    assert hass.services.calls == []

    ok = await dispatch(
        hass, "POST", "/api/services/homeassistant/turn_on", {"entity_id": "light.x"}
    )
    assert ok.status == 200
    assert hass.services.calls[0]["service"] == "turn_on"


@pytest.mark.asyncio
async def test_dispatch_template_disabled_by_default() -> None:
    hass = FakeHass()

    result = await dispatch(hass, "POST", "/api/template", {"template": "{{ 1 + 1 }}"})

    assert result.status == 403
    assert "Template rendering disabled" in result.data["message"]
