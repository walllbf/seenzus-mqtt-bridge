from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.translation import _async_get_component_strings

from seenzus_bridge.config_flow import SavanAIBridgeConfigFlow, SavanAIBridgeOptionsFlow
from tests.test_device_access import home


def fields(result):
    return {getattr(key, "schema", key) for key in result["data_schema"].schema}


@pytest.mark.asyncio
async def test_setup_separates_environment_scope_devices_and_standalone_entities(home, monkeypatch):
    hass, registry, _, _, _ = home
    flow = SavanAIBridgeConfigFlow()
    flow.hass = hass
    flow.async_show_form = lambda **kwargs: kwargs
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", AsyncMock(return_value=None))
    assert fields(await flow.async_step_user()) == {"pairing_mode"}
    result = await flow.async_step_user({"pairing_mode": "manual"})
    assert result["step_id"] == "environment" and fields(result) == {"environment"}
    result = await flow.async_step_environment({"environment": "cn"})
    assert result["step_id"] == "scope" and fields(result) == {"device_scope"}
    result = await flow.async_step_scope({"device_scope": "selected"})
    assert result["step_id"] == "devices" and fields(result) == {"areas", "allowed_device_ids"}
    result = await flow.async_step_devices({"allowed_device_ids": ["city"]})
    assert result["step_id"] == "entities" and fields(result) == {"areas", "entities"}
    selector = next(value for key, value in result["data_schema"].schema.items() if key.schema == "entities")
    assert selector.config["include_entities"] == ["input_boolean.helper"]
    result = await flow.async_step_entities({"entities": ["light.suburb"]})
    assert result["errors"] == {"base": "stable_entity_required"}
    result = await flow.async_step_entities({"entities": ["input_boolean.helper"]})
    assert result["step_id"] == "manual"
    assert flow._scope == {"device_scope": "selected", "allowed_device_ids": ["city"],
                           "allowed_entity_registry_ids": ["stable-input_boolean.helper"]}


@pytest.mark.asyncio
async def test_ha_loads_chinese_titles_and_labels_for_config_and_options():
    async def executor(function, *args):
        return function(*args)
    integration = SimpleNamespace(file_path=Path(__file__).resolve().parents[1] / "custom_components" / "seenzus_bridge",
                                  has_translations=True, name="seenzus MQTT Bridge")
    resources = await _async_get_component_strings(SimpleNamespace(async_add_executor_job=executor),
                                                   ["zh-Hans"], {"seenzus_bridge"}, {"seenzus_bridge": integration})
    translations = resources["zh-Hans"]["seenzus_bridge"]
    for category in ("config", "options"):
        for step, labels in {"environment": {"environment": "环境"}, "scope": {"device_scope": "设备授权范围"},
                             "devices": {"areas": "按区域批量加入设备", "allowed_device_ids": "设备"},
                             "entities": {"areas": "按区域批量加入独立实体", "entities": "独立实体"}}.items():
            assert translations[category]["step"][step]["title"]
            assert translations[category]["step"][step]["data"] == labels


@pytest.mark.asyncio
async def test_options_area_selection_keeps_devices_and_entities_separate(home):
    hass, registry, _, entry, _ = home
    registry.async_get("input_boolean.helper").area_id = "downtown"
    flow = SavanAIBridgeOptionsFlow(entry)
    flow.hass = hass
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_create_entry = lambda **kwargs: kwargs
    await flow.async_step_init({"action": "devices"})
    await flow.async_step_scope({"device_scope": "selected"})
    await flow.async_step_devices({"areas": ["downtown"]})
    assert entry.data["allowed_entity_registry_ids"] == ["stable-input_boolean.helper"]
    result = await flow.async_step_entities({})
    assert result["data"]["allowed_device_ids"] == ["city"]
    assert result["data"]["allowed_entity_registry_ids"] == []
    await flow.async_step_devices({})
    result = await flow.async_step_entities({"areas": ["downtown"]})
    assert result["data"]["allowed_device_ids"] == []
    assert result["data"]["allowed_entity_registry_ids"] == ["stable-input_boolean.helper"]


@pytest.mark.asyncio
async def test_no_standalone_entities_does_not_offer_an_unfiltered_picker(home):
    hass, registry, _, entry, _ = home
    registry.async_get("input_boolean.helper").device_id = "city"
    flow = SavanAIBridgeOptionsFlow(entry)
    flow.hass = hass
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_create_entry = lambda **kwargs: kwargs
    await flow.async_step_init({"action": "devices"})
    result = await flow.async_step_devices({"allowed_device_ids": ["city"]})
    assert result["step_id"] == "entities" and fields(result) == set()
    result = await flow.async_step_entities({})
    assert result["data"]["allowed_device_ids"] == ["city"]
    assert result["data"]["allowed_entity_registry_ids"] == []


@pytest.mark.asyncio
async def test_automatic_pairing_does_not_read_developer_preset(home, monkeypatch):
    hass, _, _, _, _ = home
    flow = SavanAIBridgeConfigFlow()
    flow.hass = hass
    flow.async_show_form = lambda **kwargs: kwargs
    preset = AsyncMock(side_effect=AssertionError("app environment must not come from the developer preset"))
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", preset)
    result = await flow.async_step_user({"pairing_mode": "seamless"})
    assert result["step_id"] == "scope"
    assert flow._selected_environment == "auto"
    preset.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["cn", "global", "uat", "custom"])
async def test_explicit_environment_route_still_uses_quick_pairing(home, monkeypatch, environment):
    hass, _, _, _, _ = home
    flow = SavanAIBridgeConfigFlow()
    flow.hass = hass
    flow.async_show_form = lambda **kwargs: kwargs
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", AsyncMock(return_value="http://dev.local/api"))
    result = await flow.async_step_user({"pairing_mode": "choose_environment"})
    assert result["step_id"] == "environment"
    assert "auto" not in next(iter(result["data_schema"].schema.values())).container
    result = await flow.async_step_environment({"environment": environment})
    assert result["step_id"] == "scope"
    assert flow._selected_environment == environment
    assert flow._selected_pairing_mode == "seamless"
    flow.async_step_seamless = AsyncMock(return_value={"type": "external"})
    assert await flow.async_step_scope({"device_scope": "all"}) == {"type": "external"}
    flow.async_step_seamless.assert_awaited_once_with({})


@pytest.mark.asyncio
async def test_environment_recovery_keeps_the_selected_device_grant(home, monkeypatch):
    hass, _, _, _, _ = home
    flow = SavanAIBridgeConfigFlow()
    flow.hass = hass
    flow.async_show_form = lambda **kwargs: kwargs
    flow._scope = {"device_scope": "selected", "allowed_device_ids": ["city"], "allowed_entity_registry_ids": []}
    flow._quick_pair_finish_error = "environment_required"
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", AsyncMock(return_value=None))
    result = await flow.async_step_seamless_finish()
    assert result["step_id"] == "environment"
    flow.async_step_seamless = AsyncMock(return_value={"type": "external"})
    assert await flow.async_step_environment({"environment": "cn"}) == {"type": "external"}
    assert flow._scope["allowed_device_ids"] == ["city"]
