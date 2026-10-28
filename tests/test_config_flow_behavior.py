from __future__ import annotations

from types import SimpleNamespace

import pytest
from homeassistant import data_entry_flow as data_entry_flow_module
from homeassistant.data_entry_flow import FlowResultType, UnknownFlow

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
    _backend_bridge_name,
    _build_quick_pair_callback_context,
    _build_quick_pair_entry_data,
    _flatten_form_input,
    _mode_schema,
    _sanitize_app_return_url,
    _schema,
    _validate,
)
from seenzus_bridge.dev_override import DevOverrideError
from seenzus_bridge.pairing_bootstrap import _read_app_return_url
from seenzus_bridge.const import (
    CONF_BRIDGE_ID,
    CONF_CONFIG_SOURCE,
    CONF_MQTT_SCHEME,
    CONF_MQTT_WS_PATH,
    DEFAULT_PAIRING_API_BASE,
    CONF_PAIRING_API_BASE,
    CONF_PAIRING_MODE,
    CONF_TOPIC_ROOT,
)
from seenzus_bridge.quick_pair import (
    MAX_FLOW_RESUME_STEPS,
    QUICK_PAIR_APP_RETURN_PATH,
    QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT,
    SeenzusAppReturnView,
    _clear_quick_pair_notifications,
    _notify_app_return,
    _record_quick_pair_diagnostic,
)
from tests.helpers import FakeConfig, FakeConfigEntry, FakeHass


@pytest.fixture(autouse=True)
def stable_ha_identity(monkeypatch):
    async def identity(_hass):
        return "ha-install-test"
    monkeypatch.setattr("seenzus_bridge.config_flow._resolve_ha_instance_id", identity)


def _new_config_flow():
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow.context = {"source": "user"}
    return flow


def _schema_field_names(schema) -> set[str]:
    return {getattr(key, "schema", key) for key in schema.schema}


async def _dev_override_none(_hass):
    """monkeypatch 替身:无 dev 覆盖文件 → 配对走内置生产默认地址。"""
    return None


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


def test_validate_seamless_mode_needs_no_fields() -> None:
    # 快速配对无表单字段（API 地址走内置默认 / dev 覆盖文件），_validate 恒过。
    # api_base 合法性校验已挪到 dev_override（见 test_dev_override.py）。
    assert _validate({CONF_PAIRING_MODE: "seamless"}) == {}


def test_mode_schema_only_shows_pairing_mode() -> None:
    assert _schema_field_names(_mode_schema()) == {CONF_PAIRING_MODE}


def test_seamless_schema_has_no_fields() -> None:
    # 正式使用直接提交:快速配对表单零字段(API 地址走内置生产默认,联调用
    # <config>/seenzus_bridge_dev.json 覆盖,不经 UI)。
    assert _schema_field_names(_schema("seamless", {CONF_PAIRING_MODE: "seamless"})) == set()


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
    monkeypatch.setattr("seenzus_bridge.quick_pair.time.time", lambda: 1789500000.125)

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
            "pairing_started_at": 1789500000125,
            "exp": 1789500900,
        }
    ]
    assert len(hass.http.registered_views) == 1
    assert isinstance(hass.http.registered_views[0], SavanAIQuickPairCallbackView)


@pytest.mark.asyncio
async def test_quick_pair_callback_view_routes_options_flow(monkeypatch) -> None:
    state = {
        "flow_id": "options-flow-1",
        "flow_manager": FLOW_MANAGER_OPTIONS,
        "pairing_state": "pairing-state",
    }
    hass, request = _build_quick_pair_callback_request(monkeypatch, state=state)

    response = await SavanAIQuickPairCallbackView().get(request)

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
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
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
                    "host": "broker.example.com", "scheme": "mqtts",
                    "port": 1883,
                    "username": "user-1",
                    "password": "pass-1",
                    "topicRoot": "seenzus/v2",
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
    flow = _new_config_flow()
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
async def test_user_step_seamless_skips_environment_and_launches_local_handoff_after_scope(monkeypatch) -> None:
    from seenzus_bridge.quick_pair import _encode_jwt

    flow = _new_config_flow()
    flow.hass = FakeHass()
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    redirect_uri = f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}"
    token = _encode_jwt(flow.hass, {"flow_id": "first-install", "redirect_uri": redirect_uri})
    monkeypatch.setattr(
        "seenzus_bridge.config_flow._build_quick_pair_callback_context",
        lambda *_a, **_k: (redirect_uri, "ps", token),
    )
    monkeypatch.setattr(
        "seenzus_bridge.config_flow.resolve_dev_pairing_api_base", _dev_override_none
    )
    create_calls: list[dict] = []

    async def _fake_create(**kwargs):
        create_calls.append(dict(kwargs))
        return _result_obj(
            {"ok": True, "session_id": "wps_1", "pairing_page_url": "https://app.x/wps_1"}
        )

    monkeypatch.setattr("seenzus_bridge.config_flow.create_web_pairing_session", _fake_create)
    flow.async_external_step = lambda *, step_id, url, description_placeholders=None: {
        "type": "external",
        "step_id": step_id,
        "url": url,
    }

    assert (await flow.async_step_user({CONF_PAIRING_MODE: "seamless"}))["step_id"] == "scope"
    result = await flow.async_step_scope({"device_scope": "all"})

    assert result["type"] == "external"
    assert result["step_id"] == "seamless_authorize"
    assert create_calls == []
    assert result["url"].startswith("http://homeassistant.local:8123/api/seenzus_bridge/quick_pair/origin?")


@pytest.mark.asyncio
async def test_user_step_routes_to_manual_form(monkeypatch) -> None:
    flow = _new_config_flow()
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    flow.async_show_form = lambda *, step_id, data_schema, errors=None: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors or {},
    }

    assert (await flow.async_step_user({CONF_PAIRING_MODE: "manual"}))["step_id"] == "environment"
    assert (await flow.async_step_environment({"environment": "cn"}))["step_id"] == "scope"
    result = await flow.async_step_scope({"device_scope": "all"})

    assert result["step_id"] == "manual"
    assert CONF_MQTT_SETTINGS in _schema_field_names(result["data_schema"])


