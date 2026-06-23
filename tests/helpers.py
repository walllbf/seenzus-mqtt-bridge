from __future__ import annotations

import asyncio
from types import SimpleNamespace

from homeassistant.core import CoreState


class FakeState:
    def __init__(self, entity_id: str, *, state: str = "on", attributes: dict | None = None) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes or {}

    def as_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "state": self.state,
            "attributes": dict(self.attributes),
        }


class AsyncFakeMQTTClient:
    def __init__(self) -> None:
        self.published: list[dict] = []

    async def publish(self, topic: str, payload: str, *, qos: int, retain: bool = False) -> None:
        self.published.append(
            {"topic": topic, "payload": payload, "qos": qos, "retain": retain}
        )


def make_state_changed_event(entity_id: str, *, state: str = "on", attributes: dict | None = None):
    new_state = SimpleNamespace(
        entity_id=entity_id,
        state=state,
        attributes=attributes or {},
    )
    return SimpleNamespace(data={"new_state": new_state})


class FakeBus:
    def __init__(self) -> None:
        self.listen_calls: list[dict] = []
        self.fire_calls: list[dict] = []

    def async_listen(self, _event_type, _callback):
        self.listen_calls.append({"event_type": _event_type, "callback": _callback})
        return lambda: None

    def async_fire(self, event_type: str, event_data: dict | None = None) -> None:
        self.fire_calls.append({"event_type": event_type, "event_data": event_data or {}})


class FakeStates:
    def __init__(self) -> None:
        self._states: dict[str, FakeState] = {}

    def set(self, entity_id: str, *, state: str = "on", attributes: dict | None = None) -> None:
        self._states[entity_id] = FakeState(entity_id, state=state, attributes=attributes)

    def get(self, entity_id: str):
        return self._states.get(entity_id)

    def async_all(self):
        return list(self._states.values())


class FakeConfig:
    def __init__(self, data: dict | None = None) -> None:
        self._data = data or {"location_name": "Home"}

    def as_dict(self) -> dict:
        return dict(self._data)


class FakeServices:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def async_call(self, domain: str, service: str, service_data: dict, *, blocking: bool) -> None:
        self.calls.append(
            {
                "domain": domain,
                "service": service,
                "service_data": dict(service_data),
                "blocking": blocking,
            }
        )


class FakeConfigEntries:
    def __init__(self) -> None:
        self.reload_calls: list[str] = []
        self.flow = SimpleNamespace(configure_calls=[])
        self.options = SimpleNamespace(configure_calls=[])

        async def _configure_flow(*, flow_id: str, user_input: dict):
            self.flow.configure_calls.append(
                {"flow_id": flow_id, "user_input": dict(user_input) if user_input is not None else None}
            )

        async def _configure_options(*, flow_id: str, user_input: dict):
            self.options.configure_calls.append(
                {"flow_id": flow_id, "user_input": dict(user_input) if user_input is not None else None}
            )

        self.flow.async_configure = _configure_flow
        self.options.async_configure = _configure_options

    async def async_reload(self, _entry_id: str) -> None:
        self.reload_calls.append(_entry_id)
        return None


class FakeEntityRegistryIndex:
    def __init__(self) -> None:
        self._entries: list[SimpleNamespace] = []

    def get_entries_for_config_entry_id(self, config_entry_id: str):
        return [entry for entry in self._entries if entry.config_entry_id == config_entry_id]


class FakeEntityRegistry:
    def __init__(self) -> None:
        self.entities = FakeEntityRegistryIndex()

    def add(
        self,
        entity_id: str,
        config_entry_id: str = "",
        *,
        device_id: str | None = None,
        area_id: str | None = None,
        name: str | None = None,
        original_name: str | None = None,
    ) -> None:
        self.entities._entries.append(
            SimpleNamespace(
                entity_id=entity_id,
                config_entry_id=config_entry_id,
                device_id=device_id,
                area_id=area_id,
                name=name,
                original_name=original_name,
            )
        )

    def async_get(self, entity_id: str):
        for entry in self.entities._entries:
            if entry.entity_id == entity_id:
                return entry
        return None


