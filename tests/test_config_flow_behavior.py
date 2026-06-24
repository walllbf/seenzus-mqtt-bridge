from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant import data_entry_flow as data_entry_flow_module

if not hasattr(data_entry_flow_module, "section"):
    def _section(schema, _config):
        return schema

    data_entry_flow_module.section = _section

from seenzus_bridge.config_flow import (
    CONF_MQTT_SETTINGS,
    FLOW_MANAGER_OPTIONS,
    PLUGIN_NAME,
    QUICK_PAIR_CALLBACK_PATH,
    SavanAIQuickPairCallbackView,
    SavanAIBridgeConfigFlow,
    SavanAIBridgeOptionsFlow,
    _build_quick_pair_callback_context,
    _flatten_form_input,
    _mode_schema,
    _schema,
    _validate,
)
from seenzus_bridge.const import (
    CONF_BRIDGE_ID,
    CONF_CONFIG_SOURCE,
    DEFAULT_PAIRING_API_BASE,
    CONF_PAIRING_API_BASE,
    CONF_PAIRING_MODE,
    CONF_TOPIC_ROOT,
)
from seenzus_bridge.quick_pair import (
    QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT,
    _record_quick_pair_diagnostic,
)
from tests.helpers import FakeConfig, FakeConfigEntry, FakeHass


def _schema_field_names(schema) -> set[str]:
    return {getattr(key, "schema", key) for key in schema.schema}


def _schema_field_default(schema, field_name: str):
    for key in schema.schema:
        if getattr(key, "schema", key) == field_name:
            default = getattr(key, "default", None)
            return default() if callable(default) else default
    raise AssertionError(f"{field_name} not present in schema")


def test_flatten_form_input_merges_section_values() -> None:
    flat = _flatten_form_input(
        {
            CONF_PAIRING_MODE: "seamless",
            "pairing_api_base": "https://api.example.com",
            "mqtt_settings": {"mqtt_host": "broker.example.com", "mqtt_port": 1883},
            "advanced_settings": {"bridge_id": "ha-demo"},
        }
    )

    assert flat["mqtt_host"] == "broker.example.com"
    assert flat["mqtt_port"] == 1883
    assert flat["bridge_id"] == "ha-demo"


def test_validate_requires_mqtt_host_in_manual_mode() -> None:
    assert _validate({CONF_PAIRING_MODE: "manual"}) == {"mqtt_host": "host_required"}


def test_validate_allows_empty_pairing_api_base_in_seamless_mode() -> None:
    assert _validate({CONF_PAIRING_MODE: "seamless"}) == {}


def test_validate_rejects_invalid_pairing_api_base_when_seamless_mode() -> None:
    errors = _validate(
        {
            CONF_PAIRING_MODE: "seamless",
            CONF_PAIRING_API_BASE: "ftp://evil.example.com",
        }
    )

    assert errors == {CONF_PAIRING_API_BASE: "invalid_pairing_api_base"}


def test_validate_accepts_local_http_pairing_api_base_when_seamless_mode() -> None:
    assert _validate(
        {
            CONF_PAIRING_MODE: "seamless",
            CONF_PAIRING_API_BASE: "http://192.168.9.99:5078/api",
        }
    ) == {}


def test_mode_schema_only_shows_pairing_mode() -> None:
    assert _schema_field_names(_mode_schema()) == {CONF_PAIRING_MODE}


def test_schema_shows_pairing_api_base_in_seamless_step() -> None:
    names = _schema_field_names(_schema("seamless", {CONF_PAIRING_MODE: "seamless"}))

    assert CONF_PAIRING_API_BASE in names
    assert CONF_MQTT_SETTINGS not in names
    assert CONF_PAIRING_MODE not in names


def test_schema_shows_only_manual_fields_in_manual_step() -> None:
    names = _schema_field_names(_schema("manual", {CONF_PAIRING_MODE: "manual"}))

    assert CONF_MQTT_SETTINGS in names
    assert CONF_PAIRING_MODE not in names


