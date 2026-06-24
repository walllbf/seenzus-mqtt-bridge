"""Helpers for excluding bridge-internal entities from MQTT state mirroring."""

from __future__ import annotations


_INTERNAL_ENTITY_PREFIXES = (
    "sensor.seenzusai_mqtt_bridge_",
    "binary_sensor.seenzusai_mqtt_bridge_",
    "update.seenzusai_mqtt_bridge_",
    # Legacy pre-rename prefix kept so older installs stay filtered.
    "sensor.savanai_bridge_",
    "binary_sensor.savanai_bridge_",
    "update.savanai_bridge_",
)


def looks_like_internal_bridge_entity_id(entity_id: str) -> bool:
    """Return True for this integration's own diagnostic/helper entities."""
    text = (entity_id or "").strip().lower()
    return text.startswith(_INTERNAL_ENTITY_PREFIXES)


def name_has_model_marker(name: str | None) -> bool:
    """Return True when a display name carries a model marker ('*').

    Some integrations surface a device's model/variant in the entity's
    friendly name with an asterisk (e.g. "Aqara T1*"). Such entities are
    excluded from MQTT mirroring and from the device catalog.
    """
    return "*" in (name or "")
