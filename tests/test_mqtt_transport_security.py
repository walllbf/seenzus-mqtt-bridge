"""Remote credentials and state must never leave over plaintext MQTT."""
from types import SimpleNamespace

import pytest

from seenzus_bridge.config_flow import SavanAIBridgeConfigFlow, SavanAIBridgeOptionsFlow, _build_quick_pair_entry_data, _schema
from seenzus_bridge.coordinator import _transport_connect_kwargs
from seenzus_bridge.mqtt_transport import validate_mqtt_transport
from tests.helpers import FakeHass, FakeConfigEntry


@pytest.mark.parametrize("scheme", [None, "", "quic", "mqtt", "ws"])
def test_remote_pairing_rejects_insecure_or_unspecified_transport(scheme):
    result = SimpleNamespace(mqtt={"host": "broker.example.com", "scheme": scheme}, session_id="s", confirmed_at="", bridge_id="bridge")
    with pytest.raises(ValueError):
        _build_quick_pair_entry_data(api_base="https://example.com", status_result=result)


@pytest.mark.parametrize("scheme", [None, "", "quic", "mqtt", "ws"])
def test_runtime_rejects_legacy_or_insecure_remote_transport(scheme):
    with pytest.raises(ValueError):
        _transport_connect_kwargs({"mqtt_host": "broker.example.com", "mqtt_scheme": scheme})


@pytest.mark.asyncio
async def test_manual_setup_preserves_explicit_tls():
    flow = SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    result = await flow.async_step_manual({"mqtt_settings": {
        "mqtt_host": "broker.example.com", "mqtt_scheme": "mqtts", "mqtt_port": 8883,
    }})
    assert result["type"] == "create_entry"
    assert result["data"]["mqtt_scheme"] == "mqtts"


@pytest.mark.parametrize("host", [
    "example.com", "localhost", "broker.local", "8.8.8.8", "2001:4860:4860::8888",
    "0.0.0.0", "::", "192.0.2.1", "198.51.100.2", "203.0.113.3", "100.64.0.1",
    "224.0.0.1", "ff02::1", "240.0.0.1", "::ffff:8.8.8.8", "::ffff:127.0.0.1",
    "127.1", "2130706433", "0x7f000001", "127.0.0.1.example.com", "[::1]",
])
@pytest.mark.parametrize("scheme", ["mqtt", "ws"])
def test_plaintext_only_accepts_unambiguous_local_ip_literals(host, scheme):
    with pytest.raises(ValueError, match="mqtt_tls_required"):
        validate_mqtt_transport({"mqtt_host": host, "mqtt_scheme": scheme})


@pytest.mark.parametrize("host", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.2", "169.254.1.2", "::1", "fd00::1", "fe80::1"])
@pytest.mark.parametrize("scheme", ["mqtt", "ws"])
def test_explicit_local_plaintext_remains_supported(host, scheme):
    assert validate_mqtt_transport({"mqtt_host": host, "mqtt_scheme": scheme}) == scheme


@pytest.mark.parametrize("scheme", ["mqtts", "wss"])
def test_remote_tls_requires_certificate_and_hostname_verification(scheme):
    import ssl
    context = _transport_connect_kwargs({"mqtt_host": "example.com", "mqtt_scheme": scheme})["tls_context"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [False, True])
@pytest.mark.parametrize("scheme,port,path", [("mqtts", 8883, ""), ("wss", 443, "/custom")])
async def test_manual_form_persists_secure_transport_and_scheme_port(options, scheme, port, path):
    entry = FakeConfigEntry(data={"mqtt_scheme": "mqtt", "mqtt_host": "old.example", "mqtt_port": 1883})
    flow = SavanAIBridgeOptionsFlow(entry) if options else SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    flow.async_create_entry = lambda **kw: {"type": "create_entry", **kw}
    # Pass through the actual HA form schema, including number coercion.
    data = _schema("manual")({"mqtt_settings": {
        "mqtt_host": "remote.example", "mqtt_scheme": scheme, "mqtt_ws_path": "custom",
    }, "advanced_settings": {}})
    result = await flow.async_step_manual(data)
    assert result["type"] == "create_entry"
    conf = {**entry.data, **result["data"]} if options else result["data"]
    assert conf["mqtt_scheme"] == scheme
    assert conf["mqtt_port"] == port
    assert conf["mqtt_ws_path"] == path


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [False, True])
@pytest.mark.parametrize("scheme", ["mqtt", "ws", "quic", None])
async def test_manual_create_and_options_reject_remote_plaintext(options, scheme):
    flow = SavanAIBridgeOptionsFlow(FakeConfigEntry()) if options else SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    result = await flow.async_step_manual({"mqtt_settings": {
        "mqtt_host": "remote.example", "mqtt_scheme": scheme,
    }})
    assert result["type"] == "form"
    assert "mqtt_scheme" in result["errors"]


