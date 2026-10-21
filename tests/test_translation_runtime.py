"""Custom integrations must ship runtime translations, including English fallback."""
import json
from pathlib import Path

import pytest

from homeassistant import loader
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations, recursive_flatten


INTEGRATION_PATH = Path(__file__).resolve().parents[1] / "custom_components" / "seenzus_bridge"
DOMAIN = "seenzus_bridge"


@pytest.mark.asyncio
@pytest.mark.parametrize("language", ["en", "fr", "zh-Hans"])
@pytest.mark.parametrize("category", ["config", "options", "exceptions"])
async def test_custom_integration_text_loads_in_real_ha(tmp_path, language, category):
    """Load shipped files through Core, including fallback for an unshipped locale."""
    hass = HomeAssistant(str(tmp_path))
    loader.async_setup(hass)
    manifest = json.loads((INTEGRATION_PATH / "manifest.json").read_text(encoding="utf-8"))
    integration = loader.Integration(
        hass, f"custom_components.{DOMAIN}", INTEGRATION_PATH, manifest,
        {item.name for item in INTEGRATION_PATH.iterdir()},
    )
    hass.data[loader.DATA_INTEGRATIONS][DOMAIN] = integration
    source = INTEGRATION_PATH / (
        "translations/zh-Hans.json" if language == "zh-Hans" else "strings.json"
    )
    expected = json.loads(source.read_text(encoding="utf-8"))[category]
    try:
        actual = await async_get_translations(hass, language, category, {DOMAIN})
        assert actual == recursive_flatten(f"component.{DOMAIN}.{category}.", expected)
        if category == "config":
            assert actual[f"component.{DOMAIN}.config.step.user.title"] == (
                "配置 seenzus MQTT Bridge" if language == "zh-Hans"
                else "Set up seenzus MQTT Bridge"
            )
    finally:
        await hass.async_stop(force=True)
