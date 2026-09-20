from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from seenzus_bridge import sensor_display
from seenzus_bridge.ha_dispatcher import dispatch
from tests.helpers import FakeHass


@pytest.fixture
def source(monkeypatch):
    hass = FakeHass()
    hass.config.time_zone = "Asia/Shanghai"
    entry = SimpleNamespace(platform="demo", translation_key="phase", options={"sensor": {"display_precision": 0, "suggested_display_precision": 2}})
    registry = SimpleNamespace(async_get=lambda entity_id: entry if entity_id == "sensor.demo" else None)
    monkeypatch.setattr(sensor_display.er, "async_get", lambda _: registry)

    async def translations(_hass, language, category, integrations):
        if category == "entity":
            return {
                "component.demo.entity.sensor.phase.state.dry": "干燥" if language == "zh-Hans" else "Dry",
                "component.demo.entity.sensor.phase.state.wet": "潮湿" if language == "zh-Hans" else "Wet",
                "component.demo.entity.sensor.phase.unit_of_measurement": "次" if language == "zh-Hans" else "cycles",
                "component.demo.entity.sensor.other.state.private": "Other entity",
            }
        return {"component.sensor.entity_component._.state.idle": "Idle"}

    loader = AsyncMock(side_effect=translations)
    monkeypatch.setattr(sensor_display.translation, "async_get_translations", loader)
    return hass, entry, loader


@pytest.mark.asyncio
async def test_scopes_bilingual_resources_and_caches_loads(source):
    hass, entry, loader = source
    await sensor_display.async_prepare_sensor_display(hass, ["sensor.demo"])
    await sensor_display.async_prepare_sensor_display(hass, ["sensor.demo"])
    assert loader.await_count == 4
    original = {"device_class": "enum", "seenzus_display": {"forged": True}}
    result = sensor_display.sensor_display_attributes(hass, "sensor.demo", original)
    metadata = result["seenzus_display"]
    assert metadata["sensor_options"] == {"display_precision": 0, "suggested_display_precision": 2}
    assert metadata["time_zone"] == "Asia/Shanghai"
    assert metadata["translations"]["zh-Hans"]["entity"]["state"] == {"dry": "干燥", "wet": "潮湿"}
    assert metadata["translations"]["en"]["default"]["state"] == {"idle": "Idle"}
    assert "private" not in str(metadata)
    assert "forged" not in str(metadata)
    assert original["seenzus_display"] == {"forged": True}
    entry.options["sensor"]["display_precision"] = 3
    hass.config.time_zone = "UTC"
    updated = sensor_display.sensor_display_attributes(hass, "sensor.demo", {})["seenzus_display"]
    assert updated["sensor_options"]["display_precision"] == 3
    assert updated["time_zone"] == "UTC"


@pytest.mark.asyncio
async def test_state_readback_transports_metadata_without_changing_state(source):
    hass, _, _ = source
    hass.states.set("sensor.demo", state="dry", attributes={"device_class": "enum"})
    result = await dispatch(hass, "GET", "/api/states/sensor.demo", None)
    assert result.data["state"] == "dry"
    assert result.data["attributes"]["seenzus_display"]["translations"]["zh-Hans"]["entity"]["state"]["dry"] == "干燥"
    all_states = await dispatch(hass, "GET", "/api/states", None)
    assert all_states.data[0]["attributes"] == result.data["attributes"]
    assert "seenzus_display" not in hass.states.get("sensor.demo").attributes


@pytest.mark.asyncio
async def test_failed_resources_preserve_live_reads_and_non_sensor_payloads(source):
    hass, _, loader = source
    loader.side_effect = RuntimeError("resource unavailable")
    hass.states.set("sensor.demo", state="1.234", attributes={"unit_of_measurement": "V"})
    result = await dispatch(hass, "GET", "/api/states/sensor.demo", None)
    assert result.status == 200
    assert result.data["state"] == "1.234"
    assert result.data["attributes"]["unit_of_measurement"] == "V"
    assert "translations" not in result.data["attributes"]["seenzus_display"]
    attrs = {"brightness": 100}
    assert sensor_display.sensor_display_attributes(hass, "zone.demo", attrs) is attrs


@pytest.mark.asyncio
async def test_control_names_options_and_attribute_resources_are_scoped(source, monkeypatch):
    hass, entry, loader = source
    entry.name = None
    entry.original_name = "Operation mode center"
    entry.has_entity_name = True
    entry.platform = "mqtt"
    entry.translation_key = "mode"
    monkeypatch.setattr(sensor_display.er, "async_get", lambda _: SimpleNamespace(async_get=lambda _: entry))
    loader.return_value = {
        "component.mqtt.entity.select.mode.name": "中键工作模式",
        "component.mqtt.entity.select.mode.state.decoupled": "按键解耦",
        "component.mqtt.entity.select.mode.state_attributes.preset_mode.state.sleep": "睡眠",
        "component.mqtt.entity.select.unrelated.name": "Unrelated",
    }
    loader.side_effect = None
    hass.states.set("select.mode", state="control_relay", attributes={"options": ["control_relay", "decoupled"]})
    await sensor_display.async_prepare_sensor_display(hass, ["select.mode"])
    result = await dispatch(hass, "GET", "/api/states/select.mode", None)
    native = result.data["attributes"]["seenzus_display"]
    assert native["naming"] == {"name": None, "original_name": "Operation mode center", "has_entity_name": True, "platform": "mqtt"}
    assert native["translations"]["zh-Hans"]["entity"] == {
        "name": "中键工作模式", "state": {"decoupled": "按键解耦"},
        "state_attributes": {"preset_mode": {"state": {"sleep": "睡眠"}}},
    }
    assert result.data["state"] == "control_relay"
    assert result.data["attributes"]["options"] == ["control_relay", "decoupled"]
    assert "Unrelated" not in str(native)
    entry.name = "My Center"
    assert sensor_display.sensor_display_attributes(hass, "select.mode", {})["seenzus_display"]["naming"]["name"] == "My Center"
