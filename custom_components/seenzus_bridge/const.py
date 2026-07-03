"""常量定义 - 不引入任何第三方依赖，供 config_flow 安全导入."""

DOMAIN = "seenzus_bridge"
BRIDGE_VERSION = "0.1.9"

# 产品显示名（Python 侧唯一来源：config_flow / sensor / quick_pair 都引用这里）。
# 注意：manifest.json、strings.json、translations 因 HA 翻译机制无法引用 Python
# 常量，仍是字面量，改名时需一并手动同步这几处 JSON。
PRODUCT_NAME = "seenzus MQTT Bridge"

# 用户可配置项
CONF_MQTT_HOST      = "mqtt_host"
CONF_MQTT_PORT      = "mqtt_port"
CONF_MQTT_USERNAME  = "mqtt_username"
CONF_MQTT_PASSWORD  = "mqtt_password"
# 传输方式（issue #14）：web-pair 兑换响应下发，manual 流程不暴露 UI。
# 旧 entry 无此键 → 按 DEFAULT_MQTT_SCHEME 走裸 TCP 现状路径。
CONF_MQTT_SCHEME = "mqtt_scheme"
CONF_MQTT_WS_PATH = "mqtt_ws_path"
CONF_TOPIC_ROOT = "topic_root"
CONF_BRIDGE_ID = "bridge_id"
CONF_SOURCE_ID = "source_id"
CONF_SOURCE_TYPE = "source_type"
CONF_SOURCE_NAME = "source_name"
CONF_ENABLE_STATE_EVENTS = "enable_state_events"
CONF_ENABLE_TEMPLATE_API = "enable_template_api"
CONF_ALLOW_DANGEROUS_SERVICES = "allow_dangerous_services"
CONF_EXPOSE_FULL_CONFIG = "expose_full_config"
CONF_PAIRING_API_BASE = "pairing_api_base"
CONF_PAIRING_MODE = "pairing_mode"
CONF_PAIRING_SESSION_ID = "pairing_session_id"
CONF_PAIRING_BOUND_AT = "pairing_bound_at"
CONF_ADVANCED_SETTINGS = "advanced_settings"
CONF_CONFIG_SOURCE = "config_source"

# hass.data[DOMAIN] 键：最近一次快速配对失败的诊断摘要。quick_pair 写入/清除，
# 配对状态传感器读取——把原先只存在于「日志 + 通知」的诊断收敛到一个持久的
# UI 面（传感器属性）。定义在 const（无依赖）供 quick_pair 与 sensor 共用。
QUICK_PAIR_LAST_DIAGNOSTIC = "quick_pair_last_diagnostic"

# 配对模式（coordinator/config_flow 共用的唯一一份取值，勿在调用点写字符串字面量）
PAIRING_MODE_MANUAL = "manual"
PAIRING_MODE_SEAMLESS = "seamless"
VALID_PAIRING_MODES = {PAIRING_MODE_MANUAL, PAIRING_MODE_SEAMLESS}

# 配对状态（值随 MQTT presence payload 上线——硬不变量，勿改）
PAIRING_STATUS_IDLE = "idle"
PAIRING_STATUS_PAIRED = "paired"
PAIRING_STATUS_BOUND = "bound"
PAIRING_STATUS_WAITING_EXTERNAL_AUTH = "waiting_external_auth"
PAIRING_STATUS_BRIDGE_STARTING = "bridge_starting"
PAIRING_STATUS_BRIDGE_READY = "bridge_ready"
PAIRING_STATUS_MQTT_AUTH_FAILED = "mqtt_auth_failed"

# MQTT 传输 scheme（与后端 exchange 响应的 mqtt.scheme 语义一致）：
# mqtt=裸 TCP，mqtts=TCP+TLS，ws=WebSocket，wss=WebSocket+TLS。
MQTT_SCHEME_TCP = "mqtt"
MQTT_SCHEME_TCP_TLS = "mqtts"
MQTT_SCHEME_WS = "ws"
MQTT_SCHEME_WSS = "wss"
VALID_MQTT_SCHEMES = {MQTT_SCHEME_TCP, MQTT_SCHEME_TCP_TLS, MQTT_SCHEME_WS, MQTT_SCHEME_WSS}

# 默认值
DEFAULT_MQTT_PORT      = 1883
DEFAULT_MQTT_SCHEME = MQTT_SCHEME_TCP
DEFAULT_MQTT_WS_PATH = "/mqtt"
# 各 scheme 的惯例默认端口。后端契约总是显式下发 port，这里仅作缺省兜底：
# wss/mqtts 响应缺 port 时若一律回退 1883，会拿 TLS 去连明文 TCP 端口。
DEFAULT_PORT_BY_MQTT_SCHEME = {
    MQTT_SCHEME_TCP: DEFAULT_MQTT_PORT,
    MQTT_SCHEME_TCP_TLS: 8883,
    MQTT_SCHEME_WS: 80,
    MQTT_SCHEME_WSS: 443,
}
DEFAULT_TOPIC_ROOT = "seenzus/v2"
DEFAULT_ENABLE_STATE_EVENTS = True
# Security defaults: deny/redact. The MQTT command channel has no app-layer
# auth, so the safe baseline blocks RCE-class services, arbitrary template
# rendering and home-location exposure. Operators opt back in per-switch.
DEFAULT_ENABLE_TEMPLATE_API = False
DEFAULT_ALLOW_DANGEROUS_SERVICES = False
DEFAULT_EXPOSE_FULL_CONFIG = False
DEFAULT_PAIRING_MODE = PAIRING_MODE_SEAMLESS
DEFAULT_PAIRING_API_BASE = "https://seenzus.ai/api/seenzus"
CONFIG_SOURCE_MANUAL = "manual"
CONFIG_SOURCE_WEB_PAIR = "web_pair"


def normalize_pairing_mode(raw: object) -> str:
    """归一化配对模式：合法值原样返回，其余（空/未知/None）回退默认值。

    仅依赖标准库，coordinator._resolve_pairing_mode 与
    config_flow._default_pairing_mode 共用这一份实现。
    """
    mode = str(raw).strip()
    if mode in VALID_PAIRING_MODES:
        return mode
    return DEFAULT_PAIRING_MODE
