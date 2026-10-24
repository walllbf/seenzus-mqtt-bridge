"""Verify local icon resources against installed Core frontend/HTTP APIs."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from homeassistant import loader
from homeassistant.components.frontend import DATA_EXTRA_MODULE_URL, UrlManager
from homeassistant.config_entries import ConfigEntries
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
import pytest

import seenzus_bridge
from seenzus_bridge import frontend_compat as compat


@pytest.fixture
def frontend_hass():
    async def executor(function):
        return await asyncio.to_thread(function)
    return SimpleNamespace(
        data={DATA_EXTRA_MODULE_URL: UrlManager(lambda *args: None, [])},
        http=SimpleNamespace(async_register_static_paths=AsyncMock()),
        async_add_executor_job=executor,
    )


async def test_headless_setup_is_a_noop():
    hass = SimpleNamespace(data={})
    assert await compat.async_setup_frontend_compat(hass) is False
    assert hass.data == {}


async def test_optional_frontend_not_initialized_is_a_noop(frontend_hass):
    frontend_hass.data.clear()
    assert await compat.async_setup_frontend_compat(frontend_hass) is False
    frontend_hass.http.async_register_static_paths.assert_not_awaited()


async def test_concurrent_and_repeated_setup_registers_once(frontend_hass):
    assert all(await asyncio.gather(*(
        compat.async_setup_frontend_compat(frontend_hass) for _ in range(5)
    )))
    assert await compat.async_setup_frontend_compat(frontend_hass)
    frontend_hass.http.async_register_static_paths.assert_awaited_once()
    paths = frontend_hass.http.async_register_static_paths.call_args.args[0]
    assert [path.url_path for path in paths] == [compat._SCRIPT_URL, compat._BRAND_URL]
    assert all(Path(path.path).exists() for path in paths)
    urls = frontend_hass.data[DATA_EXTRA_MODULE_URL].urls
    assert len(urls) == 1
    assert next(iter(urls)).startswith(compat._SCRIPT_URL + "?v=")


async def test_static_registration_failure_is_nonfatal_and_retryable(frontend_hass):
    frontend_hass.http.async_register_static_paths.side_effect = OSError("fixture failure")
    assert await compat.async_setup_frontend_compat(frontend_hass) is False
    assert not frontend_hass.data[DATA_EXTRA_MODULE_URL].urls
    frontend_hass.http.async_register_static_paths.side_effect = None
    assert await compat.async_setup_frontend_compat(frontend_hass)


async def test_resource_failure_does_not_reregister_static_routes(frontend_hass):
    class FailingManager:
        def add(self, url):
            raise RuntimeError("fixture failure")
    frontend_hass.data[DATA_EXTRA_MODULE_URL] = FailingManager()
    assert await compat.async_setup_frontend_compat(frontend_hass) is False
    frontend_hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *args: None, [])
    assert await compat.async_setup_frontend_compat(frontend_hass)
    frontend_hass.http.async_register_static_paths.assert_awaited_once()


async def test_asset_version_changes_when_bundled_icon_changes(tmp_path, monkeypatch):
    (tmp_path / "frontend").mkdir()
    (tmp_path / "brand").mkdir()
    (tmp_path / "frontend/brand-compat.js").write_bytes(b"fixture script")
    image = tmp_path / "brand/icon.png"
    image.write_bytes(b"first")
    monkeypatch.setattr(compat, "_ROOT", tmp_path)
    first = await asyncio.to_thread(compat._asset_version)
    assert first == await asyncio.to_thread(compat._asset_version)
    image.write_bytes(b"second")
    assert first != await asyncio.to_thread(compat._asset_version)


async def test_component_setup_keeps_mqtt_working_without_frontend(monkeypatch):
    mqtt = AsyncMock()
    frontend = AsyncMock(return_value=False)
    monkeypatch.setattr(seenzus_bridge, "async_ensure_mqtt_requirements", mqtt)
    monkeypatch.setattr(seenzus_bridge, "async_setup_frontend_compat", frontend)
    hass = SimpleNamespace(data={})
    assert await seenzus_bridge.async_setup(hass, {}) is True
    mqtt.assert_awaited_once_with(hass)
    frontend.assert_awaited_once_with(hass)


async def test_installed_core_serves_actual_module_and_png_without_auth(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    hass.config_entries = ConfigEntries(hass, {})
    try:
        assert await async_setup_component(hass, "http", {
            "http": {"server_host": "127.0.0.1"},
        })
        hass.data[DATA_EXTRA_MODULE_URL] = UrlManager(lambda *args: None, [])
        assert await compat.async_setup_frontend_compat(hass)
        async with TestClient(TestServer(hass.http.app)) as client:
            script = await client.get(next(iter(hass.data[DATA_EXTRA_MODULE_URL].urls)))
            assert script.status == 200
            assert b"hacs-frontend" in await script.read()
            for name in ("icon.png", "icon@2x.png", "logo.png", "logo@2x.png"):
                response = await client.get(f"{compat._BRAND_URL}/{name}")
                assert response.status == 200
                assert response.content_type == "image/png"
                assert (await response.read()).startswith(b"\x89PNG\r\n\x1a\n")
            response = await client.get(compat._BRAND_URL + "/../manifest.json")
            assert response.status == 404
    finally:
        await hass.async_stop(force=True)
