"""Deployment identity is persisted per entry; never inferred from MQTT hosts."""
from urllib.parse import urlencode, urlsplit, urlunsplit

from .const import CONF_CONFIG_SOURCE, CONF_PAIRING_MODE, CONF_PAIRING_API_BASE, CONFIG_SOURCE_MANUAL, PAIRING_MODE_MANUAL, DEFAULT_PAIRING_API_BASE

CONF_ENVIRONMENT = "environment"
CONF_WORLD_ID = "world_id"
CONF_WORLD_NAME = "world_name"
RELAY_URL = "https://app.seenzus.cn/ha-connect"
ENVIRONMENTS = {
    "cn": ("国内 PRD", "https://app.seenzus.cn/api/seenzus"),
    "global": ("境外 PRD", "https://seenzus.ai/api/seenzus"),
    "uat": ("UAT", "https://uat.seenzus.ai/api/seenzus"),
}
ENVIRONMENT_SLOTS = frozenset((*ENVIRONMENTS, "custom"))


def environment_for_api(api_base: str) -> str:
    origin = urlsplit(api_base)
    for key, (_, api) in ENVIRONMENTS.items():
        known = urlsplit(api)
        if (origin.scheme, origin.hostname, origin.port or 443) == (known.scheme, known.hostname, 443):
            return key
    return "custom"


def bootstrap_url(api_base: str | None, params: dict) -> str:
    if api_base:
        parsed = urlsplit(api_base)
        base = urlunsplit((parsed.scheme, parsed.netloc, "/pair/bootstrap", "", ""))
    else:
        base = RELAY_URL
    return f"{base}?{urlencode({k: v for k, v in params.items() if v})}"


def entry_environment(config: dict) -> str:
    """One slot per deployment class, including a single slot for all Dev Server URLs."""
    saved = config.get(CONF_ENVIRONMENT)
    if saved in ENVIRONMENT_SLOTS:
        return saved
    if config.get(CONF_CONFIG_SOURCE) == CONFIG_SOURCE_MANUAL or config.get(CONF_PAIRING_MODE) == PAIRING_MODE_MANUAL:
        return "custom"
    return environment_for_api(config.get(CONF_PAIRING_API_BASE) or DEFAULT_PAIRING_API_BASE)
