"""Quick-pair HTTP 回调子系统：callback view、payload 信箱、JWT state 上下文与配对诊断.

只负责「浏览器回跳 -> HA」这一段 HTTP 管道，不包含任何表单 / flow 逻辑。
config_flow 通过模块级 re-export 暴露这些名字，flow mixin 的调用点经由
config_flow 全局解析。
"""
from __future__ import annotations

from html import escape as _html_escape
import json
import logging
import secrets
from typing import Any
from urllib.parse import quote, urlsplit

from aiohttp import web

from homeassistant.components import http, persistent_notification
from homeassistant.core import HomeAssistant

# NOTE: _encode_jwt/_decode_jwt 是 Home Assistant 的私有 helper（带下划线，
# 无稳定 API 保证）。替换为自建 JWT 实现风险更高（需要密钥管理与轮换），因此
# 按 brief 决定保留，并把对私有 API 的依赖隔离在本模块这一处。
from homeassistant.helpers.config_entry_oauth2_flow import (
    HEADER_FRONTEND_BASE,
    _decode_jwt,
    _encode_jwt,
)
from homeassistant.helpers.network import get_url

from .const import DOMAIN, PRODUCT_NAME

QUICK_PAIR_CALLBACK_PATH = "/api/seenzus_bridge/quick_pair/callback"
QUICK_PAIR_CALLBACK_VIEW_REGISTERED = "quick_pair_callback_view_registered"
QUICK_PAIR_CALLBACK_PAYLOADS = "quick_pair_callback_payloads"
# payload 信箱只在 flow 恢复时被 pop——若浏览器回跳后 flow 永远没恢复，
# 条目会永久滞留。设上限并「先逐最旧、再插入」，保证刚存入的 payload 必然幸存。
QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT = 16
QUICK_PAIR_APP_RETURN_PATH = "/api/seenzus_bridge/quick_pair/app_return"
QUICK_PAIR_APP_RETURN_VIEW_REGISTERED = "quick_pair_app_return_view_registered"
# 返回链接允许的 scheme 白名单：backend 契约只发 http(s) 通用链接或
# seenzus:// 深链。config_flow._sanitize_app_return_url（渲染前的严格校验）
# 与 app-return 中转 view（跳转前的二次校验）共用，单一事实源防止两处漂移。
APP_RETURN_URL_ALLOW_SCHEMES = {"http", "https", "seenzus"}
FLOW_MANAGER_CONFIG = "config"
FLOW_MANAGER_OPTIONS = "options"
_LOGGER = logging.getLogger(__name__)


class SeenzusQuickPairCallbackView(http.HomeAssistantView):
    """Receive quick-pair authorization callbacks for config and options flows."""

    requires_auth = False
    url = QUICK_PAIR_CALLBACK_PATH
    name = "api:seenzus_bridge:quick_pair_callback"

    async def get(self, request: web.Request) -> web.Response:
        if "state" not in request.query:
            return web.Response(text="Missing state parameter", status=400)

        # 现代 HA（2026.x）里 request.app 用 aiohttp AppKey（http.KEY_HASS）
        # 存放 hass；字符串 "hass" 回退保住旧版本与测试 fake 的 plain-dict app。
        hass = request.app.get(http.KEY_HASS) or request.app.get("hass")
        state = _decode_jwt(hass, request.query["state"])
        if state is None:
            _LOGGER.warning("Quick pair callback rejected invalid state token")
            return web.Response(text="Invalid state parameter", status=400)

        user_input: dict[str, Any] = {"state": state}
        if "code" in request.query:
            user_input["code"] = request.query["code"]
        elif "error" in request.query:
            user_input["error"] = request.query["error"]
        else:
            return web.Response(text="Missing code or error parameter", status=400)

        flow_id = str(state.get("flow_id", "")).strip()
        flow_manager = str(state.get("flow_manager", FLOW_MANAGER_CONFIG)).strip()
        if not flow_id:
            return web.Response(text="Missing flow id", status=400)

        callback_state = str(state.get("pairing_state", "")).strip()
        if not callback_state:
            return web.Response(text="Missing pairing state", status=400)

        _store_quick_pair_callback_payload(hass, callback_state, user_input)

        flow_manager_obj = hass.config_entries.flow
        if flow_manager == FLOW_MANAGER_OPTIONS:
            flow_manager_obj = hass.config_entries.options
        try:
            configure = flow_manager_obj.async_configure
            try:
                await configure(flow_id=flow_id, user_input=None)
            except TypeError:
                try:
                    await configure(flow_id)
                except TypeError:
                    await configure()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning(
                "Quick pair callback stored but failed to resume %s flow %s: %s",
                flow_manager,
                flow_id,
                err,
            )
            return web.Response(
                text="Authorization received. Please return to Home Assistant.",
                status=202,
            )

        _LOGGER.info(
            "Quick pair callback stored and resumed for %s flow %s",
            flow_manager,
            flow_id,
        )
        return web.Response(
            headers={"content-type": "text/html"},
            text="<script>window.close()</script>",
        )


