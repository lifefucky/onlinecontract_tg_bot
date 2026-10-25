"""Load the project ``.env`` once. Variables already set in the process stay as they are."""

from pathlib import Path

from dotenv import load_dotenv

_loaded = False


def load_project_env() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    root = Path(__file__).resolve().parent.parent
    load_dotenv(root / ".env", override=False)
    # Older checkouts kept the embedding key here. It fills gaps only.
    load_dotenv(root / "docs" / "research" / ".env", override=False)
