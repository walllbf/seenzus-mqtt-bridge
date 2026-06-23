"""SavanAI Bridge - MQTT v2 bridge with pairing support."""
from __future__ import annotations

# Permanent re-export surface pinned by the test suite: tests import
# `BridgeCoordinator`, `_async_reload_entry`, `BRIDGE_VERSION`, `er`, `dr`,
# `PRESENCE_HEARTBEAT_INTERVAL_SECONDS` from the package root and monkeypatch
# `seenzusaimqttbridge.asyncio.sleep`. Keep these module-level names stable.
import asyncio  # noqa: F401
import logging

from homeassistant.config_entries import ConfigEntry, ConfigEntryNotReady
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr  # noqa: F401
from homeassistant.helpers import entity_registry as er  # noqa: F401

from .catalog import IOT_DEVICE_DOMAINS  # noqa: F401
from .const import BRIDGE_VERSION, DOMAIN  # noqa: F401
from .coordinator import (  # noqa: F401
    PRESENCE_HEARTBEAT_INTERVAL_SECONDS,
    BridgeCoordinator,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor"]


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    coordinator: BridgeCoordinator | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if coordinator is not None:
        await coordinator.async_prepare_for_reload()
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    coordinator = BridgeCoordinator(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    try:
        await coordinator.async_start()
    except Exception as err:  # noqa: BLE001
        _LOGGER.exception("SavanAI Bridge failed to start: %s", err)
        raise ConfigEntryNotReady(f"start_failed:{err}") from err

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: BridgeCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