class FakeDeviceRegistry:
    def __init__(self) -> None:
        self._entries: dict[str, SimpleNamespace] = {}

    def add(
        self,
        device_id: str,
        *,
        name: str | None = None,
        name_by_user: str | None = None,
        manufacturer: str | None = None,
        model: str | None = None,
        area_id: str | None = None,
        via_device_id: str | None = None,
    ) -> None:
        self._entries[device_id] = SimpleNamespace(
            id=device_id,
            name=name,
            name_by_user=name_by_user,
            manufacturer=manufacturer,
            model=model,
            area_id=area_id,
            via_device_id=via_device_id,
        )

    def async_get(self, device_id: str):
        return self._entries.get(device_id)


class FakeHass:
    def __init__(self) -> None:
        self.bus = FakeBus()
        self.states = FakeStates()
        self.services = FakeServices()
        self.config = FakeConfig()
        self.config_entries = FakeConfigEntries()
        self.http = SimpleNamespace(registered_views=[])
        self.data: dict = {}
        self.scheduled_tasks: list[asyncio.Task] = []
        self.state = CoreState.running

        def _register_view(view):
            self.http.registered_views.append(view)

        self.http.register_view = _register_view

    def async_add_job(self, callback, *args):
        return callback(*args)

    def async_create_task(self, coro):
        task = asyncio.create_task(coro)
        self.scheduled_tasks.append(task)
        return task


class FakeConfigEntry:
    def __init__(self, *, entry_id: str = "01kpcrmg59ph", data: dict | None = None, options: dict | None = None) -> None:
        self.entry_id = entry_id
        self.data = data or {}
        self.options = options or {}

    def add_update_listener(self, listener):
        return listener

    def async_on_unload(self, listener):
        return listener


class FakeMqttError(Exception):
    """Stands in for aiomqtt.MqttError in MQTT-loop tests."""


class FakeMqttMessage:
    """Minimal aiomqtt message: str()-able topic + bytes payload."""

    def __init__(self, topic: str, payload: bytes | str) -> None:
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else str(payload).encode()


class _FakeMessageStream:
    """Scriptable `client.messages` iterator with a per-cycle end behavior.

    After the scripted messages are exhausted, `end` decides how the cycle ends:
    - "block": park forever (test must cancel the loop task; pytest-timeout backstop)
    - "stop": end the async-for normally
    - an exception instance or class: raise it out of the iterator
    """

    def __init__(self, messages, end) -> None:
        self._messages = list(messages)
        self._end = end

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return self._messages.pop(0)
        end = self._end
        if end == "block":
            await asyncio.Event().wait()
            raise StopAsyncIteration
        if end == "stop":
            raise StopAsyncIteration
        if isinstance(end, BaseException):
            raise end
        if isinstance(end, type) and issubclass(end, BaseException):
            raise end()
        raise StopAsyncIteration


class FakeAiomqttClient(AsyncFakeMQTTClient):
    """Async-context-manager aiomqtt.Client stand-in.

    Records connect kwargs, subscriptions and publishes; serves a scriptable
    message stream and optionally fails the connect (`connect_error`).
    """

    def __init__(self, *, messages=(), end="block", connect_error: BaseException | None = None, **connect_kwargs) -> None:
        super().__init__()
        self.connect_kwargs = dict(connect_kwargs)
        self.subscriptions: list[dict] = []
        self.connected = False
        self._connect_error = connect_error
        self.messages = _FakeMessageStream(messages, end)

    async def __aenter__(self):
        if self._connect_error is not None:
            raise self._connect_error
        self.connected = True
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self.connected = False
        return False

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscriptions.append({"topic": topic, "qos": qos})


class FakeAiomqttModule:
    """aiomqtt-module stand-in injected through the coordinator._aiomqtt seam.

    Each `Client(...)` call consumes the next cycle spec — a dict with optional
    keys "messages", "end", "connect_error" (see _FakeMessageStream) — so tests
    can script connect/serve/fail behavior per reconnect cycle.
    """

    MqttError = FakeMqttError

    def __init__(self, cycles: list[dict] | None = None) -> None:
        self._cycles = list(cycles or [])
        self.clients: list[FakeAiomqttClient] = []

    def add_cycle(self, *, messages=(), end="block", connect_error: BaseException | None = None) -> None:
        self._cycles.append({"messages": messages, "end": end, "connect_error": connect_error})

    def Client(self, **connect_kwargs) -> FakeAiomqttClient:
        spec = self._cycles.pop(0) if self._cycles else {}
        client = FakeAiomqttClient(**spec, **connect_kwargs)
        self.clients.append(client)
        return client
