"""常量定义 - 不引入任何第三方依赖，供 config_flow 安全导入."""

DOMAIN = "seenzus_bridge"
BRIDGE_VERSION = "0.1.4"

# 用户可配置项
CONF_MQTT_HOST      = "mqtt_host"
CONF_MQTT_PORT      = "mqtt_port"
CONF_MQTT_USERNAME  = "mqtt_username"
CONF_MQTT_PASSWORD  = "mqtt_password"
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

# 默认值
DEFAULT_MQTT_PORT      = 1883
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
