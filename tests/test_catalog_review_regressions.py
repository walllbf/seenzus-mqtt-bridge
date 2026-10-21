"""Exercise catalog-review fixes through Core lifecycle and UI boundaries."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from voluptuous_serialize import convert

from homeassistant import loader
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import CoreState, HassJob, HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.config_validation import custom_serializer
from homeassistant.helpers.translation import async_get_translations

from seenzus_bridge import BridgeCoordinator
from seenzus_bridge.config_flow import SavanAIBridgeConfigFlow, _build_quick_pair_entry_data
from seenzus_bridge.pairing_models import PairingStatusResult
from seenzus_bridge.quick_pair import SeenzusAppReturnView, _notify_app_return, _record_quick_pair_diagnostic
from seenzus_bridge.sensor import BridgeStatusSensor
from tests.helpers import FakeAiomqttModule
from tests.test_lifecycle_behavior import _make_core_config_entry


DOMAIN = "seenzus_bridge"
INTEGRATION_PATH = Path(__file__).resolve().parents[1] / "custom_components" / DOMAIN
pytestmark = pytest.mark.timeout(10)


@pytest.fixture
async def real_runtime(tmp_path, monkeypatch):
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    manifest = json.loads((INTEGRATION_PATH / "manifest.json").read_text(encoding="utf-8"))
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = loader.Integration(
        hass, f"custom_components.{DOMAIN}", INTEGRATION_PATH, manifest,
        {item.name for item in INTEGRATION_PATH.iterdir()},
    )
    hass.config_entries = ConfigEntries(hass, {})
    entry = _make_core_config_entry()
    hass.config_entries._entries[entry.entry_id] = entry
    hass.config_entries.async_update_entry(entry, data={
        "mqtt_host": "broker.example.com", "mqtt_scheme": "mqtts",
        "enable_state_events": False,
    })
    await ar.async_load(hass)
    if setup_devices := getattr(dr, "async_setup", None):
        setup_devices(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    hass.set_state(CoreState.running)
    coordinator = BridgeCoordinator(hass, entry)
    coordinator._aiomqtt = fake = FakeAiomqttModule()
    monkeypatch.setattr(coordinator, "_async_import_aiomqtt", AsyncMock(return_value=fake))
    try:
        yield hass, entry, coordinator, fake
    finally:
        await coordinator.async_stop()
        await hass.async_stop(force=True)


async def start_connected(coordinator):
    await coordinator.async_start()
    async with asyncio.timeout(2):
        while not coordinator.mqtt_connected:
            if coordinator._task.done():
                coordinator._task.result()
            await asyncio.sleep(0)


@pytest.mark.parametrize("blocked_publish", [False, True])
async def test_real_core_stop_cleans_connected_bridge_before_task_cancellation(
    real_runtime, monkeypatch, blocked_publish,
):
    hass, _, coordinator, fake = real_runtime
    await start_connected(coordinator)
    client = fake.clients[0]
    runtime_task = coordinator._task
    attempted = []
    original_publish = client.publish

    async def publish(topic, payload, **kwargs):
        if topic.endswith("/presence") and json.loads(payload)["status"] == "offline":
            attempted.append(client.connected)
            if blocked_publish:
                await asyncio.Event().wait()
        await original_publish(topic, payload, **kwargs)

    monkeypatch.setattr(client, "publish", publish)
    monkeypatch.setattr("seenzus_bridge.coordinator.SHUTDOWN_CLEANUP_TIMEOUT_SECONDS", 0.03)
    other_shutdown_job = AsyncMock()
    hass.async_add_shutdown_job(HassJob(other_shutdown_job))
    async with asyncio.timeout(1):
        await hass.async_stop(force=True)

    assert attempted == [True], "Offline must be attempted while MQTT is still connected"
    assert runtime_task.done()
    assert not client.connected
    assert coordinator.status == "stopped"
    other_shutdown_job.assert_awaited_once()
    if blocked_publish:
        assert "shutdown" in coordinator.last_cleanup_diagnostic
    else:
        presence = [json.loads(item["payload"]) for item in client.published
                    if item["topic"].endswith("/presence")]
        assert presence[-1]["status"] == "offline"


async def test_real_entity_remove_unsubscribes_before_readding(real_runtime, monkeypatch):
    hass, entry, coordinator, _ = real_runtime
    for _ in range(2):
        sensor = BridgeStatusSensor(coordinator, entry)
        sensor.hass = hass
        sensor.entity_id = "sensor.bridge_lifecycle"
        write = Mock()
        monkeypatch.setattr(sensor, "async_write_ha_state", write)
        await sensor.async_added_to_hass()
        coordinator._fire()
        write.assert_called_once()
        await sensor.async_remove(force_remove=True)
        assert not coordinator._listeners
        write.reset_mock()
        coordinator._fire()
        write.assert_not_called()


@pytest.mark.parametrize("language, expected", [
    ("en", "Quick pair (recommended)"),
    ("fr", "Quick pair (recommended)"),
    ("zh-Hans", "快速配对（推荐）"),
])
async def test_serialized_setup_choices_use_runtime_selector_translations(
    real_runtime, language, expected,
):
    hass, _, _, _ = real_runtime
    flow = SavanAIBridgeConfigFlow()
    flow.hass = hass
    # This isolated entry is for lifecycle tests, not an existing integration setup.
    hass.config_entries._entries.clear()
    result = await flow.async_step_user()
    field = convert(result["data_schema"], custom_serializer=custom_serializer)[0]
    select = field["selector"]["select"]
    translations = await async_get_translations(hass, language, "selector", {DOMAIN})
    key = select["translation_key"]
    assert translations[f"component.{DOMAIN}.selector.{key}.options.seamless"] == expected
    assert select["options"] == ["seamless", "manual"]


@pytest.mark.parametrize("language, expected", [
    ("en", "Status"), ("fr", "Status"), ("zh-Hans", "状态"),
])
async def test_entity_name_uses_core_translation_path(real_runtime, language, expected):
    hass, entry, coordinator, _ = real_runtime
    translations = await async_get_translations(hass, language, "entity", {DOMAIN})
    sensor = BridgeStatusSensor(coordinator, entry)
    metadata = SimpleNamespace(platform_name=DOMAIN, domain="sensor")
    sensor.platform = metadata
    sensor.platform_data = metadata
    assert sensor._name_internal(None, translations) == expected
    coordinator.status = "active"
    assert sensor.native_value == "active"
    assert translations[f"component.{DOMAIN}.entity.sensor.status.state.active"] == (
        "运行中" if language == "zh-Hans" else "Running"
    )


@pytest.mark.parametrize("language, expected", [
    ("en", "Return to seenzus"), ("fr", "Return to seenzus"),
    ("zh-Hans", "返回 seenzus 应用"),
])
async def test_pairing_notifications_use_real_cached_language(
    real_runtime, monkeypatch, language, expected,
):
    hass, _, _, _ = real_runtime
    hass.config.language = language
    await async_get_translations(hass, language, "common", {DOMAIN})
    created = []
    monkeypatch.setattr("seenzus_bridge.quick_pair.persistent_notification.async_create",
                        lambda _hass, message, **kwargs: created.append((message, kwargs)))
    monkeypatch.setattr("seenzus_bridge.quick_pair.persistent_notification.async_dismiss",
                        lambda *_args: None)
    _notify_app_return(hass, "seenzus://pairing/done")
    assert expected in created[-1][0]
    _record_quick_pair_diagnostic(hass, "quick_pair_session_failed", {"message": "test"})
    assert ("快速配对失败" if language == "zh-Hans" else "Quick pairing failed") in created[-1][0]


@pytest.mark.parametrize("credential", [" leading-and-trailing ", " ", "normal"])
@pytest.mark.parametrize("source", ["manual", "quick_pair"])
async def test_credentials_are_preserved_from_entry_to_mqtt_client(
    real_runtime, credential, source,
):
    hass, entry, coordinator, fake = real_runtime
    settings = {"host": "broker.example.com", "scheme": "mqtts",
                "username": credential, "password": credential}
    if source == "quick_pair":
        result = PairingStatusResult(ok=True, status="confirmed", mqtt=settings)
        data = _build_quick_pair_entry_data(api_base="https://example.com", status_result=result)
        assert data["mqtt_username"] == credential
        assert data["mqtt_password"] == credential
    else:
        flow = SavanAIBridgeConfigFlow()
        flow.hass = hass
        result = await flow.async_step_manual({"mqtt_settings": {
            "mqtt_host": settings["host"], "mqtt_scheme": settings["scheme"],
            "mqtt_username": credential, "mqtt_password": credential,
        }})
        data = result["data"]
    hass.config_entries.async_update_entry(entry, data=data)
    await start_connected(coordinator)
    assert fake.clients[0].connect_kwargs["username"] == credential
    assert fake.clients[0].connect_kwargs["password"] == credential


async def test_manual_stop_unregisters_shutdown_job(real_runtime):
    hass, _, coordinator, fake = real_runtime
    await start_connected(coordinator)
    client = fake.clients[0]
    await coordinator.async_stop()
    await hass.async_stop(force=True)
    offline = [item for item in client.published if item["topic"].endswith("/presence")
               and json.loads(item["payload"])["status"] == "offline"]
    assert len(offline) == 1


@pytest.mark.parametrize("password", [None, "", " "])
async def test_empty_password_is_distinct_from_missing_password(real_runtime, password):
    hass, entry, coordinator, fake = real_runtime
    data = _build_quick_pair_entry_data(
        api_base="https://example.com",
        status_result=PairingStatusResult(ok=True, status="confirmed", mqtt={
            "host": "broker.example.com", "scheme": "mqtts",
            "username": "user", "password": password,
        }),
    )
    assert data["mqtt_password"] == password
    hass.config_entries.async_update_entry(entry, data=data)
    await start_connected(coordinator)
    assert fake.clients[0].connect_kwargs["password"] == password


@pytest.mark.parametrize("username", [None, "", " "])
async def test_empty_username_is_distinct_from_missing_username(real_runtime, username):
    hass, entry, coordinator, fake = real_runtime
    data = _build_quick_pair_entry_data(
        api_base="https://example.com",
        status_result=PairingStatusResult(ok=True, status="confirmed", mqtt={
            "host": "broker.example.com", "scheme": "mqtts",
            "username": username, "password": "password",
        }),
    )
    assert data["mqtt_username"] == username
    hass.config_entries.async_update_entry(entry, data=data)
    await start_connected(coordinator)
    assert fake.clients[0].connect_kwargs["username"] == username
    assert fake.clients[0].connect_kwargs["password"] == "password"


@pytest.mark.parametrize("stage", ["ha_start", "catalog"])
@pytest.mark.parametrize("stop", ["shutdown", "unload"])
async def test_stop_before_readiness_retracts_initial_online(
    real_runtime, monkeypatch, stage, stop,
):
    hass, _, coordinator, fake = real_runtime
    if stage == "ha_start":
        hass.set_state(CoreState.starting)
    else:
        async def blocked_catalog(*_args, **_kwargs):
            await asyncio.Event().wait()
        monkeypatch.setattr(coordinator, "_publish_device_catalog", blocked_catalog)
    await coordinator.async_start()
    async with asyncio.timeout(2):
        while not fake.clients or not fake.clients[0].published:
            await asyncio.sleep(0)
    client = fake.clients[0]
    assert json.loads(client.published[0]["payload"])["status"] == "online"
    assert not coordinator.mqtt_connected
    assert coordinator._mqtt_client is None
    if stop == "shutdown":
        await hass.async_stop(force=True)
    else:
        await coordinator.async_stop()
    presence = [json.loads(item["payload"])["status"] for item in client.published
                if item["topic"].endswith("/presence")]
    assert presence == ["online", "offline"]
    assert not client.connected


@pytest.mark.parametrize("language, expected", [
    ("en", "Opening the seenzus app"), ("fr", "Opening the seenzus app"),
    ("zh-Hans", "正在打开 seenzus 应用"),
])
async def test_app_return_html_is_localized(real_runtime, monkeypatch, language, expected):
    hass, _, _, _ = real_runtime
    hass.config.language = language
    monkeypatch.setattr("seenzus_bridge.quick_pair._decode_jwt",
                        lambda *_args: {"app_return_url": "seenzus://pairing/done"})
    monkeypatch.setattr("seenzus_bridge.quick_pair.persistent_notification.async_dismiss",
                        lambda *_args: None)
    response = await SeenzusAppReturnView().get(SimpleNamespace(
        app={"hass": hass}, query={"token": "test-token"},
    ))
    assert response.status == 200
    assert f'lang="{language}"' in response.text
    assert expected in response.text
    assert "seenzus://pairing/done" in response.text