# 向后兼容别名：品牌重命名时类名由 SavanAI* 改为 Seenzus*，但 config_flow.py
# 与测试套件仍按固定 re-export 约定导入历史名，保留别名避免 ImportError。
SavanAIQuickPairCallbackView = SeenzusQuickPairCallbackView


class SeenzusAppReturnView(http.HomeAssistantView):
    """Dismiss the pairing-complete notification, then bounce to the app.

    通知里的 markdown 链接无法执行任何脚本，所以「点击即关闭通知」只能在服务端
    实现：通知链接指向本 view，携带签名 JWT（内含真实回跳 URL，签名防篡改 /
    开放重定向）；view 先 dismiss 通知再跳转。http(s) 目标用 302；seenzus://
    深链返回一个极小 HTML 页做 JS 跳转（部分浏览器 / WebView 对指向自定义
    scheme 的 302 Location 不可靠），并留手动点击的后备链接。
    """

    requires_auth = False
    url = QUICK_PAIR_APP_RETURN_PATH
    name = "api:seenzus_bridge:quick_pair_app_return"

    async def get(self, request: web.Request) -> web.Response:
        token = request.query.get("token", "")
        if not token:
            return web.Response(text="Missing token parameter", status=400)

        hass = request.app.get(http.KEY_HASS) or request.app.get("hass")
        payload = _decode_jwt(hass, token)
        if payload is None:
            _LOGGER.warning("App return link rejected invalid token")
            return web.Response(text="Invalid token parameter", status=400)

        target = str(payload.get("app_return_url", "") or "").strip()
        scheme = urlsplit(target).scheme.lower()
        if scheme not in APP_RETURN_URL_ALLOW_SCHEMES:
            return web.Response(text="Invalid return URL", status=400)

        # 点击即视为「已返回应用」，先关掉配对完成通知再跳转；dismiss 幂等，
        # 重复点击（如从历史记录）仍可正常跳转。
        try:
            persistent_notification.async_dismiss(hass, _NOTIFY_APP_RETURN_ID)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("dismissing app return notification failed", exc_info=True)

        if scheme in ("http", "https"):
            return web.Response(status=302, headers={"Location": target})
        return web.Response(
            content_type="text/html",
            text=(
                '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
                "<title>返回 seenzus 应用</title></head><body>"
                "<p>正在打开 seenzus 应用…</p>"
                f'<p><a href="{_html_escape(target, quote=True)}">如未自动跳转，请点击这里</a></p>'
                f"<script>window.location.href = {json.dumps(target)};</script>"
                "</body></html>"
            ),
        )


def _ensure_quick_pair_callback_view(hass: HomeAssistant) -> None:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(QUICK_PAIR_CALLBACK_VIEW_REGISTERED):
        return

    hass_http = getattr(hass, "http", None)
    if hass_http is None:
        raise RuntimeError("home_assistant_http_not_available")

    hass_http.register_view(SeenzusQuickPairCallbackView())
    domain_data[QUICK_PAIR_CALLBACK_VIEW_REGISTERED] = True


