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
from homeassistant.helpers import entity_registry as er, device_registry as dr
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    DeviceSelector,
    DeviceSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    AreaSelector,
    AreaSelectorConfig,
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
    CONF_MQTT_SCHEME,
    CONF_MQTT_USERNAME,
    CONF_MQTT_WS_PATH,
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
    DEFAULT_MQTT_SCHEME,
    DEFAULT_PAIRING_API_BASE,
    DEFAULT_MQTT_PORT,
    DEFAULT_PORT_BY_MQTT_SCHEME,
    MQTT_SCHEME_WS,
    MQTT_SCHEME_WSS,
    VALID_MQTT_SCHEMES,
    DEFAULT_PAIRING_MODE,
    DEFAULT_TOPIC_ROOT,
    DOMAIN,
    PAIRING_MODE_MANUAL,
    PAIRING_MODE_SEAMLESS,
    PRODUCT_NAME,
    normalize_pairing_mode,
)
from .dev_override import DevOverrideError, resolve_dev_pairing_api_base
from .device_access import CONF_SCOPE, CONF_DEVICES, CONF_ENTITIES, SCOPE_KEYS
from .environments import CONF_ENVIRONMENT, CONF_WORLD_ID, CONF_WORLD_NAME, ENVIRONMENTS, ENVIRONMENT_SLOTS, bootstrap_url, entry_environment, environment_for_api
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
    APP_RETURN_URL_ALLOW_SCHEMES,
    FLOW_MANAGER_CONFIG,
    FLOW_MANAGER_OPTIONS,
    QUICK_PAIR_CALLBACK_PATH,
    SavanAIQuickPairCallbackView,
    _build_quick_pair_callback_context,
    _clear_quick_pair_notifications,
    _format_quick_pair_diagnostic,
    _notify_app_return,
    _mark_quick_pair_completed,
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

    Without a home name each entry would register the same ``PLUGIN_NAME`` in the app
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


# This is a setup route only; persisted pairing_mode remains seamless/manual.
_PAIRING_MODE_CHOOSE_ENVIRONMENT = "choose_environment"


def _mode_schema(default_mode: str = DEFAULT_PAIRING_MODE) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(
                CONF_PAIRING_MODE,
                default=default_mode,
            ): vol.In(
                {
                    PAIRING_MODE_SEAMLESS: "快速配对（自动识别环境，推荐）",
                    _PAIRING_MODE_CHOOSE_ENVIRONMENT: "快速配对（手动选择环境）",
                    PAIRING_MODE_MANUAL: "手动配置（高级）",
                }
            ),
        }
    )


def _schema(pairing_mode: str, defaults: dict | None = None) -> vol.Schema:
    d = _flatten_form_input(defaults)
    schema_fields: dict = {}

    if pairing_mode == PAIRING_MODE_SEAMLESS:
        # 设备范围已在前置步骤确定，环境可由中转页识别；此空表单只用于失败后的重试。
        return vol.Schema({})

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
        # 环境和设备范围由前置步骤校验；重试表单没有连接参数。
        return errors

    if not str(data.get(CONF_MQTT_HOST, "")).strip():
        errors[CONF_MQTT_HOST] = "host_required"
    return errors


