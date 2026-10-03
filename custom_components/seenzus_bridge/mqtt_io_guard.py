"""Serialize WebSocket writes without blocking reads; retire stale callbacks."""
from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
import ssl
from typing import Any


class _WebSocketWriter:
    """Adapt one Paho WebSocket's two send paths to an ordered TLS writer.

    Both supported Paho versions send binary frames through ``_sendbuffer``
    and send control replies directly from their parser. Queue those replies
    without interrupting the binary frame (or an SSL WANT_WRITE retry).
    Paho still owns framing, parsing, MQTT delivery and QoS acknowledgements.
    """

    def __init__(self, wrapper: Any, request_write: Callable[[], None]) -> None:
        self._wrapper = wrapper
        self._socket = wrapper._socket
        self._request_write = request_write
        self._controls: deque[bytes] = deque()
        self._native_frame = wrapper._create_frame
        self._native_send = wrapper._send_impl
        wrapper._socket = self
        wrapper._create_frame = self._create_frame
        wrapper._send_impl = self._send_binary

    def __getattr__(self, name: str) -> Any:
        return getattr(self._socket, name)

    @property
    def has_controls(self) -> bool:
        return bool(self._controls)

    def _create_frame(self, opcode: int, data: bytearray, do_masking: int = 1) -> bytearray:
        # RFC 6455 requires masking for *all* client frames, including PONG
        # and CLOSE. Paho 1.6/2.1 explicitly pass do_masking=0 for those.
        return self._native_frame(opcode, data, 1)

    def send(self, data: Any) -> int:
        if data is self._wrapper._sendbuffer:
            # Preserve Paho's exact buffer and partial-write accounting.
            return self._socket.send(data)
        # Installed after the HTTP upgrade: the only other native send path
        # is a complete control reply. Bound memory if a peer floods PINGs
        # while it refuses to read; closing is safer than an unbounded queue.
        if len(self._controls) >= 128:
            raise ConnectionError("WebSocket control reply queue is full")
        self._controls.append(bytes(data))
        self._request_write()
        return len(data)

    def flush(self) -> bool:
        if self._wrapper._sendbuffer:
            return False
        while self._controls:
            data = self._controls[0]
            try:
                written = self._socket.send(data)
            except (BlockingIOError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                return False
            if written == 0:
                return False
            if written < len(data):
                self._controls[0] = data[written:]
                return False
            self._controls.popleft()
        return True

    def _send_binary(self, data: bytes) -> int:
        if not self._wrapper._sendbuffer and not self.flush():
            raise BlockingIOError
        return self._native_send(data)

    def close(self) -> None:
        self._controls.clear()
        self._socket.close()


@asynccontextmanager
async def websocket_connection(client: Any) -> AsyncIterator[Any]:
    """Own one bridge connection, including a connect worker that outlives cancellation."""
    retire = guard_websocket_io(client)
    cancellation: asyncio.CancelledError | None = None
    try:
        async with client as connected:
            try:
                yield connected
            except asyncio.CancelledError as err:
                cancellation = err
                raise
    except Exception:
        # aiomqtt 2.0 may replace body cancellation with an earlier disconnect
        # error in __aexit__. Preserve the stop request so the retry loop exits.
        if cancellation is not None:
            raise cancellation from None
        raise
    finally:
        retire()


def guard_websocket_io(client: Any) -> Callable[[], None]:
    """Install connection-scoped I/O protection before connecting.

    Paho answers WebSocket PING/CLOSE frames from its receive path with a
    direct socket.send(). Queue those replies behind the current frame so
    OpenSSL retries keep the same bytes and length. Reads remain active during
    backpressure, allowing both peers to drain each other's data. QoS waits
    remain concurrent; there is no lock around client.publish().

    Deferred reader/writer registration must also check the live socket when
    it runs: Paho may close it before the event loop gets to that callback.
    A cancelled connection waiter cannot safely handle more Paho reads.

    The adapter is scoped to this aiomqtt client. It uses aiomqtt's _client,
    connection futures and keepalive task, and Paho's _sendbuffer.
    HA runtime constraints select either
    aiomqtt 2.0/Paho 1.6 or aiomqtt 2.5/Paho 2.1; both pairs are tested in CI.
    """
    # Import only after HA has selected/installed its compatible MQTT pair.
    from aiomqtt import MqttError

    loop = asyncio.get_running_loop()
    paho_client = client._client
    original_write = paho_client.loop_write
    original_want_write = paho_client.want_write
    retired = False

    def writer_for(sock: Any) -> _WebSocketWriter | None:
        writer = getattr(sock, "_socket", None)
        return writer if isinstance(writer, _WebSocketWriter) else None

    def adapt_socket(sock: Any) -> None:
        if hasattr(sock, "_send_impl") and writer_for(sock) is None:
            _WebSocketWriter(sock, paho_client._call_socket_register_write)

    def retire_connection() -> None:
        nonlocal retired
        retired = True
        if not client._connected.done():
            client._connected.cancel()
        paho_client._sock_close()

    def socket_is_active(sock: Any) -> bool:
        if paho_client.socket() is not sock or sock.fileno() < 0:
            return False
        if retired or client._connected.cancelled():
            # aiomqtt's disconnect hook calls _connected.exception(), which
            # raises CancelledError for a timed-out/cancelled connect. Retire
            # the socket before another readiness callback invokes that hook.
            paho_client._sock_close()
            return False
        return True

    def fail_connection(err: Exception) -> None:
        if client._disconnected.done():
            return
        if isinstance(err, OSError):
            # Control writes bypass Paho's normal OSError -> MQTT error path.
            # aiomqtt 2.0 re-raises this future's exception on context exit,
            # so keep transport faults recognizable by the reconnect loop.
            failure = MqttError(str(err))
            failure.__cause__ = err
            client._disconnected.set_exception(failure)
        else:
            client._disconnected.set_exception(err)

    def read_ready(sock: Any) -> None:
        # Preserve aiomqtt's SSL-buffer draining and disconnect notification.
        try:
            while socket_is_active(sock):
                paho_client.loop_read()
                if paho_client.socket() is not sock:
                    break
                if not hasattr(sock, "pending") or sock.pending() == 0:
                    break
        except Exception as err:  # noqa: BLE001
            fail_connection(err)

    def write_ready(sock: Any) -> None:
        if not socket_is_active(sock):
            return
        try:
            paho_client.loop_write()
        except Exception as err:  # noqa: BLE001
            fail_connection(err)

    def install_reader(sock: Any) -> None:
        if not socket_is_active(sock):
            return
        loop.add_reader(sock.fileno(), read_ready, sock)
        # Match aiomqtt's socket-open lifecycle, without its independently
        # queued, unchecked add_reader callback. Native socket-close still
        # cancels this same task.
        client._misc_task = loop.create_task(client._misc_loop())

    def install_writer(sock: Any) -> None:
        if socket_is_active(sock) and paho_client.want_write():
            loop.add_writer(sock.fileno(), write_ready, sock)

    def socket_open(mqtt_client: Any, userdata: Any, sock: Any) -> None:
        # Paho invokes these hooks from its connect executor as well as the
        # event loop. Check identity and descriptor only after dispatching.
        # This hook runs before Paho sends CONNECT, after WebSocket upgrade.
        # Install the instance adapter before the connect worker can send.
        adapt_socket(sock)
        loop.call_soon_threadsafe(install_reader, sock)

    def socket_register_write(mqtt_client: Any, userdata: Any, sock: Any) -> None:
        loop.call_soon_threadsafe(install_writer, sock)

    def guarded_write(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_write(*args, **kwargs)
        finally:
            sock = paho_client.socket()
            if sock is not None and socket_is_active(sock):
                writer = writer_for(sock)
                if writer is not None:
                    writer.flush()
                if not paho_client.want_write():
                    paho_client._call_socket_unregister_write()

    def want_write() -> bool:
        writer = writer_for(paho_client.socket())
        return original_want_write() or (writer is not None and writer.has_controls)

    paho_client.loop_write = guarded_write
    paho_client.want_write = want_write
    paho_client.on_socket_open = socket_open
    paho_client.on_socket_register_write = socket_register_write
    # Also support an already-open socket (used by transport-level fixtures).
    if (sock := paho_client.socket()) is not None:
        adapt_socket(sock)
    return retire_connection
