"""Contracts that must execute against the actual Home Assistant package in CI."""
import os

from homeassistant.const import Platform, __version__ as HA_VERSION

from seenzus_bridge.catalog import HELPER_ENTITY_DOMAINS, OFFICIAL_ENTITY_DOMAINS


def test_installed_core_matches_the_explicit_job_selection() -> None:
    expected = os.environ.get("EXPECTED_HA_VERSION")
    assert expected, "Set EXPECTED_HA_VERSION to the exact Core version selected for this run"
    assert HA_VERSION == expected


def test_publisher_platform_baseline_comes_from_the_running_core() -> None:
    assert OFFICIAL_ENTITY_DOMAINS == frozenset(platform.value for platform in Platform)
    assert {
        "input_boolean", "input_number", "input_select", "input_text", "input_datetime",
    } <= HELPER_ENTITY_DOMAINS
