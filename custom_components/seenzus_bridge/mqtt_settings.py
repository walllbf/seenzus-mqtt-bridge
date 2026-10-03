"""Validate local MQTT settings without Home Assistant or MQTT dependencies."""
from __future__ import annotations

from collections.abc import Mapping
import math

from .const import (
    CONF_MQTT_MAX_PACKET_SIZE_KIB,
    DEFAULT_MQTT_MAX_PACKET_SIZE_KIB,
    MAX_MQTT_PACKET_SIZE_KIB,
)


def mqtt_packet_size_limit(conf: Mapping[str, object]) -> int:
    """Return the explicit outbound packet budget in bytes; reject invalid values.

    NumberSelector returns floats even with step=1, so integral finite floats
    are valid. Missing settings retain the existing 1 MiB protection; no
    broker capacity is inferred from transport, credentials, or pairing data.
    """
    value = conf.get(CONF_MQTT_MAX_PACKET_SIZE_KIB, DEFAULT_MQTT_MAX_PACKET_SIZE_KIB)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or (isinstance(value, float) and (not math.isfinite(value) or not value.is_integer()))
        or not 1 <= value <= MAX_MQTT_PACKET_SIZE_KIB
    ):
        raise ValueError(
            f"{CONF_MQTT_MAX_PACKET_SIZE_KIB} must be an integer from 1 to "
            f"{MAX_MQTT_PACKET_SIZE_KIB} KiB"
        )
    return int(value) * 1024
