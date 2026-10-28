"""Two-phase pairing on a real HA core: on-disk entries and journal, restarts.

The backend is a local aiohttp stand-in with the deferred-commit wire
contract; tests/test_pairing_commit_contract.py runs the same lifecycle
against the actual seenzus server when it is available.
"""
from __future__ import annotations

import asyncio
import inspect
from types import MappingProxyType

import pytest
from aiohttp import web

from homeassistant.config_entries import ConfigEntries, ConfigEntry
from homeassistant.core import HomeAssistant

from seenzus_bridge import pairing_commit
from seenzus_bridge.pairing_commit import (
    async_resolve_orphans,
    async_schedule_commit,
    pairing_journal,
)

API_PATH = "/api/seenzus"


def core_entry(entry_id: str, *, data: dict | None = None, options: dict | None = None, title: str = "UAT · Home") -> ConfigEntry:
    values = {
        "created_at": None, "data": data or {}, "disabled_by": None, "discovery_keys": MappingProxyType({}),
        "domain": "seenzus_bridge", "entry_id": entry_id, "minor_version": 1, "modified_at": None,
        "options": options or {}, "pref_disable_new_entities": None, "pref_disable_polling": None,
        "source": "user", "subentries_data": MappingProxyType({}), "title": title, "unique_id": "uat", "version": 2,
    }
    parameters = inspect.signature(ConfigEntry).parameters
    return ConfigEntry(**{key: value for key, value in values.items() if key in parameters})


class Backend:
    """Scripted commit/cancel endpoints recording every call."""

    def __init__(self) -> None:
        self.commit_replies: list[tuple[int, dict]] = []
        self.cancel_replies: list[tuple[int, dict]] = []
        self.calls: list[tuple[str, dict]] = []
        self.runner: web.AppRunner | None = None
        self.api_base = ""

    async def start(self) -> None:
        async def handle(request: web.Request) -> web.Response:
            action = request.match_info["action"]
            body = await request.json()
            self.calls.append((action, body))
            if action == "cancel":
                status, reply = self.cancel_replies.pop(0) if self.cancel_replies else (200, {"ok": True, "state": "cancelled"})
                return web.json_response(reply, status=status)
            status, reply = self.commit_replies.pop(0) if self.commit_replies else (200, {"ok": True, "state": "committed"})
            return web.json_response(reply, status=status)

        app = web.Application()
        app.router.add_post(f"{API_PATH}/integrations/ha/web-pairing/callback/{{action}}", handle)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.api_base = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}{API_PATH}"

    def actions(self) -> list[str]:
        return [action for action, _ in self.calls]


@pytest.fixture
async def backend():
    server = Backend()
    await server.start()
    yield server
    await server.runner.cleanup()


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    monkeypatch.setattr(pairing_commit, "SAVED_POLL_SECONDS", 0.05)
    monkeypatch.setattr(pairing_commit, "RETRY_DELAYS_SECONDS", (0.01,) * 6)


async def start_core(path) -> HomeAssistant:
    hass = HomeAssistant(str(path))
    hass.config_entries = ConfigEntries(hass, {})
    await hass.config_entries.async_initialize()
    return hass


def marker(backend: Backend, session_id: str = "wps_new", **extra) -> dict:
    return {"session_id": session_id, "api_base": backend.api_base, "token": "t" * 43,
            "flow_manager": "config", "flow_id": "gone-flow", **extra}


def add_entry(hass: HomeAssistant, entry: ConfigEntry) -> ConfigEntry:
    hass.config_entries._entries[entry.entry_id] = entry
    hass.config_entries._async_schedule_save()
    return entry


async def drive(hass: HomeAssistant, entry: ConfigEntry) -> None:
    async_schedule_commit(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)