@pytest.mark.asyncio
async def test_seamless_step_starts_external_quick_pair(monkeypatch, bridge_manifest_version) -> None:
    flow = _new_config_flow()
    flow._selected_environment = "global"
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
                "pairing_page_url": "https://app.seenzus.xxx/web-pairing/wps_1",
            }
        )

    monkeypatch.setattr(
        "seenzus_bridge.config_flow.create_web_pairing_session",
        _fake_create_web_pairing_session,
    )
    monkeypatch.setattr(
        "seenzus_bridge.config_flow.resolve_dev_pairing_api_base", _dev_override_none
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
    assert result["url"] == "https://app.seenzus.xxx/web-pairing/wps_1"
    # 无 dev 覆盖文件 → 用内置生产默认地址。
    assert create_calls[0]["api_base"] == DEFAULT_PAIRING_API_BASE
    assert create_calls[0]["bridge_version"] == bridge_manifest_version
    assert create_calls[0]["redirect_uri"] == f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}"
    assert create_calls[0]["state"] == "jwt-state"


@pytest.mark.asyncio
async def test_seamless_uses_dev_override_api_base_when_present(monkeypatch) -> None:
    # 存在合法 dev 覆盖文件 → 配对连联调地址而非生产默认。
    flow = _new_config_flow()
    flow._selected_environment = "custom"
    flow.hass = FakeHass()
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    monkeypatch.setattr(
        "seenzus_bridge.config_flow._build_quick_pair_callback_context",
        lambda *_a, **_k: (f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}", "ps", "jwt"),
    )

    async def _override(_hass):
        return "http://192.168.9.99:5078/api"

    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", _override)

    create_calls: list[dict] = []

    async def _fake_create(**kwargs):
        create_calls.append(dict(kwargs))
        return _result_obj(
            {"ok": True, "session_id": "wps_1", "pairing_page_url": "https://app.x/wps_1"}
        )

    monkeypatch.setattr("seenzus_bridge.config_flow.create_web_pairing_session", _fake_create)
    flow.async_external_step = lambda *, step_id, url, description_placeholders=None: {
        "type": "external",
        "url": url,
    }

    await flow.async_step_seamless({})

    assert create_calls[0]["api_base"] == "http://192.168.9.99:5078/api"


@pytest.mark.asyncio
async def test_seamless_dev_override_invalid_blocks_pairing(monkeypatch) -> None:
    # fail-loud:dev 文件存在但非法 → 报错拦住配对,绝不静默连生产,不创建会话。
    flow = _new_config_flow()
    flow._selected_environment = "custom"
    flow.hass = FakeHass()
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    monkeypatch.setattr(
        "seenzus_bridge.config_flow._build_quick_pair_callback_context",
        lambda *_a, **_k: (f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}", "ps", "jwt"),
    )

    async def _override(_hass):
        raise DevOverrideError("bad url")

    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", _override)

    create_calls: list[dict] = []

    async def _fake_create(**kwargs):
        create_calls.append(dict(kwargs))
        return _result_obj({"ok": True})

    monkeypatch.setattr("seenzus_bridge.config_flow.create_web_pairing_session", _fake_create)
    flow.async_show_form = lambda *, step_id, data_schema, errors, description_placeholders=None: {
        "type": "form",
        "step_id": step_id,
        "errors": errors,
    }

    result = await flow.async_step_seamless({})

    assert result["type"] == "form"
    assert result["errors"] == {"base": "dev_override_invalid"}
    assert create_calls == []


