from types import SimpleNamespace
from unittest.mock import AsyncMock
import asyncio

import pytest

from seenzus_bridge import async_migrate_entry
from seenzus_bridge.config_flow import SavanAIBridgeConfigFlow, SavanAIBridgeOptionsFlow
from seenzus_bridge.environments import ENVIRONMENTS, entry_environment
from tests.helpers import FakeHass, FakeConfigEntry
from tests.test_device_access import home


def flow_for(hass, world="world-a"):
    flow = SavanAIBridgeConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    flow._quick_pair_api_base = ENVIRONMENTS["cn"][1]
    flow._quick_pair_session_id = "wps_test"
    flow._quick_pair_page_url = "https://app.seenzus.cn/pair"
    flow._quick_pair_exchange_result = SimpleNamespace(ok=True, session_id="wps_test", world_id=world,
        world_name=world, source_id=f"source-{world}", confirmed_at=None, mqtt={"host": "mqtt.test", "scheme": "mqtts", "password": "rotated"})
    flow.async_create_entry = lambda **kwargs: {"type": "created", **kwargs}
    flow.async_abort = lambda **kwargs: {"type": "abort", **kwargs}
    return flow


@pytest.mark.asyncio
@pytest.mark.parametrize("world", ["world-a", "world-b"])
async def test_same_environment_cannot_add_a_second_entry_even_for_another_world(world):
    hass = FakeHass()
    original = FakeConfigEntry(data={"environment": "cn", "pairing_api_base": ENVIRONMENTS["cn"][1],
        "world_id": "world-a", "source_id": "source-world-a", "mqtt_password": "keep-secret", "device_scope": "selected", "allowed_device_ids": ["city"]})
    hass.config_entries.entries.append(original)
    second = flow_for(hass, world)
    second._scope = {"device_scope": "all"}
    assert (await second.async_step_seamless_finish())["reason"] == "environment_already_configured"
    assert original.data["mqtt_password"] == "keep-secret"
    assert original.data["device_scope"] == "selected"
    assert original.data["allowed_device_ids"] == ["city"]
    assert len(hass.config_entries.entries) == 1


@pytest.mark.asyncio
async def test_four_environment_slots_and_all_custom_urls_share_one_dev_slot():
    hass = FakeHass()
    for environment, api in [(key, value[1]) for key, value in ENVIRONMENTS.items()] + [("custom", "http://dev-one.local/api")]:
        flow = flow_for(hass)
        flow._quick_pair_api_base = api
        result = await flow.async_step_seamless_finish()
        assert result["type"] == "created" and flow.context["unique_id"] == environment
        hass.config_entries.entries.append(FakeConfigEntry(entry_id=environment, data=result["data"]))
    assert (await flow_for(hass).async_step_user())["reason"] == "all_environments_configured"
    another_dev = flow_for(hass, "another-world")
    another_dev._quick_pair_api_base = "http://dev-two.local/different-api"
    assert (await another_dev.async_step_seamless_finish())["reason"] == "environment_already_configured"
    assert len(hass.config_entries.entries) == 4


@pytest.mark.asyncio
async def test_duplicate_is_blocked_before_session_and_callback_exchange(monkeypatch):
    hass = FakeHass()
    hass.config_entries.entries.append(FakeConfigEntry(data={"environment": "cn", "world_id": "world-a"}))
    create, exchange = AsyncMock(), AsyncMock()
    monkeypatch.setattr("seenzus_bridge.config_flow.create_web_pairing_session", create)
    monkeypatch.setattr("seenzus_bridge.config_flow.exchange_web_pairing_callback_code", exchange)
    explicit = flow_for(hass)
    explicit._selected_environment = "cn"
    assert (await explicit.async_step_seamless({}))["reason"] == "environment_already_configured"
    automatic = flow_for(hass)
    automatic._quick_pair_api_base = None
    automatic._quick_pair_callback_state = "nonce"
    automatic._quick_pair_callback_state_token = "signed-state"
    automatic.async_external_step_done = lambda **kwargs: {"type": "external_done", **kwargs}
    result = await automatic.async_step_seamless_authorize({"state": {"pairing_state": "nonce"},
        "environment": "cn", "session_id": "wps_test", "code": "unused"})
    assert result["type"] == "external_done"
    assert (await automatic.async_step_seamless_finish())["reason"] == "environment_already_configured"
    create.assert_not_awaited()
    exchange.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_callback_finishes_external_step_before_abort():
    flow = flow_for(FakeHass())
    flow._quick_pair_api_base = None
    flow._quick_pair_callback_state = "nonce"
    flow._quick_pair_callback_state_token = "signed-state"
    flow.async_external_step_done = lambda **kwargs: {"type": "external_done", **kwargs}
    result = await flow.async_step_seamless_authorize({
        "state": {"pairing_state": "nonce"}, "error": "environment_already_configured",
    })
    assert result == {"type": "external_done", "next_step_id": "seamless_finish"}
    assert (await flow.async_step_seamless_finish())["reason"] == "environment_already_configured"


