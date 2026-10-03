"""Exercise HA's already-installed fast path before bridge startup."""
from __future__ import annotations

import asyncio
import json
import sys
from importlib import metadata
from pathlib import Path
from types import ModuleType

import pytest
from homeassistant import requirements as ha_requirements
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import package
from packaging.requirements import Requirement

import seenzus_bridge


ROOT = Path(__file__).resolve().parents[1]
LEGACY_CONSTRAINTS = "paho-mqtt==1.6.1\n"
MODERN_CONSTRAINTS = "aiomqtt>=2.5.0\npaho-mqtt==2.1.0\n"


def _installed_mqtt(monkeypatch, tmp_path, constraints, versions, resolved):
    """Keep the real HA manager/checks, replacing only disk metadata and uv."""
    constraints_path = tmp_path / "package_constraints.txt"
    constraints_path.write_text(constraints, encoding="utf-8")
    monkeypatch.setattr(ha_requirements, "CONSTRAINT_FILE", str(constraints_path))
    installed = {"aiohttp": "3.14.3", **versions}
    monkeypatch.setattr(package, "version", installed.__getitem__)
    monkeypatch.setattr(metadata, "version", installed.__getitem__)

    def installed_requires(name):
        assert name == "aiomqtt"
        if installed[name] == "2.0.1":
            return ["paho-mqtt>=1.6.0,<2.0.0"]
        return ["paho-mqtt>=2.1.0,<3.0.0"]

    monkeypatch.setattr(metadata, "requires", installed_requires)
    # Other test modules import real MQTT libraries during collection. This
    # fixture represents a startup before those libraries have been imported.
    for name in list(sys.modules):
        if name == "aiomqtt" or name.startswith(("aiomqtt.", "paho.")) or name == "paho":
            monkeypatch.delitem(sys.modules, name)

    installs = []

    def install_package(raw_requirement, **kwargs):
        assert Path(kwargs["constraints"]) == constraints_path
        requirement = Requirement(raw_requirement)
        assert resolved[requirement.name] in requirement.specifier
        installs.append(raw_requirement)
        installed.update(resolved)
        return True

    monkeypatch.setattr(package, "install_package", install_package)
    return installed, installs


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraints", "versions", "resolved"),
    [
        (MODERN_CONSTRAINTS, {"aiomqtt": "2.4.0", "paho-mqtt": "2.1.0"},
         {"aiomqtt": "2.5.1", "paho-mqtt": "2.1.0"}),
        (MODERN_CONSTRAINTS, {"aiomqtt": "2.0.1", "paho-mqtt": "1.6.1"},
         {"aiomqtt": "2.5.1", "paho-mqtt": "2.1.0"}),
        (MODERN_CONSTRAINTS, {"aiomqtt": "2.5.1", "paho-mqtt": "1.6.1"},
         {"aiomqtt": "2.5.1", "paho-mqtt": "2.1.0"}),
        (LEGACY_CONSTRAINTS, {"aiomqtt": "2.5.1", "paho-mqtt": "1.6.1"},
         {"aiomqtt": "2.0.1", "paho-mqtt": "1.6.1"}),
        (LEGACY_CONSTRAINTS, {"aiomqtt": "2.5.1", "paho-mqtt": "2.1.0"},
         {"aiomqtt": "2.0.1", "paho-mqtt": "1.6.1"}),
    ],
    ids=["stale-aiomqtt", "core-upgrade", "mixed-modern", "mixed-legacy", "core-downgrade"],
)
async def test_startup_repairs_packages_accepted_by_manifest(
    monkeypatch, tmp_path, constraints, versions, resolved,
) -> None:
    installed, installs = _installed_mqtt(monkeypatch, tmp_path, constraints, versions, resolved)
    hass = HomeAssistant(str(tmp_path))
    try:
        manifest = json.loads((ROOT / "custom_components/seenzus_bridge/manifest.json").read_text())
        await ha_requirements.async_process_requirements(hass, "seenzus_bridge", manifest["requirements"])
        assert not installs, "Reproduce HA accepting the already-installed packages"

        assert await seenzus_bridge.async_setup(hass, {})

        assert {name: installed[name] for name in resolved} == resolved
        assert installs, "Startup must repair packages HA's manifest fast path accepted"
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("constraints", "versions"),
    [
        (LEGACY_CONSTRAINTS, {"aiomqtt": "2.0.1", "paho-mqtt": "1.6.1"}),
        (MODERN_CONSTRAINTS, {"aiomqtt": "2.5.1", "paho-mqtt": "2.1.0"}),
    ],
    ids=["ha-2025", "ha-2026"],
)
async def test_compatible_packages_do_not_trigger_install(monkeypatch, tmp_path, constraints, versions) -> None:
    installed, installs = _installed_mqtt(monkeypatch, tmp_path, constraints, versions, versions)
    hass = HomeAssistant(str(tmp_path))
    try:
        assert await seenzus_bridge.async_setup(hass, {})
        assert not installs
        assert {name: installed[name] for name in versions} == versions
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("skip", ["all", "paho-mqtt", "aiomqtt"])
async def test_skipped_installation_rejects_incompatible_pair(monkeypatch, tmp_path, skip) -> None:
    versions = {"aiomqtt": "2.5.1", "paho-mqtt": "1.6.1"}
    _, installs = _installed_mqtt(monkeypatch, tmp_path, LEGACY_CONSTRAINTS, versions, versions)
    hass = HomeAssistant(str(tmp_path))
    hass.config.skip_pip = skip == "all"
    hass.config.skip_pip_packages = [] if skip == "all" else [skip]
    try:
        with pytest.raises(ha_requirements.RequirementsNotFound):
            await seenzus_bridge.async_setup(hass, {})
        assert not installs
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_install_failure_keeps_ha_retry_limit_and_failure_history(monkeypatch, tmp_path) -> None:
    versions = {"aiomqtt": "2.4.0", "paho-mqtt": "2.1.0"}
    _installed_mqtt(monkeypatch, tmp_path, MODERN_CONSTRAINTS, versions, versions)
    attempts = []

    def fail_install(requirement, **kwargs):
        attempts.append(requirement)
        return False

    monkeypatch.setattr(package, "install_package", fail_install)
    hass = HomeAssistant(str(tmp_path))
    try:
        for _ in range(2):
            with pytest.raises(ha_requirements.RequirementsNotFound):
                await seenzus_bridge.async_setup(hass, {})
        assert len(attempts) == ha_requirements.MAX_INSTALL_FAILURES
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_install_success_is_rechecked_against_actual_metadata(monkeypatch, tmp_path) -> None:
    versions = {"aiomqtt": "2.5.1", "paho-mqtt": "1.6.1"}
    _installed_mqtt(monkeypatch, tmp_path, MODERN_CONSTRAINTS, versions, versions)
    monkeypatch.setattr(package, "install_package", lambda *args, **kwargs: True)
    hass = HomeAssistant(str(tmp_path))
    try:
        with pytest.raises(ha_requirements.RequirementsNotFound):
            await seenzus_bridge.async_setup(hass, {})
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "loaded_version"),
    [("aiomqtt", "2.4.0"), ("paho.mqtt", "1.6.1")],
)
async def test_cached_old_modules_require_restart(monkeypatch, tmp_path, module_name, loaded_version) -> None:
    versions = {"aiomqtt": "2.5.1", "paho-mqtt": "2.1.0"}
    _installed_mqtt(monkeypatch, tmp_path, MODERN_CONSTRAINTS, versions, versions)
    loaded = ModuleType(module_name)
    loaded.__version__ = loaded_version
    monkeypatch.setitem(sys.modules, module_name, loaded)
    hass = HomeAssistant(str(tmp_path))
    try:
        with pytest.raises(HomeAssistantError, match="restart Home Assistant"):
            await seenzus_bridge.async_setup(hass, {})
        assert sys.modules[module_name] is loaded
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
async def test_inactive_core_and_dependency_markers_are_ignored(monkeypatch, tmp_path) -> None:
    versions = {"aiomqtt": "2.5.1", "paho-mqtt": "2.1.0"}
    constraints = MODERN_CONSTRAINTS + "paho-mqtt==0.0.0; python_version<'1'\n"
    _, installs = _installed_mqtt(monkeypatch, tmp_path, constraints, versions, versions)
    monkeypatch.setattr(metadata, "requires", lambda name: [
        "paho-mqtt>=2.1.0,<3.0.0", "paho-mqtt==0.0.0; python_version<'1'",
    ])
    hass = HomeAssistant(str(tmp_path))
    try:
        assert await seenzus_bridge.async_setup(hass, {})
        assert not installs
    finally:
        await hass.async_stop(force=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("old_aiomqtt", ["2.4.0", "2.5.1"])
@pytest.mark.parametrize("repair_timing", ["after-check", "before-installer"])
async def test_other_integration_repairs_mqtt_before_bridge_resolves(
    monkeypatch, tmp_path, old_aiomqtt, repair_timing,
) -> None:
    """A stale incompatibility result must not exclude a newly repaired version."""
    versions = {"aiomqtt": old_aiomqtt, "paho-mqtt": "1.6.1"}
    resolved = {"aiomqtt": "2.0.1", "paho-mqtt": "1.6.1"}
    installed, installs = _installed_mqtt(monkeypatch, tmp_path, LEGACY_CONSTRAINTS, versions, resolved)
    hass = HomeAssistant(str(tmp_path))
    repair_requested = asyncio.Event()
    repair_done = asyncio.Event()
    real_executor = hass.async_add_executor_job
    real_process = ha_requirements.async_process_requirements

    def install_package(raw_requirement, **kwargs):
        installs.append(raw_requirement)
        requirement = Requirement(raw_requirement)
        if resolved[requirement.name] not in requirement.specifier:
            return False
        installed.update(resolved)
        return True

    monkeypatch.setattr(package, "install_package", install_package)

    async def checked_executor(job, *args):
        result = await real_executor(job, *args)
        # The bridge's metadata check takes one requirement list. HA's uv
        # worker takes that list plus kwargs, so it is not paused here.
        if repair_timing == "after-check" and len(args) == 1 and isinstance(args[0], list):
            failures = result[0] if isinstance(result, tuple) else result
            if failures and not repair_requested.is_set():
                repair_requested.set()
                await asyncio.sleep(0)  # Let the other integration request HA's lock.
                if not ha_requirements._async_get_manager(hass).pip_lock.locked():
                    await repair_done.wait()
        return result

    async def process(hass, name, requirements, *args, **kwargs):
        if repair_timing == "before-installer" and name == "seenzus_bridge" and any(
            "!=" in req for req in requirements
        ):
            repair_requested.set()
            await repair_done.wait()
        await real_process(hass, name, requirements, *args, **kwargs)

    async def repair_other_integration():
        await repair_requested.wait()
        try:
            await real_process(hass, "other_integration", ["aiomqtt==2.0.1"])
        finally:
            repair_done.set()

    monkeypatch.setattr(hass, "async_add_executor_job", checked_executor)
    monkeypatch.setattr(ha_requirements, "async_process_requirements", process)
    tasks = []
    try:
        manifest = json.loads((ROOT / "custom_components/seenzus_bridge/manifest.json").read_text())
        await real_process(hass, "seenzus_bridge", manifest["requirements"])
        assert not installs, "Start from the already-installed manifest fast path"
        tasks = [
            asyncio.create_task(seenzus_bridge.async_setup(hass, {})),
            asyncio.create_task(repair_other_integration()),
        ]
        results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
        assert results[0] is True
        assert {name: installed[name] for name in resolved} == resolved
        assert installs == ["aiomqtt==2.0.1"], "The bridge must accept the repaired pair"
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await hass.async_stop(force=True)