def test_build_quick_pair_callback_context_uses_plugin_callback(monkeypatch) -> None:
    hass = FakeHass()
    encoded_payloads: list[dict] = []

    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.get_url",
        lambda *_args, **_kwargs: "http://homeassistant.local:8123",
    )
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.secrets.token_urlsafe",
        lambda *_args, **_kwargs: "pairing-state",
    )

    def _fake_encode(_hass, payload):
        encoded_payloads.append(dict(payload))
        return "jwt-state"

    monkeypatch.setattr("seenzus_bridge.quick_pair._encode_jwt", _fake_encode)

    redirect_uri, callback_state, callback_state_token = _build_quick_pair_callback_context(
        hass,
        "flow-1",
        FLOW_MANAGER_OPTIONS,
    )

    assert redirect_uri == f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}"
    assert callback_state == "pairing-state"
    assert callback_state_token == "jwt-state"
    assert encoded_payloads == [
        {
            "flow_id": "flow-1",
            "flow_manager": FLOW_MANAGER_OPTIONS,
            "redirect_uri": f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}",
            "pairing_state": "pairing-state",
        }
    ]
    assert len(hass.http.registered_views) == 1
    assert isinstance(hass.http.registered_views[0], SavanAIQuickPairCallbackView)


@pytest.mark.asyncio
async def test_quick_pair_callback_view_routes_options_flow(monkeypatch) -> None:
    hass = FakeHass()
    view = SavanAIQuickPairCallbackView()
    state = {
        "flow_id": "options-flow-1",
        "flow_manager": FLOW_MANAGER_OPTIONS,
        "pairing_state": "pairing-state",
    }
    request = SimpleNamespace(
        app={"hass": hass},
        query={"state": "jwt-state", "code": "auth-code"},
    )
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair._decode_jwt",
        lambda _hass, _token: state,
    )

    response = await view.get(request)

    assert response.status == 200
    assert hass.data["seenzus_bridge"]["quick_pair_callback_payloads"]["pairing-state"] == {
        "state": state,
        "code": "auth-code",
    }
    assert hass.config_entries.options.configure_calls == [
        {"flow_id": "options-flow-1", "user_input": None}
    ]
    assert hass.config_entries.flow.configure_calls == []


@pytest.mark.asyncio
async def test_seamless_authorize_consumes_stored_callback_payload(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.savant.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    flow._quick_pair_callback_state = "pairing-state"
    flow._quick_pair_callback_state_token = "jwt-state"
    flow.hass.data.setdefault("seenzus_bridge", {})["quick_pair_callback_payloads"] = {
        "pairing-state": {
            "code": "cb-code",
            "state": {"pairing_state": "pairing-state"},
        }
    }
    flow.async_external_step_done = lambda *, next_step_id: {
        "type": "external_done",
        "next_step_id": next_step_id,
    }
    monkeypatch.setattr(
        "seenzus_bridge.config_flow.exchange_web_pairing_callback_code",
        lambda *_args, **_kwargs: _async_result(
            {
                "ok": True,
                "session_id": "wps_1",
                "bridge_id": "ha-web-bridge",
                "config_source": "web_pair",
                "confirmed_at": "2026-04-20T12:01:22Z",
                "mqtt": {
                    "host": "broker.example.com",
                    "port": 1883,
                    "username": "user-1",
                    "password": "pass-1",
                    "topicRoot": "savant/v2",
                    "bridgeId": "ha-web-bridge",
                },
            }
        ),
    )

    result = await flow.async_step_seamless_authorize()

    assert result == {"type": "external_done", "next_step_id": "seamless_finish"}
    assert flow._quick_pair_exchange_result is not None
    assert flow._quick_pair_finish_error is None
    assert flow.hass.data["seenzus_bridge"]["quick_pair_callback_payloads"] == {}


@pytest.mark.asyncio
async def test_user_step_shows_mode_selection_form(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    flow.async_show_form = lambda *, step_id, data_schema, errors=None: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors or {},
    }

    result = await flow.async_step_user()

    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert _schema_field_names(result["data_schema"]) == {CONF_PAIRING_MODE}


@pytest.mark.asyncio
async def test_user_step_routes_to_seamless_form(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    flow.async_show_form = lambda *, step_id, data_schema, errors=None: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors or {},
    }

    result = await flow.async_step_user({CONF_PAIRING_MODE: "seamless"})

    assert result["step_id"] == "seamless"
    assert _schema_field_names(result["data_schema"]) == {CONF_PAIRING_API_BASE}


@pytest.mark.asyncio
async def test_user_step_routes_to_manual_form(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    flow.async_show_form = lambda *, step_id, data_schema, errors=None: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors or {},
    }

    result = await flow.async_step_user({CONF_PAIRING_MODE: "manual"})

    assert result["step_id"] == "manual"
    assert CONF_MQTT_SETTINGS in _schema_field_names(result["data_schema"])


@pytest.mark.asyncio
async def test_seamless_step_starts_external_quick_pair(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    create_calls: list[dict] = []

    monkeypatch.setattr(
        "seenzus_bridge.config_flow._build_quick_pair_callback_context",
        lambda *_args, **_kwargs: (
            f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}",
            "pairing-state",
            "jwt-state",
        ),
    )

    async def _fake_create_web_pairing_session(**kwargs):
        create_calls.append(dict(kwargs))
        return _result_obj(
            {
                "ok": True,
                "session_id": "wps_1",
                "pairing_page_url": "https://app.savant.xxx/web-pairing/wps_1",
            }
        )

    monkeypatch.setattr(
        "seenzus_bridge.config_flow.create_web_pairing_session",
        _fake_create_web_pairing_session,
    )
    monkeypatch.setattr(
        flow,
        "async_external_step",
        lambda *, step_id, url, description_placeholders=None: {
            "type": "external",
            "step_id": step_id,
            "url": url,
        },
    )

    result = await flow.async_step_seamless(
        {}
    )

    assert result["type"] == "external"
    assert result["step_id"] == "seamless_authorize"
    assert result["url"] == "https://app.savant.xxx/web-pairing/wps_1"
    assert create_calls[0]["api_base"] == DEFAULT_PAIRING_API_BASE
    assert create_calls[0]["redirect_uri"] == f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}"
    assert create_calls[0]["state"] == "jwt-state"


