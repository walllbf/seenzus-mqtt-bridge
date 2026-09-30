"""Exercise the actual Paho WebSocket/TLS path under socket backpressure."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import logging
import socket
import ssl
import struct
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
import aiomqtt
import paho.mqtt.client as mqtt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


ROOT = Path(__file__).resolve().parents[1]
GUARD_SOURCE = ROOT / "custom_components/seenzus_bridge/mqtt_io_guard.py"
spec = importlib.util.spec_from_file_location("bridge_mqtt_io_guard", GUARD_SOURCE)
guard_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard_module)


def _contexts(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    certificate = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(hours=1))
        .sign(key, hashes.SHA256())
    )
    cert_path = Path(directory) / "test-cert.pem"
    key_path = Path(directory) / "test-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    client_context.check_hostname = False
    client_context.verify_mode = ssl.CERT_NONE
    return server_context, client_context


def _receive_exact(connection, length):
    data = bytearray()
    while len(data) < length:
        chunk = connection.recv(length - len(data))
        if not chunk:
            raise EOFError("client closed before the full frame arrived")
        data.extend(chunk)
    return bytes(data)


def _receive_frame(connection):
    header = _receive_exact(connection, 2)
    opcode = header[0] & 0x0F
    length = header[1] & 0x7F
    if length == 126:
        length = struct.unpack("!H", _receive_exact(connection, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _receive_exact(connection, 8))[0]
    mask = _receive_exact(connection, 4) if header[1] & 0x80 else b""
    payload = _receive_exact(connection, length)
    if mask:
        payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return opcode, payload


@pytest.mark.parametrize("control_opcode", [0x89, 0x88], ids=["ping", "close"])
def test_control_reply_waits_for_large_tls_write_and_reader_recovers(control_opcode, caplog):
    # A close frame is also echoed by Paho; this fixture checks ordering of that
    # echo, while MQTT-level connection cleanup is covered separately.
    caplog.set_level(logging.ERROR, logger="mqtt-guard-regression")
    loop = asyncio.SelectorEventLoop()

    async def run(directory):
        server_context, client_context = _contexts(directory)
        listener = socket.socket()
        # Linux negotiates TCP window scaling during accept: set the initial
        # receive limit before listen, not after the handshake has advertised
        # a large window that can absorb the whole test payload.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        ready = threading.Event()
        drain = threading.Event()
        stop = threading.Event()
        server_errors = []
        received = []
        payload = b"x" * 524288

        def server():
            try:
                connection, _address = listener.accept()
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                with server_context.wrap_socket(connection, server_side=True) as tls_connection:
                    tls_connection.settimeout(8)
                    tls_connection.sendall(bytes([control_opcode, 1]) + b"p")
                    ready.set()
                    assert drain.wait(5)
                    # Backpressure has been established; now drain quickly on
                    # both Windows and Linux instead of keeping a tiny window.
                    tls_connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
                    received.append(_receive_frame(tls_connection))
                    received.append(_receive_frame(tls_connection))
                    # Fresh MQTT input after the echo proves the reader was restored.
                    if control_opcode == 0x89:
                        tls_connection.sendall(b"\x82\x02\xd0\x00")
                    stop.wait(5)
            except Exception as error:
                server_errors.append(error)
                ready.set()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        connection = socket.socket()
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
        connection.connect(listener.getsockname())
        tls_connection = client_context.wrap_socket(connection, server_hostname="localhost")
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, transport="websockets")
        wrapper = mqtt._WebsocketWrapper.__new__(mqtt._WebsocketWrapper)
        for name, value in {
            "_socket": tls_connection, "_ssl": True, "connected": True,
            "_sendbuffer": bytearray(), "_requested_size": 0,
            "_readbuffer": bytearray(), "_readbuffer_head": 0, "_payload_head": 0,
        }.items():
            setattr(wrapper, name, value)
        client._sock = wrapper
        client.enable_logger(logging.getLogger("mqtt-guard-regression"))
        client.on_socket_register_write = lambda *_args: None
        client.on_socket_unregister_write = lambda *_args: None
        client.on_socket_close = lambda _client, _userdata, sock: loop.remove_reader(sock.fileno())
        guard_module.guard_websocket_io(SimpleNamespace(_client=client))
        try:
            assert ready.wait(3)
            assert not server_errors
            tls_connection.setblocking(False)
            loop.add_reader(wrapper.fileno(), client.loop_read)
            client._packet_queue(0, payload, 1, 0)
            assert client.loop_write() == mqtt.MQTT_ERR_SUCCESS
            assert wrapper._sendbuffer, "fixture did not create a pending TLS write"
            assert client.loop_read() == mqtt.MQTT_ERR_SUCCESS
            assert client.socket() is wrapper, "BAD_LENGTH closed the MQTT socket while handling the control frame"
            drain.set()
            client._ping_t = 123
            deadline = loop.time() + 8
            while loop.time() < deadline:
                if client.socket() is not wrapper:
                    break
                client.loop_write()
                await asyncio.sleep(0.005)
                if len(received) == 2 and (control_opcode != 0x89 or client._ping_t == 0):
                    break
            assert not server_errors, repr(server_errors)
            assert len(received) == 2, "server did not receive data and control reply"
            assert received[0][0] == 0x2
            assert hashlib.sha256(received[0][1]).digest() == hashlib.sha256(payload).digest()
            assert received[1] == (0xA if control_opcode == 0x89 else 0x8, b"p")
            if control_opcode == 0x89:
                assert client._ping_t == 0, "incoming MQTT packets were starved after the write completed"
            assert not any("BAD_LENGTH" in record.getMessage() for record in caplog.records)
        finally:
            drain.set()
            stop.set()
            if wrapper.fileno() >= 0:
                loop.remove_reader(wrapper.fileno())
            client._sock = None
            tls_connection.close()
            listener.close()
            thread.join(2)

    try:
        with tempfile.TemporaryDirectory(prefix="guard-tls-test-", dir=ROOT) as directory:
            loop.run_until_complete(run(directory))
    finally:
        loop.close()


def test_aiomqtt_reader_pauses_without_spinning_and_keeps_exception_delivery():
    """Use aiomqtt's real socket-open callback, including its SSL pending loop."""
    loop = asyncio.SelectorEventLoop()

    async def run():
        connection, peer = socket.socketpair()
        connection.setblocking(False)
        peer.setblocking(False)

        class PendingSocket:
            def __init__(self):
                self._sendbuffer = bytearray(b"pending TLS frame")
                self.pending_calls = 0

            def fileno(self):
                return connection.fileno()

            def pending(self):
                self.pending_calls += 1
                if self._sendbuffer:
                    # Bound the real aiomqtt while loop so a regression reports
                    # a failure instead of hanging the event loop indefinitely.
                    if self.pending_calls > 1:
                        raise RuntimeError("reader spun while a TLS write was pending")
                    return 1
                return 0

        sock = PendingSocket()
        aio_client = aiomqtt.Client("unused", transport="websockets")
        client = aio_client._client
        client._sock = sock
        fail_read = False
        consumed = []

        def read():
            if fail_read:
                raise OSError("socket read failed")
            consumed.append(connection.recv(1))
            return mqtt.MQTT_ERR_SUCCESS

        client.loop_read = read
        client.loop_write = lambda: mqtt.MQTT_ERR_SUCCESS
        client.loop_misc = lambda: mqtt.MQTT_ERR_NO_CONN
        guard_module.guard_websocket_io(aio_client)
        try:
            client.on_socket_open(client, None, sock)
            await asyncio.sleep(0)
            peer.send(b"x")
            await asyncio.sleep(0.02)
            assert not aio_client._disconnected.done(), "aiomqtt's reader spun while the frame was pending"
            assert not consumed
            sock._sendbuffer.clear()
            client.loop_write()
            await asyncio.sleep(0.02)
            assert consumed == [b"x"]
            fail_read = True
            peer.send(b"y")
            await asyncio.sleep(0.02)
            assert aio_client._disconnected.done(), "restored reader lost aiomqtt's disconnect exception handler"
            assert isinstance(aio_client._disconnected.exception(), OSError)
        finally:
            if aio_client._disconnected.done():
                aio_client._disconnected.exception()
            loop.remove_reader(connection.fileno())
            if aio_client._misc_task:
                aio_client._misc_task.cancel()
                await asyncio.gather(aio_client._misc_task, return_exceptions=True)
            client._sock = None
            connection.close()
            peer.close()

    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


