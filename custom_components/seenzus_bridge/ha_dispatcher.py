"""Map MQTT bridge requests to Home Assistant internal APIs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import service as service_helper
from .device_access import DeviceAccess, SAFE_SERVICES
from .sensor_display import async_prepare_sensor_display, sensor_display_attributes


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
    access: DeviceAccess | None = None


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
    access = policy.access
    restricted = access is not None and access.restricted

    if method == "GET" and re.fullmatch(r"/api/?", path):
        try:
            from homeassistant.const import __version__  # noqa: PLC0415
        except ImportError:
            __version__ = "unknown"
        return DispatchResult(status=200, data={"message": "API running.", "version": __version__}, touched_entities=[])

    if method == "GET" and path == "/api/config":
        data = hass.config.as_dict()
        if restricted:
            data = {k: v for k, v in data.items() if k in {"version", "unit_system", "time_zone"}}
        if not policy.expose_full_config and isinstance(data, dict):
            data = {k: v for k, v in data.items() if k not in _SENSITIVE_CONFIG_KEYS}
        return DispatchResult(status=200, data=data, touched_entities=[])

    if method == "GET" and path == "/api/states":
        states = [s for s in hass.states.async_all() if access is None or access.allows(s.entity_id)]
        await async_prepare_sensor_display(hass, [s.entity_id for s in states])
        data = []
        for state in states:
            item = access.state(state) if access else dict(state.as_dict())
            item["attributes"] = sensor_display_attributes(hass, state.entity_id, item.get("attributes", {}))
            data.append(item)
        return DispatchResult(status=200, data=data, touched_entities=[])

    if method == "GET" and path == "/api/services":
        descriptions = await service_helper.async_get_all_descriptions(hass)
        if restricted:
            # Do not expose instance-specific descriptions/options mentioning other devices.
            descriptions = {domain: {name: {} for name in services if name in SAFE_SERVICES.get(domain, {})}
                            for domain, services in descriptions.items() if domain in SAFE_SERVICES}
        return DispatchResult(status=200, data=descriptions, touched_entities=[])

    if method == "GET" and path == "/api/seenzus/services/index":
        services = []
        for domain, definitions in hass.services.async_services().items():
            if not re.fullmatch(r"[a-z0-9_]+", domain):
                return DispatchResult(status=500, data={"error": "invalid_service_registry"}, touched_entities=[])
            for service in definitions:
                if restricted and service not in SAFE_SERVICES.get(domain, {}):
                    continue
                if not re.fullmatch(r"[a-z0-9_]+", service):
                    return DispatchResult(status=500, data={"error": "invalid_service_registry"}, touched_entities=[])
                services.append(f"{domain}.{service}")
        return DispatchResult(status=200, data={
            "version": 1, "isComplete": True, "count": len(services), "services": sorted(services),
        }, touched_entities=[])

    state_match = re.fullmatch(r"/api/states/(.+)", path)
    if method == "GET" and state_match:
        entity_id = state_match.group(1)
        if access and not access.allows(entity_id):
            return DispatchResult(status=404, data={"message": "Entity not found"}, touched_entities=[])
        state = hass.states.get(entity_id)
        if state:
            await async_prepare_sensor_display(hass, [entity_id])
            data = access.state(state) if access else dict(state.as_dict())
            data["attributes"] = sensor_display_attributes(hass, entity_id, data.get("attributes", {}))
            return DispatchResult(status=200, data=data, touched_entities=[entity_id])
        return DispatchResult(status=404, data={"message": f"Entity not found: {entity_id}"}, touched_entities=[])

    service_match = re.fullmatch(r"/api/services/([^/]+)/([^/]+)", path)
    if method == "POST" and service_match:
        domain, service = service_match.group(1), service_match.group(2)
        if restricted:
            body = access.service_body(domain, service, body)
            if body is None:
                return DispatchResult(status=403, data={"message": "Service or target outside this bridge's device grant"}, touched_entities=[])
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
        if restricted:
            return DispatchResult(status=403, data={"message": "Events unavailable for selected-device bridges"}, touched_entities=[])
        hass.bus.async_fire(event_match.group(1), body or {})
        return DispatchResult(status=200, data={"message": "Event fired."}, touched_entities=[])

    if method == "POST" and path == "/api/template":
        if restricted or not policy.allow_template:
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
        data={"message": f"Endpoint not supported by seenzus Bridge: {method} {path}"},
        touched_entities=[],
    )