@pytest.mark.asyncio
@pytest.mark.parametrize("callback", ["error", "code", "environment_required"])
async def test_duplicate_callback_uses_real_ha_external_step_transition(callback, monkeypatch):
    from homeassistant.data_entry_flow import FlowManager, FlowResultType
    from seenzus_bridge.quick_pair import SeenzusQuickPairCallbackView, QUICK_PAIR_ACTIVE_STATES, _encode_jwt

    class Manager(FlowManager):
        async def async_create_flow(self, handler_key, *, context=None, data=None):
            raise NotImplementedError

        async def async_finish_flow(self, flow, result):
            return result

    hass = FakeHass()
    hass.bus.async_fire_internal = lambda *_: None
    hass.config_entries.entries.append(FakeConfigEntry(data={"environment": "uat", "world_id": "world-a"}))
    manager = Manager(hass)
    hass.config_entries.flow = manager
    flow = SavanAIBridgeConfigFlow()
    flow.hass = hass
    flow.handler = "seenzus_bridge"
    flow.flow_id = "real-flow"
    flow.context = {"source": "user"}
    flow._quick_pair_api_base = None
    flow._quick_pair_callback_state = "nonce"
    flow._quick_pair_page_url = "http://ha.local/origin"
    flow._quick_pair_callback_state_token = _encode_jwt(hass, {
        "flow_id": flow.flow_id, "flow_manager": "config", "pairing_state": "nonce",
    })
    hass.data.setdefault("seenzus_bridge", {})[QUICK_PAIR_ACTIVE_STATES] = {("config", flow.flow_id): "nonce"}
    flow.cur_step = flow.async_external_step(step_id="seamless_authorize", url=flow._quick_pair_page_url)
    manager._async_add_flow_progress(flow)
    exchange = AsyncMock()
    monkeypatch.setattr("seenzus_bridge.config_flow.exchange_web_pairing_callback_code", exchange)
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", AsyncMock(return_value=None))
    query = {"state": flow._quick_pair_callback_state_token}
    if callback == "environment_required":
        query["error"] = "environment_required"
    elif callback == "error":
        query["error"] = "environment_already_configured"
    else:
        query.update({"environment": "uat", "session_id": "wps_test", "code": "unused"})
    original = flow.cur_step
    flow.cur_step = {"type": FlowResultType.FORM, "step_id": "environment"}
    rejected = await SeenzusQuickPairCallbackView().get(SimpleNamespace(app={"hass": hass}, query=query))
    assert rejected.status == 410
    assert not hass.data.get("seenzus_bridge", {}).get("quick_pair_callback_payloads")
    flow.cur_step = original
    stale = _encode_jwt(hass, {"flow_id": flow.flow_id, "flow_manager": "config", "pairing_state": "stale-nonce"})
    stale_result = await SeenzusQuickPairCallbackView().get(SimpleNamespace(app={"hass": hass}, query={**query, "state": stale}))
    assert stale_result.status == 410
    assert not hass.data.get("seenzus_bridge", {}).get("quick_pair_callback_payloads")
    response = await SeenzusQuickPairCallbackView().get(SimpleNamespace(app={"hass": hass}, query=query))
    assert response.status == 200
    assert flow.cur_step["type"] == FlowResultType.EXTERNAL_STEP_DONE
    result = await manager.async_configure(flow.flow_id)
    if callback == "environment_required":
        assert result["type"] == FlowResultType.FORM
        assert result["step_id"] == "environment"
    else:
        assert result["type"] == FlowResultType.ABORT
        assert result["reason"] == "environment_already_configured"
    exchange.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
