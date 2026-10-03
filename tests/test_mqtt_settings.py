"""Packet budgets are explicit local settings and never replace a pairing."""
from copy import deepcopy

import pytest
import voluptuous as vol
from homeassistant.helpers.config_validation import custom_serializer

# Match the installed Core's actual form serializer. New Core uses probatio;
# older supported Core uses voluptuous_serialize, which is no longer installed
# by the newer Core. Neither dependency belongs to the bridge itself.
try:
    from homeassistant.helpers.config_validation import to_field_list
except ImportError:
    from voluptuous_serialize import convert as to_field_list

from seenzus_bridge.config_flow import SavanAIBridgeConfigFlow, SavanAIBridgeOptionsFlow
from seenzus_bridge.const import CONF_MQTT_MAX_PACKET_SIZE_KIB
from seenzus_bridge.mqtt_settings import mqtt_packet_size_limit
from tests.helpers import FakeConfigEntry, FakeHass


KEY = CONF_MQTT_MAX_PACKET_SIZE_KIB


def test_legacy_entries_keep_one_mib_without_migration():
    conf = {"mqtt_host": "broker.example"}
    assert mqtt_packet_size_limit(conf) == 1024 * 1024
    assert conf == {"mqtt_host": "broker.example"}


@pytest.mark.parametrize("value", [1, 1024, 2048, 262144, 2048.0])
def test_packet_budget_is_an_integer_number_of_kib(value):
    limit = mqtt_packet_size_limit({KEY: value})
    assert limit == int(value) * 1024
    assert type(limit) is int


@pytest.mark.parametrize(
    "value", [None, True, False, "2048", 0, -1, 262145, 1.5, float("nan"), float("inf"), float("-inf")]
)
def test_invalid_packet_budget_is_rejected(value):
    with pytest.raises(ValueError, match=KEY):
        mqtt_packet_size_limit({KEY: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("pairing_mode", [None, "manual", "seamless"])
async def test_packet_budget_options_preserve_identity_and_credentials(pairing_mode, monkeypatch):
    data = {
        "mqtt_host": "old.example", "mqtt_port": 1883, "mqtt_username": "old-user",
        "mqtt_password": "old-password", "mqtt_scheme": "mqtt", "mqtt_ws_path": "",
        "bridge_id": "old-bridge", "source_id": "old-source", "source_name": "Old home",
    }
    if pairing_mode:
        data.update(pairing_mode=pairing_mode, config_source="web_pair" if pairing_mode == "seamless" else "manual")
    options = {
        "mqtt_host": "current.example", "mqtt_port": 443, "mqtt_username": "current-user",
        "mqtt_password": "current-password", "mqtt_scheme": "wss", "mqtt_ws_path": "/custom-mqtt",
        "bridge_id": "current-bridge", "source_id": "current-source", "source_name": "Current home",
        "pairing_session_id": "session-1", "pairing_bound_at": "2026-10-04T00:00:00Z",
        "enable_template_api": True,
    }
    entry = FakeConfigEntry(data=deepcopy(data), options=deepcopy(options))
    flow = SavanAIBridgeOptionsFlow(entry)

    def forbidden_pairing(*_args, **_kwargs):
        pytest.fail("Changing the packet budget must not start or exchange a pairing")

    monkeypatch.setattr("seenzus_bridge.config_flow.create_web_pairing_session", forbidden_pairing)
    monkeypatch.setattr("seenzus_bridge.config_flow.exchange_web_pairing_callback_code", forbidden_pairing)
    flow.async_create_entry = lambda *, title, data: {"type": "create_entry", "data": data}

    result = await flow.async_step_connection_settings({KEY: 2048.0})

    assert result["type"] == "create_entry"
    assert result["data"] == {**options, KEY: 2048}
    assert type(result["data"][KEY]) is int
    assert entry.data == data
    assert entry.options == options
    assert {**entry.data, **result["data"]} == {**data, **options, KEY: 2048}


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, "2048", 0, 262145, 1.5, float("nan"), float("inf")])
async def test_options_show_field_error_and_keep_saved_budget_on_invalid_input(value):
    entry = FakeConfigEntry(data={"mqtt_host": "broker.example"}, options={KEY: 2048})
    flow = SavanAIBridgeOptionsFlow(entry)
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}

    result = await flow.async_step_connection_settings({KEY: value})

    assert result["type"] == "form"
    assert result["errors"] == {KEY: "invalid_mqtt_packet_size"}
    assert entry.options == {KEY: 2048}