@pytest.mark.asyncio
async def test_seamless_authorize_exchanges_callback_code(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.savant.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    flow._quick_pair_callback_state = "pairing-state"
    flow._quick_pair_callback_state_token = "jwt-state"
    flow.async_external_step_done = lambda *, next_step_id: {
        "type": "external_done",
        "next_step_id": next_step_id,
    }
    monkeypatch.setattr(
        "seenzus_bridge.config_flow.exchange_web_pairing_callback_code",
        lambda *_args, **_kwargs: _async_result(
            {
                "ok": True,
                "session_id": "wps_1",
                "bridge_id": "ha-web-bridge",
                "source_id": "ha-bridge-ha-web-bridge",
                "source_type": "haos_bridge",
                "source_name": "MQTT Bridge 01",
                "config_source": "web_pair",
                "confirmed_at": "2026-04-20T12:01:22Z",
                "mqtt": {
                    "host": "broker.example.com",
                    "port": 1883,
                    "username": "user-1",
                    "password": "pass-1",
                    "topicRoot": "savant/v2",
                    "bridgeId": "ha-web-bridge",
                },
            }
        ),
    )

    result = await flow.async_step_seamless_authorize(
        {
            "code": "cb-code",
            "state": {"pairing_state": "pairing-state"},
        }
    )

    assert result == {"type": "external_done", "next_step_id": "seamless_finish"}
    assert flow._quick_pair_exchange_result is not None
    assert flow._quick_pair_finish_error is None


@pytest.mark.asyncio
async def test_seamless_authorize_rejects_mismatched_state(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    flow._quick_pair_callback_state = "expected-state"
    flow.async_external_step_done = lambda *, next_step_id: {
        "type": "external_done",
        "next_step_id": next_step_id,
    }

    result = await flow.async_step_seamless_authorize(
        {
            "code": "cb-code",
            "state": {"pairing_state": "wrong-state"},
        }
    )

    assert result == {"type": "external_done", "next_step_id": "seamless_finish"}
    assert flow._quick_pair_finish_error == "quick_pair_callback_state_mismatch"


@pytest.mark.asyncio
async def test_seamless_authorize_does_not_raise_when_exchange_fails(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.savant.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    flow._quick_pair_callback_state = "pairing-state"
    flow._quick_pair_callback_state_token = "jwt-state"
    flow.async_external_step_done = lambda *, next_step_id: {
        "type": "external_done",
        "next_step_id": next_step_id,
    }

    async def _raise(*_args, **_kwargs):
        raise RuntimeError("exchange exploded")

    monkeypatch.setattr(
        "seenzus_bridge.config_flow.exchange_web_pairing_callback_code",
        _raise,
    )

    result = await flow.async_step_seamless_authorize(
        {
            "code": "cb-code",
            "state": {"pairing_state": "pairing-state"},
        }
    )

    assert result == {"type": "external_done", "next_step_id": "seamless_finish"}
    assert flow._quick_pair_finish_error == "quick_pair_code_exchange_failed"
    assert flow._quick_pair_diagnostic["error_code"] == "RuntimeError"


@pytest.mark.asyncio
async def test_seamless_finish_creates_entry_with_bootstrapped_mqtt(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow.hass.config = FakeConfig({"version": "2026.3.0"})
    flow._quick_pair_api_base = "https://api.savant.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    flow._quick_pair_exchange_result = _result_obj(
        {
            "ok": True,
            "session_id": "wps_1",
            "bridge_id": "ha-web-bridge",
            "config_source": "web_pair",
            "source_id": "ha-bridge-ha-web-bridge",
            "source_type": "haos_bridge",
            "source_name": "MQTT Bridge 01",
            "confirmed_at": "2026-04-20T12:01:22Z",
            "mqtt": {
                "host": "broker.example.com",
                "port": 1883,
                "username": "user-1",
                "password": "pass-1",
                "topicRoot": "savant/v2",
                "bridgeId": "ha-web-bridge",
            },
        }
    )
    monkeypatch.setattr(
        flow,
        "async_create_entry",
        lambda *, title, data: {"type": "create_entry", "title": title, "data": data},
    )

    result = await flow.async_step_seamless_finish()

    assert result["type"] == "create_entry"
    assert result["data"][CONF_PAIRING_MODE] == "seamless"
    assert result["data"][CONF_CONFIG_SOURCE] == "web_pair"
    assert result["data"]["mqtt_host"] == "broker.example.com"
    assert result["data"]["bridge_id"] == "ha-web-bridge"
    assert result["data"]["source_id"] == "ha-bridge-ha-web-bridge"
    assert result["data"]["source_type"] == "haos_bridge"
    assert result["data"]["source_name"] == "MQTT Bridge 01"


@pytest.mark.asyncio
async def test_seamless_finish_creates_entry_with_web_pairing_mqtt(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow.hass.config = FakeConfig({"version": "2026.3.0"})
    flow._quick_pair_api_base = "https://api.savant.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    flow._quick_pair_exchange_result = _result_obj(
        {
            "ok": True,
            "session_id": "wps_1",
            "bridge_id": "ha-web-bridge",
            "config_source": "web_pair",
            "confirmed_at": "2026-04-20T12:01:22Z",
            "mqtt": {
                "host": "broker.example.com",
                "port": 1883,
                "username": "user-1",
                "password": "pass-1",
                "topicRoot": "savant/v2",
                "bridgeId": "ha-web-bridge",
            },
        }
    )
    monkeypatch.setattr(
        flow,
        "async_create_entry",
        lambda *, title, data: {"type": "create_entry", "title": title, "data": data},
    )

    result = await flow.async_step_seamless_finish()

    assert result["type"] == "create_entry"
    assert result["data"][CONF_PAIRING_MODE] == "seamless"
    assert result["data"][CONF_CONFIG_SOURCE] == "web_pair"
    assert result["data"][CONF_PAIRING_API_BASE] == "https://api.savant.xxx/api"
    assert result["data"]["mqtt_host"] == "broker.example.com"
    assert result["data"][CONF_TOPIC_ROOT] == "savant/v2"
    assert result["data"][CONF_BRIDGE_ID] == "ha-web-bridge"


@pytest.mark.asyncio
async def test_options_init_shows_mode_selection_form() -> None:
    config_entry = type(
        "Entry",
        (),
        {"data": {"mqtt_host": "old-broker"}, "options": {}, "entry_id": "entry-1"},
    )()
    flow = SavanAIBridgeOptionsFlow(config_entry)
    flow.async_show_form = lambda *, step_id, data_schema, errors=None: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors or {},
    }

    result = await flow.async_step_init()

    assert result["step_id"] == "init"
    assert _schema_field_names(result["data_schema"]) == {CONF_PAIRING_MODE}


@pytest.mark.asyncio
async def test_options_flow_creates_entry_with_flattened_data() -> None:
    config_entry = type(
        "Entry",
        (),
        {"data": {"mqtt_host": "old-broker"}, "options": {}, "entry_id": "entry-1"},
    )()
    flow = SavanAIBridgeOptionsFlow(config_entry)
    flow.async_create_entry = lambda *, title, data: {"type": "create_entry", "data": data}

    result = await flow.async_step_manual(
        {
            CONF_MQTT_SETTINGS: {"mqtt_host": "broker.example.com"},
            "advanced_settings": {"enable_state_events": False},
        }
    )

    assert result["data"]["mqtt_host"] == "broker.example.com"
    assert result["data"]["enable_state_events"] is False
    assert result["data"][CONF_PAIRING_MODE] == "manual"


@pytest.mark.asyncio
async def test_options_seamless_step_uses_options_flow_manager(monkeypatch) -> None:
    config_entry = FakeConfigEntry(data={"mqtt_host": "old-broker"})
    flow = SavanAIBridgeOptionsFlow(config_entry)
    flow.hass = FakeHass()
    flow.flow_id = "options-flow-7"
    context_calls: list[tuple] = []
    create_calls: list[dict] = []

    def _fake_context(hass, flow_id, flow_manager):
        context_calls.append((hass, flow_id, flow_manager))
        return (
            f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}",
            "pairing-state",
            "jwt-state",
        )

    monkeypatch.setattr(
        "seenzus_bridge.config_flow._build_quick_pair_callback_context",
        _fake_context,
    )

    async def _fake_create_web_pairing_session(**kwargs):
        create_calls.append(dict(kwargs))
        return _result_obj(
            {
                "ok": True,
                "session_id": "wps_1",
                "pairing_page_url": "https://app.savant.xxx/web-pairing/wps_1",
            }
        )

    monkeypatch.setattr(
        "seenzus_bridge.config_flow.create_web_pairing_session",
        _fake_create_web_pairing_session,
    )
    flow.async_external_step = lambda *, step_id, url, description_placeholders=None: {
        "type": "external",
        "step_id": step_id,
        "url": url,
    }

    result = await flow.async_step_seamless({})

    assert context_calls == [(flow.hass, "options-flow-7", FLOW_MANAGER_OPTIONS)]
    assert create_calls[0]["api_base"] == DEFAULT_PAIRING_API_BASE
    assert create_calls[0]["state"] == "jwt-state"
    assert result["type"] == "external"
    assert result["step_id"] == "seamless_authorize"
    assert result["url"] == "https://app.savant.xxx/web-pairing/wps_1"


@pytest.mark.asyncio
async def test_options_seamless_form_seeds_api_base_from_entry_data() -> None:
    config_entry = FakeConfigEntry(
        data={
            "mqtt_host": "old-broker",
            CONF_PAIRING_API_BASE: "http://192.168.9.99:5078",
        }
    )
    flow = SavanAIBridgeOptionsFlow(config_entry)
    flow.async_show_form = lambda *, step_id, data_schema, errors, description_placeholders=None: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors,
    }

    result = await flow.async_step_seamless()

    assert result["type"] == "form"
    assert result["step_id"] == "seamless"
    assert result["errors"] == {}
    assert (
        _schema_field_default(result["data_schema"], CONF_PAIRING_API_BASE)
        == "http://192.168.9.99:5078"
    )


