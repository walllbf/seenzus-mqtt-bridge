"""Protect a WebSocket frame's pending TLS write from control-frame replies."""
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

    The adapter is scoped to this aiomqtt client. It uses aiomqtt's _client /
    _disconnected and Paho's _sendbuffer. HA runtime constraints select either
    aiomqtt 2.0/Paho 1.6 or aiomqtt 2.5/Paho 2.1; both pairs are tested in CI.
    """
    loop = asyncio.get_running_loop()
    paho_client = client._client
    original_read = paho_client.loop_read
    original_write = paho_client.loop_write
    original_socket_open = paho_client.on_socket_open
    paused_socket: Any | None = None

    def update_reader() -> None:
        nonlocal paused_socket
        sock = paho_client.socket()
        if sock is None or sock.fileno() < 0:
            paused_socket = None
            return
        if getattr(sock, "_sendbuffer", b""):
            if paused_socket is not sock:
                loop.remove_reader(sock.fileno())
                paused_socket = sock
        elif paused_socket is sock:
            paused_socket = None
            loop.add_reader(sock.fileno(), read_ready)
            # TLS can already have decrypted input even when the fd is no
            # longer readable, so explicitly give the restored reader a turn.
            loop.call_soon(read_ready)

    def read_ready() -> None:
        # Preserve aiomqtt's SSL-buffer draining and disconnect notification,
        # while yielding to the writer whenever a frame is pending. Returning
        # 0 from loop_read alone would spin aiomqtt's SSL pending() while loop.
        try:
            while True:
                paho_client.loop_read()
                sock = paho_client.socket()
                if sock is None or getattr(sock, "_sendbuffer", b""):
                    break
                if not hasattr(sock, "pending") or sock.pending() == 0:
                    break
        except Exception as err:  # noqa: BLE001
            if not client._disconnected.done():
                client._disconnected.set_exception(err)

    def socket_open(mqtt_client: Any, userdata: Any, sock: Any) -> None:
        # aiomqtt also starts its keepalive task here. Keep that lifecycle and
        # replace only its reader; Paho invokes this hook in the connect thread.
        if original_socket_open is not None:
            original_socket_open(mqtt_client, userdata, sock)
        loop.call_soon_threadsafe(loop.add_reader, sock.fileno(), read_ready)

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
