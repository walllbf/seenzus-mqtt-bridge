"""Cross-repository contract: this bridge against the actual seenzus pairing routes.

Runs when SEENZUS_APP_REPO points at a seenzus application checkout. The test
starts its `script/ha-pairing-contract-server.ts` (in-memory repository,
recording broker stand-in, no deployment or database) and drives the bridge's
real HTTP client and a real HA core with on-disk entries and journal.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest

from homeassistant.core import HomeAssistant

from seenzus_bridge import pairing_commit
from seenzus_bridge.pairing_bootstrap import create_web_pairing_session, exchange_web_pairing_callback_code
from seenzus_bridge.pairing_commit import async_resolve_orphans, async_schedule_commit, pairing_journal
from tests.test_pairing_commit import add_entry, core_entry, start_core

APP_REPO = os.environ.get("SEENZUS_APP_REPO")
pytestmark = pytest.mark.skipif(not APP_REPO, reason="set SEENZUS_APP_REPO to run against the seenzus server")
CALLBACK = "http://homeassistant.local:8123/api/seenzus_bridge/quick_pair/callback"


@pytest.fixture
def app_server():
    # Started synchronously: HA's Windows test loop cannot spawn subprocesses.
    npx = shutil.which("npx.cmd" if sys.platform == "win32" else "npx")
    process = subprocess.Popen([npx, "tsx", "script/ha-pairing-contract-server.ts"], cwd=APP_REPO,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env={**os.environ, "NODE_ENV": "test"})
    try:
        for line in process.stdout:
            if line.startswith(b"READY "):
                yield f"http://127.0.0.1:{int(line.split()[1])}"
                break
        else:
            raise RuntimeError("contract server exited before it was ready")
    finally:
        process.kill()
        process.wait()


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(pairing_commit, "SAVED_POLL_SECONDS", 0.05)
    monkeypatch.setattr(pairing_commit, "RETRY_DELAYS_SECONDS", (0.05,) * 6)


async def call(origin: str, method: str, path: str, body: dict | None = None) -> dict:
    async with aiohttp.ClientSession() as session:
        async with session.request(method, f"{origin}{path}", json=body) as response:
            return json.loads(await response.text())


async def exchange(origin: str, ha_instance_id: str = "ha-contract"):
    """Bridge session → signed-in authorization → deferred exchange, all over HTTP."""
    api_base = f"{origin}/api/seenzus"
    session = await create_web_pairing_session(api_base=api_base, bridge_name="HA", bridge_version="test", ha_version="test",
                                               redirect_uri=CALLBACK, state="signed-state", ha_instance_id=ha_instance_id, protocol_version=2)
    assert session.ok, session
    state = await call(origin, "GET", "/__contract/state")
    authorized = await call(origin, "POST", f"/api/seenzus/integrations/ha/web-pairing/session/{session.session_id}/authorize",
                            {"targetSiteId": state["siteId"]})
    query = parse_qs(urlparse(authorized["redirectUrl"]).query)
    assert query["environment"] == ["custom"] and query["session_id"] == [session.session_id]
    result = await exchange_web_pairing_callback_code(api_base=api_base, code=query["code"][0], state="signed-state",
                                                      session_id=session.session_id, deferred_commit=True)
    assert result.ok and result.commit_token and result.mqtt["password"], result
    return api_base, result


def journal_marker(api_base: str, result, **extra) -> dict:
    return {"session_id": result.session_id, "api_base": api_base, "token": result.commit_token,
            "flow_manager": "config", "flow_id": "ended-flow", **extra}


async def commit(hass: HomeAssistant, entry) -> None:
    async_schedule_commit(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.asyncio
async def test_new_pairing_reaches_the_server_only_after_ha_saves_it(tmp_path, app_server):
    hass = await start_core(tmp_path)
    try:
        api_base, result = await exchange(app_server)
        assert (await call(app_server, "GET", "/__contract/state"))["sources"] == []
        await pairing_journal(hass).async_record(journal_marker(api_base, result))
        entry = add_entry(hass, core_entry("contract-new", data={"pairing_session_id": result.session_id}))
        await commit(hass, entry)
        state = await call(app_server, "GET", "/__contract/state")
        assert [source["username"] for source in state["sources"]] == [result.mqtt["username"]]
        assert state["broker"]["revoked"] == [] and state["liveCandidates"] == []
        assert await pairing_journal(hass).async_markers() == []
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_cancelled_repair_keeps_the_server_source_and_old_credential(tmp_path, app_server):
    hass = await start_core(tmp_path)
    try:
        api_base, first = await exchange(app_server)
        await pairing_journal(hass).async_record(journal_marker(api_base, first))
        await commit(hass, add_entry(hass, core_entry("contract-cancel", data={"pairing_session_id": first.session_id})))
        _, second = await exchange(app_server)
        await pairing_journal(hass).async_record(journal_marker(api_base, second))
        # The flow ended and no entry carries the re-pair's candidate.
        await async_resolve_orphans(hass)
        state = await call(app_server, "GET", "/__contract/state")
        assert [source["username"] for source in state["sources"]] == [first.mqtt["username"]]
        assert state["broker"]["revoked"] == [second.mqtt["username"]]
        assert second.mqtt["username"] not in state["activeLedger"]
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_repair_commit_survives_an_ha_restart_and_a_server_crash(tmp_path, app_server):
    first_core = await start_core(tmp_path)
    api_base, first = await exchange(app_server)
    await pairing_journal(first_core).async_record(journal_marker(api_base, first))
    entry = add_entry(first_core, core_entry("contract-restart", data={"pairing_session_id": first.session_id}))
    await commit(first_core, entry)
    _, second = await exchange(app_server)
    await pairing_journal(first_core).async_record(journal_marker(api_base, second, flow_manager="options", entry_id=entry.entry_id,
        previous={"options": dict(entry.options), "title": entry.title, "unique_id": entry.unique_id}))
    first_core.config_entries.async_update_entry(entry, options={"pairing_session_id": second.session_id})
    await first_core.async_stop(force=True)  # HA restarts before committing.

    await call(app_server, "POST", "/__contract/fail-commit-finishes", {"count": 1})
    second_core = await start_core(tmp_path)
    try:
        restored = second_core.config_entries.async_get_entry("contract-restart")
        async_schedule_commit(second_core, restored)
        # The first commit dies mid-write on the server; the bridge keeps retrying.
        for _ in range(200):
            state = await call(app_server, "GET", "/__contract/state")
            if not await pairing_journal(second_core).async_markers():
                break
            await asyncio.sleep(0.05)
        assert [source["username"] for source in state["sources"]] == [second.mqtt["username"]]
    finally:
        await second_core.async_stop(force=True)
    # Rolled forward past the crash; the old credential is reclaimed from the ledger.
    await call(app_server, "POST", "/__contract/sweep")
    state = await call(app_server, "GET", "/__contract/state")
    assert state["broker"]["revoked"] == [first.mqtt["username"]]
    assert state["activeLedger"] == [second.mqtt["username"]]


@pytest.mark.asyncio
async def test_a_refused_repair_restores_ha_to_the_connection_the_server_kept(tmp_path, app_server):
    hass = await start_core(tmp_path)
    try:
        api_base, first = await exchange(app_server)
        await pairing_journal(hass).async_record(journal_marker(api_base, first))
        entry = add_entry(hass, core_entry("contract-refused", data={"pairing_session_id": first.session_id,
                                                                     "mqtt_username": first.mqtt["username"]}))
        await commit(hass, entry)
        _, second = await exchange(app_server)
        previous = {"options": dict(entry.options), "title": entry.title, "unique_id": entry.unique_id}
        # The server-side window closed (cancel stands in for expiry) before HA committed.
        await call(app_server, "POST", "/api/seenzus/integrations/ha/web-pairing/callback/cancel",
                   {"sessionId": second.session_id, "commitToken": second.commit_token})
        await pairing_journal(hass).async_record(journal_marker(api_base, second, flow_manager="options",
                                                                entry_id=entry.entry_id, previous=previous))
        hass.config_entries.async_update_entry(entry, options={"pairing_session_id": second.session_id,
                                                               "mqtt_username": second.mqtt["username"]})
        await commit(hass, entry)
        assert {**entry.data, **entry.options}["mqtt_username"] == first.mqtt["username"]
        state = await call(app_server, "GET", "/__contract/state")
        assert [source["username"] for source in state["sources"]] == [first.mqtt["username"]]
        assert first.mqtt["username"] not in state["broker"]["revoked"]
    finally:
        await hass.async_stop(force=True)
