"""Keep bridge cancellation intact when an established WSS connection has failed."""
from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace

import aiomqtt
import pytest

import seenzus_bridge.coordinator as coordinator_module
from seenzus_bridge import BridgeCoordinator
from tests.helpers import FakeConfigEntry, FakeHass
from tests.test_mqtt_io_guard import _contexts, _receive_frame, _websocket_upgrade


def _mqtt_payload_offset(packet):
    offset = 1
    while packet[offset] & 0x80:
        offset += 1
    return offset + 1


def test_wss_owner_cancellation_survives_an_existing_disconnect_error(monkeypatch, tmp_path):
    """Old aiomqtt must not replace shutdown with its previous disconnect error."""
    loop = asyncio.SelectorEventLoop()
    callback_errors = []
    loop.set_exception_handler(lambda _loop, context: callback_errors.append(context))

    async def run():
        server_context, client_context = _contexts(tmp_path)
        listener = socket.socket()
        listener.settimeout(3)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        bootstrap_completed = threading.Event()
        close_broker = threading.Event()
        server_errors = []
        clients = []
        waiting_for_ha_start = asyncio.Event()

        def server():
            try:
                connection, _address = listener.accept()
                with connection, server_context.wrap_socket(connection, server_side=True) as tls_connection:
                    tls_connection.settimeout(3)
                    _websocket_upgrade(tls_connection)
                    opcode, connect = _receive_frame(tls_connection)
                    assert opcode == 2 and connect[0] == 0x10
                    tls_connection.sendall(b"\x82\x04\x20\x02\x00\x00")

                    opcode, subscribe = _receive_frame(tls_connection)
                    assert opcode == 2 and subscribe[0] >> 4 == 8
                    offset = _mqtt_payload_offset(subscribe)
                    subscribe_mid = subscribe[offset:offset + 2]
                    tls_connection.sendall(b"\x82\x05\x90\x03" + subscribe_mid + b"\x01")

                    opcode, presence = _receive_frame(tls_connection)
                    assert opcode == 2 and presence[0] >> 4 == 3 and presence[0] & 2
                    offset = _mqtt_payload_offset(presence)
                    topic_length = int.from_bytes(presence[offset:offset + 2], "big")
                    presence_mid = presence[offset + 2 + topic_length:offset + 4 + topic_length]
                    tls_connection.sendall(b"\x82\x04\x40\x02" + presence_mid)
                    bootstrap_completed.set()
                    assert close_broker.wait(3)
            except Exception as error:
                server_errors.append(error)
                bootstrap_completed.set()

        class ObservedClient(aiomqtt.Client):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                clients.append(self)

        class ObservedStartEvent(asyncio.Event):
            async def wait(self):
                waiting_for_ha_start.set()
                return await super().wait()

        monkeypatch.setattr(coordinator_module, "_client_tls_context", lambda: client_context)
        coordinator = BridgeCoordinator(FakeHass(), FakeConfigEntry(data={
            "mqtt_host": "127.0.0.1", "mqtt_port": listener.getsockname()[1],
            "mqtt_scheme": "wss", "pairing_mode": "manual",
        }))
        coordinator._ha_started_event = ObservedStartEvent()
        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        owner = asyncio.create_task(coordinator._connect_and_serve(
            SimpleNamespace(Client=ObservedClient), "wss-shutdown-regression",
        ))
        try:
            await asyncio.wait_for(waiting_for_ha_start.wait(), timeout=2)
            assert await asyncio.to_thread(bootstrap_completed.wait, 2)
            assert not server_errors, repr(server_errors)
            assert not coordinator._ha_started_event.is_set()
            assert not owner.done()
            client = clients[0]
            assert client._connected.result() is None
            descriptor = client._client.socket().fileno()

            # Establish a real native disconnect error before cancelling the
            # owner while it is still waiting for Home Assistant to start.
            close_broker.set()
            with pytest.raises(aiomqtt.MqttCodeError):
                await asyncio.wait_for(asyncio.shield(client._disconnected), timeout=2)
            owner.cancel()
            with pytest.raises(asyncio.CancelledError):
                await owner
            assert owner.cancelled(), "the previous disconnect error swallowed shutdown"

            await asyncio.sleep(0)
            assert client._client.socket() is None
            assert client._misc_task is None or client._misc_task.done()
            assert loop._selector.get_map().get(descriptor) is None
            assert coordinator._mqtt_client is None
            assert not callback_errors, [repr(context.get("exception")) for context in callback_errors]
            assert not server_errors, repr(server_errors)
        finally:
            close_broker.set()
            owner.cancel()
            await asyncio.gather(owner, return_exceptions=True)
            for client in clients:
                client._client._sock_close()
                if client._misc_task:
                    client._misc_task.cancel()
                    await asyncio.gather(client._misc_task, return_exceptions=True)
            listener.close()
            await asyncio.to_thread(thread.join, 2)

    try:
        loop.run_until_complete(asyncio.wait_for(run(), timeout=8))
    finally:
        loop.close()