def _ensure_quick_pair_app_return_view(hass: HomeAssistant) -> None:
    domain_data = hass.data.setdefault(DOMAIN, {})
    if domain_data.get(QUICK_PAIR_APP_RETURN_VIEW_REGISTERED):
        return

    hass_http = getattr(hass, "http", None)
    if hass_http is None:
        raise RuntimeError("home_assistant_http_not_available")

    hass_http.register_view(SeenzusAppReturnView())
    domain_data[QUICK_PAIR_APP_RETURN_VIEW_REGISTERED] = True


def _build_app_return_transit_url(hass: HomeAssistant, app_return_url: str) -> str | None:
    """Relative transit URL for the notification link (dismiss + redirect).

    相对路径（不用 get_url 拼绝对地址）：链接随用户当前访问 HA 的 base 解析，
    内网 / 外网 / cloud 均可点。view 注册或签发 token 失败（FakeHass / 受限
    环境）时返回 None，调用方回退为直接链接原始 URL——只损失自动关闭通知。
    """
    try:
        _ensure_quick_pair_app_return_view(hass)
        token = _encode_jwt(hass, {"app_return_url": app_return_url})
        return f"{QUICK_PAIR_APP_RETURN_PATH}?token={quote(token, safe='')}"
    except Exception:  # noqa: BLE001
        _LOGGER.debug("building app return transit link failed", exc_info=True)
        return None


def _quick_pair_callback_payloads(hass: HomeAssistant) -> dict[str, dict[str, Any]]:
    domain_data = hass.data.setdefault(DOMAIN, {})
    payloads = domain_data.setdefault(QUICK_PAIR_CALLBACK_PAYLOADS, {})
    return payloads


def _store_quick_pair_callback_payload(
    hass: HomeAssistant, callback_state: str, payload: dict[str, Any]
) -> None:
    """Store a callback payload, keeping the mailbox bounded.

    Evicts oldest-first BEFORE inserting so the just-stored payload always
    survives; re-storing an existing state refreshes it to newest.
    """
    payloads = _quick_pair_callback_payloads(hass)
    payloads.pop(callback_state, None)
    while len(payloads) >= QUICK_PAIR_CALLBACK_PAYLOAD_LIMIT:
        payloads.pop(next(iter(payloads)))
    payloads[callback_state] = payload


def _pop_quick_pair_callback_payload(hass: HomeAssistant, callback_state: str | None) -> dict[str, Any] | None:
    normalized = str(callback_state or "").strip()
    if not normalized:
        return None
    return _quick_pair_callback_payloads(hass).pop(normalized, None)


def _build_quick_pair_callback_context(
    hass: HomeAssistant,
    flow_id: str,
    flow_manager: str = FLOW_MANAGER_CONFIG,
) -> tuple[str, str, str]:
    _ensure_quick_pair_callback_view(hass)
    request = http.current_request.get()
    frontend_base = None
    if request is not None:
        frontend_base = request.headers.get(HEADER_FRONTEND_BASE)
        if not frontend_base:
            frontend_base = f"{request.scheme}://{request.host}"

    if not frontend_base:
        frontend_base = get_url(hass, allow_cloud=False, prefer_external=False)

    redirect_uri = f"{frontend_base.rstrip('/')}{QUICK_PAIR_CALLBACK_PATH}"
    callback_state = secrets.token_urlsafe(24)
    callback_state_token = _encode_jwt(
        hass,
        {
            "flow_id": flow_id,
            "flow_manager": flow_manager,
            "redirect_uri": redirect_uri,
            "pairing_state": callback_state,
        },
    )
    return redirect_uri, callback_state, callback_state_token


def _format_quick_pair_diagnostic(diagnostic: dict[str, str]) -> str:
    if not diagnostic:
        return "无诊断信息"
    parts = []
    for key in ("url", "http_status", "error_code", "message", "response"):
        value = diagnostic.get(key)
        if value:
            parts.append(f"{key}={value}")
    return " | ".join(parts)


# 单实例集成：两条通知各用一个固定 id，后一次配对覆盖前一次（不堆叠）。
_NOTIFY_DIAGNOSTIC_ID = "seenzus_bridge_quick_pair_diagnostic"
_NOTIFY_APP_RETURN_ID = "seenzus_bridge_app_return"