def _build_quick_pair_entry_data(
    *,
    api_base: str,
    status_result: PairingCallbackResult | PairingStatusResult,
) -> dict:
    mqtt = status_result.mqtt or {}
    host = str(mqtt.get("host", "")).strip()
    if not host:
        raise ValueError("quick_pair_mqtt_missing")

    # 传输 scheme（issue #14）：web-pair 兑换响应下发。`or ""` 归一显式 JSON
    # null（str(None) 会变 "None"）；未知值按裸 TCP 回退并告警，避免坏值直接
    # 炸掉连接层。
    scheme = str(mqtt.get("scheme") or "").strip().lower()
    if scheme and scheme not in VALID_MQTT_SCHEMES:
        _LOGGER.warning(
            "Quick pair returned unknown mqtt scheme %r; falling back to bare TCP",
            scheme,
        )
        scheme = ""
    scheme = scheme or DEFAULT_MQTT_SCHEME
    ws_path = ""
    if scheme in (MQTT_SCHEME_WS, MQTT_SCHEME_WSS):
        raw_ws_path = str(mqtt.get("path") or "").strip()
        if raw_ws_path:
            ws_path = raw_ws_path if raw_ws_path.startswith("/") else f"/{raw_ws_path}"

    data = {
        CONF_PAIRING_MODE: PAIRING_MODE_SEAMLESS,
        CONF_CONFIG_SOURCE: str(getattr(status_result, "config_source", "") or CONFIG_SOURCE_WEB_PAIR),
        CONF_PAIRING_API_BASE: api_base,
        CONF_PAIRING_SESSION_ID: status_result.session_id or "",
        CONF_PAIRING_BOUND_AT: status_result.confirmed_at or "",
        CONF_MQTT_HOST: host,
        # 端口缺省按 scheme 取惯例值（wss 443 / ws 80 / mqtts 8883 / mqtt 1883）：
        # wss 响应缺 port 时若一律回退 1883，会拿 TLS websocket 去连明文 TCP 端口。
        CONF_MQTT_PORT: int(mqtt.get("port") or DEFAULT_PORT_BY_MQTT_SCHEME[scheme]),
        CONF_MQTT_USERNAME: str(mqtt.get("username", "")).strip(),
        CONF_MQTT_PASSWORD: str(mqtt.get("password", "")).strip(),
        # scheme/ws_path 总是显式落盘（裸 TCP 即 "mqtt"/""）：options 流重配对走
        # {**entry.data, **entry.options} 合并，若裸 TCP 结果省略这两个键，旧
        # entry.data 里残留的 wss/path 会穿透合并继续生效，把新的裸 TCP broker
        # 拨成 websockets+TLS。旧 entry（升级前创建）无键 → coordinator 按缺省
        # 走裸 TCP 现状路径。
        CONF_MQTT_SCHEME: scheme,
        CONF_MQTT_WS_PATH: ws_path,
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
    world_id = getattr(status_result, "world_id", None)
    if world_id:
        data[CONF_WORLD_ID] = world_id
        data[CONF_WORLD_NAME] = getattr(status_result, "world_name", None) or world_id
    data[CONF_ENVIRONMENT] = environment_for_api(api_base)
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
# 定义在 quick_pair（中转 view 跳转前也要校验），这里保留旧名作本模块内引用。
_APP_RETURN_URL_ALLOW_SCHEMES = APP_RETURN_URL_ALLOW_SCHEMES


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

    def _notification_scope(self) -> str:
        return getattr(getattr(self, "_config_entry", None), "entry_id", None) or getattr(self, "flow_id", "")

    def _current_config(self) -> dict:
        """Existing config used to seed form defaults; the config flow has none."""
        return {}

    def _configured_environments(self) -> set[str]:
        own_entry_id = getattr(getattr(self, "_config_entry", None), "entry_id", None)
        return {entry_environment({**entry.data, **entry.options})
                for entry in self.hass.config_entries.async_entries(DOMAIN)
                if entry.entry_id != own_entry_id}

    def _claim_pairing_slot(self) -> bool:
        """Serialize credential rotation across config and options flows on this HA."""
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        owner = domain_data.get("pairing_flow_owner")
        current = (self._flow_manager, self.flow_id)
        if owner and owner != current:
            manager = self.hass.config_entries.options if owner[0] == FLOW_MANAGER_OPTIONS else self.hass.config_entries.flow
            if any(flow["flow_id"] == owner[1] for flow in manager.async_progress()):
                return False
        domain_data["pairing_flow_owner"] = current
        return True

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
        self._selected_environment = entry_environment(self._current_config()) if self._current_config() else "auto"
        self._scope = {key: self._current_config()[key] for key in SCOPE_KEYS if key in self._current_config()}
        self._dev_api_base: str | None = None
        self._scope_only = False

    def _reshow_seamless_form(self, error_key: str) -> config_entries.ConfigFlowResult:
        """Re-show the (field-less) seamless form after a failure, with the error."""
        return _show_form_with_diagnostic(
            self,
            step_id="seamless",
            data_schema=_schema(PAIRING_MODE_SEAMLESS),
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
            _record_quick_pair_diagnostic(self.hass, "quick_pair_mqtt_missing", self._quick_pair_diagnostic, self._notification_scope())
            return self._reshow_seamless_form("quick_pair_mqtt_missing")
        if not data.get(CONF_WORLD_ID):
            return self._reshow_seamless_form("world_identity_missing")
        existing = self._current_config()
        if existing.get(CONF_WORLD_ID) and data[CONF_WORLD_ID] != existing[CONF_WORLD_ID]:
            return self._reshow_seamless_form("world_mismatch")
        if not existing.get(CONF_WORLD_ID) and existing.get(CONF_SOURCE_ID) and data.get(CONF_SOURCE_ID) != existing[CONF_SOURCE_ID]:
            return self._reshow_seamless_form("world_mismatch")
        data = {**existing, **data, **self._scope}
        self._entry_title = f"{ENVIRONMENTS.get(data[CONF_ENVIRONMENT], ('开发环境', ''))[0]} · {data[CONF_WORLD_NAME]}"
        # Checked before session creation / credential exchange as well. Keep a
        # final guard so no alternate completion path can create a duplicate slot.
        if data[CONF_ENVIRONMENT] in self._configured_environments():
            return self.async_abort(reason="environment_already_configured")
        if self._flow_manager == FLOW_MANAGER_CONFIG:
            self.context["unique_id"] = data[CONF_ENVIRONMENT]

        # A confirmed return URL on the final result wins over the one captured
        # when the session was created; keep the latter as fallback.
        result_return_url = _sanitize_app_return_url(getattr(status_result, "app_return_url", None))
        if result_return_url:
            self._quick_pair_app_return_url = result_return_url
        result = self._finish_quick_pair(data)
        _mark_quick_pair_completed(self.hass, self._quick_pair_callback_state)
        return result

    def _finish_quick_pair(self, data: dict) -> config_entries.ConfigFlowResult:
        """Create the entry, attaching the app return link to the success page.

        The link is surfaced on HA's native create-entry success screen via the
        ``app_return`` create_entry description (``config.create_entry.app_return``).
        The entry is therefore committed *before* the link is ever shown, so
        tapping the link cannot abandon the flow and strand a confirmed binding.
        On the manual / no-link path we create the entry plainly.
        """
        if self._flow_manager == FLOW_MANAGER_OPTIONS and data.get(CONF_WORLD_ID):
            self.hass.config_entries.async_update_entry(self._config_entry, title=self._entry_title,
                unique_id=data[CONF_ENVIRONMENT])
        if self._quick_pair_app_return_url:
            # Durable, path-independent surface for the return link: the
            # create-entry success page shows it inline on the first-time config
            # flow, but that page is transient and the options / re-pair flow
            # doesn't render a create_entry description at all — the notification
            # covers both paths and survives the success dialog being closed.
            _notify_app_return(self.hass, self._quick_pair_app_return_url, self._notification_scope())
            return self.async_create_entry(
                title=self._entry_title,
                data=data,
                description="app_return",
                description_placeholders={"app_return_url": self._quick_pair_app_return_url},
            )
        # No return link this time (manual / no-URL re-pair). Still a success, so
        # clear any stale return-link or failure notification from a prior attempt
        # — a leftover link would send the user back with an expired session.
        _clear_quick_pair_notifications(self.hass, self._notification_scope())
        return self.async_create_entry(title=self._entry_title, data=data)

    async def _environment_choices(self) -> dict[str, str]:
        choices = {"auto": "跟随应用（未识别时选择）", "cn": "国内 PRD", "global": "境外 PRD"}
        self._dev_api_base = await resolve_dev_pairing_api_base(self.hass)
        if self._dev_api_base:
            choices["uat"] = "UAT（开发）"
            choices["custom"] = "Dev Server（开发预设）"
        return choices

    async def async_step_environment(self, user_input: dict | None = None):
        try:
            choices = await self._environment_choices()
        except DevOverrideError:
            return self._reshow_seamless_form("dev_override_invalid")
        choices.pop("auto", None)
        if user_input is not None:
            environment = user_input.get(CONF_ENVIRONMENT)
            if environment not in choices:
                return self.async_abort(reason="invalid_environment")
            if environment in self._configured_environments():
                return self.async_abort(reason="environment_already_configured")
            self._selected_environment = environment
            if CONF_SCOPE in self._scope:
                return await self._after_scope()
            return await self.async_step_scope()
        return self.async_show_form(step_id="environment", data_schema=vol.Schema({
            vol.Required(CONF_ENVIRONMENT): vol.In(choices),
        }))

    async def async_step_seamless(self, user_input: dict | None = None):
        if user_input is None:
            return self._reshow_seamless_form(self._quick_pair_finish_error or "quick_pair_missing_context")
        if not self._claim_pairing_slot():
            return self._reshow_seamless_form("pairing_in_progress")
        existing = self._current_config()
        if existing and not existing.get(CONF_WORLD_ID) and not existing.get(CONF_SOURCE_ID):
            return self._reshow_seamless_form("world_identity_missing")
        if self._selected_environment in self._configured_environments():
            return self.async_abort(reason="environment_already_configured")
        try:
            redirect_uri, callback_state, token = _build_quick_pair_callback_context(
                self.hass, getattr(self, "flow_id", "seenzus-quick-pair"), self._flow_manager)
            # A stored endpoint is authoritative on re-pair. Developer presets only
            # seed a NEW entry; editing the global file cannot retarget any entry.
            api_base = existing.get(CONF_PAIRING_API_BASE)
            if not api_base and self._selected_environment in ENVIRONMENTS:
                api_base = ENVIRONMENTS[self._selected_environment][1]
            if not api_base and self._selected_environment == "custom":
                api_base = self._dev_api_base or await resolve_dev_pairing_api_base(self.hass)
                if not api_base:
                    raise DevOverrideError("missing preset")
            if api_base and environment_for_api(api_base) in self._configured_environments():
                return self.async_abort(reason="environment_already_configured")
            ha_instance_id = await _resolve_ha_instance_id(self.hass)
            if not ha_instance_id:
                return self._reshow_seamless_form("ha_identity_unavailable")
            self._quick_pair_api_base = api_base
            self._quick_pair_session_id = None
            self._quick_pair_callback_state = callback_state
            self._quick_pair_callback_state_token = token
            self._quick_pair_external_opened = False
            self._quick_pair_exchange_result = None
            self._quick_pair_finish_error = None
            if api_base:
                result = await create_web_pairing_session(api_base=api_base,
                    bridge_name=_backend_bridge_name(self.hass), bridge_version=BRIDGE_VERSION,
                    ha_version=str(self.hass.config.as_dict().get("version", "")),
                    redirect_uri=redirect_uri, state=token, ha_instance_id=ha_instance_id,
                    protocol_version=2, expected_world_id=existing.get(CONF_WORLD_ID),
                    expected_source_id=existing.get(CONF_SOURCE_ID) if not existing.get(CONF_WORLD_ID) else None)
                if not result.ok or not result.pairing_page_url or not result.session_id:
                    self._quick_pair_diagnostic = _diagnostic_from_result(result)
                    return self._reshow_seamless_form("quick_pair_session_failed")
                self._quick_pair_page_url = result.pairing_page_url
                self._quick_pair_session_id = result.session_id
                self._quick_pair_app_return_url = _sanitize_app_return_url(getattr(result, "app_return_url", None))
                return await self.async_step_seamless_authorize()
            self._quick_pair_page_url = bootstrap_url(api_base, {
                "state": token, "redirect_uri": redirect_uri,
                "ha_instance_id": ha_instance_id, "bridge_name": _backend_bridge_name(self.hass),
                "expected_world_id": existing.get(CONF_WORLD_ID),
                "expected_source_id": existing.get(CONF_SOURCE_ID) if not existing.get(CONF_WORLD_ID) else None,
                "configured_environments": ",".join(sorted(self._configured_environments())),
            })
            return await self.async_step_seamless_authorize()
        except DevOverrideError:
            return self._reshow_seamless_form("dev_override_invalid")
        except (NoURLAvailableError, RuntimeError, ValueError):
            return self._reshow_seamless_form("quick_pair_callback_unavailable")

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
                if (
                    not isinstance(state_payload, dict)
                    or str(state_payload.get("pairing_state", "")).strip() != self._quick_pair_callback_state
                ):
                    self._quick_pair_finish_error = "quick_pair_callback_state_mismatch"
                elif user_input.get("error"):
                    if user_input["error"] == "environment_already_configured":
                        return self.async_abort(reason="environment_already_configured")
                    self._quick_pair_finish_error = "environment_required" if user_input["error"] == "environment_required" and not self._current_config() else "quick_pair_authorization_failed"
                elif not self._quick_pair_callback_state_token:
                    self._quick_pair_finish_error = "quick_pair_missing_context"
                else:
                    environment = user_input.get("environment")
                    if self._quick_pair_api_base:
                        if environment is not None and environment != environment_for_api(self._quick_pair_api_base):
                            raise ValueError("environment_mismatch")
                    elif environment in ENVIRONMENTS:
                        self._quick_pair_api_base = ENVIRONMENTS[environment][1]
                    else:
                        raise ValueError("invalid_environment")
                    if environment_for_api(self._quick_pair_api_base) in self._configured_environments():
                        return self.async_abort(reason="environment_already_configured")
                    session_id = user_input.get("session_id", self._quick_pair_session_id)
                    if self._quick_pair_session_id and session_id != self._quick_pair_session_id:
                        raise ValueError("session_mismatch")
                    if not isinstance(session_id, str) or not session_id.startswith("wps_") or len(session_id) > 256:
                        raise ValueError("invalid_session")
                    self._quick_pair_session_id = session_id
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
                        _record_quick_pair_diagnostic(self.hass, "quick_pair_code_exchange_failed", self._quick_pair_diagnostic, self._notification_scope())
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
                _record_quick_pair_diagnostic(self.hass, "quick_pair_callback_exception", self._quick_pair_diagnostic, self._notification_scope())
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
        if self._quick_pair_finish_error == "environment_required":
            return await self.async_step_environment()
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
            _record_quick_pair_diagnostic(self.hass, "quick_pair_bootstrap_failed", self._quick_pair_diagnostic, self._notification_scope())
            return self._reshow_seamless_form("quick_pair_bootstrap_failed")
        self._quick_pair_diagnostic = _diagnostic_from_result(status_result)

        return self._entry_data_or_reshow(status_result)

    async def async_step_scope(self, user_input: dict | None = None):
        if user_input:
            self._scope[CONF_SCOPE] = user_input[CONF_SCOPE]
            if user_input[CONF_SCOPE] == "all":
                self._scope.update({CONF_DEVICES: [], CONF_ENTITIES: []})
                return await self._after_scope()
            return await self.async_step_devices()
        return self.async_show_form(step_id="scope", data_schema=vol.Schema({
            vol.Required(CONF_SCOPE, default=self._scope.get(CONF_SCOPE, "all")): vol.In({"all": "全部设备", "selected": "指定设备"}),
        }))

    async def async_step_devices(self, user_input: dict | None = None):
        if user_input is not None:
            device_ids = set(user_input.get(CONF_DEVICES, []))
            area_ids = set(user_input.get("areas", []))
            device_ids.update(device.id for device in dr.async_get(self.hass).devices.values() if device.area_id in area_ids)
            self._scope[CONF_DEVICES] = sorted(device_ids)
            return await self.async_step_entities()
        return self.async_show_form(step_id="devices", data_schema=vol.Schema({
            vol.Optional("areas", default=[]): AreaSelector(AreaSelectorConfig(multiple=True)),
            vol.Optional(CONF_DEVICES, default=self._scope.get(CONF_DEVICES, [])): DeviceSelector(DeviceSelectorConfig(multiple=True)),
        }))

    async def async_step_entities(self, user_input: dict | None = None):
        registry = er.async_get(self.hass)
        eligible = [entry for entry in registry.entities.values()
                    if not entry.device_id and getattr(entry, "platform", None) != DOMAIN]
        errors = {}
        if user_input is not None:
            area_ids = set(user_input.get("areas", []))
            entity_ids = set(user_input.get("entities", []))
            entity_ids.update(entry.entity_id for entry in eligible if entry.area_id in area_ids)
            selected = [registry.async_get(entity_id) for entity_id in entity_ids]
            if any(entry is None or entry.device_id or getattr(entry, "platform", None) == DOMAIN for entry in selected):
                errors["base"] = "stable_entity_required"
            else:
                self._scope[CONF_ENTITIES] = sorted(entry.id for entry in selected)
                return await self._after_scope()
        defaults = [entry.entity_id for entry in eligible if entry.id in self._scope.get(CONF_ENTITIES, [])]
        fields = {}
        if eligible:
            fields = {
                vol.Optional("areas", default=(user_input or {}).get("areas", [])): AreaSelector(AreaSelectorConfig(multiple=True)),
                vol.Optional("entities", default=(user_input or {}).get("entities", defaults)): EntitySelector(
                    EntitySelectorConfig(multiple=True, include_entities=[entry.entity_id for entry in eligible])),
            }
        # An empty include_entities list would remove the selector's filter, so
        # show an empty confirmation form when no standalone entities exist.
        return self.async_show_form(step_id="entities", data_schema=vol.Schema(fields), errors=errors)

    async def _after_scope(self):
        if self._scope_only:
            return self.async_create_entry(title="", data={**self._current_config(), **self._scope})
        if self._selected_pairing_mode == PAIRING_MODE_MANUAL:
            if self._selected_environment == "auto":
                return await self.async_step_environment()
            return await self.async_step_manual()
        return await self.async_step_seamless({})

    async def async_step_manual(self, user_input: dict | None = None):
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _flatten_form_input(user_input)
            data[CONF_PAIRING_MODE] = PAIRING_MODE_MANUAL
            data[CONF_CONFIG_SOURCE] = CONFIG_SOURCE_MANUAL
            # 手动配置只支持裸 TCP（issue #14 明确不做 ws 的手动 UI）。显式落盘
            # 以覆盖 options 合并里可能残留的旧 wss scheme/path（见
            # _build_quick_pair_entry_data 的同款注释）。
            data[CONF_MQTT_SCHEME] = DEFAULT_MQTT_SCHEME
            data[CONF_MQTT_WS_PATH] = ""
            errors = _validate(data)
            if not errors:
                environment = self._current_config().get(CONF_ENVIRONMENT, self._selected_environment)
                if environment not in ENVIRONMENT_SLOTS:
                    return await self.async_step_environment()
                if environment in self._configured_environments():
                    return self.async_abort(reason="environment_already_configured")
                if not self._claim_pairing_slot():
                    return self.async_abort(reason="pairing_in_progress")
                data[CONF_ENVIRONMENT] = environment
                if self._flow_manager == FLOW_MANAGER_CONFIG:
                    self.context["unique_id"] = environment
                # Manual (re)config carries no return link; clear any stale
                # return-link / failure notification from an earlier quick pair.
                _clear_quick_pair_notifications(self.hass, self._notification_scope())
                return self.async_create_entry(title=self._entry_title, data={**self._current_config(), **data, **self._scope})

        return self.async_show_form(
            step_id="manual",
            data_schema=_schema(PAIRING_MODE_MANUAL, user_input or self._current_config()),
            errors=errors,
        )


# ──────────────────────────────────────────────
# 首次添加集成
# ──────────────────────────────────────────────
class SavanAIBridgeConfigFlow(_QuickPairFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    _flow_manager = FLOW_MANAGER_CONFIG
    _entry_title = PLUGIN_NAME

    def __init__(self) -> None:
        self._selected_pairing_mode = DEFAULT_PAIRING_MODE
        self._init_quick_pair_state()

    async def async_step_user(self, user_input: dict | None = None):
        if self._configured_environments() == ENVIRONMENT_SLOTS:
            return self.async_abort(reason="all_environments_configured")
        if user_input is not None:
            if user_input.get(CONF_PAIRING_MODE) == _PAIRING_MODE_CHOOSE_ENVIRONMENT:
                self._selected_pairing_mode = PAIRING_MODE_SEAMLESS
                return await self.async_step_environment()
            self._selected_pairing_mode = _default_pairing_mode(user_input)
            if self._selected_pairing_mode == PAIRING_MODE_SEAMLESS:
                self._selected_environment = "auto"
                return await self.async_step_scope()
            return await self.async_step_environment()
        return self.async_show_form(step_id="user", data_schema=_mode_schema(), errors={})

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
        self._selected_pairing_mode = _default_pairing_mode(self._current_config())
        # 重配对始终沿用已保存的环境、API 和 World；开发预设只作用于新增条目。

    def _current_config(self) -> dict:
        return {**self._config_entry.data, **self._config_entry.options}

    async def async_step_init(self, user_input: dict | None = None):
        if user_input is not None:
            action = user_input.get("action", "repair")
            if action == "devices":
                self._scope_only = True
                return await self.async_step_scope()
            if action == "manual" and not self._current_config().get(CONF_WORLD_ID):
                self._selected_pairing_mode = PAIRING_MODE_MANUAL
                return await self.async_step_manual()
            return await self.async_step_seamless({})
        actions = {"repair": "重新配对（保留环境、世界和设备范围）", "devices": "设备授权范围"}
        if self._current_config().get(CONF_CONFIG_SOURCE) == CONFIG_SOURCE_MANUAL:
            actions["manual"] = "手动连接设置"
        return self.async_show_form(step_id="init", data_schema=vol.Schema({vol.Required("action", default="devices"): vol.In(actions)}))
