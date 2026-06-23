"""Map MQTT bridge requests to Home Assistant internal APIs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant


@dataclass(slots=True)
class DispatchResult:
    """Result payload for one command execution."""

    status: int
    data: Any
    touched_entities: list[str]


def _extract_entity_ids(service_body: dict[str, Any] | None) -> list[str]:
    if not service_body:
        return []
    value = service_body.get("entity_id")
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


async def dispatch(hass: HomeAssistant, method: str, path: str, body: dict | None) -> DispatchResult:
    """
    Map cloud HTTP-style request to internal HA APIs.
    Return status, data and touched entities.
    """
    method = (method or "GET").upper()

    if method == "GET" and re.fullmatch(r"/api/?", path):
        try:
            from homeassistant.const import __version__  # noqa: PLC0415
        except ImportError:
            __version__ = "unknown"
        return DispatchResult(status=200, data={"message": "API running.", "version": __version__}, touched_entities=[])

    if method == "GET" and path == "/api/config":
        return DispatchResult(status=200, data=hass.config.as_dict(), touched_entities=[])

    if method == "GET" and path == "/api/states":
        return DispatchResult(status=200, data=[s.as_dict() for s in hass.states.async_all()], touched_entities=[])

    state_match = re.fullmatch(r"/api/states/(.+)", path)
    if method == "GET" and state_match:
        entity_id = state_match.group(1)
        state = hass.states.get(entity_id)
        if state:
            return DispatchResult(status=200, data=state.as_dict(), touched_entities=[entity_id])
        return DispatchResult(status=404, data={"message": f"Entity not found: {entity_id}"}, touched_entities=[])

    service_match = re.fullmatch(r"/api/services/([^/]+)/([^/]+)", path)
    if method == "POST" and service_match:
        domain, service = service_match.group(1), service_match.group(2)
        await hass.services.async_call(domain, service, body or {}, blocking=True)
        entities = _extract_entity_ids(body or {})
        return DispatchResult(status=200, data=[], touched_entities=entities)

    event_match = re.fullmatch(r"/api/events/([^/]+)", path)
    if method == "POST" and event_match:
        hass.bus.async_fire(event_match.group(1), body or {})
        return DispatchResult(status=200, data={"message": "Event fired."}, touched_entities=[])

    if method == "POST" and path == "/api/template":
        from homeassistant.helpers import template as tpl  # noqa: PLC0415

        template_value = ""
        if isinstance(body, dict):
            template_value = str(body.get("template", ""))
        tmpl = tpl.Template(template_value, hass)
        return DispatchResult(status=200, data={"rendered": tmpl.async_render()}, touched_entities=[])

    return DispatchResult(
        status=404,
        data={"message": f"Endpoint not supported by SavanAI Bridge: {method} {path}"},
        touched_entities=[],
    )
