"""Fail closed on insecure remote MQTT endpoints, before opening a socket."""
from __future__ import annotations

from collections.abc import Mapping
from ipaddress import ip_address, ip_network

from .const import CONF_MQTT_HOST, CONF_MQTT_SCHEME, VALID_MQTT_SCHEMES

# Explicit ranges, not is_private (which also includes reserved/documentation IPs).
# Hostnames always require TLS: resolving one now cannot prevent DNS rebinding
# when the MQTT library resolves it again at connection time.
_LOCAL_NETWORKS = tuple(ip_network(network) for network in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8",
    "169.254.0.0/16", "::1/128", "fc00::/7", "fe80::/10",
))


class MqttTransportError(ValueError):
    """Connection needs an explicit, safe transport configuration."""


def validate_mqtt_transport(conf: Mapping[str, object]) -> str:
    """Return the explicit scheme or reject it; never infer plaintext or TLS."""
    scheme = str(conf.get(CONF_MQTT_SCHEME) or "").strip().lower()
    if scheme not in VALID_MQTT_SCHEMES:
        raise MqttTransportError("mqtt_transport_required")
    if scheme in ("mqtts", "wss"):
        return scheme
    host = str(conf.get(CONF_MQTT_HOST) or "").strip()
    try:
        address = ip_address(host)
    except ValueError:
        raise MqttTransportError("mqtt_tls_required") from None
    # Reject IPv4-mapped and other alternate representations rather than let
    # the socket layer interpret a different destination from our allow-list.
    if not any(address in network for network in _LOCAL_NETWORKS):
        raise MqttTransportError("mqtt_tls_required")
    return scheme
