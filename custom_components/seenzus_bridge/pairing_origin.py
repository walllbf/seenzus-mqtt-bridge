"""Obtain a new flow's deployment from its initiating application window.

Routing hints never authorize a World or supply MQTT credentials. The existing
signed callback and server-side code exchange remain the authorization boundary.
"""
from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import urlencode, urlsplit, urlunsplit

from aiohttp import web
from homeassistant.components import http
from homeassistant.data_entry_flow import UnknownFlow

from .const import DOMAIN
from .environments import ENVIRONMENTS
from .quick_pair import _decode_jwt, _ensure_view_registered

ORIGIN_PATH = "/api/seenzus_bridge/quick_pair/origin"
_CONTEXTS = "quick_pair_origin_contexts"
_REGISTERED = "quick_pair_origin_view_registered"
_LIMIT = 16
_TTL = 15 * 60

# The outer app retains HA's WindowProxy. HA opens this same-origin external
# page using window.open(url); this page can then reach the app via opener.opener.
# COOP/noopener may break that chain. Fail visibly rather than guess a deployment.
ORIGIN_SCRIPT = r"""
(() => {
  'use strict';
  const context = JSON.parse(document.getElementById('context').textContent);
  const status = document.getElementById('status');
  // getRandomValues also works on a local HA served over plain HTTP.
  const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('');
  let app;
  try { app = window.opener && window.opener.opener; } catch (_) {}
  const fail = () => {
    clearInterval(timer);
    clearTimeout(deadline);
    window.removeEventListener('message', receive);
    status.textContent = context.chinese
      ? '无法取得发起应用的信息。请在 HA 取消本次配对并关闭 HA 标签页，再从应用重新发起，保持应用页面打开。'
      : 'The initiating application is unavailable. Cancel this flow in HA, close its tab, then start again from the app, keeping the app page open.';
  };
  const receive = (event) => {
    if (event.source !== app) return;
    const data = event.data;
    const environment = Object.keys(context.origins).find(key => context.origins[key] === event.origin);
    if (!environment || !data || data.type !== 'seenzus:pairing-origin:response'
        || data.requestId !== requestId || data.state !== context.params.state
        || data.environment !== environment) return;
    clearInterval(timer);
    clearTimeout(deadline);
    window.removeEventListener('message', receive);
    const params = context.params;
    if ((params.configured_environments || '').split(',').includes(environment)) {
      const callback = new URL(params.redirect_uri);
      callback.searchParams.set('state', params.state);
      callback.searchParams.set('error', 'environment_already_configured');
      location.replace(callback.href);
      return;
    }
    const target = new URL('/pair/bootstrap', event.origin);
    for (const key of ['state', 'redirect_uri', 'ha_instance_id', 'bridge_name', 'expected_world_id', 'expected_source_id']) {
      if (params[key]) target.searchParams.set(key, params[key]);
    }
    // Further authorization runs on the issuing deployment. Drop window access
    // once the routing-only exchange is complete.
    window.opener = null;
    location.replace(target.href);
  };
  const ask = () => {
    if (!app || app.closed) { fail(); return; }
    for (const origin of Object.values(context.origins)) {
      // postMessage targets an already-open window; this makes no request to
      // another deployment and needs no third-party storage or network relay.
      app.postMessage({ type: 'seenzus:pairing-origin:request', requestId, state: context.params.state,
        redirectUri: context.params.redirect_uri }, origin);
    }
  };
  window.addEventListener('message', receive);
  const timer = setInterval(ask, 500);
  const deadline = setTimeout(fail, 10000);
  ask();
})();
"""


def _prune(contexts: dict) -> None:
    now = time.time()
    for key, context in list(contexts.items()):
        if context["expires"] <= now:
            del contexts[key]


def build_pairing_origin_url(hass, params: dict) -> str:
    """Register on first config flow, without requiring a pre-existing entry."""
    _ensure_view_registered(hass, flag=_REGISTERED, view_factory=PairingOriginView)
    state = _decode_jwt(hass, params["state"])
    if not state or state.get("redirect_uri") != params["redirect_uri"]:
        raise ValueError("invalid_pairing_origin_state")
    key = (state.get("flow_manager", "config"), state["flow_id"])
    contexts = hass.data.setdefault(DOMAIN, {}).setdefault(_CONTEXTS, {})
    _prune(contexts)
    contexts.pop(key, None)
    while len(contexts) >= _LIMIT:
        contexts.pop(next(iter(contexts)))
    contexts[key] = {"params": dict(params), "expires": time.time() + _TTL}
    callback = urlsplit(params["redirect_uri"])
    return urlunsplit((callback.scheme, callback.netloc, ORIGIN_PATH,
                       urlencode({"state": params["state"]}), ""))


class PairingOriginView(http.HomeAssistantView):
    """Serve only the context of a live, signed config/options flow."""

    requires_auth = False
    url = ORIGIN_PATH
    name = "api:seenzus_bridge:quick_pair_origin"

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app.get(http.KEY_HASS) or request.app.get("hass")
        token = request.query.get("state", "")
        state = _decode_jwt(hass, token)
        headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                   "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY"}
        if not state:
            return web.Response(text="Invalid pairing state", status=400, headers=headers)
        manager_name = state.get("flow_manager", "config")
        if manager_name not in ("config", "options"):
            return web.Response(text="Invalid flow manager", status=400, headers=headers)
        contexts = hass.data.get(DOMAIN, {}).get(_CONTEXTS, {})
        _prune(contexts)
        context = contexts.get((manager_name, state.get("flow_id")))
        if not context or context["params"]["state"] != token:
            return web.Response(text="Pairing expired. Start again from the app.", status=410, headers=headers)
        manager = hass.config_entries.options if manager_name == "options" else hass.config_entries.flow
        try:
            manager.async_get(state["flow_id"])
        except UnknownFlow:
            return web.Response(text="Pairing cancelled. Start again from the app.", status=410, headers=headers)
        chinese = str(getattr(hass.config, "language", "en")).startswith("zh")
        payload = {"params": context["params"], "chinese": chinese,
                   "origins": {key: urlunsplit((*urlsplit(api)[:2], "", "", ""))
                               for key, (_, api) in ENVIRONMENTS.items()}}
        # JSON is embedded in HTML, including HA's user-supplied location name.
        encoded = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c")
        digest = base64.b64encode(hashlib.sha256(ORIGIN_SCRIPT.encode()).digest()).decode()
        headers["Content-Security-Policy"] = (
            "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            f"script-src 'sha256-{digest}'; style-src 'unsafe-inline'; form-action 'none'"
        )
        title = "正在连接发起应用…" if chinese else "Connecting to the initiating app…"
        body = ("<!doctype html><html><head><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>seenzus · Home Assistant</title>"
                "<style>body{font:16px system-ui;max-width:560px;margin:12vh auto;padding:24px;line-height:1.8}</style>"
                f"</head><body><h1>seenzus</h1><p id=status>{title}</p>"
                f"<script id=context type=application/json>{encoded}</script>"
                + f"<script>{ORIGIN_SCRIPT}</script></body></html>")
        return web.Response(text=body, content_type="text/html", headers=headers)