@pytest.mark.asyncio
async def test_seamless_authorize_exchanges_callback_code(monkeypatch) -> None:
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
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
                    "host": "broker.example.com", "scheme": "mqtts",
                    "port": 1883,
                    "username": "user-1",
                    "password": "pass-1",
                    "topicRoot": "seenzus/v2",
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
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
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
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
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
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow.hass.config = FakeConfig({"version": "2026.3.0"})
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
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
                "host": "broker.example.com", "scheme": "mqtts",
                "port": 1883,
                "username": "user-1",
                "password": "pass-1",
                "topicRoot": "seenzus/v2",
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
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow.hass.config = FakeConfig({"version": "2026.3.0"})
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
    flow._quick_pair_exchange_result = _result_obj(
        {
            "ok": True,
            "session_id": "wps_1",
            "bridge_id": "ha-web-bridge",
            "config_source": "web_pair",
            "confirmed_at": "2026-04-20T12:01:22Z",
            "mqtt": {
                "host": "broker.example.com", "scheme": "mqtts",
                "port": 1883,
                "username": "user-1",
                "password": "pass-1",
                "topicRoot": "seenzus/v2",
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
    assert result["data"][CONF_PAIRING_API_BASE] == "https://api.seenzus.xxx/api"
    assert result["data"]["mqtt_host"] == "broker.example.com"
    assert result["data"][CONF_TOPIC_ROOT] == "seenzus/v2"
    assert result["data"][CONF_BRIDGE_ID] == "ha-web-bridge"


@pytest.mark.asyncio
async def test_options_init_offers_connection_settings_without_repairing() -> None:
    config_entry = type(
        "Entry",
        (),
        {"data": {"mqtt_host": "old-broker"}, "options": {}, "entry_id": "entry-1"},
    )()
    flow = SavanAIBridgeOptionsFlow(config_entry)
    result = await flow.async_step_init()

    assert result["type"] == FlowResultType.MENU
    assert result["step_id"] == "init"
    assert result["menu_options"] == ["connection_settings", "device_access", "pairing"]


@pytest.mark.asyncio
async def test_options_flow_creates_entry_with_flattened_data() -> None:
    config_entry = type(
        "Entry",
        (),
        {"data": {"mqtt_host": "old-broker"}, "options": {}, "entry_id": "entry-1"},
    )()
    flow = SavanAIBridgeOptionsFlow(config_entry)
    flow.async_create_entry = lambda *, title, data: {"type": "create_entry", "data": data}
    flow.hass = FakeHass()

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
async def test_options_seamless_step_uses_options_flow_manager(monkeypatch, bridge_manifest_version) -> None:
    config_entry = FakeConfigEntry(data={"mqtt_host": "old-broker", "source_id": "source-1", "pairing_api_base": "https://uat.seenzus.ai/api/seenzus"})
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
                "pairing_page_url": "https://app.seenzus.xxx/web-pairing/wps_1",
            }
        )

    monkeypatch.setattr(
        "seenzus_bridge.config_flow.create_web_pairing_session",
        _fake_create_web_pairing_session,
    )
    monkeypatch.setattr(
        "seenzus_bridge.config_flow.resolve_dev_pairing_api_base", _dev_override_none
    )
    flow.async_external_step = lambda *, step_id, url, description_placeholders=None: {
        "type": "external",
        "step_id": step_id,
        "url": url,
    }

    result = await flow.async_step_seamless({})

    assert context_calls == [(flow.hass, "options-flow-7", FLOW_MANAGER_OPTIONS)]
    # 模型 A:重配对无视 entry 存值,无 dev 文件 → 内置生产默认。
    assert create_calls[0]["api_base"] == "https://uat.seenzus.ai/api/seenzus"
    assert create_calls[0]["expected_source_id"] == "source-1"
    assert create_calls[0]["bridge_version"] == bridge_manifest_version
    assert create_calls[0]["state"] == "jwt-state"
    assert result["type"] == "external"
    assert result["step_id"] == "seamless_authorize"
    assert result["url"] == "https://app.seenzus.xxx/web-pairing/wps_1"


@pytest.mark.asyncio
async def test_options_seamless_retry_form_has_no_editable_binding() -> None:
    # 即使 entry 里残留联调地址,options 快速配对表单也是零字段——地址只在
    # 配对发起时由 dev_override 解析,重配对绝不沿用 entry 存值(模型 A)。
    config_entry = FakeConfigEntry(
        data={
            "mqtt_host": "old-broker",
            CONF_PAIRING_API_BASE: "http://192.168.9.99:5078",
        }
    )
    flow = SavanAIBridgeOptionsFlow(config_entry)
    flow.hass = FakeHass()
    flow.async_show_form = lambda *, step_id, data_schema, errors, description_placeholders=None: {
        "type": "form",
        "step_id": step_id,
        "data_schema": data_schema,
        "errors": errors,
    }

    result = await flow.async_step_seamless()

    assert result["type"] == "form"
    assert result["step_id"] == "seamless"
    assert result["errors"] == {"base": "quick_pair_missing_context"}
    assert _schema_field_names(result["data_schema"]) == set()


@pytest.mark.asyncio
async def test_options_seamless_finish_creates_entry_with_empty_title() -> None:
    config_entry = FakeConfigEntry(data={"mqtt_host": "old-broker"})
    flow = SavanAIBridgeOptionsFlow(config_entry)
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
    flow._quick_pair_exchange_result = _result_obj(
        {
            "ok": True,
            "session_id": "wps_1",
            "bridge_id": "ha-web-bridge",
            "config_source": "web_pair",
            "confirmed_at": "2026-04-20T12:01:22Z",
            "mqtt": {
                "host": "broker.example.com", "scheme": "mqtts",
                "port": 1883,
                "username": "user-1",
                "password": "pass-1",
                "topicRoot": "seenzus/v2",
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
    assert result["title"].endswith(" · 世界甲")
    assert result["data"]["mqtt_host"] == "broker.example.com"
    assert result["data"][CONF_BRIDGE_ID] == "ha-web-bridge"


@pytest.mark.asyncio
async def test_seamless_finish_error_reshows_seamless_form_without_placeholder_support() -> None:
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
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
    # 重显的快速配对表单无字段(直接提交),错误通过 errors 传达。
    assert _schema_field_names(result["data_schema"]) == set()


@pytest.mark.asyncio
async def test_seamless_finish_polls_legacy_status_until_bound(monkeypatch) -> None:
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
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
                    "host": "broker.example.com", "scheme": "mqtts",
                    "port": 1883,
                    "username": "user-1",
                    "password": "pass-1",
                    "topicRoot": "seenzus/v2",
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
    assert all(call["api_base"] == "https://api.seenzus.xxx/api" for call in status_calls)
    assert sleeps == [1]
    assert result["type"] == "create_entry"
    assert result["title"].endswith(" · 世界甲")
    assert result["data"][CONF_CONFIG_SOURCE] == "web_pair"
    assert result["data"]["mqtt_host"] == "broker.example.com"
    assert result["data"][CONF_BRIDGE_ID] == "ha-web-bridge"


@pytest.mark.asyncio
async def test_seamless_finish_reshows_form_when_session_never_bound(monkeypatch) -> None:
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
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
    # 重显的快速配对表单无字段(直接提交),错误通过 errors 传达。
    assert _schema_field_names(result["data_schema"]) == set()


@pytest.mark.asyncio
async def test_quick_pair_callback_view_rejects_missing_state() -> None:
    view = SavanAIQuickPairCallbackView()
    request = SimpleNamespace(app={"hass": FakeHass()}, query={})

    response = await view.get(request)

    assert response.status == 400
    assert response.text == "Missing state parameter"


def _build_quick_pair_callback_request(
    monkeypatch,
    *,
    state: dict | None = None,
    query: dict | None = None,
) -> tuple[FakeHass, SimpleNamespace]:
    """回调 view 测试共用脚手架：FakeHass + _decode_jwt 替身 + GET 请求。

    只构建不执行——需要先脚本化 hass.config_entries(flow / options 的
    configure_results 或自定义 async_configure)的用例，拿到返回值后自行调用
    ``SavanAIQuickPairCallbackView().get(request)``。
    """
    hass = FakeHass()
    state = state or {"flow_id": "flow-1", "pairing_state": "pairing-state"}
    if state.get("flow_id") and state.get("pairing_state"):
        manager = state.get("flow_manager", "config")
        hass.data.setdefault("seenzus_bridge", {}).setdefault("quick_pair_active_states", {})[(manager, state["flow_id"])] = state["pairing_state"]
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair._decode_jwt",
        lambda _hass, _token: state,
    )
    query_full = {"state": "jwt-state", "code": "auth-code"}
    query_full.update(query or {})
    return hass, SimpleNamespace(app={"hass": hass}, query=query_full)


@pytest.mark.asyncio
async def test_quick_pair_callback_view_returns_202_when_flow_resume_fails(monkeypatch) -> None:
    state = {"flow_id": "flow-9", "pairing_state": "pairing-state"}
    hass, request = _build_quick_pair_callback_request(monkeypatch, state=state)

    async def _explode(*_args, **_kwargs):
        raise RuntimeError("flow gone")

    hass.config_entries.flow.async_configure = _explode

    response = await SavanAIQuickPairCallbackView().get(request)

    assert response.status == 202
    assert "return to Home Assistant" in response.text
    assert hass.data["seenzus_bridge"]["quick_pair_callback_payloads"]["pairing-state"] == {
        "state": state,
        "code": "auth-code",
    }


@pytest.mark.parametrize("app_value", [None, "0", "false", "true", "desktop"])
@pytest.mark.asyncio
async def test_quick_pair_callback_view_without_app_flag_keeps_single_configure(
    monkeypatch, app_value
) -> None:
    """浏览器路径(无 app 标记)行为一字不变:只 resume 一次。

    首次 configure 停在 EXTERNAL_STEP_DONE、剩余推进交给常驻 HA 前端标签页
    ——这是 issue #43 修复后刻意保留的网页版语义。
    """
    query = {"app": app_value} if app_value is not None else None
    hass, request = _build_quick_pair_callback_request(monkeypatch, query=query)
    hass.config_entries.flow.configure_results = [
        {"type": FlowResultType.EXTERNAL_STEP_DONE, "step_id": "seamless_authorize"}
    ]

    response = await SavanAIQuickPairCallbackView().get(request)

    assert response.status == 200
    assert len(hass.config_entries.flow.configure_calls) == 1
    assert "window.close()" in response.text


@pytest.mark.asyncio
async def test_quick_pair_callback_view_with_app_flag_drives_flow_to_create_entry(
    monkeypatch,
) -> None:
    """App 内嵌 WebView(?app=1)没有常驻前端页面,回调 view 自己把 flow 推到终态。"""
    hass, request = _build_quick_pair_callback_request(monkeypatch, query={"app": "1"})
    hass.config_entries.flow.configure_results = [
        {"type": FlowResultType.EXTERNAL_STEP_DONE, "step_id": "seamless_authorize"},
        {"type": FlowResultType.CREATE_ENTRY, "title": "seenzus Bridge"},
    ]

    response = await SavanAIQuickPairCallbackView().get(request)

    assert response.status == 200
    assert len(hass.config_entries.flow.configure_calls) == 2
    # 关不掉的 WebView 里 300ms 后跳回集成页,不再停在白屏。
    assert "window.close()" in response.text
    assert "location.replace('/config/integrations')" in response.text


@pytest.mark.asyncio
async def test_quick_pair_callback_view_with_app_flag_stops_at_form(monkeypatch) -> None:
    """FORM 是合法终点(code exchange 失败重显表单),不能再推。"""
    hass, request = _build_quick_pair_callback_request(monkeypatch, query={"app": "1"})
    hass.config_entries.flow.configure_results = [
        {"type": FlowResultType.EXTERNAL_STEP_DONE, "step_id": "seamless_authorize"},
        {"type": FlowResultType.FORM, "step_id": "seamless", "errors": {"base": "quick_pair_code_exchange_failed"}},
    ]

    response = await SavanAIQuickPairCallbackView().get(request)

    assert response.status == 200
    assert len(hass.config_entries.flow.configure_calls) == 2


@pytest.mark.asyncio
async def test_quick_pair_callback_view_with_app_flag_caps_resume_steps(monkeypatch) -> None:
    """永远停在 EXTERNAL_STEP_DONE 的病态 flow:推进带上限,防死循环。"""
    hass, request = _build_quick_pair_callback_request(monkeypatch, query={"app": "1"})
    hass.config_entries.flow.configure_results = [
        {"type": FlowResultType.EXTERNAL_STEP_DONE} for _ in range(MAX_FLOW_RESUME_STEPS * 4)
    ]

    response = await SavanAIQuickPairCallbackView().get(request)

    assert response.status == 500
    assert "setup did not complete" in response.text
    # 首次 configure + 至多 MAX_FLOW_RESUME_STEPS 次推进。
    assert len(hass.config_entries.flow.configure_calls) == 1 + MAX_FLOW_RESUME_STEPS


@pytest.mark.asyncio
async def test_quick_pair_callback_view_treats_unknown_flow_as_success(monkeypatch) -> None:
    """前端抢先把 flow 跑完并移除 → UnknownFlow,视为成功而非报错。"""
    hass, request = _build_quick_pair_callback_request(monkeypatch, query={"app": "1"})
    calls: list[dict] = []

    async def _configure(*, flow_id: str, user_input: dict):
        calls.append({"flow_id": flow_id, "user_input": user_input})
        if len(calls) == 1:
            return {"type": FlowResultType.EXTERNAL_STEP_DONE}
        from seenzus_bridge.quick_pair import _mark_quick_pair_completed, _confirm_quick_pair_completed, _quick_pair_finish_removed
        from homeassistant.config_entries import ConfigEntryState
        from tests.helpers import FakeConfigEntry
        entry_data = {"pairing_session_id": "wps_test", "environment": "uat", "world_id": "world-a"}
        entry = FakeConfigEntry(data=entry_data)
        entry.state = ConfigEntryState.LOADED
        hass.config_entries.entries.append(entry)
        _mark_quick_pair_completed(hass, "pairing-state", entry_data)
        _quick_pair_finish_removed(hass, "pairing-state")
        assert _confirm_quick_pair_completed(hass, "pairing-state")
        raise UnknownFlow()

    hass.config_entries.flow.async_configure = _configure

    response = await SavanAIQuickPairCallbackView().get(request)

    assert response.status == 200
    assert "window.close()" in response.text
    assert len(calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_saved", [False, True])
async def test_callback_does_not_redrive_flow_while_entry_is_being_finalized(monkeypatch, entry_saved):
    from seenzus_bridge.quick_pair import _mark_quick_pair_completed
    hass, request = _build_quick_pair_callback_request(monkeypatch)
    data = {"pairing_session_id": "wps_test", "environment": "uat", "world_id": "world-a"}
    _mark_quick_pair_completed(hass, "pairing-state", data)
    if entry_saved:
        from tests.helpers import FakeConfigEntry
        hass.config_entries.entries.append(FakeConfigEntry(data=data))
    response = await SavanAIQuickPairCallbackView().get(request)
    assert response.status == 202
    assert "still being finalized" in response.text
    assert hass.config_entries.flow.configure_calls == []


@pytest.mark.asyncio
async def test_cancelled_uncommitted_entry_does_not_report_success(monkeypatch):
    from seenzus_bridge.quick_pair import _mark_quick_pair_completed
    hass, request = _build_quick_pair_callback_request(monkeypatch)
    _mark_quick_pair_completed(hass, "pairing-state", {"pairing_session_id": "wps_test", "environment": "uat", "world_id": "world-a"})
    hass.config_entries.flow.async_get = lambda _: (_ for _ in ()).throw(UnknownFlow())
    response = await SavanAIQuickPairCallbackView().get(request)
    assert response.status == 410


@pytest.mark.asyncio
async def test_cancelled_quick_pair_callback_does_not_report_success(monkeypatch):
    hass, request = _build_quick_pair_callback_request(monkeypatch, query={"app": "1"})
    async def cancelled(**kwargs):
        raise UnknownFlow()
    hass.config_entries.flow.async_configure = cancelled
    response = await SavanAIQuickPairCallbackView().get(request)
    assert response.status == 410
    from seenzus_bridge.quick_pair import _pop_quick_pair_callback_payload
    assert _pop_quick_pair_callback_payload(hass, "pairing-state") is None


@pytest.mark.asyncio
async def test_quick_pair_callback_view_with_app_flag_drives_options_flow(monkeypatch) -> None:
    """Options flow(FLOW_MANAGER_OPTIONS)走同一条推进路径,一并覆盖。"""
    state = {
        "flow_id": "options-flow-1",
        "flow_manager": FLOW_MANAGER_OPTIONS,
        "pairing_state": "pairing-state",
    }
    hass, request = _build_quick_pair_callback_request(
        monkeypatch, state=state, query={"app": "1"}
    )
    hass.config_entries.options.configure_results = [
        {"type": FlowResultType.EXTERNAL_STEP_DONE, "step_id": "seamless_authorize"},
        {"type": FlowResultType.CREATE_ENTRY, "title": "seenzus Bridge"},
    ]

    response = await SavanAIQuickPairCallbackView().get(request)

    assert response.status == 200
    assert len(hass.config_entries.options.configure_calls) == 2
    assert hass.config_entries.flow.configure_calls == []


@pytest.mark.asyncio
async def test_quick_pair_callback_mailbox_evicts_oldest_beyond_cap(monkeypatch) -> None:
    hass = FakeHass()
    view = SavanAIQuickPairCallbackView()

    for index in range(QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT + 1):
        state = {"flow_id": f"flow-{index}", "pairing_state": f"pairing-state-{index}"}
        hass.data.setdefault("seenzus_bridge", {}).setdefault("quick_pair_active_states", {})[("config", state["flow_id"])] = state["pairing_state"]
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
            "message": "Quick pairing failed: quick_pair_session_failed\n\nhttp_status=500 | message=boom",
            "title": "seenzus Bridge quick pairing diagnostics",
            "notification_id": "seenzus_bridge_quick_pair_diagnostic",
        }
    ]
    # 同一份诊断落进 hass.data，配对状态传感器作为持久 UI 面读取。
    assert (
        hass.data["seenzus_bridge"]["quick_pair_last_diagnostic"]
        == "quick_pair_session_failed | http_status=500 | message=boom"
    )


def test_notify_app_return_creates_notification_and_clears_diagnostic(monkeypatch) -> None:
    hass = FakeHass()
    created: list[dict] = []
    dismissed: list[str] = []

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
        SimpleNamespace(
            async_create=_fake_async_create,
            async_dismiss=lambda target_hass, notification_id: dismissed.append(notification_id),
        ),
    )
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair._encode_jwt",
        lambda _hass, _payload: "app-return-token",
    )

    # 预置一条上次失败的诊断：配对成功后必须被清除（传感器不再显示陈旧失败）。
    hass.data.setdefault("seenzus_bridge", {})["quick_pair_last_diagnostic"] = "stale | x=y"

    _notify_app_return(hass, "seenzus://pairing/done?session=wps_1")

    assert "quick_pair_last_diagnostic" not in hass.data["seenzus_bridge"]
    # The link routes through the transit endpoint (click dismisses the
    # notification server-side), not the raw app URL — and it must be an inline
    # HTML anchor WITH target: a same-origin targetless <a> gets intercepted by
    # the frontend as an SPA pushState route and never reaches the transit view.
    assert created == [
        {
            "hass": hass,
            "message": (
                "seenzus MQTT Bridge is paired.\n\n## 👉 "
                f'<a href="{QUICK_PAIR_APP_RETURN_PATH}?token=app-return-token" '
                'target="_blank">Return to seenzus</a>'
            ),
            "title": "seenzus Bridge paired",
            "notification_id": "seenzus_bridge_app_return",
        }
    ]
    # Success supersedes a prior failure: the diagnostic notification is cleared.
    assert dismissed == ["seenzus_bridge_quick_pair_diagnostic"]
    # Building the transit link registered the app-return view.
    assert any(isinstance(view, SeenzusAppReturnView) for view in hass.http.registered_views)


