"""Real WSS peers exercise control framing and simultaneous socket backpressure."""
from __future__ import annotations

import asyncio
import socket
import ssl
import threading

import aiomqtt
import paho.mqtt.client as mqtt
import pytest

from tests.test_mqtt_io_guard import (
    _contexts,
    _receive_exact,
    _receive_frame,
    _websocket_upgrade,
    guard_module,
)


def _remaining_length(value):
    encoded = bytearray()
    while True:
        digit = value % 128
        value //= 128
        encoded.append(digit | (128 if value else 0))
        if not value:
            return bytes(encoded)


@pytest.mark.parametrize("opcode,payload", [(0x89, b"ping"), (0x88, b"\x03\xe8")], ids=["ping", "close"])
def test_wss_peer_accepts_masked_control_reply(opcode, payload, tmp_path):
    """An RFC 6455 peer must reject unmasked client frames, including controls."""
    loop = asyncio.SelectorEventLoop()

    async def run():
        server_context, client_context = _contexts(tmp_path)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        release = threading.Event()
        server_errors = []
        replies = []

        def server():
            try:
                connection, _ = listener.accept()
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    tls.settimeout(4)
                    _websocket_upgrade(tls)
                    assert _receive_frame(tls)[1][0] == 0x10
                    tls.sendall(b"\x82\x04\x20\x02\x00\x00")
                    assert release.wait(3)
                    tls.sendall(bytes([opcode, len(payload)]) + payload)
                    header = _receive_exact(tls, 2)
                    assert header[1] & 128, "client sent an unmasked WebSocket control frame"
                    assert header[1] & 127 == len(payload)
                    mask = _receive_exact(tls, 4)
                    body = _receive_exact(tls, len(payload))
                    replies.append((header[0], bytes(value ^ mask[i % 4] for i, value in enumerate(body))))
                    if opcode == 0x89:
                        # Prove the connection still processes MQTT traffic.
                        assert _receive_frame(tls)[1] == b"\xc0\x00"
                        tls.sendall(b"\x82\x02\xd0\x00")
                    assert _receive_frame(tls)[1] == b"\xe0\x00"
            except Exception as error:
                server_errors.append(error)

        worker = threading.Thread(target=server, daemon=True)
        worker.start()
        client = aiomqtt.Client("127.0.0.1", port=listener.getsockname()[1], transport="websockets", tls_context=client_context, timeout=3)
        try:
            async with guard_module.websocket_connection(client):
                release.set()
                deadline = loop.time() + 3
                while not replies and not server_errors and loop.time() < deadline:
                    await asyncio.sleep(0.005)
                assert not server_errors, repr(server_errors)
                assert replies == [(0x8A if opcode == 0x89 else 0x88, payload)]
                if opcode == 0x89:
                    client._client._send_pingreq()
                    deadline = loop.time() + 3
                    while client._client._ping_t and loop.time() < deadline:
                        await asyncio.sleep(0.005)
                    assert not client._client._ping_t, "MQTT keepalive did not receive its response"
        finally:
            release.set()
            listener.close()
            await asyncio.to_thread(worker.join, 4)
        assert not server_errors, repr(server_errors)

    try:
        loop.run_until_complete(asyncio.wait_for(run(), 12))
    finally:
        loop.close()


def test_large_wss_publish_and_incoming_message_progress_together(tmp_path):
    """A peer writing before it drains our frame must not deadlock our reader."""
    loop = asyncio.SelectorEventLoop()

    async def run():
        server_context, client_context = _contexts(tmp_path)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        release = threading.Event()
        server_errors = []
        payload = b"x" * 524288
        incoming = b"\x30" + _remaining_length(len(payload) + 7) + b"\x00\x05probe" + payload
        frame = b"\x82\x7f" + len(incoming).to_bytes(8, "big") + incoming

        def server():
            try:
                connection, _ = listener.accept()
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    tls.settimeout(5)
                    _websocket_upgrade(tls)
                    assert _receive_frame(tls)[1][0] == 0x10
                    tls.sendall(b"\x82\x04\x20\x02\x00\x00")
                    assert release.wait(3)
                    tls.sendall(frame)
                    tls.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
                    opcode, publish = _receive_frame(tls)
                    assert opcode == 2 and publish[0] == 0x32
                    position = 1
                    while publish[position] & 128:
                        position += 1
                    position += 1
                    topic_length = int.from_bytes(publish[position:position + 2], "big")
                    position += 2 + topic_length
                    mid = publish[position:position + 2]
                    assert publish[position + 2:] == payload
                    tls.sendall(b"\x82\x04\x40\x02" + mid)
                    assert _receive_frame(tls)[1] == b"\xe0\x00"
            except Exception as error:
                server_errors.append(error)

        worker = threading.Thread(target=server, daemon=True)
        worker.start()
        client = aiomqtt.Client("127.0.0.1", port=listener.getsockname()[1], transport="websockets", tls_context=client_context, timeout=6)
        try:
            async with guard_module.websocket_connection(client):
                sock = client._client.socket()._socket
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
                publication = asyncio.create_task(client.publish("probe", payload, qos=1))
                reception = asyncio.create_task(anext(client.messages))
                try:
                    deadline = loop.time() + 2
                    while not client._client.socket()._sendbuffer and loop.time() < deadline:
                        await asyncio.sleep(0.005)
                    assert client._client.socket()._sendbuffer, "fixture did not create send backpressure"
                    release.set()
                    await asyncio.wait_for(publication, 7)
                    message = await asyncio.wait_for(reception, 2)
                    assert message.payload == payload
                finally:
                    publication.cancel()
                    reception.cancel()
                    await asyncio.gather(publication, reception, return_exceptions=True)
        finally:
            release.set()
            listener.close()
            await asyncio.to_thread(worker.join, 6)
        assert not server_errors, repr(server_errors)

    try:
        loop.run_until_complete(asyncio.wait_for(run(), 16))
    finally:
        loop.close()


