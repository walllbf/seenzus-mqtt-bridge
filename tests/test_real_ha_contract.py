"""Bridge wire behavior through a real isolated HA, with only the MQTT socket replaced.

Service handlers stand in for physical devices. HA state/registries, descriptions,
schema validation and dispatch are the installed upstream implementation.
"""
import json
from inspect import signature
from datetime import datetime, timezone
from types import MappingProxyType

import pytest
import voluptuous as vol
from homeassistant import config_entries, loader
from homeassistant.const import EntityCategory, __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er

from seenzus_bridge import BridgeCoordinator
from seenzus_bridge.bridge_protocol import build_topics
from tests.helpers import AsyncFakeMQTTClient, FakeConfigEntry


@pytest.fixture
async def real_bridge(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    try:
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        await ar.async_load(hass)
        # Newer Core bootstraps the device registry explicitly before loading it.
        # Mirror that setup; do not replace the actual registry implementation.
        if setup_devices := getattr(dr, "async_setup", None):
            setup_devices(hass)
        await dr.async_load(hass)
        await er.async_load(hass)
        bridge = BridgeCoordinator(hass, FakeConfigEntry(data={"mqtt_host": "unused.test"}))
        bridge._topics = build_topics("seenzus/v2", "contract-test")
        client = AsyncFakeMQTTClient()
        yield hass, bridge, client
    finally:
        await hass.async_stop(force=True)


async def command(bridge, client, method, path, body=None):
    await bridge._handle_v2_command("contract", json.dumps({
        "method": method, "path": path, "body": body,
    }), client)
    return json.loads(next(item["payload"] for item in reversed(client.published)
                           if "/result/" in item["topic"]))


async def test_action_catalog_uses_installed_ha_service_descriptions(real_bridge):
    hass, bridge, client = real_bridge

    async def device_handler(call):
        pass

    for domain, service in (("switch", "turn_on"), ("number", "set_value"),
                            ("select", "select_option"), ("button", "press")):
        hass.services.async_register(domain, service, device_handler)
    result = await command(bridge, client, "GET", "/api/services")
    assert result["success"] is True
    descriptions = result["data"]
    assert "turn_on" in descriptions["switch"]
    assert "value" in descriptions["number"]["set_value"]["fields"]
    assert "option" in descriptions["select"]["select_option"]["fields"]
    assert "press" in descriptions["button"]
    # Changing the real service registry must invalidate HA's cached catalog.
    hass.services.async_remove("button", "press")
    result = await command(bridge, client, "GET", "/api/services")
    assert "press" not in result["data"].get("button", {})


@pytest.mark.parametrize("domain,service,arguments,new_state", [
    ("switch", "turn_on", {}, "on"),
    ("number", "set_value", {"value": 42}, "42"),
    ("select", "select_option", {"option": "eco"}, "eco"),
    ("button", "press", {}, "2026-09-20T00:00:00+00:00"),
])
async def test_real_service_dispatch_publishes_result_and_observed_state(
    real_bridge, domain, service, arguments, new_state,
):
    hass, bridge, client = real_bridge
    entity_id = f"{domain}.isolated"
    hass.states.async_set(entity_id, "unknown")

    async def simulated_device(call):
        hass.states.async_set(call.data["entity_id"], new_state, {"friendly_name": "Isolated device"})

    schema = {vol.Required("entity_id"): str}
    schema.update({vol.Required(key): type(value) for key, value in arguments.items()})
    hass.services.async_register(domain, service, simulated_device, schema=vol.Schema(schema))
    result = await command(bridge, client, "POST", f"/api/services/{domain}/{service}",
                           {"entity_id": entity_id, **arguments})
    assert result["success"] is True and result["status"] == 200
    observed = json.loads(client.published[-1]["payload"])
    assert observed["entityId"] == entity_id
    assert observed["state"] == new_state
    assert observed["attributes"]["friendly_name"] == "Isolated device"
    assert observed["attributes"]["seenzus_display"]["time_zone"] == "UTC"
    # Display enrichment belongs to the wire payload, not HA's original state.
    assert hass.states.get(entity_id).attributes == {"friendly_name": "Isolated device"}
    assert observed["correlationMsgId"] == "contract"


@pytest.mark.parametrize("failure", ["missing", "exception", "invalid_data"])
async def test_real_service_failure_is_not_reported_as_success(real_bridge, failure):
    hass, bridge, client = real_bridge
    hass.states.async_set("switch.isolated", "off")

    async def broken_device(call):
        raise HomeAssistantError("isolated device rejected operation")

    if failure != "missing":
        hass.services.async_register("switch", "turn_on", broken_device,
                                     schema=vol.Schema({vol.Required("entity_id"): str}))
    body = {} if failure == "invalid_data" else {"entity_id": "switch.isolated"}
    result = await command(bridge, client, "POST", "/api/services/switch/turn_on", body)
    assert result["success"] is False and result["status"] >= 400
    assert result["error"]
    assert all("/state/" not in item["topic"] for item in client.published)
    assert hass.states.get("switch.isolated").state == "off"


async def test_service_acceptance_does_not_fabricate_device_confirmation(real_bridge):
    hass, bridge, client = real_bridge
    hass.states.async_set("switch.isolated", "off")

    async def device_has_not_reported_yet(call):
        pass

    hass.services.async_register("switch", "turn_on", device_has_not_reported_yet)
    result = await command(bridge, client, "POST", "/api/services/switch/turn_on",
                           {"entity_id": "switch.isolated"})
    assert result["success"] is True
    # Command accepted by HA, but the subsequent state evidence still says off.
    assert json.loads(client.published[-1]["payload"])["state"] == "off"


async def test_real_registry_catalog_and_snapshot_preserve_identity_and_evidence(real_bridge):
    hass, bridge, client = real_bridge
    entry = config_entries.ConfigEntry(
        domain="switch", title="Isolated fixture", data={}, options={}, source="user",
        version=1, minor_version=1, unique_id="isolated", discovery_keys=MappingProxyType({}),
        **({"subentries_data": []} if "subentries_data" in signature(config_entries.ConfigEntry).parameters else {}),
    )
    # Seed a real entry without setting up hardware integrations or opening network sockets.
    hass.config_entries._entries[entry.entry_id] = entry
    area = ar.async_get(hass).async_create("Test room")
    override_area = ar.async_get(hass).async_create("Entity room")
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("test", "physical-1")}, name="Test device",
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area.id)
    registry = er.async_get(hass)
    attached = registry.async_get_or_create(
        "vendor_custom", "test", "custom-1", device_id=device.id,
        entity_category=EntityCategory.DIAGNOSTIC, suggested_object_id="attached",
    )
    registry.async_update_entity(attached.entity_id, area_id=override_area.id)
    hass.states.async_set(attached.entity_id, "unknown", {"unit_of_measurement": "W"})
    hass.states.async_set("vendor_custom.orphan", "on")
    hass.states.async_set("input_boolean.helper", "off")
    hass.states.async_set("switch.unreachable", "unavailable")
    result = await command(bridge, client, "GET", "/api/seenzus/device-catalog")
    assert result["success"] is True
    catalog = result["data"]
    assert catalog["homeAssistantVersion"] == HA_VERSION
    assert catalog["wireVersion"] == "2.1"
    assert catalog["isComplete"] is True
    devices = {item["deviceId"]: item for item in catalog["devices"]}
    assert devices[device.id]["areaId"] == area.id
    entities = {item["entityId"]: item for device in devices.values() for item in device["entities"]}
    assert set(entities) == {attached.entity_id, "input_boolean.helper", "switch.unreachable"}
    custom = entities[attached.entity_id]
    assert custom["deviceId"] == device.id and custom["stableEntityId"] == attached.id
    assert custom["entityCategory"] == "diagnostic" and custom["areaId"] == override_area.id
    assert custom["available"] is True and custom["unit"] == "W"
    assert entities["switch.unreachable"]["available"] is False

    before = datetime.now(timezone.utc)
    result = await command(bridge, client, "GET", "/api/states")
    after = datetime.now(timezone.utc)
    states = {item["entity_id"]: item for item in result["data"]}
    evidence = states[attached.entity_id]
    assert datetime.fromisoformat(evidence["last_changed"]) == hass.states.get(attached.entity_id).last_changed
    assert datetime.fromisoformat(evidence["last_updated"]) == hass.states.get(attached.entity_id).last_updated
    publications = {p["entityId"]: p for item in client.published if "/state/" in item["topic"]
                    for p in [json.loads(item["payload"]) ]}
    assert publications[attached.entity_id]["available"] is True
    assert publications["switch.unreachable"]["available"] is False
    assert publications[attached.entity_id]["attributes"]["unit_of_measurement"] == "W"
    assert before <= datetime.fromisoformat(publications[attached.entity_id]["ts"]) <= after