def test_notify_app_return_falls_back_to_raw_url_without_http(monkeypatch) -> None:
    # No hass.http surface -> transit link can't be built -> the notification
    # still goes out, linking the raw URL directly (no auto-dismiss).
    hass = FakeHass()
    del hass.http
    created: list[str] = []

    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.persistent_notification",
        SimpleNamespace(
            async_create=lambda target_hass, message, *, title=None, notification_id=None: created.append(message),
            async_dismiss=lambda target_hass, notification_id: None,
        ),
    )

    _notify_app_return(hass, "seenzus://pairing/done?session=wps_1")

    # 回退直链附带可复制的明文地址：自定义 scheme 的 href 会被前端 markdown
    # 的 xss 过滤器清空，锚点可能是死链接。
    assert created == [
        "seenzus MQTT Bridge is paired.\n\n## 👉 "
        '<a href="seenzus://pairing/done?session=wps_1" target="_blank">Return to seenzus</a>'
        "\n\nIf the link does not open, open this address manually: `seenzus://pairing/done?session=wps_1`"
    ]


@pytest.mark.asyncio
async def test_app_return_view_rejects_missing_or_invalid_token(monkeypatch) -> None:
    view = SeenzusAppReturnView()

    response = await view.get(SimpleNamespace(app={"hass": FakeHass()}, query={}))
    assert response.status == 400
    assert response.text == "Missing token parameter"

    monkeypatch.setattr("seenzus_bridge.quick_pair._decode_jwt", lambda _hass, _token: None)
    response = await view.get(SimpleNamespace(app={"hass": FakeHass()}, query={"token": "bogus"}))
    assert response.status == 400
    assert response.text == "Invalid token parameter"


