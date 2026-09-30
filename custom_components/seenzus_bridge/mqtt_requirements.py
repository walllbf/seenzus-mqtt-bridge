"""Reconcile installed MQTT libraries with the running Core before importing them."""
from __future__ import annotations

import json
import sys
from importlib import metadata
from pathlib import Path

from homeassistant import requirements as ha_requirements
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import package
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

from .const import DOMAIN

MQTT_PACKAGES = {"aiomqtt", "paho-mqtt"}


def _core_mqtt_requirements(config_dir: str) -> list[str]:
    """Intersect the shipped bounds with Core's actual runtime constraints."""
    manifest = json.loads(Path(__file__).with_name("manifest.json").read_text(encoding="utf-8"))
    requirements: dict[str, Requirement] = {}
    for raw in manifest["requirements"]:
        req = Requirement(raw)
        name = canonicalize_name(req.name)
        if name in MQTT_PACKAGES:
            requirements[name] = req
    constraints = Path(ha_requirements.pip_kwargs(config_dir)["constraints"])
    for raw in constraints.read_text(encoding="utf-8").splitlines():
        if not (line := raw.split("#", 1)[0].strip()):
            continue
        constraint = Requirement(line)
        if constraint.marker and not constraint.marker.evaluate():
            continue
        if req := requirements.get(canonicalize_name(constraint.name)):
            req.specifier &= constraint.specifier
    return [str(req) for req in requirements.values()]


def _unsatisfied_requirements(requirements: list[str]) -> list[str]:
    """Check both Core bounds and aiomqtt's declared Paho dependency."""
    missing = [req for req in requirements if not package.is_installed(req)]
    if missing:
        return missing
    for raw in metadata.requires("aiomqtt") or []:
        dependency = Requirement(raw)
        if canonicalize_name(dependency.name) != "paho-mqtt":
            continue
        if dependency.marker and not dependency.marker.evaluate():
            continue
        if not package.is_installed(str(dependency)):
            missing.append(str(dependency))
    return missing


def _check_loaded_versions() -> None:
    """An installer cannot replace module objects already used by other integrations."""
    for distribution, module_name in (("aiomqtt", "aiomqtt"), ("paho-mqtt", "paho.mqtt")):
        module = sys.modules.get(module_name)
        loaded = getattr(module, "__version__", None)
        if loaded is not None and loaded != metadata.version(distribution):
            raise HomeAssistantError(
                f"{distribution} was updated after import; restart Home Assistant "
                "to load the compatible MQTT libraries"
            )


async def async_ensure_mqtt_requirements(hass: HomeAssistant) -> None:
    """Use HA's installer, constraints, pip lock and failure history for repairs."""
    requirements = await hass.async_add_executor_job(_core_mqtt_requirements, hass.config.config_dir)
    if not hass.config.skip_pip:
        # Stricter strings defeat HA's manifest cache/installed-version fast
        # path without clearing its cache or bypassing its installer lock.
        await ha_requirements.async_process_requirements(hass, DOMAIN, requirements)

    failures = await hass.async_add_executor_job(_unsatisfied_requirements, requirements)
    if failures and not hass.config.skip_pip and not (
        MQTT_PACKAGES & set(hass.config.skip_pip_packages)
    ):
        # Both versions can meet Core's individual bounds while aiomqtt needs
        # a different Paho major. Exclude the incompatible installed aiomqtt
        # version so HA actually invokes its resolver, which backtracks under
        # Core's constraints (including Paho 1.6 on legacy Core).
        current = await hass.async_add_executor_job(metadata.version, "aiomqtt")
        aiomqtt = next(Requirement(raw) for raw in requirements if Requirement(raw).name == "aiomqtt")
        aiomqtt.specifier &= SpecifierSet(f"!={current}")
        await ha_requirements.async_process_requirements(hass, DOMAIN, [str(aiomqtt)])
        failures = await hass.async_add_executor_job(_unsatisfied_requirements, requirements)

    if failures:
        raise ha_requirements.RequirementsNotFound(DOMAIN, failures)
    await hass.async_add_executor_job(_check_loaded_versions)