@pytest.mark.asyncio
async def test_connection_settings_schema_roundtrips_ha_number_selector():
    entry = FakeConfigEntry(data={KEY: 1024}, options={KEY: 2048})
    flow = SavanAIBridgeOptionsFlow(entry)
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    form = await flow.async_step_connection_settings()
    validated = form["data_schema"]({})
    assert validated == {KEY: 2048}
    serialized = to_field_list(form["data_schema"], custom_serializer=custom_serializer)
    assert serialized[0]["selector"]["number"] == {
        "min": 1.0, "max": 262144.0, "step": 1.0, "mode": "box",
    }
    flow.async_create_entry = lambda *, title, data: {"type": "create_entry", "data": data}
    result = await flow.async_step_connection_settings(form["data_schema"]({KEY: 4096}))
    assert result["data"][KEY] == 4096
    assert type(result["data"][KEY]) is int


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False, "2048", 1.5, float("nan"), float("inf")])
async def test_number_selector_does_not_coerce_invalid_budget_into_valid_input(value):
    flow = SavanAIBridgeOptionsFlow(FakeConfigEntry())
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    form = await flow.async_step_connection_settings()
    with pytest.raises(vol.Invalid):
        form["data_schema"]({KEY: value})


@pytest.mark.asyncio
@pytest.mark.parametrize("value, valid", [(2048.0, True), (1.5, False), (True, False), (float("inf"), False)])
async def test_manual_setup_validates_budget_and_stores_integer(value, valid):
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    flow.async_create_entry = lambda *, title, data: {"type": "create_entry", "data": data}

    result = await flow.async_step_manual({
        "mqtt_settings": {"mqtt_host": "broker.example"},
        "advanced_settings": {KEY: value},
    })

    if valid:
        assert result["type"] == "create_entry"
        assert result["data"][KEY] == 2048
        assert type(result["data"][KEY]) is int
    else:
        assert result["type"] == "form"
        assert result["errors"] == {KEY: "invalid_mqtt_packet_size"}


@pytest.mark.asyncio
async def test_options_pairing_menu_keeps_manual_and_quick_pair_routes(monkeypatch):
    flow = SavanAIBridgeOptionsFlow(FakeConfigEntry())
    flow.async_show_form = lambda **kwargs: {"type": "form", **kwargs}
    mode_form = await flow.async_step_pairing()
    assert mode_form["step_id"] == "pairing"
    assert set(key.schema for key in mode_form["data_schema"].schema) == {"pairing_mode"}

    async def manual():
        return {"step_id": "manual"}

    async def seamless(data):
        assert data == {}
        return {"step_id": "seamless_authorize"}

    monkeypatch.setattr(flow, "async_step_manual", manual)
    monkeypatch.setattr(flow, "async_step_seamless", seamless)
    assert await flow.async_step_pairing({"pairing_mode": "manual"}) == {"step_id": "manual"}
    assert await flow.async_step_pairing({"pairing_mode": "seamless"}) == {"step_id": "seamless_authorize"}


def test_quick_repair_preserves_explicit_local_packet_budget():
    entry = FakeConfigEntry(data={KEY: 1024}, options={KEY: 2048})
    flow = SavanAIBridgeOptionsFlow(entry)
    flow.hass = FakeHass()
    flow.async_create_entry = lambda *, title, data: {"type": "create_entry", "data": data}
    result = flow._finish_quick_pair({"mqtt_host": "new.example", "bridge_id": "new-bridge"})
    assert result["data"] == {"mqtt_host": "new.example", "bridge_id": "new-bridge", KEY: 2048}
