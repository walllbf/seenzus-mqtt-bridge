from seenzus_bridge.bridge_protocol import build_bridge_id


def test_build_bridge_id_sanitizes_custom_value() -> None:
    assert build_bridge_id(" HA Demo / 01 ", "entry-id-ignored") == "ha-demo-01"


def test_build_bridge_id_uses_entry_prefix_when_empty() -> None:
    assert build_bridge_id("", "01KPCRMG59PHXXXX") == "ha-01kpcrmg59ph"