@pytest.mark.asyncio
async def test_options_seamless_finish_creates_entry_with_empty_title() -> None:
    config_entry = FakeConfigEntry(data={"mqtt_host": "old-broker"})
    flow = SavanAIBridgeOptionsFlow(config_entry)
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.savant.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    flow._quick_pair_exchange_result = _result_obj(
        {
            "ok": True,
            "session_id": "wps_1",
            "bridge_id": "ha-web-bridge",
            "config_source": "web_pair",
            "confirmed_at": "2026-04-20T12:01:22Z",
            "mqtt": {
                "host": "broker.example.com",
                "port": 1883,
                "username": "user-1",
                "password": "pass-1",
                "topicRoot": "savant/v2",
                "bridgeId": "ha-web-bridge",
            },
        }
    )
    flow.async_create_entry = lambda *, title, data: {
        "type": "create_entry",
        "title": title,
        "data": data,
    }

    result = await flow.async_step_seamless_finish()

    assert result["type"] == "create_entry"
    assert result["title"] == ""
    assert result["data"]["mqtt_host"] == "broker.example.com"
    assert result["data"][CONF_BRIDGE_ID] == "ha-web-bridge"


@pytest.mark.asyncio
async def test_seamless_finish_error_reshows_seamless_form_without_placeholder_support() -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.savant.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    flow._quick_pair_finish_error = "quick_pair_callback_timeout"
    flow._quick_pair_diagnostic = {"http_status": "500", "message": "boom"}
    # Deliberately NO description_placeholders parameter: pins the TypeError
    # fallback inside _show_form_with_diagnostic for cores without that kwarg.
    flow.async_show_form = lambda *, step_id, data_schema, errors: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors,
    }

    result = await flow.async_step_seamless_finish()

    assert result["type"] == "form"
    assert result["step_id"] == "seamless"
    assert result["errors"] == {"base": "quick_pair_callback_timeout"}
    assert (
        _schema_field_default(result["data_schema"], CONF_PAIRING_API_BASE)
        == "https://api.savant.xxx/api"
    )