@pytest.mark.parametrize("flow_kind", ["config", "options"])
async def test_duplicate_callback_and_frontend_poll_during_single_use_exchange(monkeypatch, cancel, flow_kind):
    from homeassistant.data_entry_flow import FlowManager, FlowResultType
    from seenzus_bridge.quick_pair import SeenzusQuickPairCallbackView, QUICK_PAIR_ACTIVE_STATES, _encode_jwt

    class Manager(FlowManager):
        async def async_create_flow(self, handler_key, *, context=None, data=None):
            raise NotImplementedError
        async def async_finish_flow(self, flow, result):
            return result

    hass = FakeHass()
    hass.bus.async_fire_internal = lambda *_: None
    manager = Manager(hass)
    setattr(hass.config_entries, "flow" if flow_kind == "config" else "options", manager)
    flow = (SavanAIBridgeConfigFlow() if flow_kind == "config" else
            SavanAIBridgeOptionsFlow(FakeConfigEntry(data={"environment": "uat", "source_id": "source-1"})))
    flow.hass, flow.handler, flow.flow_id = hass, "seenzus_bridge", "concurrent-flow"
    flow.context = {"source": "user"}
    flow._quick_pair_callback_state = "nonce"
    flow._quick_pair_page_url = "http://ha.local/origin"
    flow._quick_pair_external_opened = True
    token = _encode_jwt(hass, {"flow_id": flow.flow_id, "flow_manager": flow_kind, "pairing_state": "nonce"})
    flow._quick_pair_callback_state_token = token
    hass.data.setdefault("seenzus_bridge", {})[QUICK_PAIR_ACTIVE_STATES] = {(flow_kind, flow.flow_id): "nonce"}
    flow.cur_step = flow.async_external_step(step_id="seamless_authorize", url=flow._quick_pair_page_url)
    manager._async_add_flow_progress(flow)
    started, release, exchange_cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def exchange(**_kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            exchange_cancelled.set()
            raise
        return SimpleNamespace(ok=True, mqtt={"host": "mqtt.test"})

    mock = AsyncMock(side_effect=exchange)
    monkeypatch.setattr("seenzus_bridge.config_flow.exchange_web_pairing_callback_code", mock)
    request = SimpleNamespace(app={"hass": hass}, query={"state": token, "environment": "uat", "session_id": "wps_test", "code": "once"})
    view = SeenzusQuickPairCallbackView()
    first = asyncio.create_task(view.get(request))
    await asyncio.wait_for(started.wait(), timeout=2)
    try:
        duplicate = await view.get(request)
        assert duplicate.status == 202
        assert "still being processed" in duplicate.text
        stale_token = _encode_jwt(hass, {"flow_id": flow.flow_id, "flow_manager": flow_kind, "pairing_state": "old-nonce"})
        stale = await view.get(SimpleNamespace(app={"hass": hass}, query={**request.query, "state": stale_token}))
        assert stale.status == 410
        frontend = await manager.async_configure(flow.flow_id)
        assert frontend["type"] == FlowResultType.EXTERNAL_STEP
        assert flow._quick_pair_finish_error is None
        if cancel:
            manager.async_abort(flow.flow_id)
            # The browser is answered at once; the exchange itself finishes in
            # the background so that a candidate it created can be released.
            assert (await asyncio.wait_for(first, timeout=2)).status == 410
            assert not exchange_cancelled.is_set()
    finally:
        release.set()
    assert (await first).status == (410 if cancel else 200)
    if not cancel:
        assert flow.cur_step["type"] == FlowResultType.EXTERNAL_STEP_DONE
    else:
        assert not hass.config_entries.entries
    mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_flow_abort_releases_the_candidate_of_an_inflight_exchange(monkeypatch):
    """Aborting answers the browser at once; the exchange still completes so
    that the candidate it created is cancelled instead of left to expire."""
    from aiohttp import web
    from homeassistant.data_entry_flow import FlowManager
    from seenzus_bridge.pairing_bootstrap import exchange_web_pairing_callback_code as real_exchange
    from seenzus_bridge.pairing_bootstrap import finalize_web_pairing as real_finalize
    from seenzus_bridge.quick_pair import SeenzusQuickPairCallbackView, QUICK_PAIR_ACTIVE_STATES, _encode_jwt

    started, stop, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancels = []

    async def handle(request):
        assert (await request.json())["commitMode"] == "deferred"
        started.set()
        await stop.wait()
        return web.json_response({"ok": True, "sessionId": "wps_test", "commit": {"token": "t" * 43},
                                  "mqtt": {"host": "broker.example", "scheme": "mqtts"}})

    async def cancel(request):
        cancels.append(await request.json())
        cancelled.set()
        return web.json_response({"ok": True, "state": "cancelled"})

    app = web.Application()
    app.router.add_post("/integrations/ha/web-pairing/callback/exchange", handle)
    app.router.add_post("/integrations/ha/web-pairing/callback/cancel", cancel)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    class Manager(FlowManager):
        async def async_create_flow(self, handler_key, *, context=None, data=None):
            raise NotImplementedError
        async def async_finish_flow(self, flow, result):
            return result

    hass = FakeHass()
    hass.bus.async_fire_internal = lambda *_: None
    manager = Manager(hass)
    hass.config_entries.flow = manager
    flow = SavanAIBridgeConfigFlow()
    flow.hass, flow.handler, flow.flow_id = hass, "seenzus_bridge", "network-cancel"
    flow.context = {"source": "user"}
    flow._quick_pair_callback_state = "nonce"
    flow._quick_pair_page_url = "http://ha.local/origin"
    flow._quick_pair_external_opened = True
    token = _encode_jwt(hass, {"flow_id": flow.flow_id, "flow_manager": "config", "pairing_state": "nonce"})
    flow._quick_pair_callback_state_token = token
    hass.data.setdefault("seenzus_bridge", {})[QUICK_PAIR_ACTIVE_STATES] = {("config", flow.flow_id): "nonce"}
    flow.cur_step = flow.async_external_step(step_id="seamless_authorize", url=flow._quick_pair_page_url)
    manager._async_add_flow_progress(flow)

    async def exchange(**kwargs):
        kwargs["api_base"] = f"http://127.0.0.1:{port}"
        return await real_exchange(**kwargs)

    async def finalize(**kwargs):
        kwargs["api_base"] = f"http://127.0.0.1:{port}"
        return await real_finalize(**kwargs)

    monkeypatch.setattr("seenzus_bridge.config_flow.exchange_web_pairing_callback_code", exchange)
    monkeypatch.setattr("seenzus_bridge.config_flow.finalize_web_pairing", finalize)
    request = SimpleNamespace(app={"hass": hass}, query={"state": token, "environment": "uat", "session_id": "wps_test", "code": "once"})
    callback = asyncio.create_task(SeenzusQuickPairCallbackView().get(request))
    try:
        await asyncio.wait_for(started.wait(), timeout=3)
        manager.async_abort(flow.flow_id)
        assert (await asyncio.wait_for(callback, timeout=3)).status == 410
        assert not cancels
        stop.set()
        await asyncio.wait_for(cancelled.wait(), timeout=3)
        assert cancels == [{"sessionId": "wps_test", "commitToken": "t" * 43}]
    finally:
        stop.set()
        if not callback.done():
            callback.cancel()
        await asyncio.gather(callback, return_exceptions=True)
        await runner.cleanup()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_owner", [False, True])
async def test_concurrent_frontend_finish_does_not_commit_entry_twice(cancel_owner):
    from homeassistant.data_entry_flow import FlowManager, FlowResultType, UnknownFlow

    started, release = asyncio.Event(), asyncio.Event()
    class Manager(FlowManager):
        commits = 0
        async def async_create_flow(self, handler_key, *, context=None, data=None):
            raise NotImplementedError
        async def async_finish_flow(self, flow, result):
            self.commits += 1
            started.set()
            await release.wait()
            return result

    hass = FakeHass()
    manager = Manager(hass)
    hass.config_entries.flow = manager
    flow = SavanAIBridgeConfigFlow()
    flow.hass, flow.handler, flow.flow_id = hass, "seenzus_bridge", "finish-flow"
    flow.context = {"source": "user"}
    flow._quick_pair_api_base = ENVIRONMENTS["uat"][1]
    flow._quick_pair_page_url = "http://ha.local/origin"
    flow._quick_pair_session_id = "wps_test"
    flow._quick_pair_callback_state = "nonce"
    flow._quick_pair_exchange_result = SimpleNamespace(ok=True, session_id="wps_test", confirmed_at=None,
        world_id="world-a", world_name="world-a", source_id="source-a", mqtt={"host": "mqtt.test", "scheme": "mqtts"})
    flow.cur_step = flow.async_external_step_done(next_step_id="seamless_finish")
    manager._async_add_flow_progress(flow)
    first = asyncio.create_task(manager.async_configure(flow.flow_id))
    await asyncio.wait_for(started.wait(), timeout=2)
    if cancel_owner:
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0)
        assert not manager.async_progress()
        with pytest.raises(UnknownFlow):
            await asyncio.wait_for(manager.async_configure(flow.flow_id), timeout=2)
        assert manager.commits == 1
        assert not manager.async_progress()
        return
    second = asyncio.create_task(manager.async_configure(flow.flow_id))
    await asyncio.sleep(0)
    assert manager.commits == 1
    release.set()
    assert (await first)["type"] == FlowResultType.CREATE_ENTRY
    with pytest.raises(UnknownFlow):
        await second
    assert manager.commits == 1


