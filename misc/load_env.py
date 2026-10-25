"""Load the project ``.env`` once. Variables already set in the process stay as they are.

The root ``.env`` is the source. ``docs/research/.env`` is a legacy gap-fill: it is
read only when that file exists and does not override variables already set.
"""

from dotenv import load_dotenv

from misc.paths import project_root

_loaded = False


def load_project_env() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    root = project_root()
    load_dotenv(root / ".env", override=False)
    legacy = root / "docs" / "research" / ".env"
    if legacy.is_file():
        load_dotenv(legacy, override=False)