@pytest.mark.asyncio
async def test_seamless_finish_polls_legacy_status_until_bound(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.savant.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    status_calls: list[dict] = []
    sleeps: list[float] = []

    async def _fake_fetch(**kwargs):
        status_calls.append(dict(kwargs))
        if len(status_calls) == 1:
            return _result_obj({"ok": True, "bound": False, "mqtt": None})
        return _result_obj(
            {
                "ok": True,
                "bound": True,
                "session_id": "wps_1",
                "bridge_id": "ha-web-bridge",
                "config_source": "web_pair",
                "confirmed_at": "2026-04-20T12:01:22Z",
                "mqtt": {
                    "host": "broker.example.com",
                    "port": 1883,
                    "username": "user-1",
                    "password": "pass-1",
                    "topicRoot": "savant/v2",
                    "bridgeId": "ha-web-bridge",
                },
            }
        )

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        "seenzus_bridge.config_flow.fetch_web_pairing_session_status",
        _fake_fetch,
    )
    monkeypatch.setattr("seenzus_bridge.config_flow.asyncio.sleep", _fake_sleep)
    flow.async_create_entry = lambda *, title, data: {
        "type": "create_entry",
        "title": title,
        "data": data,
    }

    result = await flow.async_step_seamless_finish()

    assert len(status_calls) <= 3
    assert [call["session_id"] for call in status_calls] == ["wps_1", "wps_1"]
    assert all(call["api_base"] == "https://api.savant.xxx/api" for call in status_calls)
    assert sleeps == [1]
    assert result["type"] == "create_entry"
    assert result["title"] == PLUGIN_NAME
    assert result["data"][CONF_CONFIG_SOURCE] == "web_pair"
    assert result["data"]["mqtt_host"] == "broker.example.com"
    assert result["data"][CONF_BRIDGE_ID] == "ha-web-bridge"