def _record_quick_pair_diagnostic(hass: HomeAssistant, reason: str, diagnostic: dict[str, str]) -> None:
    if not diagnostic:
        return
    formatted = _format_quick_pair_diagnostic(diagnostic)
    _LOGGER.warning("Quick pair failed: reason=%s %s", reason, formatted)
    # 直接调 persistent_notification 组件 API（已废弃的
    # hass.components.persistent_notification getattr 阶梯在现代 HA 上静默
    # no-op）；FakeHass / 受限环境缺少完整 hass 表面时由 try/except 保持
    # 静默 no-op 行为。
    try:
        persistent_notification.async_create(
            hass,
            f"快速配对失败：{reason}\n\n{formatted}",
            title="seenzus Bridge 快速配对诊断",
            notification_id=_NOTIFY_DIAGNOSTIC_ID,
        )
    except Exception:  # noqa: BLE001
        pass


def _notify_app_return(hass: HomeAssistant, app_return_url: str) -> None:
    """Surface the backend's 'return to seenzus app' link as a persistent notification.

    The create-entry success page already carries this link on the first-time
    config flow, but that page is transient (one dialog) and the options /
    re-pair flow does not render a create_entry description at all. A persistent
    notification gives every pairing path one durable, dismissable surface for
    the link, and survives the success dialog being closed. ``app_return_url`` is
    already sanitised by ``_sanitize_app_return_url`` before it reaches here.

    The link goes through the ``SeenzusAppReturnView`` transit endpoint so a
    click also dismisses this notification server-side (markdown can't run
    script); if the transit link can't be built we fall back to linking the raw
    URL — still functional, just without the auto-dismiss.

    Success supersedes any earlier failure, so we also clear a leftover
    diagnostic notification — otherwise the user sees contradictory
    「快速配对失败」+「配对完成」 side by side. Defensive try/except mirrors the
    diagnostic helper for FakeHass / restricted environments; we log at debug so a
    real production failure is still traceable instead of fully silent.
    """
    link_url = _build_app_return_transit_url(hass, app_return_url) or app_return_url
    # 必须用带 target 的内联 HTML 锚点，不能用 markdown [x](url) 语法：前端对
    # 「同源 + 无 target」的 <a> 会拦截成 SPA pushState 路由（frontend
    # is-navigation-click.ts），请求根本不会发到中转 view——表现为点击后通知
    # 仍在、第二次点击（地址未变）完全无操作。带 target 的锚点被放行为真实
    # 浏览器导航；HA 的 markdown 渲染（xss 库白名单 a[target|href|title]）
    # 会保留 target 属性。
    link_html = f'<a href="{_html_escape(link_url, quote=True)}" target="_blank">返回 seenzus 应用</a>'
    try:
        persistent_notification.async_dismiss(hass, _NOTIFY_DIAGNOSTIC_ID)
        persistent_notification.async_create(
            hass,
            f"{PRODUCT_NAME} 已成功绑定。\n\n## 👉 {link_html}",
            title="seenzus Bridge 配对完成",
            notification_id=_NOTIFY_APP_RETURN_ID,
        )
    except Exception:  # noqa: BLE001
        _LOGGER.debug("app return notification failed", exc_info=True)


def _clear_quick_pair_notifications(hass: HomeAssistant) -> None:
    """Clear pairing notifications on a success that carries no return link.

    A re-pair via a manual / no-URL path still succeeds but must not leave the
    previous pairing's return-link notification (a stale ``app_return_url`` that
    would send the user back with an expired session), nor a leftover failure
    diagnostic. Same defensive try/except + debug log as ``_notify_app_return``.
    """
    try:
        persistent_notification.async_dismiss(hass, _NOTIFY_APP_RETURN_ID)
        persistent_notification.async_dismiss(hass, _NOTIFY_DIAGNOSTIC_ID)
    except Exception:  # noqa: BLE001
        _LOGGER.debug("clearing quick-pair notifications failed", exc_info=True)
