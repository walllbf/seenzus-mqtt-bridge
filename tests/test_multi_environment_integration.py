"""Grants must also cover the snapshot, replay and display paths added on main."""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import pytest

from seenzus_bridge.ha_dispatcher import dispatch
from tests.helpers import AsyncFakeMQTTClient
from tests.test_device_access import home


def messages(client):
    return [json.loads(message["payload"]) for message in client.published]


@pytest.mark.asyncio
async def test_selected_scope_filters_negotiated_snapshot_and_cached_retry(home):
    _, _, _, entry, bridge = home
    client = AsyncFakeMQTTClient()
    request = json.dumps({"method": "GET", "path": "/api/seenzus/states/snapshot"})
    await bridge._handle_v2_command("snapshot", request, client)
    published = messages(client)
    states = [message for message in published if "entityId" in message]
    assert {state["entityId"] for state in states} == {"light.city", "input_boolean.helper"}
    assert published[0]["data"]["expectedCount"] == 2
    assert published[-1]["data"]["sentCount"] == 2
    assert all("private_url" not in state["attributes"] for state in states)
    assert "seenzus_display" in next(state for state in states if state["entityId"] == "light.city")["attributes"]

    entry.options = {"device_scope": "selected", "allowed_device_ids": [], "allowed_entity_registry_ids": []}
    client.published.clear()
    await bridge._handle_v2_command("snapshot", request, client)
    retried = messages(client)
    assert len(retried) == 2
    assert retried[0]["data"]["expectedCount"] == 0
    assert retried[-1]["data"]["sentCount"] == 0


@pytest.mark.asyncio
async def test_recorder_replay_cannot_publish_other_entries_devices(home):
    hass, _, _, _, bridge = home
    client = AsyncFakeMQTTClient()
    bridge._async_fetch_history_states = AsyncMock(return_value=[
        hass.states.get("light.city"), hass.states.get("light.suburb"),
    ])
    now = datetime.now(timezone.utc)
    await bridge._run_history_replay(now, now, client=client)
    published = messages(client)
    assert len(published) == 1 and published[0]["entityId"] == "light.city"
    assert "private_url" not in published[0]["attributes"]
    assert "seenzus_display" in published[0]["attributes"]


@pytest.mark.asyncio
async def test_revoke_during_display_loading_stops_snapshot_before_state_publish(home, monkeypatch):
    _, _, _, entry, bridge = home
    client = AsyncFakeMQTTClient()

    async def revoke(*_args):
        entry.options = {"device_scope": "selected", "allowed_device_ids": [], "allowed_entity_registry_ids": []}

    monkeypatch.setattr("seenzus_bridge.coordinator.async_prepare_sensor_display", revoke)
    await bridge._publish_snapshot_stream(client, "snapshot")
    published = messages(client)
    assert len(published) == 2
    assert published[-1]["status"] == 409
    assert published[-1]["data"]["outcome"] == "failed"
    assert published[-1]["data"]["sentCount"] == 0


@pytest.mark.asyncio
async def test_restricted_service_index_exposes_only_callable_services(home):
    hass, _, _, _, bridge = home
    hass.services.async_services = lambda: {
        "light": {"turn_on": {}, "private_service": {}},
        "script": {"private_alarm": {}}, "shell_command": {"secret_command": {}},
    }
    result = await dispatch(hass, "GET", "/api/seenzus/services/index", None, bridge._dispatch_policy())
    assert result.data == {"version": 1, "isComplete": True, "count": 1, "services": ["light.turn_on"]}


@pytest.mark.asyncio
async def test_scope_menu_preserves_local_budget_transport_and_credentials(home):
    from seenzus_bridge.config_flow import SavanAIBridgeOptionsFlow

    hass, _, _, entry, _ = home
    entry.data.update({"mqtt_scheme": "wss", "mqtt_password": "unchanged", "world_id": "world-a", "environment": "cn"})
    entry.options["mqtt_max_packet_size_kib"] = 2048
    flow = SavanAIBridgeOptionsFlow(entry)
    flow.hass = hass
    flow.async_create_entry = lambda **kwargs: kwargs
    menu = await flow.async_step_init()
    assert menu["menu_options"] == ["connection_settings", "device_access", "pairing"]
    await flow.async_step_device_access()
    result = await flow.async_step_scope({"device_scope": "all"})
    assert result["data"]["mqtt_max_packet_size_kib"] == 2048
    assert result["data"]["mqtt_password"] == "unchanged"
    assert result["data"]["mqtt_scheme"] == "wss"
    assert result["data"]["world_id"] == "world-a"
    assert result["data"]["environment"] == "cn"


@pytest.mark.asyncio
@pytest.mark.parametrize("preset,expected", [(None, "cn,global"), ("http://dev.local/api", "cn,global,uat")])
async def test_relay_recovery_choices_follow_local_developer_preset(home, monkeypatch, preset, expected):
    from seenzus_bridge.config_flow import SavanAIBridgeConfigFlow

    hass, _, _, _, _ = home
    flow = SavanAIBridgeConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow.flow_id = "relay-choices"
    flow.async_external_step = lambda **kwargs: kwargs
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", AsyncMock(return_value=preset))
    monkeypatch.setattr("seenzus_bridge.config_flow._resolve_ha_instance_id", AsyncMock(return_value="ha-install"))
    monkeypatch.setattr("seenzus_bridge.config_flow._build_quick_pair_callback_context", lambda *_: (
        "http://ha.local/api/seenzus_bridge/quick_pair/callback", "nonce", "signed-state",
    ))
    result = await flow.async_step_seamless({})
    assert parse_qs(urlparse(result["url"]).query)["available_environments"] == [expected]
    assert urlparse(result["url"]).netloc == "app.seenzus.cn"