@pytest.mark.asyncio
async def test_app_return_view_rejects_disallowed_scheme(monkeypatch) -> None:
    dismissed: list[str] = []
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.persistent_notification",
        SimpleNamespace(async_dismiss=lambda _hass, notification_id: dismissed.append(notification_id)),
    )
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair._decode_jwt",
        lambda _hass, _token: {"app_return_url": "javascript:alertOne"},
    )

    response = await SeenzusAppReturnView().get(
        SimpleNamespace(app={"hass": FakeHass()}, query={"token": "signed"})
    )

    assert response.status == 400
    assert response.text == "Invalid return URL"
    # A rejected target must not consume the notification.
    assert dismissed == []


@pytest.mark.asyncio
async def test_app_return_view_dismisses_notification_and_redirects_http(monkeypatch) -> None:
    dismissed: list[str] = []
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.persistent_notification",
        SimpleNamespace(async_dismiss=lambda _hass, notification_id: dismissed.append(notification_id)),
    )
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair._decode_jwt",
        lambda _hass, _token: {"app_return_url": "https://app.seenzus.ai/integrations/ha?bound=1"},
    )

    response = await SeenzusAppReturnView().get(
        SimpleNamespace(app={"hass": FakeHass()}, query={"token": "signed"})
    )

    assert response.status == 302
    assert response.headers["Location"] == "https://app.seenzus.ai/integrations/ha?bound=1"
    assert dismissed == ["seenzus_bridge_app_return"]


