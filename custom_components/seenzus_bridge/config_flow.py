"""Config Flow - seenzus Bridge UI 配置."""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant import config_entries
from homeassistant import data_entry_flow as data_entry_flow_module
from homeassistant.core import callback
from homeassistant.helpers.network import NoURLAvailableError
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    BRIDGE_VERSION,
    CONFIG_SOURCE_MANUAL,
    CONFIG_SOURCE_WEB_PAIR,
    CONF_ADVANCED_SETTINGS,
    CONF_BRIDGE_ID,
    CONF_CONFIG_SOURCE,
    CONF_ALLOW_DANGEROUS_SERVICES,
    CONF_ENABLE_STATE_EVENTS,
    CONF_ENABLE_TEMPLATE_API,
    CONF_EXPOSE_FULL_CONFIG,
    CONF_MQTT_HOST,
    CONF_MQTT_PASSWORD,
    CONF_MQTT_PORT,
    CONF_MQTT_USERNAME,
    CONF_PAIRING_API_BASE,
    CONF_PAIRING_BOUND_AT,
    CONF_PAIRING_MODE,
    CONF_PAIRING_SESSION_ID,
    CONF_SOURCE_ID,
    CONF_SOURCE_NAME,
    CONF_SOURCE_TYPE,
    CONF_TOPIC_ROOT,
    DEFAULT_ALLOW_DANGEROUS_SERVICES,
    DEFAULT_ENABLE_STATE_EVENTS,
    DEFAULT_ENABLE_TEMPLATE_API,
    DEFAULT_EXPOSE_FULL_CONFIG,
    DEFAULT_PAIRING_API_BASE,
    DEFAULT_MQTT_PORT,
    DEFAULT_PAIRING_MODE,
    DEFAULT_TOPIC_ROOT,
    DOMAIN,
    PAIRING_MODE_MANUAL,
    PAIRING_MODE_SEAMLESS,
    PRODUCT_NAME,
    normalize_pairing_mode,
)
from .pairing_bootstrap import (
    create_web_pairing_session,
    exchange_web_pairing_callback_code,
    fetch_web_pairing_session_status,
)
from .pairing_models import (
    PairingCallbackResult,
    PairingSessionCreateResult,
    PairingStatusResult,
)

# Re-export only the quick-pair names resolved through config_flow's namespace —
# either by the mixin's call sites or by tests' monkeypatch
# (seenzusaimqttbridge.config_flow.* 仍可拦截)。实现在 quick_pair.py。
from .quick_pair import (  # noqa: F401
    FLOW_MANAGER_CONFIG,
    FLOW_MANAGER_OPTIONS,
    QUICK_PAIR_CALLBACK_PATH,
    SavanAIQuickPairCallbackView,
    _build_quick_pair_callback_context,
    _clear_quick_pair_notifications,
    _format_quick_pair_diagnostic,
    _notify_app_return,
    _pop_quick_pair_callback_payload,
    _record_quick_pair_diagnostic,
)


CONF_MQTT_SETTINGS = "mqtt_settings"
# Backwards-compatible alias kept for the test suite / re-export; the single
# source of truth is const.PRODUCT_NAME.
PLUGIN_NAME = PRODUCT_NAME
section = getattr(data_entry_flow_module, "section", lambda schema, _config: schema)
_LOGGER = logging.getLogger(__name__)


def _backend_bridge_name(hass) -> str:
    """Bridge name POSTed to the backend, suffixed with the HA home name.

    The integration is single-instance per HA, so every install would otherwise
    register the same ``PLUGIN_NAME`` and be indistinguishable in the seenzus app
    when one account has several homes paired. Appending ``location_name`` (the
    home name the user set in HA) gives each bridge a human-meaningful label.
    Falls back to the bare plugin name when no home name is set or it would just
    duplicate the plugin name. The HA config-entry title stays ``PLUGIN_NAME``.
    """
    home = str(getattr(hass.config, "location_name", "") or "").strip()
    # Sanitize the user-controlled home name before it rides on the bridgeName POST
    # field: drop non-printable/control chars (newlines/tabs would break the single-
    # line label) and cap length, so an oversized or malformed home name can't turn a
    # pairing that used to succeed into a backend rejection. Printable Unicode
    # (Chinese, emoji) is kept — only control/format chars are stripped.
    home = "".join(ch for ch in home if ch.isprintable())[:64].strip()
    if not home or home == PLUGIN_NAME:
        return PLUGIN_NAME
    return f"{PLUGIN_NAME} · {home}"


