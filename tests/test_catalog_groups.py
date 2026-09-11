from types import SimpleNamespace

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from seenzus_bridge.catalog import build_device_catalog_payload
from tests.helpers import FakeDeviceRegistry, FakeEntityRegistry, FakeHass


def test_catalog_groups_accounts_without_splitting_devices_or_leaking_config(monkeypatch):
    hass = FakeHass()
    entities = FakeEntityRegistry()
    devices = FakeDeviceRegistry()
    devices.add("multi", name="Shared device")
    for entity_id, entry_id, device_id in [
        ("light.a", "xiaomi-home", "multi"),
        ("sensor.a", "xiaomi-office", "multi"),
        ("switch.b", "xiaomi-home", "multi"),
        ("light.office", "xiaomi-office", None),
        ("input_boolean.helper", "", None),
        ("light.removed", "deleted-entry", None),
    ]:
        entities.add(entity_id, entry_id, device_id=device_id)
        hass.states.set(entity_id)
    for entry_id, title in [("xiaomi-home", "家中账号"), ("xiaomi-office", "公司账号")]:
        hass.config_entries.entries[entry_id] = SimpleNamespace(
            title=title, domain="xiaomi_home", data={"token": "private-token"}, options={"password": "secret"}
        )
    monkeypatch.setattr(er, "async_get", lambda _: entities)
    monkeypatch.setattr(dr, "async_get", lambda _: devices)

    result = build_device_catalog_payload(hass, bridge_id="ha", source="test", is_own_entity=lambda _: False)
    by_id = {device["deviceId"]: device for device in result["devices"]}
    assert result["deviceCount"] == 4
    assert result["wireVersion"] == "2.1"
    assert by_id["multi"]["entityCount"] == 3
    assert by_id["multi"]["configEntries"] == [
        {"id": "xiaomi-home", "title": "家中账号", "domain": "xiaomi_home"},
        {"id": "xiaomi-office", "title": "公司账号", "domain": "xiaomi_home"},
    ]
    assert by_id["light.office"]["configEntries"] == [by_id["multi"]["configEntries"][1]]
    assert by_id["input_boolean.helper"]["configEntries"] == []
    assert by_id["light.removed"]["configEntries"] == []
    assert "private-token" not in str(result)
    assert "password" not in str(result)