@pytest.mark.asyncio
async def test_app_return_view_dismisses_notification_and_renders_deep_link_page(monkeypatch) -> None:
    dismissed: list[str] = []
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.persistent_notification",
        SimpleNamespace(async_dismiss=lambda _hass, notification_id: dismissed.append(notification_id)),
    )
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair._decode_jwt",
        lambda _hass, _token: {"app_return_url": "seenzus://pairing/done?session=wps_1"},
    )

    response = await SeenzusAppReturnView().get(
        SimpleNamespace(app={"hass": FakeHass()}, query={"token": "signed"})
    )

    # Custom scheme -> HTML redirect page (a 302 Location to seenzus:// is
    # unreliable in some browsers/webviews), with a manual fallback link.
    assert response.status == 200
    assert response.content_type == "text/html"
    assert 'href="seenzus://pairing/done?session=wps_1"' in response.text
    assert 'window.location.href = "seenzus://pairing/done?session=wps_1"' in response.text
    assert dismissed == ["seenzus_bridge_app_return"]


@pytest.mark.asyncio
async def test_app_return_view_escapes_script_breakout_in_deep_link_page(monkeypatch) -> None:
    # json.dumps 不转义 "<"：字面 "</script>" 会终结脚本块。scheme 白名单挡不住
    # 这种载荷（scheme 仍是 seenzus），页面必须自行把 "<" 转义为 <。
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.persistent_notification",
        SimpleNamespace(async_dismiss=lambda _hass, notification_id: None),
    )
    hostile = "seenzus://x</script><script>doEvil()</script>"
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair._decode_jwt",
        lambda _hass, _token: {"app_return_url": hostile},
    )

    response = await SeenzusAppReturnView().get(
        SimpleNamespace(app={"hass": FakeHass()}, query={"token": "signed"})
    )

    assert response.status == 200
    # 脚本字符串里不得出现未转义的 "<"；href 属性由 html.escape 兜底。
    assert "<script>doEvil()" not in response.text
    assert '"seenzus://x\\u003c/script>\\u003cscript>doEvil()\\u003c/script>"' in response.text
    assert 'href="seenzus://x&lt;/script&gt;&lt;script&gt;doEvil()&lt;/script&gt;"' in response.text


def test_backend_bridge_name_appends_home_name() -> None:
    hass = SimpleNamespace(config=SimpleNamespace(location_name="我的家"))
    assert _backend_bridge_name(hass) == f"{PLUGIN_NAME} · 我的家"


def test_backend_bridge_name_falls_back_when_no_home_name() -> None:
    # Empty / whitespace / missing home name -> bare plugin name, never a dangling " · ".
    assert _backend_bridge_name(SimpleNamespace(config=SimpleNamespace(location_name=""))) == PLUGIN_NAME
    assert _backend_bridge_name(SimpleNamespace(config=SimpleNamespace(location_name="   "))) == PLUGIN_NAME
    assert _backend_bridge_name(SimpleNamespace(config=SimpleNamespace())) == PLUGIN_NAME
    # A home name identical to the plugin name adds no distinguishing value.
    assert _backend_bridge_name(SimpleNamespace(config=SimpleNamespace(location_name=PLUGIN_NAME))) == PLUGIN_NAME


