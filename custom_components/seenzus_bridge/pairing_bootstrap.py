"""HTTP client for HA bridge quick pairing."""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import re

import aiohttp

from .pairing_models import (
    PairingCallbackResult,
    PairingSessionCreateResult,
    PairingStatusResult,
)

_LOGGER = logging.getLogger(__name__)

# JSON-aware, case-insensitive: masks the value of any key whose name contains
# password/token/secret/authorization, e.g. "password": "pass-1" -> "password": "***".
# The regex only locates the key+colon; the VALUE span is consumed procedurally by
# _consume_secret_value so arbitrarily deep object/array values ({...}/[...], any
# nesting) are masked whole — a regex value branch can only balance one level and
# leaks the trailing content of 2+-level nests.
_SECRET_KEY_PATTERN = re.compile(
    r'"[^"]*(?:password|token|secret|authorization)[^"]*"\s*:\s*',
    re.IGNORECASE,
)


def _consume_secret_value(text: str, start: int) -> int:
    """Return the index just past the value span beginning at ``start``.

    Handles three value shapes:
    * quoted string — consumed honoring backslash escapes;
    * object/array — consumed to the BALANCED closing brace/bracket via a depth
      counter, skipping over quoted strings (and their escapes) so braces inside
      strings don't perturb the count;
    * bare scalar — consumed up to the next delimiter ``[,}\\]`` or whitespace.
    """
    n = len(text)
    if start >= n:
        return start
    ch = text[start]
    if ch == '"':
        i = start + 1
        while i < n:
            c = text[i]
            if c == "\\":
                i += 2
                continue
            if c == '"':
                return i + 1
            i += 1
        return n
    if ch in "{[":
        opens = "{["
        closes = "}]"
        depth = 0
        i = start
        in_str = False
        while i < n:
            c = text[i]
            if in_str:
                if c == "\\":
                    i += 2
                    continue
                if c == '"':
                    in_str = False
                i += 1
                continue
            if c == '"':
                in_str = True
            elif c in opens:
                depth += 1
            elif c in closes:
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return n
    # Bare scalar: stop at a delimiter or whitespace.
    i = start
    while i < n and text[i] not in ",}]" and not text[i].isspace():
        i += 1
    return i


def _redact_secrets(text: str) -> str:
    """Mask values of secret-bearing keys in diagnostic text."""
    source = str(text or "")
    out: list[str] = []
    pos = 0
    for match in _SECRET_KEY_PATTERN.finditer(source):
        if match.start() < pos:
            # The previous value span swallowed this key (nested secret); skip it.
            continue
        out.append(source[pos:match.end()])
        value_end = _consume_secret_value(source, match.end())
        out.append('"***"')
        pos = value_end
    out.append(source[pos:])
    return "".join(out)


def _summarize_response_body(text: str, *, limit: int = 1000) -> str:
    """Keep diagnostics readable without dumping very large responses.

    Single choke point for all body diagnostics (INFO logs + response_summary
    flowing into persistent notifications) — secrets are redacted here so they
    never leave the parsed payload path.
    """
    compact = _redact_secrets(" ".join(str(text or "").split()))
    return compact[:limit] + ("..." if len(compact) > limit else "")