async def _resolve_ha_instance_id(hass) -> str | None:
    """HA's stable per-install UUID, POSTed to the backend for re-pair dedup.

    The backend keys a re-pair of the same install by this id so it reuses /
    supersedes the existing bridge instead of spawning a duplicate (see
    docs/HANDOFF_REPAIR_DEDUP.zh-CN.md). Best-effort: the helper is imported
    lazily and every failure degrades to ``None`` — the field is optional, so a
    restricted / test hass or an old core just omits it and pairing still works.
    """
    try:
        from homeassistant.helpers import instance_id

        return await instance_id.async_get(hass)
    except Exception:  # noqa: BLE001
        return None


def _flatten_form_input(data: dict | None) -> dict:
    flat = dict(data or {})
    mqtt_settings = flat.pop(CONF_MQTT_SETTINGS, None)
    if isinstance(mqtt_settings, dict):
        flat.update(mqtt_settings)

    advanced_settings = flat.pop(CONF_ADVANCED_SETTINGS, None)
    if isinstance(advanced_settings, dict):
        flat.update(advanced_settings)

    return flat


def _default_pairing_mode(data: dict) -> str:
    return normalize_pairing_mode(data.get(CONF_PAIRING_MODE, ""))


def _mode_schema(default_mode: str = DEFAULT_PAIRING_MODE) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_PAIRING_MODE,
                default=default_mode,
            ): vol.In(
                {
                    PAIRING_MODE_SEAMLESS: "快速配对（推荐）",
                    PAIRING_MODE_MANUAL: "手动配置（高级）",
                }
            ),
        }
    )


