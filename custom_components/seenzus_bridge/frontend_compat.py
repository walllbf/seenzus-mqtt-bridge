"""Serve opt-in-by-enabling-the-integration, local HACS icon compatibility.

This registers ordinary HA frontend resources. It never edits HACS files,
patches its Python objects, or changes another integration's configuration.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
from pathlib import Path

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)
_STATE_KEY = "seenzus_bridge_frontend_compat"
_ROOT = Path(__file__).parent
_SCRIPT_URL = "/seenzus_bridge/frontend/brand-compat.js"
_BRAND_URL = "/seenzus_bridge/brand"


def _asset_version() -> str:
    digest = hashlib.sha256()
    for path in [_ROOT / "frontend/brand-compat.js", *sorted((_ROOT / "brand").glob("*.png"))]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


async def async_setup_frontend_compat(hass: HomeAssistant) -> bool:
    """Register once per HA process, independently of MQTT entry reloads."""
    # Frontend is optional on headless installations. after_dependencies makes
    # sure an existing frontend is initialized before the integration starts.
    if getattr(hass, "http", None) is None or "frontend_extra_module_url" not in hass.data:
        return False

    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    state = hass.data.setdefault(_STATE_KEY, {"lock": asyncio.Lock()})
    async with state["lock"]:
        if state.get("url"):
            return True
        try:
            version = await hass.async_add_executor_job(_asset_version)
            if not state.get("static_registered"):
                await hass.http.async_register_static_paths([
                    StaticPathConfig(_SCRIPT_URL, str(_ROOT / "frontend/brand-compat.js"), True),
                    StaticPathConfig(_BRAND_URL, str(_ROOT / "brand"), True),
                ])
                state["static_registered"] = True
            url = f"{_SCRIPT_URL}?v={version}"
            add_extra_js_url(hass, url)
        except (OSError, RuntimeError) as error:
            # Cosmetic compatibility must not disable the MQTT bridge.
            _LOGGER.warning("Could not register seenzus icon compatibility: %s", error)
            return False
        state["url"] = url
        return True
