"""Transport HA-owned sensor display resources without interpreting or formatting states."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import translation

_LOGGER = logging.getLogger(__name__)
_CACHE = "seenzus_bridge_sensor_display_resources"
_LANGUAGES = ("en", "zh-Hans")
_DOMAINS = {"sensor", "binary_sensor", "select", "input_select", "number", "switch", "button",
            "climate", "light", "fan", "cover", "valve", "lock", "media_player", "vacuum",
            "water_heater", "humidifier", "siren", "alarm_control_panel", "lawn_mower"}


def supports_entity_display(entity_id: str) -> bool:
    return entity_id.split(".", 1)[0] in _DOMAINS


async def async_prepare_sensor_display(hass: Any, entity_ids: list[str]) -> None:
    """Load only the official resources addressed by the authorized entities being sent."""
    try:
        registry = er.async_get(hass)
        requested: dict[str, set[str]] = {"entity": set(), "entity_component": set()}
        for entity_id in entity_ids:
            domain = entity_id.split(".", 1)[0]
            if domain not in _DOMAINS:
                continue
            requested["entity_component"].add(domain)
            entry = registry.async_get(entity_id)
            if entry and getattr(entry, "translation_key", None) and getattr(entry, "platform", None):
                requested["entity"].add(entry.platform)
        cache = hass.data.setdefault(_CACHE, {})
        for category, integrations in requested.items():
            for language in _LANGUAGES:
                missing = {name for name in integrations if (language, category, name) not in cache}
                if not missing:
                    continue
                resources = await translation.async_get_translations(hass, language, category, integrations=missing)
                for name in missing:
                    prefix = f"component.{name}."
                    cache[language, category, name] = {key: value for key, value in resources.items() if key.startswith(prefix)}
    except Exception:  # Display resources must never stop live state transport.
        _LOGGER.debug("Sensor display resources temporarily unavailable", exc_info=True)


def _fragment(resources: dict[str, str], prefix: str) -> dict[str, Any]:
    states = {}
    state_prefix = f"{prefix}.state."
    for key, value in resources.items():
        if key.startswith(state_prefix) and isinstance(value, str) and len(value) <= 256:
            state = key[len(state_prefix):]
            if state and len(state) <= 128 and len(states) < 128:
                states[state] = value
    result: dict[str, Any] = {"state": states} if states else {}
    name = resources.get(f"{prefix}.name")
    if isinstance(name, str) and len(name) <= 256:
        result["name"] = name
    attribute_prefix = f"{prefix}.state_attributes."
    attributes: dict[str, Any] = {}
    for key, value in resources.items():
        if not key.startswith(attribute_prefix) or not isinstance(value, str) or len(value) > 256:
            continue
        attribute, separator, state = key[len(attribute_prefix):].partition(".state.")
        if separator and attribute and state and len(attribute) <= 128 and len(state) <= 128:
            if attribute not in attributes and len(attributes) >= 32:
                continue
            values = attributes.setdefault(attribute, {"state": {}})["state"]
            if len(values) < 128:
                values[state] = value
    if attributes:
        result["state_attributes"] = attributes
    unit = resources.get(f"{prefix}.unit_of_measurement")
    if isinstance(unit, str) and len(unit) <= 256:
        result["unit_of_measurement"] = unit
    return result


def sensor_display_attributes(hass: Any, entity_id: str, attributes: dict[str, Any]) -> dict[str, Any]:
    """Add scoped registry options, timezone and translation dictionaries; retain the raw value."""
    if entity_id.split(".", 1)[0] not in _DOMAINS:
        return attributes
    result = dict(attributes)
    # Reserved transport key cannot be forged by an integration's state attributes.
    result.pop("seenzus_display", None)
    metadata: dict[str, Any] = {}
    zone = getattr(hass.config, "time_zone", None)
    if isinstance(zone, str):
        metadata["time_zone"] = zone
    try:
        entry = er.async_get(hass).async_get(entity_id)
    except (AttributeError, KeyError):
        entry = None
    if entry is not None:
        metadata["naming"] = {
            "name": getattr(entry, "name", None),
            "original_name": getattr(entry, "original_name", None),
            "has_entity_name": getattr(entry, "has_entity_name", False),
            "platform": getattr(entry, "platform", None),
        }
    options = (getattr(entry, "options", None) or {}).get("sensor", {})
    sensor_options = {key: options[key] for key in ("display_precision", "suggested_display_precision") if key in options and type(options[key]) is int}
    if sensor_options:
        metadata["sensor_options"] = sensor_options
    cache = hass.data.get(_CACHE, {})
    domain = entity_id.split(".", 1)[0]
    device_class = attributes.get("device_class")
    platform = getattr(entry, "platform", None)
    translation_key = getattr(entry, "translation_key", None)
    languages = {}
    for language in _LANGUAGES:
        tiers = {}
        if platform and translation_key:
            fragment = _fragment(cache.get((language, "entity", platform), {}), f"component.{platform}.entity.{domain}.{translation_key}")
            if fragment:
                tiers["entity"] = fragment
        resources = cache.get((language, "entity_component", domain), {})
        for tier, class_key in (("device_class", device_class), ("default", "_")):
            if class_key:
                fragment = _fragment(resources, f"component.{domain}.entity_component.{class_key}")
                if fragment:
                    tiers[tier] = fragment
        if tiers:
            languages[language] = tiers
    if languages:
        metadata["translations"] = languages
    if metadata:
        result["seenzus_display"] = metadata
    return result
