"""Verify dependencies against the constraints used by HA's runtime installer."""
import json
from importlib.metadata import version
from pathlib import Path

import homeassistant
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]


def test_manifest_dependencies_satisfy_running_core_constraints() -> None:
    manifest = json.loads(
        (ROOT / "custom_components/seenzus_bridge/manifest.json").read_text(encoding="utf-8")
    )
    constraints_path = Path(homeassistant.__file__).with_name("package_constraints.txt")
    constraints = [
        Requirement(line)
        for raw_line in constraints_path.read_text(encoding="utf-8").splitlines()
        if (line := raw_line.split("#", 1)[0].strip())
    ]
    for raw_requirement in manifest["requirements"]:
        requirement = Requirement(raw_requirement)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        installed = version(requirement.name)
        assert installed in requirement.specifier, (
            f"Installed {requirement.name}=={installed} does not satisfy {requirement}"
        )
        for constraint in constraints:
            if canonicalize_name(constraint.name) != canonicalize_name(requirement.name):
                continue
            if constraint.marker and not constraint.marker.evaluate():
                continue
            assert installed in constraint.specifier, (
                f"{requirement.name}=={installed} violates HA runtime constraint {constraint}"
            )