@pytest.mark.parametrize("options", [False, True])
def test_failed_pairing_explains_transport_error_without_saving_credentials(options):
    entry = FakeConfigEntry(data={"mqtt_scheme": "wss", "mqtt_host": "old.example"})
    flow = SavanAIBridgeOptionsFlow(entry) if options else SavanAIBridgeConfigFlow()
    flow.hass = FakeHass()
    flow._quick_pair_api_base = "https://example.com"
    flow.async_show_form = lambda **kw: {"type": "form", **kw}
    result = flow._entry_data_or_reshow(SimpleNamespace(mqtt={"host": "remote.example"}))
    assert result["errors"] == {"base": "mqtt_transport_required"}
    assert entry.options == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", [None, "mqtt", "ws", "unknown"])
async def test_existing_unsafe_entry_fails_setup_before_starting_coordinator(monkeypatch, scheme):
    import seenzus_bridge
    from homeassistant.exceptions import ConfigEntryError
    monkeypatch.setattr(seenzus_bridge, "BridgeCoordinator", lambda *_: pytest.fail("must not start"))
    entry = FakeConfigEntry(data={"mqtt_host": "remote.example", "mqtt_scheme": "wss"}, options={"mqtt_scheme": scheme})
    with pytest.raises(ConfigEntryError):
        await seenzus_bridge.async_setup_entry(FakeHass(), entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("scheme", [None, "mqtt", "ws", "unknown"])
async def test_runtime_blocks_before_client_creation_and_does_not_retry(monkeypatch, scheme):
    from tests.test_mqtt_loop_behavior import _make_coordinator, HAPPY_ENTRY_DATA
    coordinator, mqtt = _make_coordinator(monkeypatch, data={**HAPPY_ENTRY_DATA, "mqtt_scheme": scheme})
    await coordinator._mqtt_loop()
    assert mqtt.clients == []
    assert coordinator.mqtt_connected is False
    assert "mqtt_" in coordinator.last_error
    assert coordinator.resolve_transport() == ("invalid", None)


@pytest.mark.timeout(15)
@pytest.mark.parametrize("scheme", ["mqtts", "wss"])
@pytest.mark.parametrize("failure", ["untrusted", "hostname"])
def test_real_tls_rejects_bad_certificates_before_mqtt_credentials(monkeypatch, tmp_path, scheme, failure):
    import asyncio
    import socket
    import ssl
    import threading
    import aiomqtt
    from seenzus_bridge import coordinator as module
    from tests.test_mqtt_io_guard import _contexts

    server_context, _ = _contexts(tmp_path)
    # The generated certificate names localhost, not the 127.0.0.1 destination.
    # Trust it only in the hostname test, so each failure is independently proved.
    client_context = ssl.create_default_context(
        cafile=str(tmp_path / "test-cert.pem") if failure == "hostname" else None,
    )
    monkeypatch.setattr(module, "_TLS_CONTEXT_CACHE", client_context)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    listener.settimeout(5)
    received = []
    errors = []

    def server():
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(4)
                try:
                    with server_context.wrap_socket(connection, server_side=True) as tls:
                        received.append(tls.recv(4096))
                except (ssl.SSLError, ConnectionResetError):
                    # A rejected certificate can surface as a TCP reset on
                    # Windows. The client must still prove verification failed.
                    pass
        except Exception as err:
            errors.append(err)

    worker = threading.Thread(target=server, daemon=True)
    worker.start()

    async def connect():
        kwargs = _transport_connect_kwargs({"mqtt_host": "127.0.0.1", "mqtt_scheme": scheme})
        with pytest.raises(aiomqtt.MqttError, match="CERTIFICATE_VERIFY_FAILED"):
            async with aiomqtt.Client(
                hostname="127.0.0.1", port=listener.getsockname()[1],
                username="must-not-reach-broker", password="test-only-password", timeout=4,
                **kwargs,
            ):
                pytest.fail("Invalid certificate must fail before MQTT authentication")

    loop = asyncio.SelectorEventLoop()
    try:
        loop.run_until_complete(connect())
    finally:
        loop.close()
        worker.join(5)
        listener.close()
    assert not worker.is_alive()
    assert errors == []
    assert received == []
