from __future__ import annotations

from dataclasses import dataclass
import json
import logging

import pytest

from seenzus_bridge.pairing_bootstrap import (
    create_web_pairing_session,
    exchange_web_pairing_callback_code,
    fetch_web_pairing_session_status,
)


@dataclass
class _FakeResponse:
    status: int
    payload: dict

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def json(self, content_type=None):
        return dict(self.payload)

    async def text(self):
        return json.dumps(self.payload)


class _FakeClientSession:
    def __init__(self, *, timeout=None):
        self.timeout = timeout
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def post(self, url: str, *, json: dict, headers: dict | None = None):
        self.calls.append({"method": "POST", "url": url, "json": dict(json), "headers": headers or {}})
        if url.endswith("/integrations/ha/web-pairing/callback/exchange"):
            return _FakeResponse(
                200,
                {
                    "ok": True,
                    "sessionId": "wps_abc123",
                    "bridgeId": "ha-web-bridge",
                    "sourceId": "ha-bridge-ha-web-bridge",
                    "sourceType": "haos_bridge",
                    "sourceName": "HA Bridge",
                    "configSource": "web_pair",
                    "confirmedAt": "2026-04-20T12:01:22Z",
                    "appReturnUrl": "seenzus://pairing/done",
                    "mqtt": {
                        "host": "broker.example.com",
                        "port": 1883,
                        "username": "user-1",
                        "password": "pass-1",
                        "topicRoot": "savant/v2",
                        "bridgeId": "ha-web-bridge",
                        "configSource": "web_pair",
                    },
                },
            )
        if url.endswith("/integrations/ha/web-pairing/session"):
            return _FakeResponse(
                200,
                {
                    "ok": True,
                    "sessionId": "wps_abc123",
                    "pairingPageUrl": "https://app.seenzus.xxx/web-pairing/wps_abc123",
                    "appReturnUrl": "seenzus://pairing/done",
                    "expiresAt": "2026-04-20T12:05:00Z",
                    "status": "pending",
                },
            )
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url: str, *, headers: dict | None = None):
        self.calls.append({"method": "GET", "url": url, "headers": headers or {}})
        if "/integrations/ha/web-pairing/session/" in url:
            return _FakeResponse(
                200,
                {
                    "ok": True,
                    "sessionId": "wps_abc123",
                    "status": "confirmed",
                    "bound": True,
                    "bridgeId": "ha-web-bridge",
                    "sourceId": "ha-bridge-ha-web-bridge",
                    "sourceType": "haos_bridge",
                    "sourceName": "HA Bridge",
                    "confirmedAt": "2026-04-20T12:01:22Z",
                    "appReturnUrl": "seenzus://pairing/done",
                    "mqtt": {
                        "host": "broker.example.com",
                        "port": 1883,
                        "username": "user-1",
                        "password": "pass-1",
                        "topicRoot": "savant/v2",
                        "bridgeId": "ha-web-bridge",
                    },
                },
            )
        raise AssertionError(f"unexpected GET {url}")


class _GatewayWrappedClientSession(_FakeClientSession):
    def post(self, url: str, *, json: dict, headers: dict | None = None):
        self.calls.append({"method": "POST", "url": url, "json": dict(json), "headers": headers or {}})
        return _FakeResponse(
            200,
            {
                "data": {
                    "ok": True,
                    "sessionId": "wps_wrapped",
                    "pairingPageUrl": "https://app.seenzus.xxx/web-pairing/wps_wrapped",
                    "expiresAt": "2026-04-20T12:05:00Z",
                    "status": "pending",
                },
                "code": 0,
                "message": "ok",
                "isSuccess": True,
            },
        )


class _InvalidBodyClientSession(_FakeClientSession):
    def post(self, url: str, *, json: dict, headers: dict | None = None):
        self.calls.append({"method": "POST", "url": url, "json": dict(json), "headers": headers or {}})
        return _FakeResponse(
            400,
            {
                "data": None,
                "code": 1,
                "message": "invalid_body",
                "isSuccess": False,
            },
        )


class _NestedSecretClientSession(_FakeClientSession):
    def post(self, url: str, *, json: dict, headers: dict | None = None):
        self.calls.append({"method": "POST", "url": url, "json": dict(json), "headers": headers or {}})
        return _FakeResponse(
            200,
            {
                "ok": True,
                "sessionId": "wps_nested",
                "pairingPageUrl": "https://app.savant.xxx/web-pairing/wps_nested",
                "status": "pending",
                "password": {"value": "leakme"},
                "tokens": ["leak1", "leak2"],
            },
        )


class _DeepNestedSecretClientSession(_FakeClientSession):
    def post(self, url: str, *, json: dict, headers: dict | None = None):
        self.calls.append({"method": "POST", "url": url, "json": dict(json), "headers": headers or {}})
        return _FakeResponse(
            200,
            {
                "ok": True,
                "sessionId": "wps_deep",
                "pairingPageUrl": "https://app.savant.xxx/web-pairing/wps_deep",
                "status": "pending",
                "password": {"creds": {"value": "leakme"}},
            },
        )