def _unwrap_gateway_response(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    payload = data.get("data")
    if isinstance(payload, dict) and {"code", "message", "isSuccess"}.intersection(data):
        return payload
    return data


def _read_error_code(data: object, fallback: str) -> str:
    # Redacted: the value flows into logs and persistent notifications via
    # _diagnostic_from_result and may carry server-echoed secrets.
    if not isinstance(data, dict):
        return fallback
    raw = data.get("error") or data.get("message") or data.get("code") or fallback
    return _redact_secrets(raw) if isinstance(raw, str) else str(raw)


def _read_message(data: object, fallback: str) -> str:
    # Redacted: the value flows into logs and persistent notifications via
    # _diagnostic_from_result and may carry server-echoed secrets.
    if not isinstance(data, dict):
        return fallback
    raw = data.get("message") or data.get("error") or fallback
    return _redact_secrets(raw) if isinstance(raw, str) else str(raw)


def _read_app_return_url(payload: dict) -> str | None:
    """Return the backend-supplied app return URL, if any.

    The app passes a deep link / page URL it wants the user bounced back to
    after binding; the backend echoes it on the session and pairing responses.
    Tolerates a few key spellings; validation/sanitization happens in the flow.
    """
    if not isinstance(payload, dict):
        return None
    for key in ("appReturnUrl", "appReturnUri", "returnUrl", "returnUri"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _read_ok(payload: dict, data: object) -> bool:
    """Effective success flag: payload `ok`, falling back to the gateway `isSuccess`.

    NOTE: the adapters' success tails derive `error_code=` via `_success_error_code`,
    whose ok-check is intentionally payload-only — it does NOT share this helper's
    gateway-`isSuccess` fallback. The two derivations are deliberately different;
    unifying them changes wire behavior.
    """
    return bool(payload.get("ok", data.get("isSuccess", True) if isinstance(data, dict) else True))


def _success_error_code(payload: dict, data: object) -> str | None:
    """`error_code` for an adapter success tail: None unless the payload says not-ok.

    Deliberately payload-only (`payload.get("ok", True)`) — NOT `_read_ok`'s
    gateway-`isSuccess` fallback. The two derivations differ intentionally and
    unifying them changes wire behavior.
    """
    return None if bool(payload.get("ok", True)) else _read_error_code(data, "not_ok")


@dataclass(slots=True)
class _PairingApiOutcome:
    """Internal outcome of one pairing-API HTTP round trip."""

    http_status: int | None = None
    data: object = None
    payload: dict | None = None
    response_summary: str | None = None
    error: Exception | None = None


async def _call_pairing_api(
    method: str,
    url: str,
    *,
    json_body: dict[str, object] | None = None,
    timeout_seconds: int,
    log_label: str,
    request_log_suffix: str = "",
) -> _PairingApiOutcome:
    """Shared HTTP scaffold for the three pairing endpoints.

    Performs the request, logs request/response with redacted bodies, parses
    and unwraps the gateway envelope. Exceptions are captured (not raised) so
    adapters can map them to their result-specific failure defaults.
    """
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            _LOGGER.info("%s request: %s %s%s", log_label, method, url, request_log_suffix)
            if method == "POST":
                request_ctx = session.post(url, json=json_body, headers={})
            else:
                request_ctx = session.get(url, headers={})
            async with request_ctx as response:
                text = await response.text()
                summary = _summarize_response_body(text)
                _LOGGER.info(
                    "%s response: status=%s body=%s",
                    log_label,
                    response.status,
                    summary,
                )
                data = json.loads(text) if text else None
                payload = _unwrap_gateway_response(data)
                return _PairingApiOutcome(
                    http_status=response.status,
                    data=data,
                    payload=payload,
                    response_summary=summary,
                )
    except Exception as err:  # noqa: BLE001
        _LOGGER.warning("%s request failed: %s %s error=%s", log_label, method, url, err)
        return _PairingApiOutcome(error=err, response_summary=str(err))


def _is_failure(outcome: _PairingApiOutcome) -> bool:
    """True when the call raised, returned >=400, or carried no dict payload."""
    return (
        outcome.error is not None
        or (outcome.http_status or 0) >= 400
        or not isinstance(outcome.payload, dict)
    )


def _failure_fields(outcome: _PairingApiOutcome) -> dict[str, object]:
    """Failure kwargs shared by all three result types.

    Exception branch: http_status=None, error_code=type(err).__name__.
    HTTP/shape branch: message/error_code read from the body, http_<status> fallback.
    """
    if outcome.error is not None:
        return {
            "message": str(outcome.error),
            "http_status": None,
            "error_code": type(outcome.error).__name__,
            "response_summary": str(outcome.error),
        }
    return {
        "message": _read_message(outcome.data, f"http_{outcome.http_status}"),
        "http_status": outcome.http_status,
        "error_code": _read_error_code(outcome.data, f"http_{outcome.http_status}"),
        "response_summary": outcome.response_summary,
    }


async def create_web_pairing_session(
    *,
    api_base: str,
    bridge_name: str,
    bridge_version: str,
    ha_version: str,
    redirect_uri: str | None = None,
    state: str | None = None,
    ha_instance_id: str | None = None,
    timeout_seconds: int = 10,
) -> PairingSessionCreateResult:
    """Create a web-based pairing session and return the page URL.

    ``ha_instance_id`` is HA's stable per-install UUID. The backend uses it to
    recognise a re-pair of the same HA install and reuse/supersede the existing
    bridge instead of spawning a duplicate (see docs/HANDOFF_REPAIR_DEDUP). It is
    optional: omitted when unresolvable, and older backends simply ignore it.
    """

    base = api_base.strip().rstrip("/")
    url = f"{base}/integrations/ha/web-pairing/session"
    body: dict[str, object] = {
        "bridgeName": bridge_name,
        "bridgeVersion": bridge_version,
        "platform": "homeassistant",
        "haVersion": ha_version,
        "redirectUri": redirect_uri,
        "state": state,
    }
    if ha_instance_id:
        body["haInstanceId"] = ha_instance_id

    outcome = await _call_pairing_api(
        "POST",
        url,
        json_body=body,
        timeout_seconds=timeout_seconds,
        log_label="Quick pair create session",
    )
    if _is_failure(outcome):
        return PairingSessionCreateResult(
            ok=False,
            session_id="",
            status="session_failed",
            request_url=url,
            **_failure_fields(outcome),
        )

    data = outcome.data
    payload = outcome.payload
    return PairingSessionCreateResult(
        ok=_read_ok(payload, data),
        session_id=str(payload.get("sessionId", "")),
        pairing_page_url=str(
            payload.get("pairingPageUrl")
            or payload.get("authorizeUrl")
            or payload.get("pairingUrl")
            or ""
        ),
        app_return_url=_read_app_return_url(payload),
        expires_at=str(payload.get("expiresAt", "")) or None,
        status=str(payload.get("status", "")),
        message=_read_message(data, "ok"),
        request_url=url,
        http_status=outcome.http_status,
        error_code=_success_error_code(payload, data),
        response_summary=outcome.response_summary,
    )


async def fetch_web_pairing_session_status(
    *,
    api_base: str,
    session_id: str,
    timeout_seconds: int = 10,
) -> PairingStatusResult:
    """Query final web-pairing session state and MQTT config."""

    base = api_base.strip().rstrip("/")
    url = f"{base}/integrations/ha/web-pairing/session/{session_id}"

    outcome = await _call_pairing_api(
        "GET",
        url,
        timeout_seconds=timeout_seconds,
        log_label="Quick pair status",
    )
    if _is_failure(outcome):
        return PairingStatusResult(
            ok=False,
            status="status_error",
            session_id=session_id,
            request_url=url,
            **_failure_fields(outcome),
        )

    data = outcome.data
    payload = outcome.payload
    return PairingStatusResult(
        ok=_read_ok(payload, data),
        status=str(payload.get("status", "")),
        session_id=str(payload.get("sessionId", session_id)),
        bound=bool(payload.get("bound", False)),
        bridge_id=str(payload.get("bridgeId", "")) or None,
        source_id=str(payload.get("sourceId", "")) or None,
        source_type=str(payload.get("sourceType", "")) or None,
        source_name=str(payload.get("sourceName", "")) or None,
        expires_at=str(payload.get("expiresAt", "")) or None,
        confirmed_at=str(payload.get("confirmedAt", "")) or None,
        app_return_url=_read_app_return_url(payload),
        mqtt=payload.get("mqtt") if isinstance(payload.get("mqtt"), dict) else None,
        message=_read_message(data, "ok"),
        request_url=url,
        http_status=outcome.http_status,
        error_code=_success_error_code(payload, data),
        response_summary=outcome.response_summary,
    )


async def exchange_web_pairing_callback_code(
    *,
    api_base: str,
    code: str,
    state: str,
    session_id: str | None = None,
    timeout_seconds: int = 10,
) -> PairingCallbackResult:
    """Exchange a callback code for finalized MQTT bridge config."""

    base = api_base.strip().rstrip("/")
    url = f"{base}/integrations/ha/web-pairing/callback/exchange"
    body: dict[str, object] = {
        "code": code,
        "state": state,
        "sessionId": session_id,
    }

    outcome = await _call_pairing_api(
        "POST",
        url,
        json_body=body,
        timeout_seconds=timeout_seconds,
        log_label="Quick pair callback exchange",
        request_log_suffix=f" session={session_id}",
    )
    if _is_failure(outcome):
        return PairingCallbackResult(
            ok=False,
            session_id=session_id or "",
            request_url=url,
            **_failure_fields(outcome),
        )

    data = outcome.data
    payload = outcome.payload
    return PairingCallbackResult(
        ok=_read_ok(payload, data),
        session_id=str(payload.get("sessionId", session_id or "")),
        bridge_id=str(payload.get("bridgeId", "")) or None,
        source_id=str(payload.get("sourceId", "")) or None,
        source_type=str(payload.get("sourceType", "")) or None,
        source_name=str(payload.get("sourceName", "")) or None,
        config_source=str(payload.get("configSource", "")) or None,
        confirmed_at=str(payload.get("confirmedAt", "")) or None,
        app_return_url=_read_app_return_url(payload),
        mqtt=payload.get("mqtt") if isinstance(payload.get("mqtt"), dict) else None,
        message=_read_message(data, "ok"),
        request_url=url,
        http_status=outcome.http_status,
        error_code=_success_error_code(payload, data),
        response_summary=outcome.response_summary,
    )
