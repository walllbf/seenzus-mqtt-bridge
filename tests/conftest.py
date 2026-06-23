from pathlib import Path
import sys
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Repo root holds the `tests` package; custom_components holds the integration
# package `seenzus_bridge`. Put both on sys.path so tests can import either.
REPO_ROOT = Path(__file__).resolve().parents[1]
for path in (REPO_ROOT, REPO_ROOT / "custom_components"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