def test_real_aiomqtt_wss_connection_keeps_acknowledgements_concurrent():
    """Full TLS/WebSocket/MQTT handshake, two QoS 1 publishes and incoming data."""
    loop = asyncio.SelectorEventLoop()

    async def run(directory):
        server_context, client_context = _contexts(directory)
        listener = socket.socket()
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        send_ping = threading.Event()
        ping_sent = threading.Event()
        drain = threading.Event()
        release_large_ack = threading.Event()
        server_errors = []
        publications = []
        control_frames = []

        def server():
            try:
                connection, _address = listener.accept()
                connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
                with server_context.wrap_socket(connection, server_side=True) as tls_connection:
                    tls_connection.settimeout(6)
                    request = bytearray()
                    while not request.endswith(b"\r\n\r\n"):
                        request.extend(_receive_exact(tls_connection, 1))
                    headers = dict(line.split(":", 1) for line in request.decode().split("\r\n")[1:] if ":" in line)
                    key = next(value.strip() for name, value in headers.items() if name.lower() == "sec-websocket-key")
                    accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
                    tls_connection.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\nSec-WebSocket-Protocol: mqtt\r\n\r\n").encode())
                    opcode, connect = _receive_frame(tls_connection)
                    assert opcode == 2 and connect[0] == 0x10
                    tls_connection.sendall(b"\x82\x04\x20\x02\x00\x00")
                    assert send_ping.wait(4)
                    tls_connection.sendall(b"\x89\x01p")
                    ping_sent.set()
                    assert drain.wait(4)
                    tls_connection.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
                    large_mid = None
                    while len(publications) < 2 or not control_frames:
                        opcode, frame = _receive_frame(tls_connection)
                        if opcode == 0xA:
                            control_frames.append(frame)
                            continue
                        assert opcode == 2 and frame[0] == 0x32
                        position = 1
                        while frame[position] & 0x80:
                            position += 1
                        position += 1
                        topic_length = struct.unpack("!H", frame[position:position + 2])[0]
                        position += 2
                        topic = frame[position:position + topic_length].decode()
                        position += topic_length
                        mid = frame[position:position + 2]
                        body = frame[position + 2:]
                        publications.append((topic, len(body)))
                        if topic == "test/large":
                            large_mid = mid
                            assert hashlib.sha256(body).digest() == hashlib.sha256(b"x" * 524288).digest()
                        else:
                            assert topic == "test/small" and body == b"ok"
                            # The second acknowledgement intentionally arrives first.
                            tls_connection.sendall(b"\x82\x04\x40\x02" + mid)
                    assert release_large_ack.wait(4)
                    tls_connection.sendall(b"\x82\x04\x40\x02" + large_mid)
                    incoming = b"\x30\x0b\x00\x05probedata"
                    tls_connection.sendall(b"\x82" + bytes([len(incoming)]) + incoming)
                    opcode, disconnect = _receive_frame(tls_connection)
                    assert opcode == 2 and disconnect == b"\xe0\x00"
            except Exception as error:
                server_errors.append(error)
                ping_sent.set()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        aio_client = aiomqtt.Client(
            "127.0.0.1", port=listener.getsockname()[1], transport="websockets",
            tls_context=client_context, timeout=5,
            socket_options=[(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)],
        )
        guard_module.guard_websocket_io(aio_client)
        try:
            async with aio_client as client:
                large = asyncio.create_task(client.publish("test/large", b"x" * 524288, qos=1))
                small = None
                try:
                    deadline = loop.time() + 2
                    while not client._client.socket()._sendbuffer and loop.time() < deadline:
                        await asyncio.sleep(0.001)
                    assert client._client.socket()._sendbuffer, "fixture did not create TLS backpressure"
                    send_ping.set()
                    assert ping_sent.wait(2)
                    assert not server_errors, repr(server_errors)
                    # Let the queued control-frame reader run while the large
                    # write remains blocked. This makes the unguarded baseline
                    # reproduce deterministically before the server drains it.
                    await asyncio.sleep(0.02)
                    small = asyncio.create_task(client.publish("test/small", b"ok", qos=1))
                    drain.set()
                    await asyncio.wait_for(small, timeout=5)
                    assert not large.done(), "QoS acknowledgement waits were serialized"
                    release_large_ack.set()
                    await asyncio.wait_for(large, timeout=5)
                    incoming = await asyncio.wait_for(anext(client.messages), timeout=5)
                    assert str(incoming.topic) == "probe" and incoming.payload == b"data"
                    assert publications == [("test/large", 524288), ("test/small", 2)]
                    assert control_frames == [b"p"]
                finally:
                    send_ping.set()
                    drain.set()
                    release_large_ack.set()
                    tasks = [task for task in (large, small) if task is not None]
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            listener.close()
            thread.join(2)
        assert not server_errors, repr(server_errors)

    try:
        with tempfile.TemporaryDirectory(prefix="aiomqtt-wss-test-", dir=ROOT) as directory:
            loop.run_until_complete(asyncio.wait_for(run(directory), timeout=12))
    finally:
        loop.close()
