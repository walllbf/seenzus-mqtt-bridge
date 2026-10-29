"""Durable two-phase pairing on the HA side.

The backend issues a candidate credential at code exchange but changes its
Source, and retires the previous credential, only when HA commits. A candidate
is journaled in HA storage before any entry that uses it can be saved, and is
then resolved from durable state alone:

- an entry carries the candidate's session id: commit it, retrying until the
  backend answers definitively;
- no entry carries it and its flow has ended: cancel it (best effort; the
  backend also expires an unconfirmed candidate on its own);
- the backend refuses the commit: restore the entry's previous connection on a
  re-pair, or report that a new entry must be paired again.

Both sides therefore keep the previous state unless both reach the new one,
across cancellation, lost responses, network outages and restarts of either.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.storage import Store

from .const import CONF_PAIRING_SESSION_ID, DOMAIN, QUICK_PAIR_LAST_DIAGNOSTIC
from .pairing_bootstrap import finalize_web_pairing
from .quick_pair import (
    FLOW_MANAGER_OPTIONS,
    _clear_quick_pair_notifications,
    _format_quick_pair_diagnostic,
    _notify_app_return,
    _record_committed_return,
)
from .ui_text import async_prepare_ui_text, ui_text

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.pairing_commits"
STORAGE_VERSION = 1
_JOURNAL = "pairing_commit_journal"
# HA saves config entries with a delayed write. A commit is sent only once the
# entry with this candidate is read back from HA's own storage file, so a crash
# before it reaches disk also prevents the commit; after the restart the journal
# holds a candidate no entry carries, and it is cancelled.
SAVED_POLL_SECONDS = 1.0
UNSAVED_WARNING_POLLS = 300
CONFIG_ENTRIES_FILE = (".storage", "core.config_entries")
RETRY_DELAYS_SECONDS = (10.0, 30.0, 60.0, 120.0, 300.0, 600.0)
CANCEL_ATTEMPTS = 3


class PairingJournal:
    """Candidates awaiting commit or cancel, keyed by pairing session id."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, Any]] | None = None

    async def _loaded(self) -> dict[str, dict[str, Any]]:
        if self._data is None:
            stored = await self._store.async_load()
            self._data = dict(stored) if isinstance(stored, dict) else {}
        return self._data

    async def async_record(self, marker: dict[str, Any], replacing=None) -> None:
        """Persist immediately: the caller may save an entry right after.

        ``replacing`` is the entry a re-pair will overwrite. Refusing this
        candidate must restore the connection the backend kept, the last
        committed one. While that entry's own candidate is still unresolved,
        its outcome decides which one that is, so both are kept until it
        resolves. Reading and writing share one lock with resolution, so a
        predecessor cannot resolve unseen in between.
        """
        async with self._lock:
            data = await self._loaded()
            marker = dict(marker)
            if replacing is not None:
                current = {"options": dict(replacing.options), "title": replacing.title, "unique_id": replacing.unique_id}
                predecessor = data.get(_session_of(replacing) or "")
                if predecessor is None:
                    marker["previous"] = current
                else:
                    marker.update(previous=predecessor.get("previous"), pending_session=predecessor["session_id"],
                                  pending_previous=current)
            data[marker["session_id"]] = marker
            await self._store.async_save(data)

    async def async_resolve(self, session_id: str, committed: bool) -> None:
        """Remove a resolved candidate; candidates recorded on top of it learn what to restore."""
        async with self._lock:
            data = await self._loaded()
            resolved = data.pop(session_id, None)
            if resolved is None:
                return
            for marker in data.values():
                if marker.get("pending_session") != session_id:
                    continue
                pending_previous = marker.pop("pending_previous", None)
                del marker["pending_session"]
                if committed:
                    marker["previous"] = pending_previous
                else:
                    # It never took effect: inherit exactly what it was waiting on.
                    marker["previous"] = resolved.get("previous")
                    if resolved.get("pending_session"):
                        marker.update(pending_session=resolved["pending_session"],
                                      pending_previous=resolved.get("pending_previous"))
            await self._store.async_save(data)

    async def async_get(self, session_id: str) -> dict[str, Any] | None:
        async with self._lock:
            marker = (await self._loaded()).get(session_id)
            return dict(marker) if marker else None

    async def async_markers(self) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(marker) for marker in (await self._loaded()).values()]