async def journal_on_disk(path) -> list[dict]:
    """What a restarted HA would read."""
    hass = HomeAssistant(str(path))
    try:
        return await pairing_journal(hass).async_markers()
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_commits_after_the_entry_exists_and_then_forgets_the_candidate(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        await pairing_journal(hass).async_record(marker(backend))
        assert [item["session_id"] for item in await journal_on_disk(tmp_path)] == ["wps_new"]
        entry = add_entry(hass, core_entry("entry-new", data={"pairing_session_id": "wps_new"}))
        await drive(hass, entry)
        assert backend.calls == [("commit", {"sessionId": "wps_new", "commitToken": "t" * 43})]
        assert await journal_on_disk(tmp_path) == []
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_retries_through_outages_until_the_backend_answers(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        backend.commit_replies = [(503, {"error": "pairing_commit_unavailable"}),
                                  (409, {"error": "pairing_commit_in_progress"}),
                                  (502, {"error": "bad_gateway"})]
        await pairing_journal(hass).async_record(marker(backend))
        entry = add_entry(hass, core_entry("entry-retry", data={"pairing_session_id": "wps_new"}))
        await drive(hass, entry)
        assert backend.actions() == ["commit"] * 4
        assert await pairing_journal(hass).async_markers() == []
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_retries_when_the_backend_is_unreachable(tmp_path, backend, monkeypatch):
    hass = await start_core(tmp_path)
    try:
        unreachable = marker(backend, api_base="http://127.0.0.1:9/api/seenzus")
        await pairing_journal(hass).async_record(unreachable)
        entry = add_entry(hass, core_entry("entry-offline", data={"pairing_session_id": "wps_new"}))
        async_schedule_commit(hass, entry)
        await asyncio.sleep(0.3)
        # Still pending: an outage is never treated as a refusal.
        assert [item["session_id"] for item in await pairing_journal(hass).async_markers()] == ["wps_new"]
        assert entry.data["pairing_session_id"] == "wps_new"
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_refused_repair_restores_the_previous_connection(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        previous = {"pairing_session_id": "wps_old", "mqtt_username": "bridge-old", "mqtt_password": "old-secret"}
        entry = add_entry(hass, core_entry("entry-repair", data={"environment": "uat"}, options={
            "pairing_session_id": "wps_new", "mqtt_username": "bridge-new", "mqtt_password": "new-secret",
        }, title="UAT · New"))
        await pairing_journal(hass).async_record(marker(backend, flow_manager="options", entry_id=entry.entry_id,
                                                        previous={"options": previous, "title": "UAT · Home", "unique_id": "uat"}))
        backend.commit_replies = [(410, {"ok": False, "error": "pairing_commit_rejected", "state": "abandoned"})]
        await drive(hass, entry)
        assert dict(entry.options) == previous
        assert entry.title == "UAT · Home"
        # Released once the restored options no longer carry it (the reload's orphan pass).
        await async_resolve_orphans(hass)
        assert backend.actions() == ["commit", "cancel"]
        assert await journal_on_disk(tmp_path) == []
        notifications = hass.data["persistent_notification"]
        assert "seenzus_bridge_pairing_unconfirmed.entry-repair" in notifications
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_refused_new_entry_is_reported_not_silently_kept(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        entry = add_entry(hass, core_entry("entry-refused", data={"pairing_session_id": "wps_new", "mqtt_username": "bridge-new"}))
        await pairing_journal(hass).async_record(marker(backend))
        backend.commit_replies = [(404, {"ok": False, "error": "pairing_commit_not_found"})]
        await drive(hass, entry)
        assert entry.data["mqtt_username"] == "bridge-new"
        assert "seenzus_bridge_pairing_unconfirmed.entry-refused" in hass.data["persistent_notification"]
        assert "pairing_commit_rejected" in hass.data["seenzus_bridge"]["quick_pair_last_diagnostic.entry-refused"]
        assert await pairing_journal(hass).async_markers() == []
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_restart_commits_a_candidate_whose_entry_reached_disk(tmp_path, backend):
    first = await start_core(tmp_path)
    await pairing_journal(first).async_record(marker(backend))
    add_entry(first, core_entry("entry-restart", data={"pairing_session_id": "wps_new"}))
    # Stopping flushes HA's delayed writes like a normal shutdown. No commit was sent.
    await first.async_stop(force=True)
    assert backend.calls == []

    second = await start_core(tmp_path)
    try:
        entry = second.config_entries.async_get_entry("entry-restart")
        await drive(second, entry)
        assert backend.actions() == ["commit"]
        assert await pairing_journal(second).async_markers() == []
    finally:
        await second.async_stop(force=True)


@pytest.mark.asyncio
async def test_restart_cancels_a_candidate_whose_entry_never_reached_disk(tmp_path, backend):
    first = await start_core(tmp_path)
    await pairing_journal(first).async_record(marker(backend))
    # HA died before saving any entry that uses the candidate.
    await first.async_stop(force=True)

    second = await start_core(tmp_path)
    try:
        await async_resolve_orphans(second)
        assert backend.calls == [("cancel", {"sessionId": "wps_new", "commitToken": "t" * 43})]
        assert await pairing_journal(second).async_markers() == []
    finally:
        await second.async_stop(force=True)


@pytest.mark.asyncio
async def test_orphan_resolution_leaves_a_live_flow_and_a_saved_entry_alone(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        add_entry(hass, core_entry("entry-kept", data={"pairing_session_id": "wps_saved"}))
        await pairing_journal(hass).async_record(marker(backend, "wps_saved"))
        await pairing_journal(hass).async_record(marker(backend, "wps_live", flow_id="live-flow"))
        hass.config_entries.flow.async_get = lambda flow_id: {"flow_id": flow_id}
        await async_resolve_orphans(hass)
        assert backend.calls == []
        assert {item["session_id"] for item in await pairing_journal(hass).async_markers()} == {"wps_saved", "wps_live"}
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_a_superseded_candidate_is_cancelled_instead_of_committed(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        entry = add_entry(hass, core_entry("entry-twice", data={"pairing_session_id": "wps_first"}))
        await pairing_journal(hass).async_record(marker(backend, "wps_first", entry_id="entry-twice"))
        await pairing_journal(hass).async_record(marker(backend, "wps_second", entry_id="entry-twice"))
        backend.commit_replies = [(503, {"error": "pairing_commit_unavailable"})]
        async_schedule_commit(hass, entry)
        await asyncio.sleep(0)
        # Re-paired again before the first commit got through.
        hass.config_entries.async_update_entry(entry, options={"pairing_session_id": "wps_second"})
        await hass.async_block_till_done(wait_background_tasks=True)
        assert ("cancel", {"sessionId": "wps_first", "commitToken": "t" * 43}) in backend.calls
        assert [item["session_id"] for item in await pairing_journal(hass).async_markers()] == ["wps_second"]
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_removing_the_entry_cancels_its_uncommitted_candidate(tmp_path, backend, monkeypatch):
    import seenzus_bridge

    hass = await start_core(tmp_path)
    try:
        add_entry(hass, core_entry("entry-removed", data={"pairing_session_id": "wps_new"}))
        await pairing_journal(hass).async_record(marker(backend))
        # Core calls the integration's hook before it drops the entry.
        await seenzus_bridge.async_remove_entry(hass, hass.config_entries.async_get_entry("entry-removed"))
        await hass.async_block_till_done(wait_background_tasks=True)
        assert backend.actions() == ["cancel"]
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_flow_journals_its_candidate_and_releases_it_if_no_entry_is_saved(tmp_path, backend):
    from types import SimpleNamespace

    from seenzus_bridge.config_flow import SavanAIBridgeOptionsFlow

    hass = await start_core(tmp_path)
    try:
        entry = add_entry(hass, core_entry("entry-flow", data={"pairing_session_id": "wps_old"},
                                           options={"mqtt_username": "bridge-old"}))
        flow = SavanAIBridgeOptionsFlow(entry)
        flow.hass, flow.flow_id = hass, "repair-flow"
        flow._quick_pair_api_base = backend.api_base
        result = SimpleNamespace(session_id="wps_new", commit_token="t" * 43, commit_expires_at="2026-10-29T00:00:00Z")
        assert await flow._journal_candidate(result)
        # Durable before Core could save the re-paired entry, with what a refusal restores.
        [stored] = await journal_on_disk(tmp_path)
        assert stored["previous"] == {"options": {"mqtt_username": "bridge-old"}, "title": "UAT · Home", "unique_id": "uat"}
        assert stored["entry_id"] == "entry-flow" and stored["flow_manager"] == "options"
        # The user cancels: Core removes the flow without saving the entry.
        flow.async_remove()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert backend.calls == [("cancel", {"sessionId": "wps_new", "commitToken": "t" * 43})]
        assert await journal_on_disk(tmp_path) == []
        assert entry.data["pairing_session_id"] == "wps_old"
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_a_candidate_that_cannot_be_journaled_is_released_at_once(tmp_path, backend, monkeypatch):
    from types import SimpleNamespace

    from seenzus_bridge.config_flow import SavanAIBridgeConfigFlow

    hass = await start_core(tmp_path)
    try:
        flow = SavanAIBridgeConfigFlow()
        flow.hass, flow.flow_id = hass, "new-flow"
        flow._quick_pair_api_base = backend.api_base

        async def broken(_marker):
            raise OSError("disk full")

        monkeypatch.setattr(pairing_journal(hass), "async_record", broken)
        assert not await flow._journal_candidate(SimpleNamespace(session_id="wps_new", commit_token="t" * 43))
        assert backend.actions() == ["cancel"]
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_commits_only_once_ha_has_written_the_entry_to_disk(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        await pairing_journal(hass).async_record(marker(backend))
        entry = core_entry("entry-unsaved", data={"pairing_session_id": "wps_new"})
        hass.config_entries._entries[entry.entry_id] = entry  # In memory, save not yet written.
        async_schedule_commit(hass, entry)
        await asyncio.sleep(0.5)
        assert backend.calls == []
        hass.config_entries._async_schedule_save()
        await hass.async_block_till_done(wait_background_tasks=True)
        assert backend.actions() == ["commit"]
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_a_bare_404_is_retried_not_treated_as_a_refusal(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        entry = add_entry(hass, core_entry("entry-rollout", data={"environment": "uat"}, options={"pairing_session_id": "wps_new"}))
        await pairing_journal(hass).async_record(marker(backend, flow_manager="options", entry_id=entry.entry_id,
                                                        previous={"options": {"pairing_session_id": "wps_old"}, "title": "t", "unique_id": "uat"}))
        # An instance or proxy without the commit route during a rollout.
        backend.commit_replies = [(404, {"message": "Not Found"})]
        await drive(hass, entry)
        assert backend.actions() == ["commit", "commit"]
        assert entry.options["pairing_session_id"] == "wps_new"
    finally:
        await hass.async_stop(force=True)


async def chained_repair(hass, backend, *cancels, depth=2):
    """Committed u0, then re-paired ``depth`` times without any commit getting through."""
    entry = add_entry(hass, core_entry("entry-chain", data={"environment": "uat"},
                                       options={"pairing_session_id": "wps_s0", "mqtt_username": "u0"}))
    journal = pairing_journal(hass)
    for level in range(1, depth + 1):
        await journal.async_record(marker(backend, f"wps_s{level}", flow_manager="options", entry_id=entry.entry_id),
                                   replacing=entry)
        hass.config_entries.async_update_entry(entry, options={"pairing_session_id": f"wps_s{level}", "mqtt_username": f"u{level}"})
    backend.cancel_replies = list(cancels)
    backend.commit_replies = [(410, {"ok": False, "error": "pairing_commit_rejected", "state": "abandoned"})]
    await drive(hass, entry)
    return entry


COMMITTED = (409, {"ok": False, "error": "pairing_already_committed", "state": "committed"})
CANCELLED = (200, {"ok": True, "state": "cancelled"})


@pytest.mark.asyncio
async def test_refusal_after_a_cancelled_uncommitted_repair_restores_the_last_committed_connection(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        entry = await chained_repair(hass, backend, CANCELLED)
        # S1 was never committed (and is now revoked): S0 is what the backend kept.
        assert backend.actions() == ["cancel", "commit"]
        assert entry.options["mqtt_username"] == "u0"
        await async_resolve_orphans(hass)
        assert await pairing_journal(hass).async_markers() == []
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_refusal_after_a_repair_that_did_commit_restores_that_repair(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        entry = await chained_repair(hass, backend, COMMITTED)
        # S1's commit reached the backend before HA moved on: S0 is revoked there.
        assert entry.options["mqtt_username"] == "u1"
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancels,restored", [
    ((COMMITTED, CANCELLED), "u1"),   # S1 committed, S2 never did.
    ((CANCELLED, CANCELLED), "u0"),   # Neither did.
    ((COMMITTED, COMMITTED), "u2"),   # Both did.
])
async def test_three_unresolved_repairs_restore_what_the_backend_kept(tmp_path, backend, cancels, restored):
    hass = await start_core(tmp_path)
    try:
        entry = await chained_repair(hass, backend, *cancels, depth=3)
        assert backend.actions() == ["cancel", "cancel", "commit"]
        assert entry.options["mqtt_username"] == restored
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_a_predecessor_resolving_out_of_order_is_still_inherited(tmp_path, backend):
    hass = await start_core(tmp_path)
    try:
        entry = add_entry(hass, core_entry("entry-order", data={"environment": "uat"},
                                           options={"pairing_session_id": "wps_s0", "mqtt_username": "u0"}))
        journal = pairing_journal(hass)
        for level in (1, 2, 3):
            await journal.async_record(marker(backend, f"wps_s{level}", flow_manager="options", entry_id=entry.entry_id),
                                       replacing=entry)
            hass.config_entries.async_update_entry(entry, options={"pairing_session_id": f"wps_s{level}", "mqtt_username": f"u{level}"})
        # S2 is found never to have committed before S1 is found to have committed.
        await journal.async_resolve("wps_s2", committed=False)
        await journal.async_resolve("wps_s1", committed=True)
        [s3] = await journal.async_markers()
        assert s3["previous"]["options"]["mqtt_username"] == "u1" and "pending_session" not in s3
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_a_refusal_never_overwrites_a_newer_repair_saved_meanwhile(tmp_path, backend, monkeypatch):
    from seenzus_bridge import pairing_bootstrap

    hass = await start_core(tmp_path)
    try:
        entry = add_entry(hass, core_entry("entry-newer", data={"environment": "uat"},
                                           options={"pairing_session_id": "wps_s0", "mqtt_username": "u0"}))
        journal = pairing_journal(hass)
        await journal.async_record(marker(backend, "wps_s1", flow_manager="options", entry_id=entry.entry_id), replacing=entry)
        hass.config_entries.async_update_entry(entry, options={"pairing_session_id": "wps_s1", "mqtt_username": "u1"})
        real = pairing_commit.finalize_web_pairing

        async def refused_while_user_repairs(**kwargs):
            result = await real(**kwargs)
            # The user finishes another re-pair while this commit is answered.
            hass.config_entries.async_update_entry(entry, options={"pairing_session_id": "wps_s2", "mqtt_username": "u2"})
            return result

        monkeypatch.setattr(pairing_commit, "finalize_web_pairing", refused_while_user_repairs)
        backend.commit_replies = [(410, {"ok": False, "error": "pairing_commit_rejected", "state": "abandoned"})]
        await drive(hass, entry)
        assert entry.options["mqtt_username"] == "u2"
    finally:
        await hass.async_stop(force=True)
