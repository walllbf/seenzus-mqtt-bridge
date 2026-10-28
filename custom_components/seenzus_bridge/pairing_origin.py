"""Obtain a new flow's deployment from the browser that initiated it.

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

# Two ways to learn which deployment started this flow, neither of which
# authorizes anything:
# 1. Desktop fast path: the app page that opened HA as a popup is reachable
#    through opener.opener and answers a postMessage with its own origin.
# 2. Everywhere else (in-app WebViews and phone browsers keep no opener, and
#    COOP may cut it): visit each reachable deployment's /ha-origin-claim/ page
#    with top-level navigation. Each reads its own first-party record of a
#    recent launch in this browser and the chain returns here with the claims;
#    the newest launch wins. No relay, third-party storage or clock comparison
#    with HA is involved, and failure leads to an explicit choice, never a guess.
ORIGIN_SCRIPT = r"""
(() => {
  'use strict';
  const context = JSON.parse(document.getElementById('context').textContent);
  const status = document.getElementById('status');
  const manual = document.getElementById('manual');
  const environments = document.getElementById('environments');
  const query = new URLSearchParams(location.search);
  // getRandomValues also works on a local HA served over plain HTTP.
  const requestId = Array.from(crypto.getRandomValues(new Uint8Array(16)), value => value.toString(16).padStart(2, '0')).join('');
  let app;
  try { app = window.opener && window.opener.opener; } catch (_) {}
  let timer, deadline, done = false;
  const stop = () => {
    done = true;
    clearInterval(timer);
    clearTimeout(deadline);
    window.removeEventListener('message', receive);
  };
  // In-app WebViews cannot get back to HA's dialog, so the choice is offered here,
  // unless deployments would refuse to return to this HA address anyway.
  const offered = (context.params.manual_environments || '').split(',').filter(key => context.origins[key]);
  const fail = (here = true) => {
    stop();
    const choices = here ? offered : [];
    status.textContent = context.chinese
      ? (choices.length ? '无法自动识别环境。请选择要连接的环境，或取消本次配对并从应用重新发起。'
        : '无法自动识别环境。可返回 HA 手动选择，或取消本次配对并从应用重新发起。')
      : (choices.length ? 'The environment could not be detected. Choose one, or cancel and restart from the app.'
        : 'The environment could not be detected. Choose it in HA, or cancel and restart from the app.');
    if (!choices.length) { manual.hidden = false; return; }
    for (const key of choices) {
      const button = document.createElement('button');
      button.type = 'button';
      button.textContent = (context.labels || {})[key] || key;
      button.addEventListener('click', () => route(key));
      environments.append(button);
    }
    environments.hidden = false;
    environments.style.display = 'flex';
  };
  const route = (environment) => {
    stop();
    const params = context.params;
    if ((params.configured_environments || '').split(',').includes(environment)) {
      const callback = new URL(params.redirect_uri);
      callback.searchParams.set('state', params.state);
      callback.searchParams.set('error', 'environment_already_configured');
      location.replace(callback.href);
      return;
    }
    const target = new URL('/pair/bootstrap', context.origins[environment]);
    for (const key of ['state', 'redirect_uri', 'ha_instance_id', 'bridge_name', 'expected_world_id', 'expected_source_id']) {
      if (params[key]) target.searchParams.set(key, params[key]);
    }
    // Further authorization runs on the issuing deployment. Drop window access.
    try { window.opener = null; } catch (_) {}
    location.replace(target.href);
  };
  const decide = (claims) => {
    let best = null;
    for (const item of claims.split(',')) {
      const match = /^(cn|global|uat|custom):(\d{1,15})$/.exec(item);
      if (match && context.origins[match[1]] && (!best || Number(match[2]) > best.at)) {
        best = { environment: match[1], at: Number(match[2]) };
      }
    }
    if (best) route(best.environment); else fail();
  };
  const reachable = async (origin) => {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 3000);
    try {
      await fetch(new URL('/ha-origin-claim/claim.js', origin), {
        mode: 'no-cors', cache: 'no-store', credentials: 'omit', signal: controller.signal,
      });
      return true;
    } catch (_) { return false; } finally { clearTimeout(timeout); }
  };
  // Deployments return only to an HTTPS or local HA address, the same rule as
  // for the pairing callback. Leaving from any other address would strand the
  // user on a deployment page, so offer the explicit choice here instead.
  const returnable = () => {
    if (location.protocol === 'https:') return true;
    const host = location.hostname.replace(/^\[|\]$/g, '').toLowerCase();
    if (host === 'localhost' || host.endsWith('.localhost') || host === '::1' || host.endsWith('.local')) return true;
    const v4 = /^(\d{1,3})\.(\d{1,3})\.\d{1,3}\.\d{1,3}$/.exec(host);
    const [a, b] = v4 ? [Number(v4[1]), Number(v4[2])] : [];
    return Boolean(v4) && (a === 127 || a === 10 || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168));
  };
  const probe = async () => {
    if (done) return;
    stop();
    if (!returnable()) { fail(false); return; }
    status.textContent = context.chinese ? '正在识别发起配对的应用…' : 'Identifying the app that started pairing…';
    // A developer origin goes first: public claim pages only continue to public deployments.
    const keys = Object.keys(context.origins).sort((a, b) => (b === 'custom') - (a === 'custom'));
    const live = (await Promise.all(keys.map(async key => (await reachable(context.origins[key])) ? key : null))).filter(Boolean);
    if (!live.length) { fail(); return; }
    const back = new URL(location.pathname, location.origin);
    back.searchParams.set('state', context.params.state);
    const first = new URL('/ha-origin-claim/', context.origins[live[0]]);
    first.searchParams.set('return', back.href);
    first.searchParams.set('next', live.slice(1).join(','));
    first.searchParams.set('claims', '');
    location.replace(first.href);
  };
  const receive = (event) => {
    if (event.source !== app) return;
    const data = event.data;
    const environment = Object.keys(context.origins).find(key => context.origins[key] === event.origin);
    if (!environment || !data || data.type !== 'seenzus:pairing-origin:response'
        || data.requestId !== requestId || data.state !== context.params.state
        || data.environment !== environment) return;
    route(environment);
  };
  const ask = () => {
    if (!app || app.closed) { probe(); return; }
    for (const origin of Object.values(context.origins)) {
      // postMessage targets an already-open window; it makes no request.
      app.postMessage({ type: 'seenzus:pairing-origin:request', requestId, state: context.params.state,
        redirectUri: context.params.redirect_uri }, origin);
    }
  };
  manual.addEventListener('click', () => {
    const callback = new URL(context.params.redirect_uri);
    callback.searchParams.set('state', context.params.state);
    callback.searchParams.set('error', 'environment_required');
    location.replace(callback.href);
  });
  if (query.has('claims')) { decide(query.get('claims')); return; }
  if (!app) { probe(); return; }
  window.addEventListener('message', receive);
  timer = setInterval(ask, 500);
  deadline = setTimeout(probe, 4000);
  ask();
})();
"""


# Same names as the environment selector in translations/*.json.
_ENVIRONMENT_LABELS = {
    "zh": {"cn": "国内 PRD", "global": "境外 PRD", "uat": "UAT（开发）", "custom": "Dev Server（开发预设）"},
    "en": {"cn": "Mainland PRD", "global": "Overseas PRD", "uat": "UAT (development)",
           "custom": "Dev Server (developer preset)"},
}


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
            progress = manager.async_get(state["flow_id"])
        except UnknownFlow:
            return web.Response(text="Pairing cancelled. Start again from the app.", status=410, headers=headers)
        if progress.get("step_id") != "seamless_authorize":
            return web.Response(text="Pairing moved to another step. Start again from Home Assistant.", status=410, headers=headers)
        chinese = str(getattr(hass.config, "language", "en")).startswith("zh")
        payload = {"params": context["params"], "chinese": chinese,
                   "origins": {key: urlunsplit((*urlsplit(api)[:2], "", "", ""))
                               for key, (_, api) in ENVIRONMENTS.items()},
                   "labels": _ENVIRONMENT_LABELS["zh" if chinese else "en"]}
        if context["params"].get("developer_origin"):
            payload["origins"]["custom"] = context["params"]["developer_origin"]
        # JSON is embedded in HTML, including HA's user-supplied location name.
        encoded = json.dumps(payload, ensure_ascii=True).replace("<", "\\u003c")
        digest = base64.b64encode(hashlib.sha256(ORIGIN_SCRIPT.encode()).digest()).decode()
        headers["Content-Security-Policy"] = (
            "default-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
            f"connect-src {' '.join(sorted(set(payload['origins'].values())))}; "
            f"script-src 'sha256-{digest}'; style-src 'unsafe-inline'; form-action 'none'"
        )
        title = "正在连接发起应用…" if chinese else "Connecting to the initiating app…"
        manual = "在 HA 中手动选择环境" if chinese else "Choose an environment in HA"
        body = ("<!doctype html><html><head><meta charset=utf-8>"
                "<meta name=viewport content='width=device-width,initial-scale=1'>"
                "<title>seenzus · Home Assistant</title>"
                "<style>body{font:16px system-ui;max-width:560px;margin:12vh auto;padding:24px;line-height:1.8}</style>"
                f"</head><body><h1>seenzus</h1><p id=status>{title}</p><button id=manual hidden>{manual}</button>"
                "<div id=environments hidden style='flex-direction:column;gap:12px;max-width:280px'></div>"
                f"<script id=context type=application/json>{encoded}</script>"
                + f"<script>{ORIGIN_SCRIPT}</script></body></html>")
        return web.Response(text=body, content_type="text/html", headers=headers)
