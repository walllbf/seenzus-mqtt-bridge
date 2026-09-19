"""Resolve exact HA compatibility jobs from the official stable release channel."""
from __future__ import annotations

import argparse
import json
import re
from urllib.request import Request, urlopen

MINIMUM_CORE = "2025.1.4"
STABLE_URL = "https://version.home-assistant.io/stable.json"
CORE_SOURCE = "https://raw.githubusercontent.com/home-assistant/core"


def read_url(url: str) -> str:
    request = Request(url, headers={"User-Agent": "seenzus-bridge-compatibility-ci/1.0"})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8")


def resolve_matrix(version: str = "", *, fetch=read_url) -> dict:
    """Select minimum + stable (or an explicit reproduction version), without fallback."""
    if not version:
        release = json.loads(fetch(STABLE_URL))
        if release["channel"] != "stable":
            raise ValueError("Expected the official stable release channel")
        version = release["homeassistant"]["default"]
    jobs = []
    for core in dict.fromkeys((MINIMUM_CORE, version)):
        if not re.fullmatch(r"20\d{2}\.(?:[1-9]|1[0-2])\.(?:0|[1-9]\d*)", core):
            raise ValueError(f"Expected an exact stable HA Core version, got {core!r}")
        if tuple(map(int, core.split("."))) < (2025, 1, 4):
            raise ValueError(f"Core must be at least {MINIMUM_CORE}")
        # The minimum release predates .python-version; retain its proven CI runtime.
        python = "3.13" if core == MINIMUM_CORE else fetch(f"{CORE_SOURCE}/{core}/.python-version").strip()
        if not re.fullmatch(r"3\.\d+(?:\.\d+)?", python):
            raise ValueError(f"Invalid upstream Python version: {python!r}")
        requirements = fetch(f"{CORE_SOURCE}/{core}/requirements_test.txt")
        pins = []
        for package in ("pytest", "pytest-asyncio", "pytest-timeout"):
            matches = re.findall(rf"^{package}==([0-9]+(?:\.[0-9]+){{1,2}})\s*$", requirements, re.MULTILINE)
            if len(matches) != 1:
                raise ValueError(f"Missing or ambiguous {package} pin for Core {core}")
            pins.append(f"{package}=={matches[0]}")
        jobs.append({
            "home_assistant": core,
            "python": python,
            "core": f"homeassistant=={core}",
            "test_dependencies": " ".join(pins),
        })
    return {"include": jobs}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="", help="Exact stable Core version to reproduce instead of latest")
    args = parser.parse_args()
    print(json.dumps(resolve_matrix(args.version), separators=(",", ":")))
