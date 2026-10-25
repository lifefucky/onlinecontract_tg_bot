"""Repository root. Importing this module does not read TOKEN."""

from pathlib import Path


def project_root() -> Path:
    """Directory that contains ``bot.py`` and this ``misc`` package."""
    return Path(__file__).resolve().parent.parent
