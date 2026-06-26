"""Data models for HA bridge quick pairing."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class PairingSessionCreateResult:
    """Session returned by the quick-pair session API."""

    ok: bool
    session_id: str
    pairing_page_url: str = ""
    app_return_url: str | None = None
    expires_at: str | None = None
    status: str = ""
    message: str | None = None
    request_url: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    response_summary: str | None = None


@dataclass(slots=True)
class PairingCallbackResult:
    """Callback code exchange result returned by the backend."""

    ok: bool
    session_id: str
    bridge_id: str | None = None
    source_id: str | None = None
    source_type: str | None = None
    source_name: str | None = None
    config_source: str | None = None
    confirmed_at: str | None = None
    app_return_url: str | None = None
    mqtt: dict[str, object] | None = None
    message: str | None = None
    request_url: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    response_summary: str | None = None


@dataclass(slots=True)
class PairingStatusResult:
    """Pairing session status returned by the backend."""

    ok: bool
    status: str
    session_id: str | None = None
    bound: bool = False
    bridge_id: str | None = None
    source_id: str | None = None
    source_type: str | None = None
    source_name: str | None = None
    expires_at: str | None = None
    confirmed_at: str | None = None
    app_return_url: str | None = None
    mqtt: dict[str, object] | None = None
    message: str | None = None
    request_url: str | None = None
    http_status: int | None = None
    error_code: str | None = None
    response_summary: str | None = None
