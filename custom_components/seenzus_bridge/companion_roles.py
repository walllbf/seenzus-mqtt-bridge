"""View-only Dangbei roles from reviewed native registry identity, never labels/routes."""
from __future__ import annotations

from typing import Any

from homeassistant import loader
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .device_access import DeviceAccess

DOMAIN, REVIEWED_VERSION, CAPABILITY_VERSION = "dangbei_local", "0.4.3", "remote:dangbei_local:2"
ROLES = {"up": "up", "down": "down", "left": "left", "right": "right", "ok": "ok", "home": "home",
         "back": "back", "menu": "menu", "volume_up": "volumeUp", "volume_down": "volumeDown",
         "power_on": "powerOn", "power_off": "powerOff"}


async def add_reviewed_companion_roles(hass: Any, payload: dict, access: DeviceAccess) -> None:
    """Absence/ambiguity/version changes preserve every original control."""
    if not any(group.get("domain") == DOMAIN for device in payload.get("devices", []) for group in device.get("configEntries", [])):
        return
    # No dependency on (or import of) the custom integration's executable modules.
    try:
        integration = await loader.async_get_integration(hass, DOMAIN)
    except Exception:
        return
    if str(integration.version) != REVIEWED_VERSION:
        return
    registry, devices = er.async_get(hass), dr.async_get(hass)
    for device in payload.get("devices", []):
        device_id = device.get("deviceId")
        native = devices.async_get(device_id)
        identifiers = [value for domain, value in getattr(native, "identifiers", ()) if domain == DOMAIN and isinstance(value, str) and value]
        if len(identifiers) != 1:
            continue
        identifier = identifiers[0]
        entities = device.get("entities", [])

        def bound(entity: dict, domain: str):
            entry = registry.async_get(entity.get("entityId"))
            config = hass.config_entries.async_get_entry(getattr(entry, "config_entry_id", None)) if entry else None
            return entry if (entry and getattr(entry, "domain", None) == domain and entry.device_id == device_id
                             and entry.platform == DOMAIN and getattr(config, "domain", None) == DOMAIN
                             and getattr(entry, "id", None) and not getattr(entry, "hidden_by", None)
                             and not getattr(entry, "disabled_by", None) and access.allows(entry.entity_id)) else None

        remotes = [(entity, bound(entity, "remote")) for entity in entities if entity.get("domain") == "remote"]
        if len(remotes) != 1 or remotes[0][1] is None:
            continue
        remote_entity, remote = remotes[0]
        if remote.unique_id != f"{identifier}_remote" or remote_entity.get("stableEntityId") != remote.id:
            continue
        groups = device.get("configEntries", [])
        if len(groups) != 1 or groups[0].get("id") != remote.config_entry_id or groups[0].get("domain") != DOMAIN:
            continue
        for native_key, role in ROLES.items():
            candidates = [(entity, bound(entity, "button")) for entity in entities if entity.get("domain") == "button"]
            matches = [(entity, entry) for entity, entry in candidates if entry is not None
                       and entry.config_entry_id == remote.config_entry_id and entry.unique_id == f"{identifier}_{native_key}"]
            if len(matches) != 1:
                continue
            matches[0][0]["remoteCompanion"] = {"version": 1, "capabilityVersion": CAPABILITY_VERSION,
                                                "remoteRegistryId": remote.id, "role": role}
