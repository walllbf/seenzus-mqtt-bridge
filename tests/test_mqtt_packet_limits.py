"""Keep the connection usable when a command result exceeds the broker limit."""
from __future__ import annotations

import asyncio
import json
import socket
import threading

import aiomqtt
import pytest

from seenzus_bridge import BridgeCoordinator
from seenzus_bridge.bridge_protocol import build_topics
from seenzus_bridge.mqtt_io_guard import websocket_connection
from tests.helpers import FakeConfigEntry, FakeHass
from tests.test_mqtt_io_guard import _contexts, _receive_frame, _websocket_upgrade


BROKER_MAX_PACKET_SIZE = 1024 * 1024


def _mqtt_payload_offset(packet: bytes) -> int:
    offset = 1
    while packet[offset] & 0x80:
        offset += 1
    return offset + 1


@pytest.mark.parametrize("result_kind", ["irreducible", "compactable"])
def test_oversized_result_keeps_real_wss_connection_for_the_next_command(tmp_path, result_kind):
    """Replay EMQX's 1 MiB cutoff through the real result publish call site."""
    loop = asyncio.SelectorEventLoop()
    callback_errors = []
    loop.set_exception_handler(lambda _loop, context: callback_errors.append(context))

    async def run():
        server_context, client_context = _contexts(tmp_path)
        listener = socket.socket()
        listener.settimeout(5)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        publications = []
        rejected_packet_sizes = []
        server_errors = []

        def server():
            try:
                connection, _address = listener.accept()
                with connection, server_context.wrap_socket(connection, server_side=True) as tls:
                    tls.settimeout(5)
                    _websocket_upgrade(tls)
                    opcode, connect = _receive_frame(tls)
                    assert opcode == 2 and connect[0] == 0x10
                    tls.sendall(b"\x82\x04\x20\x02\x00\x00")
                    while True:
                        opcode, packet = _receive_frame(tls)
                        assert opcode == 2
                        if packet[0] >> 4 == 14:
                            return
                        assert packet[0] >> 4 == 3
                        if len(packet) > BROKER_MAX_PACKET_SIZE:
                            # MQTT 3.1.1 has no DISCONNECT reason packet: the
                            # broker closes WSS and aiomqtt reports connection
                            # loss, exactly as the production EMQX trace shows.
                            rejected_packet_sizes.append(len(packet))
                            return
                        offset = _mqtt_payload_offset(packet)
                        topic_length = int.from_bytes(packet[offset:offset + 2], "big")
                        topic_start = offset + 2
                        topic = packet[topic_start:topic_start + topic_length].decode()
                        mid_start = topic_start + topic_length
                        assert packet[0] & 0x06 == 0x02, "result must retain QoS 1"
                        mid = packet[mid_start:mid_start + 2]
                        body = json.loads(packet[mid_start + 2:])
                        publications.append((topic, body, len(packet)))
                        tls.sendall(b"\x82\x04\x40\x02" + mid)
            except Exception as error:
                server_errors.append(error)

        worker = threading.Thread(target=server, daemon=True)
        worker.start()
        coordinator = BridgeCoordinator(FakeHass(), FakeConfigEntry())
        coordinator._topics = build_topics("seenzus/v2", "packet-limit-regression")
        client = aiomqtt.Client(
            "127.0.0.1", port=listener.getsockname()[1], transport="websockets",
            tls_context=client_context, timeout=3,
        )
        small_result_sent = False
        remained_connected = False
        try:
            try:
                async with websocket_connection(client):
                    # A single large attribute reproduces the real aggregate
                    # /api/states reply without any production entity data.
                    if result_kind == "irreducible":
                        states = [{
                            "entity_id": "sensor.large_result",
                            "state": "on",
                            "attributes": {"history": "x" * 1_120_000},
                        }]
                    else:
                        states = [{
                            "entity_id": f"sensor.test_{index}",
                            "state": "on",
                            "attributes": {"friendly_name": "温度传感器" * 8},
                        } for index in range(4000)]
                        assert len(json.dumps(states).encode()) > BROKER_MAX_PACKET_SIZE
                    await coordinator._publish_result(
                        client, "large-result", success=True, status=200, data=states,
                    )
                    small_result_sent = await coordinator._publish_result(
                        client, "next-result", success=True, status=200,
                        data={"entity_id": "light.demo", "state": "on"},
                    )
                    remained_connected = not client._disconnected.done()
            except aiomqtt.MqttError:
                # Legacy aiomqtt rethrows connection loss on context exit. The
                # assertions below report the actual oversized wire packet.
                pass
        finally:
            listener.close()
            await asyncio.to_thread(worker.join, 2)

        assert not rejected_packet_sizes, (
            f"result exceeded broker's {BROKER_MAX_PACKET_SIZE}-byte packet limit: "
            f"{rejected_packet_sizes}"
        )
        assert not server_errors, repr(server_errors)
        assert not worker.is_alive()
        assert not callback_errors, [repr(context.get("exception")) for context in callback_errors]
        assert remained_connected, "oversized result disconnected the WSS session"
        assert small_result_sent, "the following command could not publish its result"
        assert len(publications) == 2
        large_topic, large_result, _ = publications[0]
        small_topic, small_result, _ = publications[1]
        assert large_topic.endswith("/large-result")
        assert large_result["msgId"] == "large-result"
        if result_kind == "irreducible":
            assert large_result["success"] is False
            assert large_result["status"] == 413
            assert large_result["error"] == "response_too_large"
            assert large_result["packetSize"] > large_result["maxPacketSize"]
            assert large_result["maxPacketSize"] == BROKER_MAX_PACKET_SIZE
            assert "data" not in large_result
        else:
            assert large_result["success"] is True
            assert large_result["status"] == 200
            assert large_result["data"] == states, "compact JSON must preserve every state and attribute"
        assert small_topic.endswith("/next-result")
        assert small_result["msgId"] == "next-result"
        assert small_result["success"] is True
        assert small_result["data"] == {"entity_id": "light.demo", "state": "on"}

    try:
        loop.run_until_complete(asyncio.wait_for(run(), timeout=12))
    finally:
        loop.close()
