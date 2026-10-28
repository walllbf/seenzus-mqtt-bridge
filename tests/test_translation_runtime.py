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
                "选择配对方式" if language == "zh-Hans"
                else "Set up seenzus MQTT Bridge"
            )
    finally:
        await hass.async_stop(force=True)


@pytest.mark.parametrize("source", ["strings.json", "translations/en.json", "translations/zh-Hans.json"])
def test_frontend_text_has_no_markup(source):
    """The frontend formats flow text as ICU messages, which reject HTML tags.

    A tag such as ``<a href=...>`` renders as "Translation error: INVALID_TAG";
    markup has to arrive through a placeholder instead.
    """
    shipped = json.loads((INTEGRATION_PATH / source).read_text(encoding="utf-8"))
    for category in ("config", "options"):
        tagged = {key for key, text in recursive_flatten("", shipped[category]).items() if "<" in text}
        assert not tagged, f"{source} {category}: {sorted(tagged)}"
