"""Validate local MQTT settings without Home Assistant or MQTT dependencies."""
from __future__ import annotations

from collections.abc import Mapping
import math

from .bridge_protocol import (
    MINIMAL_OFFLINE_PRESENCE_PAYLOAD,
    build_bridge_id,
    build_topics,
    mqtt_publish_packet_size,
)
from .const import (
    CONF_BRIDGE_ID,
    CONF_MQTT_MAX_PACKET_SIZE_KIB,
    CONF_TOPIC_ROOT,
    DEFAULT_MQTT_MAX_PACKET_SIZE_KIB,
    DEFAULT_TOPIC_ROOT,
    MAX_MQTT_PACKET_SIZE_KIB,
)


class MqttPresenceBudgetError(ValueError):
    """The configured topic cannot carry even the minimal offline status."""


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


def validate_mqtt_presence_budget(conf: Mapping[str, object], entry_id: str) -> None:
    """Reject budgets that cannot retract a previous retained online status."""
    limit = mqtt_packet_size_limit(conf)
    bridge_id = build_bridge_id(str(conf.get(CONF_BRIDGE_ID, "")), entry_id)
    topics = build_topics(str(conf.get(CONF_TOPIC_ROOT, DEFAULT_TOPIC_ROOT)), bridge_id)
    minimum = mqtt_publish_packet_size(topics.presence_topic, MINIMAL_OFFLINE_PRESENCE_PAYLOAD, 1)
    if minimum > limit:
        raise MqttPresenceBudgetError(
            f"mqtt_presence_budget_too_small: minimum={minimum} limit={limit}; "
            "increase the packet budget or shorten the topic"
        )