def test_control_tls_retries_keep_bytes_before_the_next_mqtt_frame(tmp_path):
    """Inject TLS retry outcomes while a real peer validates framing and QoS."""
    loop = asyncio.SelectorEventLoop()

    async def run():
        server_context, client_context = _contexts(tmp_path)
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        release = threading.Event()
        blocked = asyncio.Event()
        allow_write = False
        server_errors = []

        def server():
            try:
                connection, _ = listener.accept()
                with server_context.wrap_socket(connection, server_side=True) as tls:
                    tls.settimeout(4)
                    _websocket_upgrade(tls)
                    assert _receive_frame(tls)[1][0] == 0x10
                    tls.sendall(b"\x82\x04\x20\x02\x00\x00")
                    assert release.wait(3)
                    tls.sendall(b"\x89\x04ping")
                    assert _receive_frame(tls) == (0xA, b"ping")
                    opcode, packet = _receive_frame(tls)
                    assert opcode == 2 and packet[:9] == b"\x32\x0b\x00\x05probe"
                    assert packet[11:] == b"ok"
                    tls.sendall(b"\x82\x04\x40\x02" + packet[9:11])
                    assert _receive_frame(tls)[1] == b"\xe0\x00"
            except Exception as error:
                server_errors.append(error)

        class RetrySocket:
            def __init__(self, sock):
                self.sock = sock
                self.attempt = 0
                self.frame = None

            def __getattr__(self, name):
                return getattr(self.sock, name)

            def send(self, data):
                if self.frame is None:
                    assert data[0] == 0x8A
                    self.frame = bytes(data)
                if self.attempt == 0:
                    assert data == self.frame, "TLS WANT_WRITE retry changed the frame"
                    blocked.set()
                    if not allow_write:
                        raise ssl.SSLWantWriteError()
                    self.attempt = 1
                    # Forward only the first two header bytes to create a
                    # real partial frame, then require a read-ready retry.
                    assert self.sock.send(data[:2]) == 2
                    return 2
                if self.attempt == 1:
                    assert data == self.frame[2:]
                    self.attempt = 2
                    raise ssl.SSLWantReadError()
                if self.attempt == 2:
                    assert data == self.frame[2:], "TLS WANT_READ retry changed the remainder"
                    self.attempt = 3
                return self.sock.send(data)

        wrapper_type = getattr(mqtt, "_WebsocketWrapper", None) or mqtt.WebsocketWrapper
        native_send = wrapper_type._send_impl
        native_frame = wrapper_type._create_frame
        worker = threading.Thread(target=server, daemon=True)
        worker.start()
        client = aiomqtt.Client("127.0.0.1", port=listener.getsockname()[1], transport="websockets", tls_context=client_context, timeout=3)
        writer = None
        try:
            async with guard_module.websocket_connection(client):
                writer = client._client.socket()._socket
                retry_socket = RetrySocket(writer._socket)
                writer._socket = retry_socket
                release.set()
                await asyncio.wait_for(blocked.wait(), 3)
                publication = asyncio.create_task(client.publish("probe", b"ok", qos=1))
                try:
                    # Give publish a turn while PONG is still pending; a
                    # binary frame must not replace the TLS retry's bytes.
                    await asyncio.sleep(0)
                    assert not publication.done()
                    allow_write = True
                    await asyncio.wait_for(publication, 3)
                    assert retry_socket.attempt == 3
                    assert not writer.has_controls
                finally:
                    publication.cancel()
                    await asyncio.gather(publication, return_exceptions=True)
        finally:
            release.set()
            listener.close()
            await asyncio.to_thread(worker.join, 4)
        assert not server_errors, repr(server_errors)
        assert client._client.socket() is None
        assert not writer.has_controls
        # Other HA integrations use the same library: class methods must
        # remain untouched after both installation and teardown.
        assert wrapper_type._send_impl is native_send
        assert wrapper_type._create_frame is native_frame

    try:
        loop.run_until_complete(asyncio.wait_for(run(), 12))
    finally:
        loop.close()
