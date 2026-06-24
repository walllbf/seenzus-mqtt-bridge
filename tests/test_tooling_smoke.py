from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_config_exists() -> None:
    assert (ROOT / "pytest.ini").exists()
    assert (ROOT / "requirements_test.txt").exists()


def test_hacs_config_matches_custom_component_layout() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))

    # HACS default-store layout: the integration lives under
    # custom_components/<domain>/, not the repo root — so hacs.json must NOT
    # declare content_in_root (validated green by hacs/action in CI).
    assert (ROOT / "custom_components" / "seenzus_bridge" / "manifest.json").exists()
    assert hacs.get("content_in_root") is not True
    assert "name" in hacs