class _ArrayOfObjectsSecretClientSession(_FakeClientSession):
    def post(self, url: str, *, json: dict, headers: dict | None = None):
        self.calls.append({"method": "POST", "url": url, "json": dict(json), "headers": headers or {}})
        return _FakeResponse(
            200,
            {
                "ok": True,
                "sessionId": "wps_arr",
                "pairingPageUrl": "https://app.savant.xxx/web-pairing/wps_arr",
                "status": "pending",
                "tokens": [{"value": "leakA"}, {"nested": {"value": "leakB"}}],
            },
        )


class _SecretBearingMessageClientSession(_FakeClientSession):
    def post(self, url: str, *, json: dict, headers: dict | None = None):
        self.calls.append({"method": "POST", "url": url, "json": dict(json), "headers": headers or {}})
        return _FakeResponse(
            400,
            {
                "data": None,
                "code": 1,
                "message": 'mqtt auth rejected: "password": "pass-1"',
                "isSuccess": False,
            },
        )


@pytest.mark.asyncio
async def test_create_web_pairing_session_posts_expected_payload(monkeypatch) -> None:
    fake_session = _FakeClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await create_web_pairing_session(
        api_base="https://app.seenzus.xxx/api",
        bridge_name="SavanAI Bridge",
        bridge_version="3.0.7",
        ha_version="2026.3.0",
        redirect_uri="http://homeassistant.local:8123/auth/external/callback",
        state="jwt-state",
    )

    assert result.ok is True
    assert result.session_id == "wps_abc123"
    assert result.pairing_page_url == "https://app.seenzus.xxx/web-pairing/wps_abc123"
    assert result.app_return_url == "seenzus://pairing/done"
    assert result.request_url == "https://app.seenzus.xxx/api/integrations/ha/web-pairing/session"
    assert result.http_status == 200
    assert fake_session.calls[0]["url"] == "https://app.seenzus.xxx/api/integrations/ha/web-pairing/session"
    assert fake_session.calls[0]["json"]["redirectUri"] == "http://homeassistant.local:8123/auth/external/callback"
    assert fake_session.calls[0]["json"]["state"] == "jwt-state"


@pytest.mark.asyncio
async def test_create_web_pairing_session_accepts_gateway_wrapped_response(monkeypatch) -> None:
    fake_session = _GatewayWrappedClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await create_web_pairing_session(
        api_base="https://app.seenzus.xxx/api",
        bridge_name="SavanAI Bridge",
        bridge_version="3.0.7",
        ha_version="2026.3.0",
    )

    assert result.ok is True
    assert result.session_id == "wps_wrapped"
    assert result.message == "ok"
    assert result.pairing_page_url == "https://app.seenzus.xxx/web-pairing/wps_wrapped"


@pytest.mark.asyncio
async def test_create_web_pairing_session_returns_diagnostics_on_backend_error(monkeypatch) -> None:
    fake_session = _InvalidBodyClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await create_web_pairing_session(
        api_base="https://app.seenzus.xxx/api",
        bridge_name="SavanAI Bridge",
        bridge_version="3.0.7",
        ha_version="2026.3.0",
    )

    assert result.ok is False
    assert result.http_status == 400
    assert result.error_code == "invalid_body"
    assert result.message == "invalid_body"
    assert result.request_url == "https://app.seenzus.xxx/api/integrations/ha/web-pairing/session"
    assert "invalid_body" in (result.response_summary or "")


@pytest.mark.asyncio
async def test_fetch_web_pairing_session_status_reads_backend_status(monkeypatch) -> None:
    fake_session = _FakeClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await fetch_web_pairing_session_status(
        api_base="https://api.seenzus.xxx",
        session_id="wps_abc123",
    )

    assert result.ok is True
    assert result.status == "confirmed"
    assert result.bound is True
    assert result.bridge_id == "ha-web-bridge"
    assert result.source_id == "ha-bridge-ha-web-bridge"
    assert result.source_type == "haos_bridge"
    assert result.source_name == "HA Bridge"
    assert result.app_return_url == "seenzus://pairing/done"
    assert result.mqtt == {
        "host": "broker.example.com",
        "port": 1883,
        "username": "user-1",
        "password": "pass-1",
        "topicRoot": "savant/v2",
        "bridgeId": "ha-web-bridge",
    }
    assert fake_session.calls[0]["url"] == "https://api.seenzus.xxx/integrations/ha/web-pairing/session/wps_abc123"


