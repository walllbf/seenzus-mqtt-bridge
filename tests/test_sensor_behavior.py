from __future__ import annotations

import pytest
from homeassistant.const import EntityCategory

from seenzus_bridge import BridgeCoordinator
from seenzus_bridge.sensor import (
    BridgeMetricSensor,
    BridgePairingStateSensor,
    BridgeStatusSensor,
)
from tests.helpers import FakeConfigEntry, FakeHass


def test_pairing_sensor_exposes_extended_pairing_attributes() -> None:
    entry = FakeConfigEntry(
        data={
            "bridge_id": "ha-web-bridge",
            "source_id": "ha-bridge-ha-web-bridge",
            "source_type": "haos_bridge",
            "source_name": "HA Bridge",
        }
    )
    coordinator = BridgeCoordinator(FakeHass(), entry)
    coordinator.pairing_mode = "seamless"
    coordinator.config_source = "web_pair"
    coordinator.pairing_status = "paired"
    coordinator.status = "active"
    coordinator.pairing_session_id = "ps_abc123"
    coordinator.pairing_expires_at = "2026-04-20T12:05:00Z"
    coordinator.pairing_verification_code = "123456"
    coordinator.pairing_last_error = None
    coordinator.pairing_bound_at = "2026-04-20T12:01:22Z"
    coordinator.pairing_last_step = "status_confirmed"
    coordinator.pairing_last_api_base = "http://192.168.9.99:5078"
    coordinator.pairing_last_diagnostic = "http_status=400 | error_code=invalid_body"
    coordinator.last_error = None
    coordinator.mqtt_connected = True

    sensor = BridgePairingStateSensor(coordinator, entry)

    assert sensor.native_value == "paired"
    assert sensor.extra_state_attributes == {
        "pairing_mode": "seamless",
        "config_source": "web_pair",
        "bridge_id": "ha-web-bridge",
        "source_id": "ha-bridge-ha-web-bridge",
        "source_type": "haos_bridge",
        "source_name": "HA Bridge",
        "mqtt_connected": True,
        "last_error": None,
        "pairing_session_id": "ps_abc123",
        "pairing_expires_at": "2026-04-20T12:05:00Z",
        "verification_code": "123456",
        "pairing_last_error": None,
        "pairing_bound_at": "2026-04-20T12:01:22Z",
        "pairing_last_step": "status_confirmed",
        "pairing_last_api_base": "http://192.168.9.99:5078",
        "pairing_last_diagnostic": "http_status=400 | error_code=invalid_body",
    }


def test_status_sensor_pins_identity_attributes_and_device_info() -> None:
    entry = FakeConfigEntry(
        data={
            "bridge_id": "ha-web-bridge",
            "topic_root": "savant/v2",
            "source_id": "ha-bridge-ha-web-bridge",
            "source_type": "haos_bridge",
            "source_name": "HA Bridge",
        }
    )
    coordinator = BridgeCoordinator(FakeHass(), entry)
    coordinator.status = "active"

    sensor = BridgeStatusSensor(coordinator, entry)

    assert sensor._attr_unique_id == f"{entry.entry_id}_status"
    assert sensor._attr_name == "Seenzus MQTT Bridge 状态"
    assert sensor._attr_entity_category is None
    assert sensor.native_value == "运行中"
    assert sensor.icon == "mdi:check-network"
    assert sensor.extra_state_attributes == {
        "raw_status": "active",
        "last_request": None,
        "last_error": None,
        "topic_root": "savant/v2",
        "bridge_id": "ha-web-bridge",
        "source_id": "ha-bridge-ha-web-bridge",
        "source_type": "haos_bridge",
        "source_name": "HA Bridge",
        "mode": "internal_api_v2",
    }
    assert sensor.device_info == {
        "identifiers": {("seenzus_bridge", entry.entry_id)},
        "name": "Seenzus MQTT Bridge",
        "manufacturer": "Custom",
        "model": "MQTT ↔ HTTP Bridge",
        "entry_type": "service",
    }


@pytest.mark.parametrize(
    ("key", "name", "icon", "expected"),
    [
        ("request_count", "请求次数", "mdi:counter", 11),
        ("result_count", "结果回包次数", "mdi:counter", 7),
        ("state_push_count", "状态推送次数", "mdi:counter", 5),
        ("error_count", "错误次数", "mdi:alert-circle", 3),
    ],
)
def test_metric_sensor_maps_key_to_coordinator_counter(key, name, icon, expected) -> None:
    entry = FakeConfigEntry(data={"bridge_id": "ha-web-bridge"})
    coordinator = BridgeCoordinator(FakeHass(), entry)
    coordinator.req_count = 11
    coordinator.result_count = 7
    coordinator.state_push_count = 5
    coordinator.err_count = 3

    sensor = BridgeMetricSensor(coordinator, entry, key, name, icon)

    assert sensor._attr_unique_id == f"{entry.entry_id}_{key}"
    assert sensor._attr_name == f"Seenzus MQTT Bridge {name}"
    assert sensor._attr_icon == icon
    assert sensor._attr_entity_category is EntityCategory.DIAGNOSTIC
    assert sensor.native_value == expected


def test_sensors_render_sanitized_bridge_id_matching_topics() -> None:
    # B4: a non-canonical configured bridge_id must render the same sanitized
    # id the bridge actually uses in MQTT topics (build_bridge_id).
    entry = FakeConfigEntry(data={"bridge_id": " HA Demo "})
    coordinator = BridgeCoordinator(FakeHass(), entry)

    status_sensor = BridgeStatusSensor(coordinator, entry)
    pairing_sensor = BridgePairingStateSensor(coordinator, entry)

    assert status_sensor.extra_state_attributes["bridge_id"] == "ha-demo"
    assert pairing_sensor.extra_state_attributes["bridge_id"] == "ha-demo"
