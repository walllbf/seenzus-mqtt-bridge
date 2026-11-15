"""Static-only additions for round 2. NOT RUN/imported/compiled in this task.

External execution was rejected by automatic approval; the main session narrowed
the work to source/test edits. Run only in a separately authorized upstream job.
"""
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from seenzus_bridge import companion_roles as roles
from seenzus_bridge.device_access import DeviceAccess


@pytest.fixture
def catalog(monkeypatch):
    records = {}
    def entity(entity_id, domain, key):
        entry = SimpleNamespace(entity_id=entity_id, domain=domain, platform="dangbei_local", config_entry_id="reviewed",
                                device_id="physical", id=f"registry-{key}", unique_id=f"native-token_{key}", hidden_by=None, disabled_by=None)
        records[entity_id] = entry
        return {"entityId": entity_id, "domain": domain, "name": "完全可改名", "stableEntityId": entry.id}
    remote = entity("remote.renamed_route", "remote", "remote")
    buttons = [entity(f"button.renamed_{index}", "button", key) for index, key in enumerate(roles.ROLES)]
    # Misleading names are deliberate: helper functions must remain independent of key names.
    extra = entity("button.renamed_extra", "button", "reconnect")
    extra["name"] = "向上"
    registry = SimpleNamespace(async_get=records.get, entities=records)
    native = SimpleNamespace(identifiers={("dangbei_local", "native-token")})
    monkeypatch.setattr(roles.er, "async_get", lambda _: registry)
    monkeypatch.setattr(roles.dr, "async_get", lambda _: SimpleNamespace(async_get=lambda _: native))
    integration = AsyncMock(return_value=SimpleNamespace(version="0.4.3"))
    monkeypatch.setattr(roles.loader, "async_get_integration", integration)
    hass = SimpleNamespace(config_entries=SimpleNamespace(async_get_entry=lambda key: SimpleNamespace(domain="dangbei_local" if key == "reviewed" else "other")))
    payload = {"devices": [{"deviceId": "physical", "configEntries": [{"id": "reviewed", "domain": "dangbei_local"}], "entities": [remote, *buttons, extra]}]}
    return SimpleNamespace(hass=hass, payload=payload, records=records, native=native, integration=integration,
                           access=DeviceAccess(hass, {"device_scope": "all"}))


@pytest.mark.asyncio
async def test_reviewed_native_registry_roles_survive_route_and_display_renames_without_exporting_unique_ids(catalog):
    c = catalog
    await roles.add_reviewed_companion_roles(c.hass, c.payload, c.access)
    entities = c.payload["devices"][0]["entities"]
    for index, role in enumerate(roles.ROLES.values(), 1):
        assert entities[index]["remoteCompanion"] == {"version": 1, "capabilityVersion": "remote:dangbei_local:2", "remoteRegistryId": "registry-remote", "role": role}
    assert "remoteCompanion" not in entities[-1]
    assert "native-token" not in json.dumps(c.payload)
    assert len(entities) == len(roles.ROLES) + 2  # Metadata adds roles; no original control is deleted.


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unknown_version", "multiple_identifiers", "multiple_remotes", "remote_identity", "button_identity", "button_platform", "button_device", "button_entry", "duplicate_role", "unauthorized"])
async def test_missing_or_ambiguous_native_proof_does_not_publish_the_up_role(catalog, failure):
    c = catalog
    entities = c.payload["devices"][0]["entities"]
    up = c.records[entities[1]["entityId"]]
    if failure == "unknown_version":
        c.integration.return_value.version = "9.9.9"
    if failure == "multiple_identifiers":
        c.native.identifiers.add(("dangbei_local", "other-token"))
    if failure == "multiple_remotes":
        extra = copy.deepcopy(entities[0]); extra["entityId"] = "remote.second"
        c.records[extra["entityId"]] = copy.copy(c.records[entities[0]["entityId"]]); entities.append(extra)
    if failure == "remote_identity":
        c.records[entities[0]["entityId"]].unique_id = "other-token_remote"
    if failure == "button_identity":
        up.unique_id = "other-token_up"
    if failure == "button_platform":
        up.platform = "tv_remote"
    if failure == "button_device":
        up.device_id = "other-device"
    if failure == "button_entry":
        up.config_entry_id = "other-entry"
    if failure == "duplicate_role":
        extra = copy.deepcopy(entities[1]); extra["entityId"] = "button.second"
        c.records[extra["entityId"]] = copy.copy(up); c.records[extra["entityId"]].entity_id = extra["entityId"]; entities.append(extra)
    if failure == "unauthorized":
        c.access = DeviceAccess(c.hass, {"device_scope": "selected", "allowed_device_ids": []})
    count = len(entities)
    await roles.add_reviewed_companion_roles(c.hass, c.payload, c.access)
    assert "remoteCompanion" not in entities[1]
    assert len(entities) == count