def test_backend_bridge_name_sanitizes_home_name() -> None:
    # Control chars / newlines are stripped so the bridgeName stays a single line.
    hass = SimpleNamespace(config=SimpleNamespace(location_name="我的\n家\t里"))
    assert _backend_bridge_name(hass) == f"{PLUGIN_NAME} · 我的家里"
    # Printable non-ASCII (Chinese / emoji) is preserved.
    hass = SimpleNamespace(config=SimpleNamespace(location_name="客厅🏠"))
    assert _backend_bridge_name(hass) == f"{PLUGIN_NAME} · 客厅🏠"
    # An oversized home name is truncated to keep the POST field bounded.
    hass = SimpleNamespace(config=SimpleNamespace(location_name="家" * 100))
    result = _backend_bridge_name(hass)
    assert result == f"{PLUGIN_NAME} · {'家' * 64}"


def test_clear_quick_pair_notifications_dismisses_both(monkeypatch) -> None:
    hass = FakeHass()
    dismissed: list[str] = []

    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.persistent_notification",
        SimpleNamespace(
            async_dismiss=lambda target_hass, notification_id: dismissed.append(notification_id),
        ),
    )

    hass.data.setdefault("seenzus_bridge", {})["quick_pair_last_diagnostic"] = "stale | x=y"

    _clear_quick_pair_notifications(hass)

    assert dismissed == ["seenzus_bridge_app_return", "seenzus_bridge_quick_pair_diagnostic"]
    # 无链接成功路径同样清除传感器上的陈旧失败诊断。
    assert "quick_pair_last_diagnostic" not in hass.data["seenzus_bridge"]


@pytest.mark.parametrize(
    "value,expected",
    [
        (
            "https://app.seenzus.ai/integrations/ha?bound=1",
            "https://app.seenzus.ai/integrations/ha?bound=1",
        ),
        ("seenzus://pairing/done?session=wps_1", "seenzus://pairing/done?session=wps_1"),
        ("  https://app.seenzus.ai/x  ", "https://app.seenzus.ai/x"),
        # opaque schemes without //host are rejected (no authority)
        ("seenzus:done", None),
        ("mailto:victim@example.com", None),
        ("tel:+10000000000", None),
        # userinfo '@' authority (phishing: trusted-looking prefix, real host differs)
        ("https://app.seenzus.ai@evil.example/x", None),
        ("https://user@host/x", None),
        # HA placeholder delimiters in the value
        ("https://app.seenzus.ai/x?y={z}", None),
        ("javascript:alertOne", None),
        ("data:text/html,hello", None),
        ("https://exa mple.com", None),
        ("https://example.com/a(b)", None),
        ("https://example.com/x?a]b", None),
        ("https://example.com/x[1]", None),
        ('https://example.com/x"y', None),
        ("https://example.com/x<y>", None),
        ("https://example.com/x`y", None),
        # backslash: urlparse keeps it in the host, browsers fold it to '/' →
        # host-spoofing differential, so it must be rejected
        ("https://\\evil.example/x", None),
        ("https://app.seenzus.ai\\@evil.example/x", None),
        # allow-list: only http(s) / seenzus survive even with a //host authority
        ("intent://scan/#Intent;scheme=foo;end", None),
        ("ftp://host/x", None),
        ("ssh://host/x", None),
        # non-printable-ASCII: zero-width, BOM, control byte, non-ASCII homograph
        ("https://app.seenzus.ai​/x", None),
        ("https://app.seenzus.ai﻿/x", None),
        ("https://app.seenzus.ai\x00/x", None),
        ("https://app.seenzús.ai/x", None),
        ("https://", None),
        ("", None),
        (None, None),
    ],
)
def test_sanitize_app_return_url(value, expected) -> None:
    assert _sanitize_app_return_url(value) == expected


def _pairing_result_with_mqtt(mqtt: dict):
    return _result_obj(
        {
            "session_id": "wps_1",
            "confirmed_at": "2026-07-03T00:00:00Z",
            "config_source": "web_pair",
            "mqtt": {
                "host": "edge.seenzus.ai",
                "port": 443,
                "username": "bridge-user",
                "password": "bridge-pass",
                **mqtt,
            },
        }
    )


def test_build_quick_pair_entry_data_stores_wss_scheme_and_path() -> None:
    data = _build_quick_pair_entry_data(
        api_base="https://api.seenzus.xxx/api",
        status_result=_pairing_result_with_mqtt({"scheme": "wss", "path": "/mqtt"}),
    )
    assert data[CONF_MQTT_SCHEME] == "wss"
    assert data[CONF_MQTT_WS_PATH] == "/mqtt"

    # scheme 大写归一为小写；path 缺前导斜杠时补上。
    data = _build_quick_pair_entry_data(
        api_base="https://api.seenzus.xxx/api",
        status_result=_pairing_result_with_mqtt({"scheme": "WSS", "path": "mqtt"}),
    )
    assert data[CONF_MQTT_SCHEME] == "wss"
    assert data[CONF_MQTT_WS_PATH] == "/mqtt"


def test_build_quick_pair_local_tcp_replaces_stale_websocket_path() -> None:
    data = _build_quick_pair_entry_data(
        api_base="https://api.example.com",
        status_result=_pairing_result_with_mqtt({"host": "192.168.1.2", "scheme": "mqtt", "path": "/old"}),
    )
    assert data[CONF_MQTT_SCHEME] == "mqtt"
    assert data[CONF_MQTT_WS_PATH] == ""


def test_build_quick_pair_entry_data_normalizes_secure_transport_path() -> None:
    for scheme, path in [("mqtts", "/stray"), ("wss", None)]:
        data = _build_quick_pair_entry_data(
            api_base="https://api.example.com",
            status_result=_pairing_result_with_mqtt({"scheme": scheme, "path": path}),
        )
        assert data[CONF_MQTT_SCHEME] == scheme
        assert data[CONF_MQTT_WS_PATH] == ""