@pytest.mark.asyncio
async def test_seamless_finish_reshows_form_when_session_never_bound(monkeypatch) -> None:
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.savant.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.savant.xxx/web-pairing/wps_1"
    status_calls: list[dict] = []
    sleeps: list[float] = []

    async def _fake_fetch(**kwargs):
        status_calls.append(dict(kwargs))
        return _result_obj({"ok": True, "bound": False, "mqtt": None})

    async def _fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(
        "seenzus_bridge.config_flow.fetch_web_pairing_session_status",
        _fake_fetch,
    )
    monkeypatch.setattr("seenzus_bridge.config_flow.asyncio.sleep", _fake_sleep)
    flow.async_show_form = lambda *, step_id, data_schema, errors, description_placeholders=None: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors,
    }

    result = await flow.async_step_seamless_finish()

    assert len(status_calls) == 3
    assert sleeps == [1, 1, 1]
    assert result["type"] == "form"
    assert result["step_id"] == "seamless"
    assert result["errors"] == {"base": "quick_pair_bootstrap_failed"}
    assert (
        _schema_field_default(result["data_schema"], CONF_PAIRING_API_BASE)
        == "https://api.savant.xxx/api"
    )


@pytest.mark.asyncio
async def test_quick_pair_callback_view_rejects_missing_state() -> None:
    view = SavanAIQuickPairCallbackView()
    request = SimpleNamespace(app={"hass": FakeHass()}, query={})

    response = await view.get(request)

    assert response.status == 400
    assert response.text == "Missing state parameter"


