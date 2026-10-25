from os import environ
from typing import Final

from misc.load_env import load_project_env

load_project_env()

_token = environ.get("TOKEN", "").strip()
if not _token:
    raise RuntimeError(
        "Environment variable TOKEN is not set. Export TOKEN before starting the bot."
    )


class TgKeys:
    TOKEN: Final = _token
