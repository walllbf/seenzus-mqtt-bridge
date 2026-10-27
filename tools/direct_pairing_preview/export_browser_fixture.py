"""Export the actual HA view HTML with a test-only signed context for Chromium."""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "custom_components")]

from seenzus_bridge.pairing_origin import PairingOriginView, build_pairing_origin_url
from tests.helpers import FakeHass
from tests.test_pairing_origin import context


async def main():
    hass = FakeHass()
    hass.config.language = "zh-Hans"
    params = context(hass)
    # Browser tests replace timestamps inside this fixture to model a new flow.
    build_pairing_origin_url(hass, params)
    hass.config_entries.flow.async_get = lambda _: {}
    response = await PairingOriginView().get(SimpleNamespace(app={"hass": hass}, query={"state": params["state"]}))
    Path(sys.argv[1]).write_text(json.dumps({"html": response.text, "headers": dict(response.headers), "params": params}), encoding="utf-8")


asyncio.run(main())