def pairing_journal(hass: HomeAssistant) -> PairingJournal:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if _JOURNAL not in domain_data:
        domain_data[_JOURNAL] = PairingJournal(hass)
    return domain_data[_JOURNAL]


def _session_of(entry) -> str | None:
    return {**entry.data, **entry.options}.get(CONF_PAIRING_SESSION_ID) or None


def _entry_carrying(hass: HomeAssistant, marker: dict[str, Any]):
    for entry in hass.config_entries.async_entries(DOMAIN):
        if _session_of(entry) == marker["session_id"] and (not marker.get("entry_id") or entry.entry_id == marker["entry_id"]):
            return entry
    return None


def _flow_alive(hass: HomeAssistant, marker: dict[str, Any]) -> bool:
    manager = hass.config_entries.options if marker.get("flow_manager") == FLOW_MANAGER_OPTIONS else hass.config_entries.flow
    try:
        manager.async_get(marker.get("flow_id") or "")
    except UnknownFlow:
        return False
    return True


def spawn(hass: HomeAssistant, coro, name: str) -> asyncio.Task:
    return hass.async_create_background_task(coro, name)


def _read_saved_session(path: str, entry_id: str) -> str | None:
    """The session id HA has written to disk for this entry, if any."""
    try:
        with open(path, encoding="utf-8") as file:
            entries = json.load(file)["data"]["entries"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    for saved in entries:
        if saved.get("entry_id") == entry_id:
            return {**(saved.get("data") or {}), **(saved.get("options") or {})}.get(CONF_PAIRING_SESSION_ID)
    return None


async def _async_entry_saved(hass: HomeAssistant, entry_id: str, session_id: str) -> bool:
    path = hass.config.path(*CONFIG_ENTRIES_FILE)
    return await hass.async_add_executor_job(_read_saved_session, path, entry_id) == session_id


async def async_cancel_candidate(hass: HomeAssistant, marker: dict[str, Any]) -> None:
    """Release a candidate HA did not keep.

    An undelivered cancel leaves the record for the next orphan pass; the
    backend also expires the candidate on its own.
    """
    for attempt in range(CANCEL_ATTEMPTS):
        result = await finalize_web_pairing(api_base=marker["api_base"], session_id=marker["session_id"],
                                            commit_token=marker["token"], action="cancel")
        if result.ok or result.rejected:
            break
        if attempt + 1 < CANCEL_ATTEMPTS:
            await asyncio.sleep(RETRY_DELAYS_SECONDS[attempt])
    else:
        _LOGGER.warning("Pairing candidate %s could not be cancelled yet", marker["session_id"])
        return
    # A commit that reached the backend before HA moved on is already final.
    await pairing_journal(hass).async_resolve(marker["session_id"], committed=result.state == "committed")


async def async_resolve_orphans(hass: HomeAssistant, removing_entry_id: str | None = None) -> None:
    """Cancel candidates whose flow ended without an entry that carries them.

    Core calls an integration's removal hook before it drops the entry, so the
    entry being removed is excluded explicitly.
    """
    for marker in await pairing_journal(hass).async_markers():
        entry = _entry_carrying(hass, marker)
        if (entry is None or entry.entry_id == removing_entry_id) and not _flow_alive(hass, marker):
            await async_cancel_candidate(hass, marker)


def async_schedule_commit(hass: HomeAssistant, entry) -> None:
    """Drive this entry's pending commit, if any, for as long as it is loaded."""
    if not _session_of(entry):
        return
    task = spawn(hass, _async_commit(hass, entry.entry_id), f"{DOMAIN} pairing commit {entry.entry_id}")
    entry.async_on_unload(task.cancel)


async def _async_commit(hass: HomeAssistant, entry_id: str) -> None:
    journal = pairing_journal(hass)
    entry = hass.config_entries.async_get_entry(entry_id)
    session_id = _session_of(entry) if entry else None
    if session_id is None or await journal.async_get(session_id) is None:
        # Nothing to commit. A candidate this entry no longer carries (for
        # example after a refusal restored its previous connection) is released.
        await async_resolve_orphans(hass)
        return
    attempt = unsaved_polls = 0
    while True:
        marker = await journal.async_get(session_id)
        if marker is None:
            return
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or _session_of(entry) != session_id:
            # The entry was removed or re-paired again: nothing carries this
            # candidate any more.
            await async_resolve_orphans(hass)
            return
        if not await _async_entry_saved(hass, entry_id, session_id):
            unsaved_polls += 1
            if unsaved_polls == UNSAVED_WARNING_POLLS:
                _LOGGER.warning("Pairing commit for %s waits for HA to save the entry; check HA storage", session_id)
            await asyncio.sleep(SAVED_POLL_SECONDS)
            continue
        if marker.get("pending_session"):
            # Resolve the candidate this one replaced first: its outcome is what
            # a refusal of this one must restore.
            await async_resolve_orphans(hass)
            if (await journal.async_get(session_id) or {}).get("pending_session"):
                await asyncio.sleep(RETRY_DELAYS_SECONDS[0])
            continue
        result = await finalize_web_pairing(api_base=marker["api_base"], session_id=session_id,
                                            commit_token=marker["token"], action="commit")
        if result.ok:
            await journal.async_resolve(session_id, committed=True)
            from .config_flow import _sanitize_app_return_url  # Avoid an import cycle.

            return_url = _sanitize_app_return_url(result.app_return_url)
            if return_url:
                _record_committed_return(hass, session_id, return_url)
                _notify_app_return(hass, return_url, entry_id)
            else:
                _clear_quick_pair_notifications(hass, entry_id)
            return
        if result.rejected:
            if marker.get("previous") is None:
                await journal.async_resolve(session_id, committed=False)
            # With a previous connection the record stays until the restored
            # options are saved: the entry then no longer carries this
            # candidate and the next orphan pass releases it. A crash before
            # that save repeats the refused commit and the restore.
            await _async_refused(hass, entry, marker, result)
            return
        delay = RETRY_DELAYS_SECONDS[min(attempt, len(RETRY_DELAYS_SECONDS) - 1)]
        attempt += 1
        _LOGGER.warning("Pairing commit for %s not confirmed yet (%s); retrying in %ss",
                        session_id, result.error_code or result.http_status, delay)
        await asyncio.sleep(delay)


async def _async_refused(hass: HomeAssistant, entry, marker: dict[str, Any], result) -> None:
    """The backend kept its previous Source and credentials; make HA match."""
    try:
        await async_prepare_ui_text(hass)
    except Exception:  # noqa: BLE001 - English fallback; the restore must still happen.
        _LOGGER.debug("pairing refusal text unavailable", exc_info=True)
    previous = marker.get("previous")
    message = ui_text(hass, "pairing_restored_message" if previous is not None else "pairing_rejected_message")
    diagnostic = _format_quick_pair_diagnostic({
        key: str(value) for key, value in {
            "url": result.request_url, "http_status": result.http_status,
            "error_code": result.error_code, "message": result.message,
        }.items() if value
    })
    _LOGGER.warning("Pairing commit %s refused: %s", marker["session_id"], diagnostic)
    # No awaits from here: restoring the options schedules a reload that
    # cancels this task at its next suspension point.
    if previous is not None:
        if _session_of(entry) != marker["session_id"]:
            # Re-paired again while this refusal was in flight: the newer
            # candidate decides now; this one is left to the orphan pass.
            return
        hass.config_entries.async_update_entry(entry, options=previous["options"], title=previous["title"],
                                               unique_id=previous.get("unique_id"))
    hass.data.setdefault(DOMAIN, {})[f"{QUICK_PAIR_LAST_DIAGNOSTIC}.{entry.entry_id}"] = f"pairing_commit_rejected | {diagnostic}"
    try:
        persistent_notification.async_create(hass, f"{message}\n\n{diagnostic}", title=ui_text(hass, "pairing_unconfirmed_title"),
                                             notification_id=f"{DOMAIN}_pairing_unconfirmed.{entry.entry_id}")
    except Exception:  # noqa: BLE001
        _LOGGER.debug("pairing refusal notification failed", exc_info=True)
