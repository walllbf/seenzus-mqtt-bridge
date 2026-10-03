"""Independent HA entries must not evict one another from the MQTT broker."""
from __future__ import annotations

import asyncio

import pytest

from seenzus_bridge.bridge_protocol import build_mqtt_client_id
from tests.helpers import FakeAiomqttClient, FakeMqttError
from tests.test_mqtt_loop_behavior import (
    HAPPY_ENTRY_DATA,
    _install_recording_sleep,
    _make_coordinator,
    _shutdown_loop,
)


pytestmark = pytest.mark.timeout(10)


class _Broker:
    """Model MQTT's takeover rule while exercising real coordinator connects."""

    MqttError = FakeMqttError

    def __init__(self):
        self.active = {}
        self.clients = []
        self.takeovers = []

    def Client(self, **kwargs):
        client = _BrokerClient(self, **kwargs)
        self.clients.append(client)
        return client


class _BrokerClient(FakeAiomqttClient):
    def __init__(self, broker, **kwargs):
        self.broker = broker
        self.disconnected = asyncio.Event()
        super().__init__(end=self.wait_for_disconnect, **kwargs)

    async def wait_for_disconnect(self):
        await self.disconnected.wait()
        return FakeMqttError("session taken over by another client")

    async def __aenter__(self):
        await super().__aenter__()
        identifier = self.connect_kwargs["identifier"]
        previous = self.broker.active.get(identifier)
        if previous is not None:
            self.broker.takeovers.append(identifier)
            previous.connected = False
            previous.disconnected.set()
        self.broker.active[identifier] = self
        return self

    async def __aexit__(self, *args):
        identifier = self.connect_kwargs["identifier"]
        if self.broker.active.get(identifier) is self:
            self.broker.active.pop(identifier)
        return await super().__aexit__(*args)


async def _wait_until(predicate, real_sleep):
    async with asyncio.timeout(2):
        while not predicate():
            await real_sleep(0)


@pytest.mark.asyncio
@pytest.mark.parametrize("entry_ids", [
    # Real HA ULID format: same millisecond, different random portions.
    ("01M41E66A00000000000000000", "01M41E66A00000000000000001"),
    # Entries created before HA adopted ULIDs can still use UUIDs.
    ("a031371d-0000-4000-8000-000000000000", "a031371d-0000-4000-8000-000000000001"),
])
async def test_independent_entries_share_a_broker_without_session_takeover(monkeypatch, entry_ids):
    assert entry_ids[0][:8] == entry_ids[1][:8]
    broker = _Broker()
    coordinators = []
    tasks = []
    _, real_sleep = _install_recording_sleep(monkeypatch, cancel_on=5)
    try:
        for index, entry_id in enumerate(entry_ids):
            coordinator, _ = _make_coordinator(monkeypatch, data={
                **HAPPY_ENTRY_DATA,
                "bridge_id": f"ha-identity-{index}",
                "mqtt_username": f"paired-bridge-{index}",
                "mqtt_password": "test-credential",
            })
            coordinator._entry.entry_id = entry_id
            coordinator._aiomqtt = broker
            coordinator._on_ha_started(None)
            coordinators.append(coordinator)
            tasks.append(asyncio.create_task(coordinator._mqtt_loop()))
            await _wait_until(lambda: coordinator.mqtt_connected, real_sleep)

        assert not broker.takeovers, "independent HA entries evicted one another"
        assert len(broker.active) == 2
        assert all(client.connected for client in broker.clients)
        for index, client in enumerate(broker.clients):
            assert client.connect_kwargs["username"] == f"paired-bridge-{index}"
            assert client.connect_kwargs["password"] == "test-credential"
            assert client.subscriptions == [{
                "topic": f"seenzus/v2/bridge/ha-identity-{index}/command/+", "qos": 1,
            }]
    finally:
        for coordinator, task in zip(coordinators, tasks):
            await _shutdown_loop(coordinator, task)


@pytest.mark.asyncio
async def test_reconnect_and_integration_restart_keep_the_same_client_identity(monkeypatch):
    _, real_sleep = _install_recording_sleep(monkeypatch)
    identifiers = []
    for _restart in range(2):
        coordinator, mqtt = _make_coordinator(
            monkeypatch, data=dict(HAPPY_ENTRY_DATA),
            cycles=[{"end": FakeMqttError("test connection loss")}, {"end": "block"}],
        )
        coordinator._entry.entry_id = "01M41E66A00000000000000000"
        coordinator._on_ha_started(None)
        task = asyncio.create_task(coordinator._mqtt_loop())
        try:
            await _wait_until(lambda: len(mqtt.clients) == 2 and coordinator.mqtt_connected, real_sleep)
            identifiers.extend(client.connect_kwargs["identifier"] for client in mqtt.clients)
        finally:
            await _shutdown_loop(coordinator, task)
    assert len(identifiers) == 4
    assert len(set(identifiers)) == 1


@pytest.mark.parametrize("entry_id", [
    "legacy-entry", "a031371d-0000-4000-8000-000000000000", "旧标识/\x00", "x" * 4096,
], ids=["legacy", "uuid", "non-ascii", "long"])
def test_legacy_entry_identity_is_bounded_ascii_and_uses_the_complete_value(entry_id):
    identifier = build_mqtt_client_id(entry_id)
    assert identifier == build_mqtt_client_id(entry_id)
    assert len(identifier.encode("ascii")) == 47
    assert all(character.isalnum() or character == "-" for character in identifier)
    assert identifier != build_mqtt_client_id(entry_id + "1")


@pytest.mark.asyncio
async def test_client_identity_upgrade_uses_a_clean_mqtt_session():
    """The old truncated ID leaves no persistent subscriptions to migrate."""
    import aiomqtt

    entry_id = "01M41E66A00000000000000000"
    old = aiomqtt.Client("broker.example.com", identifier=f"seenzus-bridge-{entry_id[:8]}")
    upgraded = aiomqtt.Client("broker.example.com", identifier=build_mqtt_client_id(entry_id))
    assert old.identifier != upgraded.identifier
    assert old._client._clean_session is True
    assert upgraded._client._clean_session is True
