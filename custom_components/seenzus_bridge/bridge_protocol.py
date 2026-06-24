"""MQTT topic helpers for v2 and legacy protocol."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(slots=True)
class BridgeTopics:
    """Resolved topic routes for one bridge instance."""

    command_sub: str
    result_prefix: str
    state_prefix: str
    catalog_topic: str
    presence_topic: str
    bridge_id: str


def _normalize_topic_root(topic_root: str) -> str:
    root = (topic_root or "").strip().strip("/")
    return root or "seenzus/v2"


def _sanitize_bridge_id(raw_bridge_id: str) -> str:
    text = (raw_bridge_id or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    return re.sub(r"-{2,}", "-", text).strip("-")


def build_bridge_id(config_bridge_id: str, entry_id: str) -> str:
    """Build stable bridge id."""
    cleaned = _sanitize_bridge_id(config_bridge_id)
    if cleaned:
        return cleaned
    return f"ha-{entry_id[:12].lower()}"


def build_topics(topic_root: str, bridge_id: str) -> BridgeTopics:
    """Resolve v2 topic tree."""
    root = _normalize_topic_root(topic_root)
    base = f"{root}/bridge/{bridge_id}"
    return BridgeTopics(
        command_sub=f"{base}/command/+",
        result_prefix=f"{base}/result",
        state_prefix=f"{base}/state",
        catalog_topic=f"{base}/catalog",
        presence_topic=f"{base}/presence",
        bridge_id=bridge_id,
    )


def retained_topics_for_bridge(topics: BridgeTopics) -> list[str]:
    """Return the retained topics currently used by one bridge."""
    return [topics.presence_topic]


def retained_topics_to_clear_on_reload(
    previous: BridgeTopics | None, current: BridgeTopics
) -> list[str]:
    """Return retained topics that should be deleted before a bridge identity change."""
    if previous is None:
        return []
    if previous.presence_topic == current.presence_topic:
        return []
    return retained_topics_for_bridge(previous)
