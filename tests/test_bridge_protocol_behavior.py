from __future__ import annotations

from seenzus_bridge.bridge_protocol import (
    BridgeTopics,
    retained_topics_to_clear_on_reload,
)
from seenzus_bridge.entity_filters import (
    looks_like_internal_bridge_entity_id,
    name_has_model_marker,
)


def _topics(bridge_id: str) -> BridgeTopics:
    return BridgeTopics(
        command_sub=f"seenzus/v2/bridge/{bridge_id}/command/+",
        result_prefix=f"seenzus/v2/bridge/{bridge_id}/result",
        state_prefix=f"seenzus/v2/bridge/{bridge_id}/state",
        catalog_topic=f"seenzus/v2/bridge/{bridge_id}/catalog",
        presence_topic=f"seenzus/v2/bridge/{bridge_id}/presence",
        bridge_id=bridge_id,
    )


def test_clears_previous_retained_topics_when_bridge_identity_changes() -> None:
    previous = _topics("ha-old")
    current = _topics("ha-new")

    assert retained_topics_to_clear_on_reload(previous, current) == [
        "seenzus/v2/bridge/ha-old/presence"
    ]


def test_keeps_retained_topics_when_bridge_identity_is_unchanged() -> None:
    current = _topics("ha-same")

    assert retained_topics_to_clear_on_reload(current, current) == []


def test_detects_internal_bridge_metric_sensor() -> None:
    assert looks_like_internal_bridge_entity_id(
        "sensor.seenzus_mqtt_bridge_zhuang_tai_tui_song_ci_shu"
    )


def test_does_not_match_regular_entity() -> None:
    assert not looks_like_internal_bridge_entity_id(
        "fan.dmaker_cn_740506461_p5c_s_2_fan"
    )


def test_name_with_asterisk_is_model_marked() -> None:
    assert name_has_model_marker("Aqara T1*")


def test_plain_name_is_not_model_marked() -> None:
    assert not name_has_model_marker("Living Room Light")


def test_missing_name_is_not_model_marked() -> None:
    assert not name_has_model_marker(None)
    assert not name_has_model_marker("")
