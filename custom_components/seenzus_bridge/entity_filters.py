"""Helpers for excluding bridge-internal entities from MQTT state mirroring."""

from __future__ import annotations

import re


_INTERNAL_ENTITY_PREFIXES = (
    "sensor.seenzus_mqtt_bridge_",
    "binary_sensor.seenzus_mqtt_bridge_",
    "update.seenzus_mqtt_bridge_",
    # Legacy prefixes kept so installs from earlier brand names stay filtered.
    "sensor.seenzusai_mqtt_bridge_",
    "binary_sensor.seenzusai_mqtt_bridge_",
    "update.seenzusai_mqtt_bridge_",
    "sensor.savanai_bridge_",
    "binary_sensor.savanai_bridge_",
    "update.savanai_bridge_",
)

# Historical integrations append a terminal asterisk to an ASCII model token,
# for example ``Aqara T1*``.  Requiring a digit keeps that narrow marker while
# avoiding user-authored names such as ``客厅灯*`` or ``Living Room Light*``.
_MODEL_MARKER_RE = re.compile(r"(?<!\S)(?=[A-Za-z0-9._-]*\d)[A-Za-z0-9][A-Za-z0-9._-]*\*$")


def looks_like_internal_bridge_entity_id(entity_id: str) -> bool:
    """Return True for this integration's own diagnostic/helper entities."""
    text = (entity_id or "").strip().lower()
    return text.startswith(_INTERNAL_ENTITY_PREFIXES)


def name_has_model_marker(name: str | None) -> bool:
    """Return True when a display name ends in a model-code marker.

    Some integrations surface a device's model/variant in the entity's
    friendly name with an asterisk (e.g. "Aqara T1*"). Such entities are
    excluded from MQTT mirroring and from the device catalog.
    """
    return _MODEL_MARKER_RE.search(name or "") is not None
