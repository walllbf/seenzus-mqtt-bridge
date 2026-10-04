"""Exercise the optional read protocol through real Paho TCP and TLS/WSS packets.

COMPACT_READ_REPORT_DIR optionally retains synthetic traces and timing/size evidence.
This is a local transport contract, not a real HA pairing or production acceptance.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import threading
import time
import tracemalloc

import aiomqtt
import pytest

from seenzus_bridge import BridgeCoordinator, er
from seenzus_bridge.bridge_protocol import build_topics
from seenzus_bridge.mqtt_io_guard import websocket_connection
from seenzus_bridge.ha_dispatcher import service_helper
from tests.helpers import FakeConfigEntry, FakeEntityRegistry, FakeHass
from tests.test_mqtt_io_guard import _contexts, _receive_exact, _receive_frame, _websocket_upgrade


@contextmanager
def wire_broker(directory, transport):
    server_context, client_context = _contexts(directory)
    listener = socket.socket()
    listener.settimeout(10)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    messages, errors = [], []

    def serve():
        try:
            connection, _ = listener.accept()
            with connection:
                stream = server_context.wrap_socket(connection, server_side=True) if transport == "wss" else connection
                with stream:
                    stream.settimeout(15)
                    if transport == "wss":
                        _websocket_upgrade(stream)
                    def send(packet):
                        stream.sendall((bytes([0x82, len(packet)]) if transport == "wss" else b"") + packet)
                    while True:
                        if transport == "wss":
                            opcode, packet = _receive_frame(stream)
                            assert opcode == 2
                        else:
                            packet = _receive_exact(stream, 1)
                            remaining, multiplier = 0, 1
                            while True:
                                digit = _receive_exact(stream, 1)
                                packet += digit
                                remaining += (digit[0] & 127) * multiplier
                                if digit[0] < 128:
                                    break
                                multiplier *= 128
                            packet += _receive_exact(stream, remaining)
                        kind = packet[0] >> 4
                        if kind == 1:
                            send(b"\x20\x02\x00\x00")
                            continue
                        if kind == 14:
                            return
                        assert kind == 3
                        assert len(packet) <= 1_048_576
                        offset = 1
                        while packet[offset] & 128:
                            offset += 1
                        offset += 1
                        size = int.from_bytes(packet[offset:offset + 2], "big")
                        topic = packet[offset + 2:offset + 2 + size].decode("utf-8")
                        offset += 2 + size
                        qos = (packet[0] >> 1) & 3
                        if qos:
                            mid = packet[offset:offset + 2]
                            offset += 2
                        payload = packet[offset:].decode("utf-8")
                        messages.append({"topic": topic, "payload": payload, "packetBytes": len(packet), "qos": qos})
                        if qos:
                            send(b"\x40\x02" + mid)
        except Exception as error:
            errors.append(error)

    worker = threading.Thread(target=serve, daemon=True)
    worker.start()
    try:
        yield listener.getsockname()[1], client_context, messages
    finally:
        listener.close()
        worker.join(5)
        assert not worker.is_alive()
        assert not errors, repr(errors)


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["tcp", "wss"])
@pytest.mark.parametrize("entity_count", [100, 2000])
async def test_snapshot_wire_budget_and_before_after_evidence(tmp_path, monkeypatch, transport, entity_count):
    monkeypatch.setattr(er, "async_get", lambda _hass: FakeEntityRegistry())
    hass = FakeHass()
    for index in range(entity_count):
        entity_id = f"sensor.sample_{index:04d}"
        hass.states.set(entity_id, state="23.4", attributes={"friendly_name": "温湿度" * 80, "nested": {"samples": [1, 2, 3]}})
        state = hass.states.get(entity_id)
        state.last_changed = state.last_updated = datetime(2026, 10, 5, tzinfo=timezone.utc)
    full_response_bytes = len(json.dumps([state.as_dict() for state in hass.states.async_all()], ensure_ascii=False).encode())
    if entity_count == 2000:
        assert full_response_bytes > 1_048_576
    reports = {}
    states_by_mode = {}
    with wire_broker(tmp_path, transport) as (port, tls_context, messages):
        client = aiomqtt.Client("127.0.0.1", port=port, transport="websockets" if transport == "wss" else "tcp",
                                tls_context=tls_context if transport == "wss" else None, timeout=5)
        async with websocket_connection(client):
            for mode, path in [("legacy", "/api/states"), ("stream", "/api/seenzus/states/snapshot")]:
                coordinator = BridgeCoordinator(hass, FakeConfigEntry())
                coordinator._topics = build_topics("seenzus/v2", "ha-demo")
                coordinator._command_prefix = "seenzus/v2/bridge/ha-demo/command"
                offset = len(messages)
                tracemalloc.start()
                started = time.perf_counter()
                await coordinator._handle_message(f"seenzus/v2/bridge/ha-demo/command/{mode}",
                    json.dumps({"msgId": mode, "method": "GET", "path": path}), client)
                # QoS 1 fence proves the local broker has consumed preceding QoS 0 writes.
                await coordinator._publish_result(client, "fence", success=True, status=200)
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                trace = messages[offset:-1]
                parse_start = time.perf_counter()
                parsed = [json.loads(item["payload"]) for item in trace]
                parse_ms = (time.perf_counter() - parse_start) * 1000
                states = [value for item, value in zip(trace, parsed) if "/state/" in item["topic"]]
                replies = [value for item, value in zip(trace, parsed) if "/result/" in item["topic"]]
                states_by_mode[mode] = [{key: value for key, value in state.items() if key not in {
                    "eventId", "correlationMsgId", "snapshotVersion",
                }} for state in states]
                assert len(states) == entity_count
                if mode == "stream":
                    assert [reply["status"] for reply in replies] == [202, 200]
                    assert replies[-1]["data"]["sentCount"] == entity_count
                    assert all(isinstance(reply["data"], dict) for reply in replies)
                else:
                    assert replies[0]["status"] == (413 if entity_count == 2000 else 200)
                reports[mode] = {
                    "totalPublishBytes": sum(item["packetBytes"] for item in trace),
                    "maxPublishBytes": max(item["packetBytes"] for item in trace),
                    "packets": len(trace), "parseMs": round(parse_ms, 2),
                    "publisherAndCapturePeakBytes": peak, "recoveryMs": round(elapsed * 1000, 2),
                    "resultStatus": [reply["status"] for reply in replies],
                }
                if directory := os.environ.get("COMPACT_READ_REPORT_DIR"):
                    output = Path(directory)
                    output.mkdir(parents=True, exist_ok=True)
                    (output / f"{transport}-{entity_count}-{mode}.json").write_text(json.dumps(trace, ensure_ascii=False), encoding="utf-8")
    assert states_by_mode["legacy"] == states_by_mode["stream"]
    if entity_count == 100:
        assert reports["stream"]["totalPublishBytes"] < reports["legacy"]["totalPublishBytes"]
    if directory := os.environ.get("COMPACT_READ_REPORT_DIR"):
        (Path(directory) / f"{transport}-{entity_count}-metrics.json").write_text(json.dumps({
            "transport": transport, "entities": entity_count, "budgetBytes": 1_048_576,
            "fullResponseDataBytes": full_response_bytes, **reports,
        }, indent=2), encoding="utf-8")


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["tcp", "wss"])
async def test_service_index_wire_budget_and_description_savings(tmp_path, monkeypatch, transport):
    hass = FakeHass()
    definitions = {"light": {f"service_{i}": {"description": "完整描述" * 80, "fields": {"entity_id": {}}} for i in range(400)}}
    hass.services.async_services = lambda: definitions
    async def descriptions(_hass):
        return definitions
    monkeypatch.setattr(service_helper, "async_get_all_descriptions", descriptions)
    reports = {}
    with wire_broker(tmp_path, transport) as (port, tls_context, messages):
        client = aiomqtt.Client("127.0.0.1", port=port, transport="websockets" if transport == "wss" else "tcp",
                                tls_context=tls_context if transport == "wss" else None, timeout=5)
        async with websocket_connection(client):
            coordinator = BridgeCoordinator(hass, FakeConfigEntry())
            coordinator._topics = build_topics("seenzus/v2", "ha-demo")
            coordinator._command_prefix = "seenzus/v2/bridge/ha-demo/command"
            for mode, path in [("full", "/api/services"), ("index", "/api/seenzus/services/index")]:
                tracemalloc.start()
                start = time.perf_counter()
                await coordinator._handle_message(f"seenzus/v2/bridge/ha-demo/command/{mode}",
                    json.dumps({"msgId": mode, "method": "GET", "path": path}), client)
                elapsed = time.perf_counter() - start
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                packet = messages[-1]
                parse_start = time.perf_counter()
                payload = json.loads(packet["payload"])
                parse_ms = (time.perf_counter() - parse_start) * 1000
                assert payload["success"] is True
                if mode == "index":
                    assert payload["data"]["services"] == sorted(f"light.{name}" for name in definitions["light"])
                else:
                    assert payload["data"] == definitions
                reports[mode] = {"totalPublishBytes": packet["packetBytes"], "parseMs": round(parse_ms, 2),
                                 "publisherAndCapturePeakBytes": peak, "readMs": round(elapsed * 1000, 2)}
    assert reports["index"]["totalPublishBytes"] < reports["full"]["totalPublishBytes"] / 20
    if directory := os.environ.get("COMPACT_READ_REPORT_DIR"):
        (Path(directory) / f"{transport}-services-metrics.json").write_text(json.dumps(reports, indent=2), encoding="utf-8")
