"""Per-entry HA grants. Areas expand once in the configuration flow, never at runtime."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from homeassistant.helpers import entity_registry as er

CONF_SCOPE = "device_scope"
CONF_DEVICES = "allowed_device_ids"
CONF_ENTITIES = "allowed_entity_registry_ids"
SCOPE_KEYS = (CONF_SCOPE, CONF_DEVICES, CONF_ENTITIES)

# Restricted bridges expose device values, not arbitrary attributes containing HA URLs,
# credentials, calendars, or references to other entities. Whole-HA grants keep legacy output.
STATE_ATTRIBUTES = frozenset({
    "friendly_name", "device_class", "unit_of_measurement", "icon", "supported_features",
    "brightness", "color_mode", "supported_color_modes", "rgb_color", "hs_color", "xy_color",
    "color_temp_kelvin", "min_color_temp_kelvin", "max_color_temp_kelvin",
    "temperature", "current_temperature", "target_temp_high", "target_temp_low", "min_temp", "max_temp",
    "target_temp_step", "hvac_mode", "hvac_modes", "hvac_action", "fan_mode", "fan_modes",
    "preset_mode", "preset_modes", "swing_mode", "swing_modes", "humidity", "current_humidity",
    "min_humidity", "max_humidity", "current_position", "current_tilt_position", "percentage",
    "percentage_step", "oscillating", "direction", "volume_level", "is_volume_muted", "battery_level",
    "min", "max", "step", "mode", "options", "editable", "assumed_state", "state_class",
})

# These services accept only local entity targets and scalar/enum device settings.
# Services with indirect targets (scripts, scenes, groups, media joins, arbitrary events,
# templates, entity reloads, camera snapshots, etc.) intentionally have no restricted path.
SAFE_SERVICES: dict[str, dict[str, set[str]]] = {
    "light": {"turn_on": {"brightness", "brightness_pct", "color_temp_kelvin", "color_temp", "rgb_color", "hs_color", "xy_color", "transition", "effect"}, "turn_off": {"transition"}, "toggle": {"transition"}},
    "switch": {"turn_on": set(), "turn_off": set(), "toggle": set()},
    "fan": {"turn_on": {"percentage", "preset_mode"}, "turn_off": set(), "toggle": set(), "set_percentage": {"percentage"}, "set_preset_mode": {"preset_mode"}, "oscillate": {"oscillating"}, "set_direction": {"direction"}},
    "cover": {"open_cover": set(), "close_cover": set(), "stop_cover": set(), "set_cover_position": {"position"}, "open_cover_tilt": set(), "close_cover_tilt": set(), "stop_cover_tilt": set(), "set_cover_tilt_position": {"tilt_position"}},
    "climate": {"set_temperature": {"temperature", "target_temp_high", "target_temp_low", "hvac_mode"}, "set_hvac_mode": {"hvac_mode"}, "set_fan_mode": {"fan_mode"}, "set_preset_mode": {"preset_mode"}, "set_swing_mode": {"swing_mode"}, "set_humidity": {"humidity"}, "turn_on": set(), "turn_off": set(), "toggle": set()},
    "lock": {"lock": {"code"}, "unlock": {"code"}, "open": {"code"}},
    "humidifier": {"turn_on": set(), "turn_off": set(), "toggle": set(), "set_humidity": {"humidity"}, "set_mode": {"mode"}},
    "number": {"set_value": {"value"}}, "select": {"select_option": {"option"}, "select_next": {"cycle"}, "select_previous": {"cycle"}},
    "input_boolean": {"turn_on": set(), "turn_off": set(), "toggle": set()},
    "input_number": {"set_value": {"value"}, "increment": set(), "decrement": set()},
    "input_select": {"select_option": {"option"}, "select_next": {"cycle"}, "select_previous": {"cycle"}},
    "media_player": {"volume_set": {"volume_level"}, "volume_mute": {"is_volume_muted"}, "volume_up": set(), "volume_down": set(), "media_play": set(), "media_pause": set(), "media_stop": set(), "media_next_track": set(), "media_previous_track": set()},
}


class DeviceAccess:
    def __init__(self, hass: Any, config: dict) -> None:
        self.hass = hass
        # Missing means the legacy whole-HA grant; malformed values fail closed.
        self.restricted = config.get(CONF_SCOPE, "all") != "all"
        self.device_ids = frozenset(config.get(CONF_DEVICES, []) or [])
        self.entity_registry_ids = frozenset(config.get(CONF_ENTITIES, []) or [])

    def allows(self, entity_id: str) -> bool:
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry and getattr(entry, "platform", None) == "seenzus_bridge":
            return False
        if not self.restricted:
            return True
        if entry is None:
            return False  # no stable registry identity, so cannot persist a grant safely
        device_id = getattr(entry, "device_id", None)
        return device_id in self.device_ids if device_id else getattr(entry, "id", None) in self.entity_registry_ids

    def attributes(self, attributes: Any) -> dict:
        return {k: v for k, v in dict(attributes).items() if not self.restricted or k in STATE_ATTRIBUTES}

    def state(self, state: Any) -> dict:
        payload = state.as_dict()
        if self.restricted:
            payload["attributes"] = self.attributes(state.attributes)
            # HA Context contains user/automation IDs unrelated to this World.
            payload.pop("context", None)
        return payload

    def catalog_access(self) -> dict:
        result = {"mode": "selected" if self.restricted else "all"}
        if self.restricted:
            result.update({
                "deviceIds": sorted(self.device_ids),
                "entityRegistryIds": sorted(self.entity_registry_ids),
                "entityIds": sorted(entry.entity_id for entry in er.async_get(self.hass).entities.values()
                                    if not getattr(entry, "device_id", None) and getattr(entry, "id", None) in self.entity_registry_ids),
            })
        return result

    def fingerprint(self) -> str:
        return hashlib.sha256(json.dumps(self.catalog_access(), sort_keys=True).encode()).hexdigest()

    def service_body(self, domain: str, service: str, body: Any) -> dict | None:
        """Validate every target before invoking HA; return an explicit entity-only request."""
        fields = SAFE_SERVICES.get(domain, {}).get(service)
        if fields is None or not isinstance(body, dict):
            return None
        # Area/device/label/floor/'all' targets can expand beyond the selected scope;
        # callers must send explicit entity IDs. Unknown service fields cannot hide targets.
        if set(body) - (fields | {"entity_id"}):
            return None
        raw = body.get("entity_id")
        entities = [raw] if isinstance(raw, str) else raw
        if not isinstance(entities, list) or not entities:
            return None
        registry = er.async_get(self.hass)
        for entity_id in entities:
            if not isinstance(entity_id, str) or not entity_id.startswith(f"{domain}.") or not self.allows(entity_id):
                return None
            state = self.hass.states.get(entity_id)
            entry = registry.async_get(entity_id)
            if state is None or getattr(entry, "platform", None) in {"group", "template", "universal"}:
                return None
            if any(key in state.attributes for key in ("entity_id", "group_members", "children")):
                return None
        return {**body, "entity_id": list(dict.fromkeys(entities))}
