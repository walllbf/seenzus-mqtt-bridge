from pathlib import Path
import sys
import asyncio
import json

import pytest

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Repo root holds the `tests` package; custom_components holds the integration
# package `seenzus_bridge`. Put both on sys.path so tests can import either.
REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "custom_components"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


@pytest.fixture
def bridge_manifest_version() -> str:
    manifest = REPO_ROOT / "custom_components" / "seenzus_bridge" / "manifest.json"
    return json.loads(manifest.read_text(encoding="utf-8"))["version"]


@pytest.fixture(autouse=True)
def fake_hass_translation_loading(monkeypatch):
    """FakeHass has no Core loader; real-HA regressions keep the actual loader."""
    from tests.helpers import FakeHass
    from seenzus_bridge import ui_text

    load = ui_text.async_get_translations

    async def load_for_hass(hass, *args, **kwargs):
        if isinstance(hass, FakeHass):
            return {}
        return await load(hass, *args, **kwargs)

    monkeypatch.setattr(ui_text, "async_get_translations", load_for_hass)
