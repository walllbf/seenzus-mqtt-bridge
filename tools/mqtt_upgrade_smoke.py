"""Repair the deliberately incompatible MQTT pair installed by the CI upgrade step."""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from importlib import metadata
from pathlib import Path

from homeassistant import requirements as ha_requirements
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.util import package
from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

from seenzus_bridge import async_setup  # noqa: E402


async def main() -> None:
    """Use actual distribution metadata, HA's cache and its uv installer."""
    before = {name: metadata.version(name) for name in ("aiomqtt", "paho-mqtt")}
    assert before == {"aiomqtt": "2.5.1", "paho-mqtt": "1.6.1"}, before
    manifest = json.loads((ROOT / "custom_components/seenzus_bridge/manifest.json").read_text(encoding="utf-8"))
    assert all(package.is_installed(req) for req in manifest["requirements"])
    paho_dependency = next(
        Requirement(raw) for raw in metadata.requires("aiomqtt") or []
        if Requirement(raw).name == "paho-mqtt"
    )
    assert before["paho-mqtt"] not in paho_dependency.specifier

    with tempfile.TemporaryDirectory(prefix="seenzus-mqtt-upgrade-") as config_dir:
        hass = HomeAssistant(config_dir)
        try:
            await ha_requirements.async_process_requirements(hass, "seenzus_bridge", manifest["requirements"])
            assert {name: metadata.version(name) for name in before} == before
            assert await async_setup(hass, {})

            # Import only after startup has reconciled the pair, just like the
            # coordinator. Constructors prove the actual Paho API matches.
            aiomqtt = await hass.async_add_executor_job(__import__, "aiomqtt")
            for transport in ("tcp", "websockets"):
                aiomqtt.Client(hostname="localhost", transport=transport)
            after = {name: metadata.version(name) for name in before}
            print(f"Core {HA_VERSION}: {before} -> {after}; TCP/WebSocket client construction passed")
        finally:
            await hass.async_stop(force=True)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
