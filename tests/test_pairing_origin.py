"""Signed, live HA context must precede the browser's origin-only handoff."""
import base64
import hashlib
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import pytest
from homeassistant.data_entry_flow import UnknownFlow

from seenzus_bridge import pairing_origin as origin
from seenzus_bridge.config_flow import SavanAIBridgeConfigFlow
from seenzus_bridge.quick_pair import _encode_jwt
from tests.helpers import FakeHass


def context(hass, flow_id="origin-test", **overrides):
    callback = "http://ha.localhost:8123/api/seenzus_bridge/quick_pair/callback"
    state = {"flow_id": flow_id, "flow_manager": "config", "pairing_state": "nonce",
             "redirect_uri": callback, "pairing_started_at": int(time.time() * 1000),
             "exp": int(time.time()) + 900}
    params = {"state": _encode_jwt(hass, state), "redirect_uri": callback,
              "bridge_name": "我的家 </script><img src=x onerror=alert(1)>",
              "ha_instance_id": "test-ha", "configured_environments": "global"}
    params.update(overrides)
    return params


@pytest.mark.asyncio
async def test_origin_view_requires_a_live_signed_flow_and_escapes_user_names():
    hass = FakeHass()
    hass.config.language = "zh-Hans"
    params = context(hass)
    url = origin.build_pairing_origin_url(hass, params)
    assert urlsplit(url).path == origin.ORIGIN_PATH
    assert parse_qs(urlsplit(url).query) == {"state": [params["state"]]}
    hass.config_entries.flow.async_get = lambda _: {"flow_id": "origin-test", "step_id": "seamless_authorize"}
    request = SimpleNamespace(app={"hass": hass}, query={"state": params["state"]})
    response = await origin.PairingOriginView().get(request)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store"
    assert "Cross-Origin-Opener-Policy" not in response.headers
    assert "</script><img" not in response.text
    assert "\\u003c/script>" in response.text
    digest = base64.b64encode(hashlib.sha256(origin.ORIGIN_SCRIPT.encode()).digest()).decode()
    assert f"'sha256-{digest}'" in response.headers["Content-Security-Policy"]
    assert "'unsafe-inline'" not in response.headers["Content-Security-Policy"].split("style-src")[0]
    # Reachability checks before the opener-free claim chain may reach the deployments only.
    assert "connect-src https://app.seenzus.cn https://seenzus.ai https://uat.seenzus.ai;" in response.headers["Content-Security-Policy"]
    payload = json.loads(response.text.split("type=application/json>")[1].split("</script>")[0])
    assert payload["origins"]["uat"] == "https://uat.seenzus.ai"
    assert payload["chinese"]

    request.query["state"] = params["state"] + "tampered"
    assert (await origin.PairingOriginView().get(request)).status == 400
    request.query["state"] = params["state"]
    hass.config_entries.flow.async_get = lambda _: {"step_id": "environment"}
    assert (await origin.PairingOriginView().get(request)).status == 410
    hass.config_entries.flow.async_get = lambda _: (_ for _ in ()).throw(UnknownFlow())
    assert (await origin.PairingOriginView().get(request)).status == 410


@pytest.mark.asyncio
async def test_context_is_bounded_expires_and_replaced_state_cannot_resume(monkeypatch):
    hass = FakeHass()
    old = context(hass, bridge_name="old")
    origin.build_pairing_origin_url(hass, old)
    new_state = {"flow_id": "origin-test", "redirect_uri": old["redirect_uri"],
                 "pairing_state": "new", "exp": int(time.time()) + 900}
    origin.build_pairing_origin_url(hass, {**old, "state": _encode_jwt(hass, new_state)})
    request = SimpleNamespace(app={"hass": hass}, query={"state": old["state"]})
    assert (await origin.PairingOriginView().get(request)).status == 410
    for number in range(20):
        origin.build_pairing_origin_url(hass, context(hass, f"flow-{number}"))
    assert len(hass.data["seenzus_bridge"][origin._CONTEXTS]) == 16
    time_now = time.time()
    monkeypatch.setattr(origin.time, "time", lambda: time_now + 901)
    origin._prune(hass.data["seenzus_bridge"][origin._CONTEXTS])
    assert not hass.data["seenzus_bridge"][origin._CONTEXTS]


@pytest.mark.asyncio
async def test_auto_flow_registers_local_origin_view_on_first_install(monkeypatch):
    hass = FakeHass()
    flow = SavanAIBridgeConfigFlow()
    flow.hass = hass
    flow.flow_id = "first-install"
    flow.context = {"source": "user"}
    flow.async_external_step = lambda **kwargs: kwargs
    params = context(hass, "first-install")
    monkeypatch.setattr("seenzus_bridge.config_flow._build_quick_pair_callback_context",
                        lambda *_: (params["redirect_uri"], "nonce", params["state"]))
    monkeypatch.setattr("seenzus_bridge.config_flow._resolve_ha_instance_id", AsyncMock(return_value="test-ha"))
    preset = AsyncMock(return_value="https://app.seenzus.cn/api/seenzus")
    monkeypatch.setattr("seenzus_bridge.config_flow.resolve_dev_pairing_api_base", preset)
    result = await flow.async_step_seamless({})
    assert urlsplit(result["url"]).path == origin.ORIGIN_PATH
    assert urlsplit(result["url"]).netloc == "ha.localhost:8123"
    assert any(isinstance(view, origin.PairingOriginView) for view in hass.http.registered_views)
    preset.assert_awaited_once()
    assert flow._quick_pair_api_base is None
    payload = hass.data["seenzus_bridge"][origin._CONTEXTS][("config", "first-install")]["params"]
    assert payload["developer_origin"] is None


@pytest.mark.asyncio
async def test_origin_view_requires_the_real_ha_flow_to_still_be_authorizing():
    from homeassistant.data_entry_flow import FlowManager, FlowResultType

    class Manager(FlowManager):
        async def async_create_flow(self, handler_key, *, context=None, data=None):
            raise NotImplementedError

        async def async_finish_flow(self, flow, result):
            return result

    hass = FakeHass()
    manager = Manager(hass)
    hass.config_entries.flow = manager
    flow = SavanAIBridgeConfigFlow()
    flow.hass, flow.handler, flow.flow_id = hass, "seenzus_bridge", "origin-test"
    flow.context = {"source": "user"}
    flow.cur_step = flow.async_external_step(step_id="seamless_authorize", url="http://ha.local/origin")
    manager._async_add_flow_progress(flow)
    params = context(hass)
    origin.build_pairing_origin_url(hass, params)
    request = SimpleNamespace(app={"hass": hass}, query={"state": params["state"]})
    assert (await origin.PairingOriginView().get(request)).status == 200
    flow.cur_step = {"type": FlowResultType.FORM, "step_id": "environment"}
    assert (await origin.PairingOriginView().get(request)).status == 410