@pytest.mark.asyncio
async def test_cancel_during_registered_but_unfinished_entry_does_not_report_success():
    from homeassistant.config_entries import ConfigEntryState
    from homeassistant.data_entry_flow import FlowManager, UnknownFlow
    from seenzus_bridge.quick_pair import SeenzusQuickPairCallbackView, QUICK_PAIR_ACTIVE_STATES, _encode_jwt

    started, release = asyncio.Event(), asyncio.Event()
    hass = FakeHass()
    class Manager(FlowManager):
        async def async_create_flow(self, handler_key, *, context=None, data=None):
            raise NotImplementedError
        async def async_finish_flow(self, flow, result):
            entry = FakeConfigEntry(data=result["data"])
            entry.state = ConfigEntryState.SETUP_IN_PROGRESS
            hass.config_entries.entries.append(entry)
            started.set()
            await release.wait()
            raise RuntimeError("entry setup failed")

    manager = Manager(hass)
    hass.config_entries.flow = manager
    flow = SavanAIBridgeConfigFlow()
    flow.hass, flow.handler, flow.flow_id = hass, "seenzus_bridge", "pending-finish"
    flow.context = {"source": "user"}
    flow._quick_pair_api_base = ENVIRONMENTS["uat"][1]
    flow._quick_pair_page_url = "http://ha.local/origin"
    flow._quick_pair_session_id = "wps_test"
    flow._quick_pair_callback_state = "nonce"
    token = _encode_jwt(hass, {"flow_id": flow.flow_id, "flow_manager": "config", "pairing_state": "nonce"})
    flow._quick_pair_callback_state_token = token
    flow._quick_pair_exchange_result = SimpleNamespace(ok=True, session_id="wps_test", confirmed_at=None,
        world_id="world-a", world_name="world-a", source_id="source-a", mqtt={"host": "mqtt.test", "scheme": "mqtts"})
    hass.data.setdefault("seenzus_bridge", {})[QUICK_PAIR_ACTIVE_STATES] = {("config", flow.flow_id): "nonce"}
    flow.cur_step = flow.async_external_step_done(next_step_id="seamless_finish")
    manager._async_add_flow_progress(flow)
    first = asyncio.create_task(manager.async_configure(flow.flow_id))
    await asyncio.wait_for(started.wait(), timeout=2)
    manager.async_abort(flow.flow_id)
    request = SimpleNamespace(app={"hass": hass}, query={"state": token, "environment": "uat", "session_id": "wps_test", "code": "once"})
    assert (await SeenzusQuickPairCallbackView().get(request)).status == 410
    release.set()
    with pytest.raises(RuntimeError, match="entry setup failed"):
        await first


