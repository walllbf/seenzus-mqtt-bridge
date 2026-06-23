from pathlib import Path
import json

ROOT = Path(__file__).resolve().parents[1]


def test_pytest_config_exists() -> None:
    assert (ROOT / "pytest.ini").exists()
    assert (ROOT / "requirements_test.txt").exists()


def test_hacs_config_matches_root_integration_layout() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))

    assert (ROOT / "manifest.json").exists()
    assert hacs["content_in_root"] is True