def _schema(pairing_mode: str, defaults: dict | None = None) -> vol.Schema:
    d = _flatten_form_input(defaults)
    schema_fields: dict = {}

    if pairing_mode == PAIRING_MODE_SEAMLESS:
        schema_fields[
            vol.Optional(
                CONF_PAIRING_API_BASE,
                default=d.get(CONF_PAIRING_API_BASE, DEFAULT_PAIRING_API_BASE),
            )
        ] = TextSelector()
        return vol.Schema(schema_fields)

    schema_fields[
        vol.Required(CONF_MQTT_SETTINGS)
    ] = section(
        vol.Schema(
            {
                vol.Required(
                    CONF_MQTT_HOST,
                    default=d.get(CONF_MQTT_HOST, ""),
                ): TextSelector(),
                vol.Optional(
                    CONF_MQTT_PORT,
                    default=d.get(CONF_MQTT_PORT, DEFAULT_MQTT_PORT),
                ): NumberSelector(
                    NumberSelectorConfig(min=1, max=65535, mode=NumberSelectorMode.BOX)
                ),
                vol.Optional(
                    CONF_MQTT_USERNAME,
                    default=d.get(CONF_MQTT_USERNAME, ""),
                ): TextSelector(),
                vol.Optional(
                    CONF_MQTT_PASSWORD,
                    default=d.get(CONF_MQTT_PASSWORD, ""),
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
            }
        ),
        {"collapsed": True},
    )
    schema_fields[
        vol.Required(CONF_ADVANCED_SETTINGS)
    ] = section(
        vol.Schema(
            {
                vol.Optional(
                    CONF_TOPIC_ROOT,
                    default=d.get(CONF_TOPIC_ROOT, DEFAULT_TOPIC_ROOT),
                ): TextSelector(),
                vol.Optional(
                    CONF_BRIDGE_ID,
                    default=d.get(CONF_BRIDGE_ID, ""),
                ): TextSelector(),
                vol.Optional(
                    CONF_ENABLE_STATE_EVENTS,
                    default=d.get(CONF_ENABLE_STATE_EVENTS, DEFAULT_ENABLE_STATE_EVENTS),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_ALLOW_DANGEROUS_SERVICES,
                    default=d.get(CONF_ALLOW_DANGEROUS_SERVICES, DEFAULT_ALLOW_DANGEROUS_SERVICES),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_ENABLE_TEMPLATE_API,
                    default=d.get(CONF_ENABLE_TEMPLATE_API, DEFAULT_ENABLE_TEMPLATE_API),
                ): BooleanSelector(),
                vol.Optional(
                    CONF_EXPOSE_FULL_CONFIG,
                    default=d.get(CONF_EXPOSE_FULL_CONFIG, DEFAULT_EXPOSE_FULL_CONFIG),
                ): BooleanSelector(),
            }
        ),
        {"collapsed": True},
    )
    return vol.Schema(schema_fields)


def _validate(data: dict) -> dict[str, str]:
    errors: dict[str, str] = {}
    pairing_mode = str(data.get(CONF_PAIRING_MODE, DEFAULT_PAIRING_MODE)).strip()
    if pairing_mode == PAIRING_MODE_SEAMLESS:
        api_base = str(data.get(CONF_PAIRING_API_BASE, "")).strip()
        if api_base and not _is_valid_api_base(api_base):
            errors[CONF_PAIRING_API_BASE] = "invalid_pairing_api_base"
        return errors

    if not str(data.get(CONF_MQTT_HOST, "")).strip():
        errors[CONF_MQTT_HOST] = "host_required"
    return errors


def _is_valid_api_base(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _resolve_pairing_api_base(data: dict | None = None) -> str:
    configured = str((data or {}).get(CONF_PAIRING_API_BASE, "")).strip()
    return configured.rstrip("/") if configured else DEFAULT_PAIRING_API_BASE


def _build_quick_pair_entry_data(
    *,
    api_base: str,
    status_result: PairingCallbackResult | PairingStatusResult,
) -> dict:
    mqtt = status_result.mqtt or {}
    host = str(mqtt.get("host", "")).strip()
    if not host:
        raise ValueError("quick_pair_mqtt_missing")

    data = {
        CONF_PAIRING_MODE: PAIRING_MODE_SEAMLESS,
        CONF_CONFIG_SOURCE: str(getattr(status_result, "config_source", "") or CONFIG_SOURCE_WEB_PAIR),
        CONF_PAIRING_API_BASE: api_base,
        CONF_PAIRING_SESSION_ID: status_result.session_id or "",
        CONF_PAIRING_BOUND_AT: status_result.confirmed_at or "",
        CONF_MQTT_HOST: host,
        CONF_MQTT_PORT: int(mqtt.get("port") or DEFAULT_MQTT_PORT),
        CONF_MQTT_USERNAME: str(mqtt.get("username", "")).strip(),
        CONF_MQTT_PASSWORD: str(mqtt.get("password", "")).strip(),
    }
    topic_root = str(mqtt.get("topicRoot", "")).strip()
    if topic_root:
        data[CONF_TOPIC_ROOT] = topic_root
    bridge_id = str(mqtt.get("bridgeId", "") or getattr(status_result, "bridge_id", "")).strip()
    if bridge_id:
        data[CONF_BRIDGE_ID] = bridge_id
    source_id = str(getattr(status_result, "source_id", "") or mqtt.get("sourceId", "") or "").strip()
    if source_id:
        data[CONF_SOURCE_ID] = source_id
    source_type = str(getattr(status_result, "source_type", "") or mqtt.get("sourceType", "") or "").strip()
    if source_type:
        data[CONF_SOURCE_TYPE] = source_type
    source_name = str(getattr(status_result, "source_name", "") or mqtt.get("sourceName", "") or "").strip()
    if source_name:
        data[CONF_SOURCE_NAME] = source_name
    return data


def _diagnostic_from_result(
    result: PairingCallbackResult | PairingSessionCreateResult | PairingStatusResult,
) -> dict[str, str]:
    diagnostic: dict[str, str] = {}
    for key, attr in {
        "url": "request_url",
        "http_status": "http_status",
        "error_code": "error_code",
        "message": "message",
        "response": "response_summary",
    }.items():
        value = getattr(result, attr, None)
        if value is not None and str(value).strip():
            diagnostic[key] = str(value).strip()
    return diagnostic


# Schemes allowed to be rendered as a clickable markdown link. A backend-supplied
# return URL flows straight into the create-entry success page description, so an
# allow-list (not a deny-list) keeps a javascript:/data:/intent: payload from
# being one tap from execution: only the forms the backend contract actually
# sends — an http(s) universal link or the seenzus:// app deep link — survive.
_APP_RETURN_URL_ALLOW_SCHEMES = {"http", "https", "seenzus"}


def _sanitize_app_return_url(value: object) -> str | None:
    """Validate a backend-supplied app return URL before rendering it as a link.

    The URL is rendered into a markdown ``[label](url)`` link on the finish
    page, so validation is strict (the backend contract,
    docs/HANDOFF_APP_RETURN_URL.zh-CN.md §4, already forbids the rejected forms;
    this is spec-aligned defense-in-depth):

    * printable ASCII only — rejects whitespace, control bytes and zero-width /
      non-ASCII homograph chars that could spoof the visible link;
    * no markdown-structural chars ``()[]<>"`` / backtick, the backslash
      (browsers fold it to a forward slash, so it could smuggle a different host
      past ``urlparse``), nor the HA placeholder delimiters ``{}`` — all could
      corrupt the rendered link or spoof the host;
    * allow only ``http(s)://…`` and the ``seenzus://…`` app deep link — an
      allow-list, so opaque schemes (``mailto:`` / ``tel:`` / ``foo:bar``) and
      unexpected app/intent schemes are rejected even with a ``//host`` authority;
    * reject userinfo (``user@host``) — a trusted-looking ``@`` prefix would let
      the link navigate to a different host than the one shown.

    Note: IPv6-literal hosts (``[::1]``) are not supported — they need the
    bracket chars rejected above; the backend contract uses domain hosts.
    """
    text = str(value or "").strip()
    if not text:
        return None
    if not all("!" <= ch <= "~" for ch in text):
        return None
    if any(c in text for c in '()[]<>"`{}\\'):
        return None
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()
    if scheme not in _APP_RETURN_URL_ALLOW_SCHEMES:
        return None
    if not parsed.netloc or "@" in parsed.netloc:
        return None
    return text


def _async_show_form_compat(
    flow,
    *,
    step_id: str,
    data_schema: vol.Schema,
    errors: dict[str, str] | None = None,
    description_placeholders: dict[str, str] | None = None,
):
    """Call ``async_show_form``, degrading on cores lacking ``description_placeholders``.

    ``errors`` is always supported; ``description_placeholders`` is the
    version-gated kwarg. We try with it first, then drop it — so an older core
    still renders the form (without the placeholder) instead of raising. Shared
    by the diagnostic form and the re-shown seamless form.
    """
    base: dict = {"step_id": step_id, "data_schema": data_schema}
    if errors is not None:
        base["errors"] = errors

    variants: list[dict] = [base]
    if description_placeholders is not None:
        variants.insert(0, {**base, "description_placeholders": description_placeholders})

    last_exc: TypeError | None = None
    for kwargs in variants:
        try:
            return flow.async_show_form(**kwargs)
        except TypeError as exc:
            last_exc = exc
    raise last_exc  # pragma: no cover


def _show_form_with_diagnostic(flow, *, step_id: str, data_schema: vol.Schema, errors: dict[str, str], diagnostic: dict[str, str] | None = None):
    return _async_show_form_compat(
        flow,
        step_id=step_id,
        data_schema=data_schema,
        errors=errors,
        description_placeholders={"quick_pair_diagnostic": _format_quick_pair_diagnostic(diagnostic or {})},
    )


# ──────────────────────────────────────────────
# Config / Options 共用的快速配对步骤
# ──────────────────────────────────────────────
class _QuickPairFlowMixin:
    """Shared quick-pair and manual step implementations for the config and options flows.

    Concrete flow classes supply three hooks: ``_flow_manager`` (which flow
    manager resumes the external step), ``_entry_title`` (title passed to
    ``async_create_entry``) and ``_current_config()`` (defaults used to seed
    forms when no input yet).
    """

    _flow_manager: str
    _entry_title: str

    def _current_config(self) -> dict:
        """Existing config used to seed form defaults; the config flow has none."""
        return {}

    def _init_quick_pair_state(self) -> None:
        """Reset the quick-pair handshake state attributes."""
        self._quick_pair_api_base: str | None = None
        self._quick_pair_page_url: str | None = None
        self._quick_pair_session_id: str | None = None
        self._quick_pair_external_opened = False
        self._quick_pair_callback_state: str | None = None
        self._quick_pair_callback_state_token: str | None = None
        self._quick_pair_exchange_result = None
        self._quick_pair_finish_error: str | None = None
        self._quick_pair_diagnostic: dict[str, str] = {}
        # App return link (backend-supplied), surfaced on HA's native create-entry
        # success page after the entry is committed (so the link can never abandon
        # the flow before it completes).
        self._quick_pair_app_return_url: str | None = None

    def _reshow_seamless_form(self, error_key: str) -> config_entries.ConfigFlowResult:
        """Re-show the seamless form after a failure, seeded with the active api base."""
        return _show_form_with_diagnostic(
            self,
            step_id="seamless",
            data_schema=_schema(PAIRING_MODE_SEAMLESS, {CONF_PAIRING_API_BASE: self._quick_pair_api_base}),
            errors={"base": error_key},
            diagnostic=self._quick_pair_diagnostic,
        )

    def _entry_data_or_reshow(
        self, status_result: PairingCallbackResult | PairingStatusResult
    ) -> config_entries.ConfigFlowResult:
        """Create the entry from a pairing result, or re-show on missing MQTT config."""
        try:
            data = _build_quick_pair_entry_data(
                api_base=self._quick_pair_api_base,
                status_result=status_result,
            )
        except ValueError:
            _record_quick_pair_diagnostic(self.hass, "quick_pair_mqtt_missing", self._quick_pair_diagnostic)
            return self._reshow_seamless_form("quick_pair_mqtt_missing")

        # A confirmed return URL on the final result wins over the one captured
        # when the session was created; keep the latter as fallback.
        result_return_url = _sanitize_app_return_url(getattr(status_result, "app_return_url", None))
        if result_return_url:
            self._quick_pair_app_return_url = result_return_url
        return self._finish_quick_pair(data)

    def _finish_quick_pair(self, data: dict) -> config_entries.ConfigFlowResult:
        """Create the entry, attaching the app return link to the success page.

        The link is surfaced on HA's native create-entry success screen via the
        ``app_return`` create_entry description (``config.create_entry.app_return``).
        The entry is therefore committed *before* the link is ever shown, so
        tapping the link cannot abandon the flow and strand a confirmed binding.
        On the manual / no-link path we create the entry plainly.
        """
        if self._quick_pair_app_return_url:
            # Durable, path-independent surface for the return link: the
            # create-entry success page shows it inline on the first-time config
            # flow, but that page is transient and the options / re-pair flow
            # doesn't render a create_entry description at all — the notification
            # covers both paths and survives the success dialog being closed.
            _notify_app_return(self.hass, self._quick_pair_app_return_url)
            return self.async_create_entry(
                title=self._entry_title,
                data=data,
                description="app_return",
                description_placeholders={"app_return_url": self._quick_pair_app_return_url},
            )
        # No return link this time (manual / no-URL re-pair). Still a success, so
        # clear any stale return-link or failure notification from a prior attempt
        # — a leftover link would send the user back with an expired session.
        _clear_quick_pair_notifications(self.hass)
        return self.async_create_entry(title=self._entry_title, data=data)

    async def async_step_seamless(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _flatten_form_input(user_input)
            data[CONF_PAIRING_MODE] = PAIRING_MODE_SEAMLESS
            errors = _validate(data)
            if not errors:
                config_data = self.hass.config.as_dict() if hasattr(self.hass.config, "as_dict") else {}
                try:
                    redirect_uri, callback_state, callback_state_token = _build_quick_pair_callback_context(
                        self.hass,
                        getattr(self, "flow_id", "seenzus-quick-pair"),
                        self._flow_manager,
                    )
                except (NoURLAvailableError, RuntimeError, ValueError):
                    errors["base"] = "quick_pair_callback_unavailable"
                else:
                    api_base = _resolve_pairing_api_base(data)
                    result = await create_web_pairing_session(
                        api_base=api_base,
                        bridge_name=_backend_bridge_name(self.hass),
                        bridge_version=BRIDGE_VERSION,
                        ha_version=str(config_data.get("version", "")),
                        redirect_uri=redirect_uri,
                        state=callback_state_token,
                        ha_instance_id=await _resolve_ha_instance_id(self.hass),
                    )
                    if not result.ok or not result.pairing_page_url or not result.session_id:
                        errors["base"] = "quick_pair_session_failed"
                        self._quick_pair_diagnostic = _diagnostic_from_result(result)
                        _record_quick_pair_diagnostic(self.hass, "quick_pair_session_failed", self._quick_pair_diagnostic)
                    else:
                        self._quick_pair_api_base = api_base
                        self._quick_pair_page_url = result.pairing_page_url
                        self._quick_pair_session_id = result.session_id
                        self._quick_pair_external_opened = False
                        self._quick_pair_callback_state = callback_state
                        self._quick_pair_callback_state_token = callback_state_token
                        self._quick_pair_exchange_result = None
                        self._quick_pair_finish_error = None
                        self._quick_pair_app_return_url = _sanitize_app_return_url(
                            getattr(result, "app_return_url", None)
                        )
                        self._quick_pair_diagnostic = _diagnostic_from_result(result)
                        return await self.async_step_seamless_authorize()

        # With no input yet, seed the form from _current_config()
        # ({} in the config flow, the existing entry config in the options flow).
        return _show_form_with_diagnostic(
            self,
            step_id="seamless",
            data_schema=_schema(PAIRING_MODE_SEAMLESS, user_input or self._current_config()),
            errors=errors,
            diagnostic=self._quick_pair_diagnostic,
        )

    async def async_step_seamless_authorize(self, user_input: dict | None = None):
        if not self._quick_pair_page_url:
            return self.async_abort(reason="quick_pair_missing_context")

        if user_input is None:
            user_input = _pop_quick_pair_callback_payload(
                self.hass,
                self._quick_pair_callback_state,
            )

        if user_input is not None:
            try:
                state_payload = user_input.get("state")
                if user_input.get("error"):
                    self._quick_pair_finish_error = "quick_pair_authorization_failed"
                elif (
                    not isinstance(state_payload, dict)
                    or str(state_payload.get("pairing_state", "")).strip() != self._quick_pair_callback_state
                ):
                    self._quick_pair_finish_error = "quick_pair_callback_state_mismatch"
                elif not self._quick_pair_api_base or not self._quick_pair_callback_state_token:
                    self._quick_pair_finish_error = "quick_pair_missing_context"
                else:
                    code = str(user_input.get("code", "")).strip()
                    result = await exchange_web_pairing_callback_code(
                        api_base=self._quick_pair_api_base,
                        code=code,
                        state=self._quick_pair_callback_state_token,
                        session_id=self._quick_pair_session_id,
                    )
                    if not result.ok or not result.mqtt:
                        self._quick_pair_finish_error = "quick_pair_code_exchange_failed"
                        self._quick_pair_diagnostic = _diagnostic_from_result(result)
                        _record_quick_pair_diagnostic(self.hass, "quick_pair_code_exchange_failed", self._quick_pair_diagnostic)
                    else:
                        self._quick_pair_exchange_result = result
                        self._quick_pair_finish_error = None
                        self._quick_pair_diagnostic = _diagnostic_from_result(result)
            except Exception as err:  # noqa: BLE001
                self._quick_pair_finish_error = "quick_pair_code_exchange_failed"
                self._quick_pair_diagnostic = {
                    "error_code": type(err).__name__,
                    "message": str(err),
                }
                _record_quick_pair_diagnostic(self.hass, "quick_pair_callback_exception", self._quick_pair_diagnostic)
            return self.async_external_step_done(next_step_id="seamless_finish")

        if not self._quick_pair_external_opened:
            self._quick_pair_external_opened = True
            return self.async_external_step(
                step_id="seamless_authorize",
                url=self._quick_pair_page_url,
            )

        self._quick_pair_finish_error = "quick_pair_callback_timeout"
        return self.async_external_step_done(next_step_id="seamless_finish")

    async def async_step_seamless_finish(self, user_input: dict | None = None):
        if not self._quick_pair_page_url or not self._quick_pair_api_base or not self._quick_pair_session_id:
            return self.async_abort(reason="quick_pair_missing_context")

        if self._quick_pair_finish_error:
            return self._reshow_seamless_form(self._quick_pair_finish_error)

        if self._quick_pair_exchange_result is not None:
            return self._entry_data_or_reshow(self._quick_pair_exchange_result)

        status_result = None
        for _ in range(3):
            status_result = await fetch_web_pairing_session_status(
                api_base=self._quick_pair_api_base,
                session_id=self._quick_pair_session_id,
            )
            if status_result.ok and status_result.bound and status_result.mqtt:
                break
            await asyncio.sleep(1)

        if not status_result or not status_result.ok or not status_result.bound:
            self._quick_pair_diagnostic = _diagnostic_from_result(status_result) if status_result else self._quick_pair_diagnostic
            _record_quick_pair_diagnostic(self.hass, "quick_pair_bootstrap_failed", self._quick_pair_diagnostic)
            return self._reshow_seamless_form("quick_pair_bootstrap_failed")
        self._quick_pair_diagnostic = _diagnostic_from_result(status_result)

        return self._entry_data_or_reshow(status_result)

    async def async_step_manual(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _flatten_form_input(user_input)
            data[CONF_PAIRING_MODE] = PAIRING_MODE_MANUAL
            data[CONF_CONFIG_SOURCE] = CONFIG_SOURCE_MANUAL
            errors = _validate(data)
            if not errors:
                # Manual (re)config carries no return link; clear any stale
                # return-link / failure notification from an earlier quick pair.
                _clear_quick_pair_notifications(self.hass)
                return self.async_create_entry(title=self._entry_title, data=data)

        return self.async_show_form(
            step_id="manual",
            data_schema=_schema(PAIRING_MODE_MANUAL, user_input or self._current_config()),
            errors=errors,
        )


# ──────────────────────────────────────────────
# 首次添加集成
# ──────────────────────────────────────────────
class SavanAIBridgeConfigFlow(_QuickPairFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    _flow_manager = FLOW_MANAGER_CONFIG
    _entry_title = PLUGIN_NAME

    def __init__(self) -> None:
        self._selected_pairing_mode = DEFAULT_PAIRING_MODE
        self._init_quick_pair_state()

    async def async_step_user(self, user_input: dict | None = None):
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            self._selected_pairing_mode = _default_pairing_mode(user_input)
            if self._selected_pairing_mode == PAIRING_MODE_MANUAL:
                return await self.async_step_manual()
            return await self.async_step_seamless()

        return self.async_show_form(
            step_id="user",
            data_schema=_mode_schema(),
            errors={},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return SavanAIBridgeOptionsFlow(config_entry)


# ──────────────────────────────────────────────
# 已添加后点击「配置」修改参数
# ──────────────────────────────────────────────
class SavanAIBridgeOptionsFlow(_QuickPairFlowMixin, config_entries.OptionsFlow):
    _flow_manager = FLOW_MANAGER_OPTIONS
    _entry_title = ""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        self._init_quick_pair_state()
        current = self._current_config()
        self._selected_pairing_mode = _default_pairing_mode(current)
        self._quick_pair_api_base = str(current.get(CONF_PAIRING_API_BASE, "")).strip() or None

    def _current_config(self) -> dict:
        return {**self._config_entry.data, **self._config_entry.options}

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            self._selected_pairing_mode = _default_pairing_mode(user_input)
            if self._selected_pairing_mode == PAIRING_MODE_MANUAL:
                return await self.async_step_manual()
            return await self.async_step_seamless()

        return self.async_show_form(
            step_id="init",
            data_schema=_mode_schema(self._selected_pairing_mode),
            errors={},
        )
