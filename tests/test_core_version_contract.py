"""Contracts that must execute against the actual Home Assistant package in CI."""
from homeassistant.const import Platform, __version__ as HA_VERSION

from seenzus_bridge.catalog import HELPER_ENTITY_DOMAINS, OFFICIAL_ENTITY_DOMAINS


def test_installed_core_is_an_acceptance_matrix_version() -> None:
    assert HA_VERSION in {"2025.1.4", "2026.8.0"}


def test_publisher_platform_baseline_comes_from_the_running_core() -> None:
    assert OFFICIAL_ENTITY_DOMAINS == frozenset(platform.value for platform in Platform)
    assert {
        "input_boolean", "input_number", "input_select", "input_text", "input_datetime",
    } <= HELPER_ENTITY_DOMAINS
