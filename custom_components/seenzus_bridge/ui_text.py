"""Localized notification text, with English fallback before resources are loaded."""
from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_cached_translations, async_get_translations

from .const import DOMAIN


ENGLISH_TEXT = {
    "diagnostic_title": "seenzus Bridge quick pairing diagnostics",
    "diagnostic_message": "Quick pairing failed: {reason}\n\n{diagnostic}",
    "paired_title": "seenzus Bridge paired",
    "paired_message": "seenzus MQTT Bridge is paired.\n\n## 👉 {link}",
    "return_label": "Return to seenzus",
    "manual_return": "If the link does not open, open this address manually: `{url}`",
    "opening_app": "Opening the seenzus app…",
    "return_fallback": "If you are not redirected automatically, click here",
    "pairing_unconfirmed_title": "seenzus Bridge pairing not confirmed",
    "pairing_restored_message": "seenzus did not confirm the new pairing, so the previous connection was restored and nothing changed in the app. Open Configure → Re-pair or manual setup to pair again.",
    "pairing_rejected_message": "seenzus did not confirm this pairing, so its credentials are not valid. Open this entry's Configure → Re-pair or manual setup to pair again.",
}


async def async_prepare_ui_text(hass: HomeAssistant) -> None:
    """Load notifications before the first pairing result is displayed."""
    await async_get_translations(hass, hass.config.language, "common", {DOMAIN})


def ui_text(hass: HomeAssistant, key: str, **placeholders: str) -> str:
    """Read Core's language cache; synchronous notification callers do no I/O."""
    language = getattr(hass.config, "language", "en")
    translations = async_get_cached_translations(hass, language, "common", DOMAIN)
    text = translations.get(f"component.{DOMAIN}.common.{key}", ENGLISH_TEXT[key])
    return text.format(**placeholders)
