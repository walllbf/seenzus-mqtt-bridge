"""Pure builders for the retained MQTT device catalog payload.

No MQTT, no coordinator state: the coordinator passes its bound
`_is_own_entity` check in as a callable. Payload key names, insertion order,
None-stripping and the device sort key are wire contract (tests compare full
payloads).
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .entity_filters import name_has_model_marker

# Membership is load-bearing — shrinking this set drops devices from backend catalogs.
IOT_DEVICE_DOMAINS = {
    "alarm_control_panel",
    "automation",
    "binary_sensor",
    "button",
    "camera",
    "climate",
    "cover",
    "fan",
    "humidifier",
    "input_boolean",
    "input_number",
    "input_select",
    "lawn_mower",
    "light",
    "lock",
    "media_player",
    "number",
    "remote",
    "scene",
    "select",
    "sensor",
    "siren",
    "script",
    "switch",
    "text",
    "vacuum",
    "valve",
    "water_heater",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_iot_catalog_entity(entity: dict[str, Any]) -> bool:
    domain = str(entity.get("domain", "")).lower()
    return domain in IOT_DEVICE_DOMAINS


def build_catalog_entity(state: Any, entity_entry: Any, device_entry: Any) -> dict[str, Any]:
    entity_id = getattr(state, "entity_id", "")
    attributes = dict(getattr(state, "attributes", {}) or {})
    domain = entity_id.split(".", 1)[0] if "." in entity_id else "unknown"
    state_value = getattr(state, "state", "")
    name = (
        getattr(entity_entry, "name", None)
        or getattr(entity_entry, "original_name", None)
        or attributes.get("friendly_name")
        or entity_id
    )
    area_id = getattr(entity_entry, "area_id", None) or getattr(device_entry, "area_id", None)
    entity = {
        "entityId": entity_id,
        "name": name,
        "domain": domain,
        "state": state_value,
        "available": str(state_value).lower() not in {"unavailable", "unknown"},
        "deviceId": getattr(entity_entry, "device_id", None),
    }
    if area_id:
        entity["areaId"] = area_id
    for attr_name, output_name in {
        "device_class": "deviceClass",
        "unit_of_measurement": "unit",
        "icon": "icon",
    }.items():
        if attr_name in attributes:
            entity[output_name] = attributes[attr_name]
    return entity


def build_catalog_device(
    device_id: str,
    device_entry: Any,
    *,
    fallback_name: str | None = None,
    entity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    name = (
        getattr(device_entry, "name_by_user", None)
        or getattr(device_entry, "name", None)
        or fallback_name
        or device_id
    )
    device = {
        "deviceId": device_id,
        "name": name,
        "displayName": name,
        "manufacturer": getattr(device_entry, "manufacturer", None),
        "model": getattr(device_entry, "model", None),
        "areaId": getattr(device_entry, "area_id", None),
        "viaDeviceId": getattr(device_entry, "via_device_id", None),
        "entities": [] if entity is None else [entity],
    }
    return {key: value for key, value in device.items() if value is not None}


def resolve_primary_domain(entities: list[dict[str, Any]]) -> str:
    order = [
        "climate", "light", "cover", "fan", "media_player", "lock",
        "humidifier", "vacuum", "water_heater", "alarm_control_panel", "switch",
        "number", "select", "button", "camera",
    ]
    domains = [str(entity.get("domain", "")) for entity in entities]
    for candidate in order:
        if candidate in domains:
            return candidate
    return domains[0] if domains else "unknown"


def build_device_catalog_payload(
    hass: HomeAssistant,
    *,
    bridge_id: str,
    source: str,
    correlation_id: str | None = None,
    is_own_entity: Callable[[str], bool],
) -> dict[str, Any]:
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    devices: dict[str, dict[str, Any]] = {}
    standalone: list[dict[str, Any]] = []

    for state in hass.states.async_all():
        entity_id = getattr(state, "entity_id", "")
        if not entity_id or is_own_entity(entity_id):
            continue

        entity_entry = entity_registry.async_get(entity_id)
        device_entry = None
        device_id = getattr(entity_entry, "device_id", None) if entity_entry else None
        if device_id:
            device_entry = device_registry.async_get(device_id)

        entity_payload = build_catalog_entity(state, entity_entry, device_entry)
        if not is_iot_catalog_entity(entity_payload):
            continue
        if name_has_model_marker(entity_payload.get("name")):
            continue
        if device_id and device_entry is not None:
            if device_id not in devices:
                devices[device_id] = build_catalog_device(device_id, device_entry)
            devices[device_id]["entities"].append(entity_payload)
        else:
            standalone_device = build_catalog_device(
                entity_id,
                None,
                fallback_name=entity_payload["name"],
                entity=entity_payload,
            )
            standalone.append(standalone_device)

    catalog = list(devices.values()) + standalone
    for device in catalog:
        entities = device["entities"]
        device["entityCount"] = len(entities)
        device["availableEntityCount"] = sum(
            1 for entity in entities if entity.get("available")
        )
        device["primaryDomain"] = resolve_primary_domain(entities)
        # `online` keeps any()-semantics (wire contract); derive it from the
        # count so the availability predicate lives in exactly one place.
        device["online"] = device["availableEntityCount"] > 0
        # `primaryAvailable` exposes core-function availability for stricter
        # consumers. A device can expose several entities of its primary domain
        # (e.g. two light entities), so aggregate over all of them rather than
        # picking the first in state-iteration order (non-deterministic).
        primary_entities = [
            entity for entity in entities
            if entity.get("domain") == device["primaryDomain"]
        ]
        device["primaryAvailable"] = (
            any(entity.get("available") for entity in primary_entities)
            if primary_entities
            else None
        )

    catalog.sort(key=lambda item: (-int(item.get("entityCount", 0)), str(item.get("name", "")).lower()))
    payload: dict[str, Any] = {
        "eventId": str(uuid.uuid4()),
        "bridgeId": bridge_id,
        "source": source,
        "ts": utc_now_iso(),
        "devices": catalog,
        "deviceCount": len(catalog),
        "entityCount": sum(int(device.get("entityCount", 0)) for device in catalog),
    }
    if correlation_id:
        payload["correlationMsgId"] = correlation_id
    return payload