@pytest.mark.asyncio
async def test_exchange_web_pairing_callback_code_posts_expected_payload(monkeypatch) -> None:
    fake_session = _FakeClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await exchange_web_pairing_callback_code(
        api_base="https://api.seenzus.xxx",
        code="cb_code_123",
        state="jwt-state",
        session_id="wps_abc123",
    )

    assert result.ok is True
    assert result.session_id == "wps_abc123"
    assert result.bridge_id == "ha-web-bridge"
    assert result.source_id == "ha-bridge-ha-web-bridge"
    assert result.source_type == "haos_bridge"
    assert result.source_name == "HA Bridge"
    assert result.config_source == "web_pair"
    assert result.app_return_url == "seenzus://pairing/done"
    assert result.mqtt == {
        "host": "broker.example.com",
        "port": 1883,
        "username": "user-1",
        "password": "pass-1",
        "topicRoot": "savant/v2",
        "bridgeId": "ha-web-bridge",
        "configSource": "web_pair",
    }
    assert fake_session.calls[0]["url"] == "https://api.seenzus.xxx/integrations/ha/web-pairing/callback/exchange"
    assert fake_session.calls[0]["json"] == {
        "code": "cb_code_123",
        "state": "jwt-state",
        "sessionId": "wps_abc123",
    }


@pytest.mark.asyncio
async def test_response_summary_redacts_mqtt_password(monkeypatch) -> None:
    fake_session = _FakeClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await fetch_web_pairing_session_status(
        api_base="https://api.savant.xxx",
        session_id="wps_abc123",
    )

    assert result.ok is True
    # Parsed payload keeps the real secret for config use...
    assert result.mqtt is not None
    assert result.mqtt["password"] == "pass-1"
    # ...but the diagnostic summary never carries it.
    assert "pass-1" not in (result.response_summary or "")
    assert "broker.example.com" in (result.response_summary or "")


@pytest.mark.asyncio
async def test_pairing_logs_never_carry_mqtt_password(monkeypatch, caplog) -> None:
    fake_session = _FakeClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    with caplog.at_level(logging.DEBUG):
        result = await exchange_web_pairing_callback_code(
            api_base="https://api.savant.xxx",
            code="cb_code_123",
            state="jwt-state",
            session_id="wps_abc123",
        )

    assert result.mqtt is not None
    assert result.mqtt["password"] == "pass-1"
    assert "pass-1" not in caplog.text


@pytest.mark.asyncio
async def test_response_summary_masks_object_and_array_secret_values(monkeypatch) -> None:
    fake_session = _NestedSecretClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await create_web_pairing_session(
        api_base="https://app.savant.xxx/api",
        bridge_name="SavanAI Bridge",
        bridge_version="3.0.7",
        ha_version="2026.3.0",
    )

    assert result.ok is True
    summary = result.response_summary or ""
    # Object value under "password" and array value under "tokens" are masked whole.
    assert "leakme" not in summary
    assert "leak1" not in summary
    assert "leak2" not in summary
    assert "wps_nested" in summary


@pytest.mark.asyncio
async def test_response_summary_masks_2level_nested_object_secret(monkeypatch) -> None:
    fake_session = _DeepNestedSecretClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await create_web_pairing_session(
        api_base="https://app.savant.xxx/api",
        bridge_name="SavanAI Bridge",
        bridge_version="3.0.7",
        ha_version="2026.3.0",
    )

    assert result.ok is True
    summary = result.response_summary or ""
    # A 2-level nested object under "password" is masked whole — no inner leak.
    assert "leakme" not in summary
    assert "wps_deep" in summary


@pytest.mark.asyncio
async def test_response_summary_masks_array_of_objects_secret(monkeypatch) -> None:
    fake_session = _ArrayOfObjectsSecretClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await create_web_pairing_session(
        api_base="https://app.savant.xxx/api",
        bridge_name="SavanAI Bridge",
        bridge_version="3.0.7",
        ha_version="2026.3.0",
    )

    assert result.ok is True
    summary = result.response_summary or ""
    # An array-of-objects under "tokens" is masked whole — no inner value leaks.
    assert "leakA" not in summary
    assert "leakB" not in summary
    assert "wps_arr" in summary


@pytest.mark.asyncio
async def test_failure_message_and_error_code_are_redacted(monkeypatch) -> None:
    fake_session = _SecretBearingMessageClientSession()
    monkeypatch.setattr(
        "seenzus_bridge.pairing_bootstrap.aiohttp.ClientSession",
        lambda *args, **kwargs: fake_session,
    )

    result = await create_web_pairing_session(
        api_base="https://app.savant.xxx/api",
        bridge_name="SavanAI Bridge",
        bridge_version="3.0.7",
        ha_version="2026.3.0",
    )

    assert result.ok is False
    assert result.http_status == 400
    # The server-echoed secret never reaches message/error_code diagnostics.
    assert "pass-1" not in (result.message or "")
    assert "pass-1" not in (result.error_code or "")
    assert "***" in (result.message or "")