@pytest.mark.asyncio
@pytest.mark.parametrize("options", [False, True])
async def test_pairing_notifies_when_saved_entry_later_recovers(options, monkeypatch):
    from homeassistant.config_entries import ConfigEntryState
    from seenzus_bridge.quick_pair import _mark_quick_pair_completed, _quick_pair_was_completed

    hass = FakeHass()
    data = {"pairing_session_id": "wps_test", "environment": "uat", "world_id": "world-a"}
    entry = FakeConfigEntry(data=data if not options else {"environment": "uat", "world_id": "world-a"},
                            options=data if options else {})
    entry.state = ConfigEntryState.SETUP_RETRY
    hass.config_entries.entries.append(entry)
    flow = SavanAIBridgeOptionsFlow(entry) if options else SavanAIBridgeConfigFlow()
    flow.hass = hass
    flow.flow_id = "retry-flow"
    flow._quick_pair_callback_state = "nonce"
    flow._quick_pair_app_return_url = "seenzus://pairing/done"
    flow._quick_pair_finish_started = True
    flow._quick_pair_finish_owner = asyncio.current_task()
    _mark_quick_pair_completed(hass, "nonce", data)
    listeners, notified = [], []
    def connect(_hass, _signal, callback):
        listeners.append(callback)
        return lambda: listeners.remove(callback)
    monkeypatch.setattr("seenzus_bridge.config_flow.async_dispatcher_connect", connect)
    monkeypatch.setattr("seenzus_bridge.config_flow._notify_app_return", lambda *args: notified.append(args))
    flow.async_remove()
    assert not _quick_pair_was_completed(hass, "nonce")
    assert len(listeners) == 1 and not notified
    entry.state = ConfigEntryState.LOADED
    listeners[0]("updated", entry)
    assert _quick_pair_was_completed(hass, "nonce")
    assert not listeners and len(notified) == 1


def test_legacy_endpoint_aliases_and_manual_entries_have_one_stable_environment_slot():
    assert entry_environment({"pairing_api_base": "https://APP.SEENZUS.CN:443/api/savant"}) == "cn"
    assert entry_environment({"pairing_api_base": "http://dev-one.local/api"}) == "custom"
    assert entry_environment({"pairing_api_base": "http://dev-two.local/api"}) == "custom"
    assert entry_environment({"pairing_mode": "manual"}) == "custom"


