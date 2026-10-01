"""Protect pending WebSocket TLS writes and retire stale socket callbacks."""
from __future__ import annotations

import asyncio
from typing import Any


def guard_websocket_io(client: Any) -> None:
    """Install connection-scoped I/O protection before connecting.

    Paho answers WebSocket PING/CLOSE frames from its receive path with a
    direct socket.send(). That must not interrupt a pending SSL write: OpenSSL
    requires the retry to contain the same data and length. Pause application
    reads while the current WebSocket frame is still being written, then restore
    them as soon as Paho's writer completes it. Acknowledgement waits remain
    concurrent; there is no lock around client.publish().

    Deferred reader/writer registration must also check the live socket when
    it runs: Paho may close it before the event loop gets to that callback.
    A cancelled connection waiter cannot safely handle more Paho reads.

    The adapter is scoped to this aiomqtt client. It uses aiomqtt's _client,
    connection futures and keepalive task, and Paho's _sendbuffer.
    HA runtime constraints select either
    aiomqtt 2.0/Paho 1.6 or aiomqtt 2.5/Paho 2.1; both pairs are tested in CI.
    """
    loop = asyncio.get_running_loop()
    paho_client = client._client
    original_read = paho_client.loop_read
    original_write = paho_client.loop_write
    paused_socket: Any | None = None

    def current_socket(sock: Any) -> bool:
        if paho_client.socket() is not sock or sock.fileno() < 0:
            return False
        if client._connected.cancelled():
            # aiomqtt's disconnect hook calls _connected.exception(), which
            # raises CancelledError for a timed-out/cancelled connect. Retire
            # the socket before another readiness callback invokes that hook.
            paho_client._sock_close()
            return False
        return True

    def update_reader() -> None:
        nonlocal paused_socket
        sock = paho_client.socket()
        if sock is None or not current_socket(sock):
            paused_socket = None
            return
        if getattr(sock, "_sendbuffer", b""):
            if paused_socket is not sock:
                loop.remove_reader(sock.fileno())
                paused_socket = sock
        elif paused_socket is sock:
            paused_socket = None
            loop.add_reader(sock.fileno(), read_ready, sock)
            # TLS can already have decrypted input even when the fd is no
            # longer readable, so explicitly give the restored reader a turn.
            loop.call_soon(read_ready, sock)

    def read_ready(sock: Any) -> None:
        # Preserve aiomqtt's SSL-buffer draining and disconnect notification,
        # while yielding to the writer whenever a frame is pending. Returning
        # 0 from loop_read alone would spin aiomqtt's SSL pending() while loop.
        try:
            while current_socket(sock):
                paho_client.loop_read()
                if paho_client.socket() is not sock or getattr(sock, "_sendbuffer", b""):
                    break
                if not hasattr(sock, "pending") or sock.pending() == 0:
                    break
        except Exception as err:  # noqa: BLE001
            if not client._disconnected.done():
                client._disconnected.set_exception(err)

    def write_ready(sock: Any) -> None:
        if not current_socket(sock):
            return
        try:
            paho_client.loop_write()
        except Exception as err:  # noqa: BLE001
            if not client._disconnected.done():
                client._disconnected.set_exception(err)

    def install_reader(sock: Any) -> None:
        if not current_socket(sock):
            return
        loop.add_reader(sock.fileno(), read_ready, sock)
        # Match aiomqtt's socket-open lifecycle, without its independently
        # queued, unchecked add_reader callback. Native socket-close still
        # cancels this same task.
        client._misc_task = loop.create_task(client._misc_loop())

    def install_writer(sock: Any) -> None:
        if current_socket(sock):
            loop.add_writer(sock.fileno(), write_ready, sock)

    def socket_open(mqtt_client: Any, userdata: Any, sock: Any) -> None:
        # Paho invokes these hooks from its connect executor as well as the
        # event loop. Check identity and descriptor only after dispatching.
        loop.call_soon_threadsafe(install_reader, sock)

    def socket_register_write(mqtt_client: Any, userdata: Any, sock: Any) -> None:
        loop.call_soon_threadsafe(install_writer, sock)

    def guarded_read(*args: Any, **kwargs: Any) -> Any:
        sock = paho_client.socket()
        if sock is not None and getattr(sock, "_sendbuffer", b""):
            update_reader()
            return 0  # MQTT_ERR_SUCCESS; the pending writer owns this turn.
        return original_read(*args, **kwargs)

    def guarded_write(*args: Any, **kwargs: Any) -> Any:
        try:
            return original_write(*args, **kwargs)
        finally:
            update_reader()

    paho_client.loop_read = guarded_read
    paho_client.loop_write = guarded_write
    paho_client.on_socket_open = socket_open
    paho_client.on_socket_register_write = socket_register_write