def test_build_quick_pair_entry_data_defaults_port_by_scheme() -> None:
    # 响应缺 port 时按 scheme 取惯例端口，而不是一律 1883（wss 回退 1883 会拿
    # TLS websocket 去连明文 TCP 端口）；显式 port 始终优先。
    cases = [({"host": "192.168.1.2", "scheme": "mqtt"}, 1883), ({"scheme": "mqtts"}, 8883), ({"host": "::1", "scheme": "ws"}, 80), ({"scheme": "wss"}, 443)]
    for mqtt, expected_port in cases:
        result = _pairing_result_with_mqtt(mqtt)
        result.mqtt.pop("port")
        data = _build_quick_pair_entry_data(api_base="https://api.seenzus.xxx/api", status_result=result)
        assert data["mqtt_port"] == expected_port, mqtt

    data = _build_quick_pair_entry_data(
        api_base="https://api.seenzus.xxx/api",
        status_result=_pairing_result_with_mqtt({"scheme": "wss", "port": 8443}),
    )
    assert data["mqtt_port"] == 8443


def test_read_app_return_url_accepts_key_aliases() -> None:
    assert _read_app_return_url({"appReturnUrl": " seenzus://done "}) == "seenzus://done"
    assert _read_app_return_url({"returnUrl": "https://x/y"}) == "https://x/y"
    assert _read_app_return_url({"foo": "bar"}) is None
    assert _read_app_return_url({}) is None


@pytest.mark.asyncio
async def test_seamless_captures_app_return_url_from_session(monkeypatch) -> None:
    flow = _new_config_flow()
    flow._selected_environment = "global"
    flow.hass = FakeHass()
    monkeypatch.setattr(flow, "_async_current_entries", lambda: [])
    monkeypatch.setattr(
        "seenzus_bridge.config_flow._build_quick_pair_callback_context",
        lambda *_args, **_kwargs: (
            f"http://homeassistant.local:8123{QUICK_PAIR_CALLBACK_PATH}",
            "pairing-state",
            "jwt-state",
        ),
    )

    async def _fake_create_web_pairing_session(**_kwargs):
        return _result_obj(
            {
                "ok": True,
                "session_id": "wps_1",
                "pairing_page_url": "https://app.seenzus.xxx/web-pairing/wps_1",
                "app_return_url": "seenzus://pairing/done",
            }
        )

    monkeypatch.setattr(
        "seenzus_bridge.config_flow.create_web_pairing_session",
        _fake_create_web_pairing_session,
    )
    monkeypatch.setattr(
        "seenzus_bridge.config_flow.resolve_dev_pairing_api_base", _dev_override_none
    )
    flow.async_external_step = lambda *, step_id, url, description_placeholders=None: {
        "type": "external",
        "step_id": step_id,
        "url": url,
    }

    await flow.async_step_seamless({})

    assert flow._quick_pair_app_return_url == "seenzus://pairing/done"


@pytest.mark.asyncio
async def test_seamless_finish_creates_entry_with_return_link(monkeypatch) -> None:
    notified: list[dict] = []
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair.persistent_notification",
        SimpleNamespace(
            async_create=lambda hass, message, *, title=None, notification_id=None: notified.append(
                {"message": message, "notification_id": notification_id}
            ),
            async_dismiss=lambda hass, notification_id: None,
        ),
    )
    monkeypatch.setattr(
        "seenzus_bridge.quick_pair._encode_jwt",
        lambda _hass, _payload: "app-return-token",
    )
    flow = _new_config_flow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://api.seenzus.xxx/api"
    flow._quick_pair_session_id = "wps_1"
    flow._quick_pair_page_url = "https://app.seenzus.xxx/web-pairing/wps_1"
    flow._quick_pair_callback_state = "pairing-state"
    flow._quick_pair_exchange_result = _result_obj(
        {
            "ok": True,
            "session_id": "wps_1",
            "config_source": "web_pair",
            "confirmed_at": "2026-04-20T12:01:22Z",
            "app_return_url": "seenzus://pairing/done?session=wps_1",
            "mqtt": {
                "host": "broker.example.com", "scheme": "mqtts",
                "port": 1883,
                "username": "user-1",
                "password": "pass-1",
                "topicRoot": "seenzus/v2",
                "bridgeId": "ha-web-bridge",
            },
        }
    )
    created: list[dict] = []

    def _create_entry(*, title, data, description=None, description_placeholders=None):
        created.append(
            {
                "data": data,
                "description": description,
                "description_placeholders": description_placeholders,
            }
        )
        return {"type": "create_entry", "data": data}

    flow.async_create_entry = _create_entry

    # The entry is created immediately; the return link rides on HA's native
    # create-entry success page (description="app_return"), so tapping it can
    # never abandon the flow before the binding is committed.
    result = await flow.async_step_seamless_finish()
    assert result["type"] == "create_entry"
    assert created and created[0]["data"]["mqtt_host"] == "broker.example.com"
    assert created[0]["data"][CONF_BRIDGE_ID] == "ha-web-bridge"
    assert created[0]["description"] == "app_return"
    # The success-page link stays the raw app URL (the dialog is HA-native and
    # cannot be closed from a link click, so no transit indirection there).
    assert created[0]["description_placeholders"] == {
        "app_return_url": "seenzus://pairing/done?session=wps_1"
    }
    assert notified == []
    from homeassistant.config_entries import ConfigEntryState
    from tests.helpers import FakeConfigEntry
    entry = FakeConfigEntry(data=created[0]["data"])
    entry.state = ConfigEntryState.LOADED
    flow.hass.config_entries.entries.append(entry)
    flow.async_remove()
    # The same link is also pushed as a durable persistent notification, so the
    # options / re-pair path (no create_entry success page) still surfaces it.
    # There it routes through the transit endpoint: clicking dismisses the
    # notification server-side before redirecting to the app.
    assert notified == [
        {
            "message": (
                "seenzus MQTT Bridge is paired.\n\n## 👉 "
                f'<a href="{QUICK_PAIR_APP_RETURN_PATH}?token=app-return-token" '
                'target="_blank">Return to seenzus</a>'
            ),
            "notification_id": "seenzus_bridge_app_return",
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

    return _Result({"world_id": "world-a", "world_name": "世界甲", **values})