@pytest.mark.asyncio
async def test_ordinary_choices_are_production_but_uat_app_can_pair_without_developer_file(monkeypatch):
    flow = flow_for(FakeHass())
    for environment in ("cn", "global"):
        flow.hass.config_entries.entries.append(FakeConfigEntry(entry_id=environment, data={"environment": environment}))
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", AsyncMock(return_value=None))
    assert set(await flow._environment_choices()) == {"auto", "cn", "global"}
    assert (await flow.async_step_environment({"environment": "uat"}))["reason"] == "invalid_environment"
    flow._quick_pair_api_base = None
    flow._quick_pair_callback_state = "nonce"
    flow._quick_pair_callback_state_token = "signed-state"
    flow.async_external_step_done = lambda **kwargs: kwargs
    exchange = AsyncMock(return_value=flow._quick_pair_exchange_result)
    monkeypatch.setattr("seenzus_bridge.config_flow.exchange_web_pairing_callback_code", exchange)
    await flow.async_step_seamless_authorize({"state": {"pairing_state": "nonce"},
        "environment": "uat", "session_id": "wps_test", "code": "uat-code"})
    assert exchange.call_args.kwargs["api_base"] == ENVIRONMENTS["uat"][1]
    result = await flow.async_step_seamless_finish()
    assert result["type"] == "created" and result["data"]["environment"] == "uat"
    assert set(await flow._environment_choices()) == {"auto", "cn", "global"}


@pytest.mark.asyncio
async def test_manual_setup_requires_environment_and_cannot_bypass_slot_limit(monkeypatch):
    flow = flow_for(FakeHass())
    flow.async_show_form = lambda **kwargs: kwargs
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", AsyncMock(return_value=None))
    assert (await flow.async_step_user({"pairing_mode": "manual"}))["step_id"] == "environment"
    assert (await flow.async_step_environment({"environment": "cn"}))["step_id"] == "scope"
    assert (await flow.async_step_scope({"device_scope": "all"}))["step_id"] == "manual"
    data = {"mqtt_settings": {"mqtt_host": "broker.test"}}
    result = await flow.async_step_manual(data)
    assert result["data"]["environment"] == "cn" and flow.context["unique_id"] == "cn"
    flow.hass.config_entries.entries.append(FakeConfigEntry(data=result["data"]))
    second = flow_for(flow.hass)
    second._selected_environment = "cn"
    assert (await second.async_step_manual(data))["reason"] == "environment_already_configured"


@pytest.mark.asyncio
async def test_repair_keeps_environment_and_world_despite_developer_preset(monkeypatch):
    hass = FakeHass()
    entry = FakeConfigEntry(data={"pairing_api_base": ENVIRONMENTS["cn"][1], "world_id": "world-a", "source_id": "source-a",
        "device_scope": "selected", "allowed_device_ids": ["city"]})
    hass.config_entries.entries.append(entry)
    flow = SavanAIBridgeOptionsFlow(entry)
    flow.hass = hass
    monkeypatch.setattr("seenzus_bridge.config_flow._resolve_ha_instance_id", AsyncMock(return_value="ha-install"))
    monkeypatch.setattr("seenzus_bridge.config_flow._build_quick_pair_callback_context", lambda *a: ("http://ha.local/api/seenzus_bridge/quick_pair/callback", "nonce", "signed-state"))
    preset = AsyncMock(side_effect=AssertionError("existing entries must not read a new developer preset"))
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", preset)
    create = AsyncMock(return_value=SimpleNamespace(ok=True, pairing_page_url="https://app.seenzus.cn/pair", session_id="wps_test"))
    monkeypatch.setattr("seenzus_bridge.config_flow.create_web_pairing_session", create)
    flow.async_external_step = lambda **kwargs: kwargs
    await flow.async_step_seamless({})
    assert create.call_args.kwargs["api_base"] == ENVIRONMENTS["cn"][1]
    assert create.call_args.kwargs["expected_world_id"] == "world-a"
    preset.assert_not_awaited()
    flow.async_create_entry = lambda **kwargs: kwargs
    result = flow._entry_data_or_reshow(flow_for(hass)._quick_pair_exchange_result)
    assert result["data"]["device_scope"] == "selected" and result["data"]["allowed_device_ids"] == ["city"]
    assert result["data"]["mqtt_password"] == "rotated" and entry.unique_id == "cn"


@pytest.mark.asyncio
async def test_auto_callback_cannot_choose_arbitrary_server(monkeypatch):
    flow = flow_for(FakeHass())
    flow._quick_pair_api_base = None
    flow._quick_pair_session_id = None
    flow._quick_pair_callback_state = "nonce"
    flow._quick_pair_callback_state_token = "signed-state"
    flow.async_external_step_done = lambda **kwargs: kwargs
    exchange = AsyncMock()
    monkeypatch.setattr("seenzus_bridge.config_flow.exchange_web_pairing_callback_code", exchange)
    await flow.async_step_seamless_authorize({"state": {"pairing_state": "nonce"}, "code": "code",
        "environment": "https://attacker.example", "session_id": "wps_bad"})
    exchange.assert_not_awaited()
    assert flow._quick_pair_finish_error == "quick_pair_code_exchange_failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("preset", [None, "http://dev.local:3000/api/seenzus"])
