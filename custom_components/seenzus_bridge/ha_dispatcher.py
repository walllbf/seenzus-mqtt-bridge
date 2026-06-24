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


@dataclass(slots=True)
class DispatchPolicy:
    """Security policy gating which dispatch operations are permitted.

    Defaults are deny/redact. The command channel has no application-layer auth
    — any party that can publish to the command topic reaches here — so the safe
    baseline must not expose RCE-class services, arbitrary template rendering, or
    home location data. Operators opt back in per-switch when a deployment
    genuinely needs it.
    """

    allow_template: bool = False
    allow_dangerous_services: bool = False
    expose_full_config: bool = False


DEFAULT_POLICY = DispatchPolicy()

# Whole domains that can reach the host, run arbitrary code, or manage the
# supervisor — blocked unless allow_dangerous_services is set.
_DANGEROUS_SERVICE_DOMAINS = frozenset(
    {"shell_command", "python_script", "hassio", "supervisor"}
)
# Specific "{domain}.{service}" pairs dangerous even though their domain is
# otherwise safe (e.g. homeassistant.turn_on must stay allowed).
_DANGEROUS_SERVICES = frozenset({"homeassistant.stop", "homeassistant.restart"})

# Keys redacted from GET /api/config unless expose_full_config is set: home
# coordinates (physical address) and instance URLs.
_SENSITIVE_CONFIG_KEYS = frozenset(
    {"latitude", "longitude", "internal_url", "external_url"}
)


def _is_dangerous_service(domain: str, service: str) -> bool:
    return (
        domain in _DANGEROUS_SERVICE_DOMAINS
        or f"{domain}.{service}" in _DANGEROUS_SERVICES
    )


def _extract_entity_ids(service_body: dict[str, Any] | None) -> list[str]:
    if not service_body:
        return []
    value = service_body.get("entity_id")
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return []


async def dispatch(
    hass: HomeAssistant,
    method: str,
    path: str,
    body: dict | None,
    policy: DispatchPolicy | None = None,
) -> DispatchResult:
    """
    Map cloud HTTP-style request to internal HA APIs.
    Return status, data and touched entities.

    ``policy`` gates security-sensitive operations; when omitted the deny/redact
    DEFAULT_POLICY applies so callers are safe by default.
    """
    policy = policy or DEFAULT_POLICY
    method = (method or "GET").upper()

    if method == "GET" and re.fullmatch(r"/api/?", path):
        try:
            from homeassistant.const import __version__  # noqa: PLC0415
        except ImportError:
            __version__ = "unknown"
        return DispatchResult(status=200, data={"message": "API running.", "version": __version__}, touched_entities=[])

    if method == "GET" and path == "/api/config":
        data = hass.config.as_dict()
        if not policy.expose_full_config and isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in _SENSITIVE_CONFIG_KEYS}
        return DispatchResult(status=200, data=data, touched_entities=[])

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
        if not policy.allow_dangerous_services and _is_dangerous_service(domain, service):
            return DispatchResult(
                status=403,
                data={"message": f"Service blocked by bridge security policy: {domain}.{service}"},
                touched_entities=[],
            )
        await hass.services.async_call(domain, service, body or {}, blocking=True)
        entities = _extract_entity_ids(body or {})
        return DispatchResult(status=200, data=[], touched_entities=entities)

    event_match = re.fullmatch(r"/api/events/([^/]+)", path)
    if method == "POST" and event_match:
        hass.bus.async_fire(event_match.group(1), body or {})
        return DispatchResult(status=200, data={"message": "Event fired."}, touched_entities=[])

    if method == "POST" and path == "/api/template":
        if not policy.allow_template:
            return DispatchResult(
                status=403,
                data={"message": "Template rendering disabled by bridge security policy"},
                touched_entities=[],
            )
        from homeassistant.helpers import template as tpl  # noqa: PLC0415

        template_value = ""
        if isinstance(body, dict):
            template_value = str(body.get("template", ""))
        tmpl = tpl.Template(template_value, hass)
        return DispatchResult(status=200, data={"rendered": tmpl.async_render()}, touched_entities=[])

    return DispatchResult(
        status=404,
        data={"message": f"Endpoint not supported by Seenzus Bridge: {method} {path}"},
        touched_entities=[],
    )
