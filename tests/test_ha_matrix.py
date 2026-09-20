"""CI release selection contracts; network responses are the external seam."""
import json
from urllib.error import HTTPError

import pytest

from tools.ha_matrix import resolve_matrix


def test_stable_channel_is_resolved_once_and_uses_release_test_dependencies():
    requests = []

    def fetch(url):
        requests.append(url)
        if url.endswith("stable.json"):
            return json.dumps({"channel": "stable", "homeassistant": {"default": "2026.9.3"}})
        if url.endswith(".python-version"):
            return "3.14.5\n"
        return "pytest==9.0.3\npytest-asyncio==1.4.0\npytest-timeout==2.4.0\n"

    matrix = resolve_matrix(fetch=fetch)
    assert [job["home_assistant"] for job in matrix["include"]] == ["2025.1.4", "2026.9.3"]
    latest = matrix["include"][1]
    assert latest["python"] == "3.14.5"
    assert latest["test_dependencies"] == "pytest==9.0.3 pytest-asyncio==1.4.0 pytest-timeout==2.4.0"
    assert latest["core"] == "homeassistant==2026.9.3"
    assert sum(url.endswith("stable.json") for url in requests) == 1


def test_explicit_reproduction_does_not_query_the_latest_channel():
    def fetch(url):
        assert "stable.json" not in url
        assert "/2025.1.4/" in url or "/2026.9.2/" in url
        if url.endswith(".python-version"):
            return "3.14.5"
        return "pytest==9.0.3\npytest-asyncio==1.4.0\npytest-timeout==2.4.0\n"

    matrix = resolve_matrix("2026.9.2", fetch=fetch)
    assert [job["home_assistant"] for job in matrix["include"]] == ["2025.1.4", "2026.9.2"]


@pytest.mark.parametrize("version", ["2026.9.0b1", "2026.10.0.dev1", "latest", "2024.12.5", "2026.13.1", "2026.9.2\nextra"])
def test_rejects_unstable_or_invalid_reproduction_versions(version):
    with pytest.raises(ValueError):
        resolve_matrix(version, fetch=lambda _: "pytest==9.0.3\npytest-asyncio==1.4.0\npytest-timeout==2.4.0\n")


def test_official_lookup_failure_is_not_hidden_by_a_pinned_fallback():
    def offline(url):
        raise HTTPError(url, 503, "Unavailable", {}, None)

    with pytest.raises(HTTPError):
        resolve_matrix(fetch=offline)


def test_missing_release_test_pins_fail_instead_of_installing_unbounded_dependencies():
    with pytest.raises(ValueError, match="pytest-asyncio"):
        resolve_matrix("2026.9.2", fetch=lambda _: "pytest==9.0.3\n")