async def test_custom_callback_requires_and_uses_admin_preset(monkeypatch, preset):
    flow = flow_for(FakeHass())
    flow._quick_pair_api_base = None
    flow._dev_api_base = preset
    flow._quick_pair_session_id = None
    flow._quick_pair_callback_state = "nonce"
    flow._quick_pair_callback_state_token = "signed-state"
    flow.async_external_step_done = lambda **kwargs: kwargs
    exchange = AsyncMock(return_value=flow._quick_pair_exchange_result)
    monkeypatch.setattr("seenzus_bridge.config_flow.exchange_web_pairing_callback_code", exchange)
    await flow.async_step_seamless_authorize({"state": {"pairing_state": "nonce"},
        "environment": "custom", "session_id": "wps_dev", "code": "once"})
    if preset:
        assert exchange.await_count == 1
        assert exchange.call_args.kwargs["api_base"] == preset
    else:
        exchange.assert_not_awaited()
        assert flow._quick_pair_finish_error == "quick_pair_code_exchange_failed"


@pytest.mark.asyncio
async def test_missing_context_requests_environment_instead_of_defaulting(monkeypatch):
    flow = flow_for(FakeHass())
    flow._quick_pair_api_base = None
    flow._quick_pair_finish_error = "environment_required"
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", AsyncMock(return_value=None))
    flow.async_show_form = lambda **kwargs: kwargs
    result = await flow.async_step_seamless_finish()
    assert result["step_id"] == "environment"
    assert flow._quick_pair_api_base is None


@pytest.mark.asyncio
async def test_migration_keeps_existing_endpoint_credentials_and_whole_ha_grant():
    hass = FakeHass()
    entry = FakeConfigEntry(data={"pairing_api_base": ENVIRONMENTS["uat"][1], "mqtt_password": "old-secret"})
    entry.version = 1
    assert await async_migrate_entry(hass, entry)
    assert entry.version == 2 and entry.data["environment"] == "uat"
    assert entry.unique_id == "uat"
    assert entry.data["mqtt_password"] == "old-secret" and entry.data["device_scope"] == "all"


@pytest.mark.asyncio
async def test_area_expansion_is_a_snapshot_and_scope_options_preserve_binding(home):
    hass, entities, devices, entry, _ = home
    entry.data.update({"mqtt_password": "keep-secret", "world_id": "world-a", "environment": "cn"})
    flow = SavanAIBridgeOptionsFlow(entry)
    flow.hass = hass
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_create_entry = lambda **kwargs: kwargs
    assert (await flow.async_step_init({"action": "devices"}))["step_id"] == "scope"
    await flow.async_step_scope({"device_scope": "selected"})
    assert (await flow.async_step_devices({"areas": ["downtown"]}))["step_id"] == "entities"
    result = await flow.async_step_entities({"entities": ["input_boolean.helper"]})
    assert result["data"]["allowed_device_ids"] == ["city"]
    assert result["data"]["allowed_entity_registry_ids"] == ["stable-input_boolean.helper"]
    assert result["data"]["mqtt_password"] == "keep-secret" and result["data"]["world_id"] == "world-a"
    devices.add("new-city", area_id="downtown")
    assert result["data"]["allowed_device_ids"] == ["city"]
    await flow.async_step_devices({})
    empty = await flow.async_step_entities({})
    assert empty["data"]["device_scope"] == "selected"
    assert empty["data"]["allowed_device_ids"] == empty["data"]["allowed_entity_registry_ids"] == []


