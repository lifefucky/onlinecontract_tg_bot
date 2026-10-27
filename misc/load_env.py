"""Load the project ``.env`` once. Variables already set in the process stay as they are.

The root ``.env`` is the only source: a legacy ``docs/research/.env`` is no longer read.
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