@pytest.mark.asyncio
async def test_quick_pair_callback_view_returns_202_when_flow_resume_fails(monkeypatch) -> None:
    hass = FakeHass()
    state = {"flow_id": "flow-9", "pairing_state": "pairing-state"}
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair._decode_jwt",
        lambda _hass, _token: state,
    )

    async def _explode(*_args, **_kwargs):
        raise RuntimeError("flow gone")

    hass.config_entries.flow.async_configure = _explode
    request = SimpleNamespace(
        app={"hass": hass},
        query={"state": "jwt-state", "code": "auth-code"},
    )

    response = await SavanAIQuickPairCallbackView().get(request)

    assert response.status == 202
    assert "return to Home Assistant" in response.text
    assert hass.data["seenzus_bridge"]["quick_pair_callback_payloads"]["pairing-state"] == {
        "state": state,
        "code": "auth-code",
    }


@pytest.mark.asyncio
async def test_quick_pair_callback_mailbox_evicts_oldest_beyond_cap(monkeypatch) -> None:
    hass = FakeHass()
    view = SavanAIQuickPairCallbackView()

    for index in range(QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT + 1):
        state = {"flow_id": f"flow-{index}", "pairing_state": f"pairing-state-{index}"}
        monkeypatch.setattr(
            "seenzus_bridge.quick_pair._decode_jwt",
            lambda _hass, _token, _state=state: _state,
        )
        request = SimpleNamespace(
            app={"hass": hass},
            query={"state": "jwt-state", "code": f"code-{index}"},
        )
        response = await view.get(request)
        assert response.status == 200

    payloads = hass.data["seenzus_bridge"]["quick_pair_callback_payloads"]
    assert len(payloads) == QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT
    # 最旧的一条被逐出，最新存入的一条必然幸存。
    assert "pairing-state-0" not in payloads
    assert list(payloads) == [
        f"pairing-state-{i}" for i in range(1, QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT + 1)
    ]
    assert payloads[f"pairing-state-{QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT}"] == {
        "state": {
            "flow_id": f"flow-{QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT}",
            "pairing_state": f"pairing-state-{QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT}",
        },
        "code": f"code-{QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT}",
    }


def test_record_quick_pair_diagnostic_creates_persistent_notification(monkeypatch) -> None:
    hass = FakeHass()
    created: list[dict] = []

    def _fake_async_create(target_hass, message, *, title=None, notification_id=None):
        created.append(
            {
                "hass": target_hass,
                "message": message,
                "title": title,
                "notification_id": notification_id,
            }
        )

    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.persistent_notification",
        SimpleNamespace(async_create=_fake_async_create),
    )

    _record_quick_pair_diagnostic(
        hass,
        "quick_pair_session_failed",
        {"message": "boom", "http_status": "500"},
    )

    assert created == [
        {
            "hass": hass,
            "message": "快速配对失败：quick_pair_session_failed\n\nhttp_status=500 | message=boom",
            "title": "Seenzus Bridge 快速配对诊断",
            "notification_id": "seenzus_bridge_quick_pair_diagnostic",
        }
    ]


def _async_result(values: dict):
    async def _runner(*_args, **_kwargs):
        return _result_obj(values)

    return _runner()


def _result_obj(values: dict):
    class _Result:
        def __init__(self, payload: dict) -> None:
            self.__dict__.update(payload)

    return _Result(values)