@pytest.mark.asyncio
async def test_relay_path_without_callback_falls_back_to_environment_selection(monkeypatch):
    """中转页不可用（未部署/无法回调）时，自动路径超时后回退桥内补选环境，而不是死循环报错。"""
    hass = FakeHass()
    flow = flow_for(hass, "world-fallback")
    flow.flow_id = "relay-timeout-flow"
    flow.async_show_form = lambda **kwargs: kwargs
    flow.async_external_step_done = lambda *, next_step_id: {"type": "external_step_done", "next_step_id": next_step_id}
    flow._selected_environment = "auto"
    flow._quick_pair_api_base = None
    flow._quick_pair_session_id = None
    flow._quick_pair_external_opened = True
    flow._quick_pair_page_url = "https://app.seenzus.cn/ha-connect?state=dead-end"

    result = await flow.async_step_seamless_authorize()
    assert result["type"] == "external_step_done"
    finish = await flow.async_step_seamless_finish()
    assert finish["step_id"] == "environment"

    create = AsyncMock(return_value=SimpleNamespace(ok=True, session_id="wps_new",
        pairing_page_url="https://seenzus.ai/pair?session_id=wps_new", app_return_url=None))
    monkeypatch.setattr("seenzus_bridge.config_flow.create_web_pairing_session", create)
    monkeypatch.setattr("seenzus_bridge.config_flow._build_quick_pair_callback_context",
        lambda *_a, **_k: ("https://homeassistant.local:8123/api/seenzus_bridge/quick_pair/callback", "ps", "jwt"))
    monkeypatch.setattr("seenzus_bridge.config_flow._resolve_ha_instance_id", AsyncMock(return_value="ha-uuid"))
    flow.async_external_step = lambda *, step_id, url, description_placeholders=None: {"type": "external", "step_id": step_id, "url": url}
    flow._selected_pairing_mode = "seamless"
    flow._scope_only = False
    flow._scope = {"device_scope": "all"}

    picked = await flow.async_step_environment({"environment": "global"})
    assert picked["type"] == "external" and picked["url"].startswith("https://seenzus.ai/pair")
    assert create.await_args.kwargs["api_base"] == ENVIRONMENTS["global"][1]


@pytest.mark.asyncio
async def test_new_pairing_supersedes_flow_parked_at_external_step(monkeypatch):
    """死路页面把流程留在外部授权步时，新的配对应接管槽位并终止僵尸流程，而不是要求重启 HA。"""
    hass = FakeHass()
    first, second = flow_for(hass), flow_for(hass)
    first.flow_id, second.flow_id = "zombie-flow", "fresh-flow"
    assert first._claim_pairing_slot()
    first._quick_pair_page_url = "https://app.seenzus.cn/ha-connect?state=dead-end"
    first._quick_pair_external_opened = False
    first.async_external_step = lambda *, step_id, url, description_placeholders=None: {"type": "external", "step_id": step_id, "url": url}
    assert (await first.async_step_seamless_authorize())["type"] == "external"

    aborted: list[str] = []
    monkeypatch.setattr(hass.config_entries.flow, "async_progress", lambda: [{"flow_id": "zombie-flow"}])
    hass.config_entries.flow.async_abort = lambda flow_id: aborted.append(flow_id)

    second._reshow_seamless_form = lambda reason: {"error": reason}
    create = AsyncMock(return_value=SimpleNamespace(ok=True, session_id="wps_new",
        pairing_page_url="https://seenzus.ai/pair?session_id=wps_new", app_return_url=None))
    monkeypatch.setattr("seenzus_bridge.config_flow.create_web_pairing_session", create)
    monkeypatch.setattr("seenzus_bridge.config_flow._build_quick_pair_callback_context",
        lambda *_a, **_k: ("https://homeassistant.local:8123/api/seenzus_bridge/quick_pair/callback", "ps", "jwt"))
    monkeypatch.setattr("seenzus_bridge.config_flow._resolve_ha_instance_id", AsyncMock(return_value="ha-uuid"))
    second.async_external_step = lambda *, step_id, url, description_placeholders=None: {"type": "external", "step_id": step_id, "url": url}
    second._selected_environment = "global"
    second._selected_pairing_mode = "seamless"
    second._scope_only = False
    second._scope = {"device_scope": "all"}

    result = await second.async_step_seamless({})
    assert result["type"] == "external"
    assert create.await_args.kwargs["api_base"] == ENVIRONMENTS["global"][1]
    assert aborted == ["zombie-flow"]
    assert hass.data["seenzus_bridge"]["pairing_flow_owner"][1] == "fresh-flow"


@pytest.mark.asyncio
async def test_pending_pairing_serializes_entries_but_cancelled_flow_releases_slot(monkeypatch):
    hass = FakeHass()
    first, second = flow_for(hass), flow_for(hass, "world-b")
    first.flow_id, second.flow_id = "first-flow", "second-flow"
    assert first._claim_pairing_slot()
    monkeypatch.setattr(hass.config_entries.flow, "async_progress", lambda: [{"flow_id": first.flow_id}])
    second._reshow_seamless_form = lambda reason: {"error": reason}
    create = AsyncMock()
    monkeypatch.setattr("seenzus_bridge.config_flow.create_web_pairing_session", create)
    assert await second.async_step_seamless({}) == {"error": "pairing_in_progress"}
    create.assert_not_awaited()
    monkeypatch.setattr(hass.config_entries.flow, "async_progress", lambda: [])
    assert second._claim_pairing_slot()
