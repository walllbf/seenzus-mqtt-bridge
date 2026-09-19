import json
from types import SimpleNamespace

import pytest

from seenzus_bridge import BridgeCoordinator, er, dr
from seenzus_bridge.bridge_protocol import build_topics
from seenzus_bridge.device_access import DeviceAccess
from seenzus_bridge.ha_dispatcher import DispatchPolicy, dispatch
from seenzus_bridge.operation_store import PersistentOperationStore
from tests.helpers import FakeHass, FakeConfigEntry, FakeEntityRegistry, FakeDeviceRegistry, AsyncFakeMQTTClient, make_state_changed_event
from tests.test_command_behavior import _MemoryStorage


@pytest.fixture
def home(monkeypatch):
    hass = FakeHass()
    entities, devices = FakeEntityRegistry(), FakeDeviceRegistry()
    devices.add("city", area_id="downtown", via_device_id="suburb")
    devices.add("suburb", area_id="suburbs")
    for name, device in (("light.city", "city"), ("light.suburb", "suburb"), ("input_boolean.helper", None), ("sensor.renamed_bridge", "city")):
        entities.add(name, device_id=device, registry_id=f"stable-{name}")
        entities.async_get(name).platform = "seenzus_bridge" if name.startswith("sensor") else "test"
        hass.states.set(name, attributes={"brightness": 100, "private_url": "secret", "entity_picture": "secret"})
    monkeypatch.setattr(er, "async_get", lambda _hass: entities)
    monkeypatch.setattr(dr, "async_get", lambda _hass: devices)
    entry = FakeConfigEntry(data={"device_scope": "selected", "allowed_device_ids": ["city"],
                                  "allowed_entity_registry_ids": ["stable-input_boolean.helper"]})
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._topics = build_topics("seenzus/v2", "city-world")
    return hass, entities, devices, entry, coordinator


@pytest.mark.asyncio
async def test_catalog_read_snapshots_and_events_share_the_grant(home):
    hass, _, _, entry, coordinator = home
    client = AsyncFakeMQTTClient()
    coordinator._mqtt_client = client
    policy = coordinator._dispatch_policy()
    result = await dispatch(hass, "GET", "/api/states", None, policy)
    assert {s["entity_id"] for s in result.data} == {"light.city", "input_boolean.helper"}
    assert all(s["attributes"] == {"brightness": 100} for s in result.data)
    assert (await dispatch(hass, "GET", "/api/states/light.suburb", None, policy)).status == 404
    catalog = coordinator._build_device_catalog_payload(source="test")
    assert {d["deviceId"] for d in catalog["devices"]} == {"city", "input_boolean.helper"}
    assert all("viaDeviceId" not in d for d in catalog["devices"])
    assert catalog["access"]["deviceIds"] == ["city"]
    await coordinator._publish_all_states(client, source="snapshot")
    await coordinator._publish_state_from_event(make_state_changed_event("light.suburb"))
    await coordinator._publish_states_for_entities(client, ["light.suburb", "light.city"])
    assert {json.loads(msg["payload"])["entityId"] for msg in client.published} == {"light.city", "input_boolean.helper"}
    assert all("private_url" not in json.loads(msg["payload"])["attributes"] for msg in client.published)


def test_config_entry_groups_only_describe_authorized_devices(home):
    hass, entities, _, _, coordinator = home
    for entity_id, entry_id, title in (
        ("light.city", "city-entry", "City account"),
        ("light.suburb", "private-suburb-entry", "Private suburban account"),
    ):
        entities.async_get(entity_id).config_entry_id = entry_id
        hass.config_entries.entries.append(SimpleNamespace(
            entry_id=entry_id, title=title, domain="test_integration",
            data={"token": "secret"}, options={"password": "secret"},
        ))
    catalog = coordinator._build_device_catalog_payload(source="test")
    devices = {device["deviceId"]: device for device in catalog["devices"]}
    assert set(devices) == {"city", "input_boolean.helper"}
    assert devices["city"]["configEntries"] == [
        {"id": "city-entry", "title": "City account", "domain": "test_integration"},
    ]
    assert devices["input_boolean.helper"]["configEntries"] == []
    assert "private-suburb-entry" not in json.dumps(catalog)
    assert "Private suburban account" not in json.dumps(catalog)
    assert "secret" not in json.dumps(catalog)


@pytest.mark.asyncio
@pytest.mark.parametrize("path,body", [
    ("/api/services/light/turn_on", {"entity_id": ["light.city", "light.suburb"]}),
    ("/api/services/light/turn_on", {"entity_id": "all"}),
    ("/api/services/light/turn_on", {"area_id": "downtown"}),
    ("/api/services/light/turn_on", {"entity_id": "light.city", "device_id": "suburb"}),
    ("/api/services/light/turn_on", {}),
    ("/api/services/script/turn_on", {"entity_id": "script.test"}),
    ("/api/services/scene/turn_on", {"entity_id": "scene.test"}),
    ("/api/services/homeassistant/restart", {}),
    ("/api/events/custom", {}), ("/api/template", {"template": "{{ states | list }}"}),
])
async def test_indirect_and_mixed_targets_fail_without_side_effects(home, path, body):
    hass, _, _, entry, _ = home
    policy = DispatchPolicy(allow_template=True, allow_dangerous_services=True, access=DeviceAccess(hass, entry.data))
    result = await dispatch(hass, "POST", path, body, policy)
    assert result.status == 403
    assert hass.services.calls == [] and hass.bus.fire_calls == []


@pytest.mark.asyncio
async def test_explicit_service_and_stable_standalone_grant(home):
    hass, entities, _, entry, _ = home
    access = DeviceAccess(hass, entry.data)
    policy = DispatchPolicy(access=access)
    assert (await dispatch(hass, "POST", "/api/services/light/turn_on", {"entity_id": "light.city", "brightness": 42}, policy)).status == 200
    helper = entities.async_get("input_boolean.helper")
    helper.entity_id = "input_boolean.renamed"
    assert access.allows("input_boolean.renamed")
    entities.add("light.new", device_id="new-device", registry_id="new")
    assert not access.allows("light.new")
    entities.async_get("light.city").platform = "group"
    assert (await dispatch(hass, "POST", "/api/services/light/turn_on", {"entity_id": "light.city"}, policy)).status == 403


@pytest.mark.asyncio
async def test_revoke_blocks_old_operation_result_replay_and_other_entry_is_unchanged(home):
    hass, _, _, entry, coordinator = home
    other = DeviceAccess(hass, {"device_scope": "selected", "allowed_device_ids": ["city"]})
    client = AsyncFakeMQTTClient()
    storage = _MemoryStorage()
    coordinator._operation_store = PersistentOperationStore(hass, entry.entry_id, storage)
    request = json.dumps({"operationKey": "read", "method": "GET", "path": "/api/states/light.city"})
    await coordinator._handle_v2_command("first", request, client)
    entry.options = {"device_scope": "selected", "allowed_device_ids": [], "allowed_entity_registry_ids": []}
    await coordinator._handle_v2_command("retry", request, client)
    response = json.loads(client.published[-1]["payload"])
    assert response["status"] == 409 and response.get("data") is None
    assert other.allows("light.city")
    assert coordinator._build_device_catalog_payload(source="revoked")["devices"] == []
