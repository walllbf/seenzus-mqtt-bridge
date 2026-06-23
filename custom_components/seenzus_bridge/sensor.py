"""Sensor - 展示 HA MQTT Bridge 连接状态与统计信息."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .bridge_protocol import build_bridge_id
from .const import (
    CONF_BRIDGE_ID,
    CONF_SOURCE_ID,
    CONF_SOURCE_NAME,
    CONF_SOURCE_TYPE,
    CONF_TOPIC_ROOT,
    DEFAULT_TOPIC_ROOT,
    DOMAIN,
)
from . import BridgeCoordinator

STATUS_LABELS: dict[str, str] = {
    "starting": "启动中",
    "active":   "运行中",
    "error":    "发生错误",
    "stopped":  "已停止",
}

STATUS_ICONS: dict[str, str] = {
    "starting": "mdi:loading",
    "active":   "mdi:check-network",
    "error":    "mdi:alert-network",
    "stopped":  "mdi:network-off",
}

# Explicit metric-key -> coordinator-attribute map (typos fail fast at
# __init__ instead of silently rendering None).
_METRIC_ATTRS: dict[str, str] = {
    "request_count": "req_count",
    "result_count": "result_count",
    "state_push_count": "state_push_count",
    "error_count": "err_count",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            BridgeStatusSensor(coordinator, entry),
            BridgeMetricSensor(coordinator, entry, "request_count", "请求次数", "mdi:counter"),
            BridgeMetricSensor(coordinator, entry, "result_count", "结果回包次数", "mdi:counter"),
            BridgeMetricSensor(coordinator, entry, "state_push_count", "状态推送次数", "mdi:counter"),
            BridgeMetricSensor(coordinator, entry, "error_count", "错误次数", "mdi:alert-circle"),
            BridgePairingStateSensor(coordinator, entry),
        ],
        update_before_add=True,
    )


class _BridgeBaseSensor(SensorEntity):
    """Shared coordinator listener behavior."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: BridgeCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry

    async def async_added_to_hass(self) -> None:
        self._coordinator.register_update_listener(self.async_write_ha_state)


class BridgeStatusSensor(_BridgeBaseSensor):
    """显示桥接运行状态、请求计数、错误信息."""

    _attr_icon = "mdi:robot"

    def __init__(self, coordinator: BridgeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"
        self._attr_name      = "SeenzusAI MQTT Bridge 状态"
        self._attr_entity_category = None

    @property
    def native_value(self) -> str:
        return STATUS_LABELS.get(self._coordinator.status, self._coordinator.status)

    @property
    def icon(self) -> str:
        return STATUS_ICONS.get(self._coordinator.status, "mdi:robot")

    # ---------- 额外属性面板 ----------
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        c = self._coordinator
        conf = {**self._entry.data, **self._entry.options}
        return {
            "raw_status":       c.status,
            "last_request":     c.last_req.isoformat() if c.last_req else None,
            "last_error":       c.last_error,
            "topic_root":       conf.get(CONF_TOPIC_ROOT, DEFAULT_TOPIC_ROOT),
            # Same sanitized id the bridge actually uses in MQTT topics.
            "bridge_id":        build_bridge_id(str(conf.get(CONF_BRIDGE_ID, "")), self._entry.entry_id),
            "source_id":        conf.get(CONF_SOURCE_ID),
            "source_type":      conf.get(CONF_SOURCE_TYPE),
            "source_name":      conf.get(CONF_SOURCE_NAME),
            "mode":             "internal_api_v2",
        }

    @property
    def device_info(self) -> dict[str, Any]:
        return {
            "identifiers":  {(DOMAIN, self._entry.entry_id)},
            "name":         "SeenzusAI MQTT Bridge",
            "manufacturer": "Custom",
            "model":        "MQTT ↔ HTTP Bridge",
            "entry_type":   "service",
        }


class BridgeMetricSensor(_BridgeBaseSensor):
    """Expose one numeric metric as independent sensor."""

    def __init__(self, coordinator: BridgeCoordinator, entry: ConfigEntry, key: str, name: str, icon: str) -> None:
        super().__init__(coordinator, entry)
        if key not in _METRIC_ATTRS:
            raise ValueError(f"Unknown bridge metric key: {key}")
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = f"SeenzusAI MQTT Bridge {name}"
        self._attr_icon = icon

    @property
    def native_value(self) -> int:
        return getattr(self._coordinator, _METRIC_ATTRS[self._key])


class BridgePairingStateSensor(_BridgeBaseSensor):
    """Expose pairing state as dedicated sensor."""

    _attr_icon = "mdi:link-variant"

    def __init__(self, coordinator: BridgeCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_pairing_status"
        self._attr_name = "SeenzusAI MQTT Bridge 配对状态"

    @property
    def native_value(self) -> str:
        return self._coordinator.pairing_status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        conf = {**self._entry.data, **self._entry.options}
        return {
            "pairing_mode": self._coordinator.pairing_mode,
            "config_source": self._coordinator.config_source,
            # Same sanitized id the bridge actually uses in MQTT topics.
            "bridge_id": build_bridge_id(str(conf.get(CONF_BRIDGE_ID, "")), self._entry.entry_id),
            "source_id": conf.get(CONF_SOURCE_ID),
            "source_type": conf.get(CONF_SOURCE_TYPE),
            "source_name": conf.get(CONF_SOURCE_NAME),
            "mqtt_connected": self._coordinator.mqtt_connected,
            "last_error": self._coordinator.last_error,
            "pairing_session_id": self._coordinator.pairing_session_id,
            "pairing_expires_at": self._coordinator.pairing_expires_at,
            "verification_code": self._coordinator.pairing_verification_code,
            "pairing_last_error": self._coordinator.pairing_last_error,
            "pairing_bound_at": self._coordinator.pairing_bound_at,
            "pairing_last_step": self._coordinator.pairing_last_step,
            "pairing_last_api_base": self._coordinator.pairing_last_api_base,
            "pairing_last_diagnostic": self._coordinator.pairing_last_diagnostic,
        }
