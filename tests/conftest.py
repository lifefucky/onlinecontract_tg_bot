"""Pytest configuration: TOKEN must exist before importing tgbot.app or misc."""

import os

# Формат как у Telegram Bot API, иначе aiogram отклонит токен при создании Bot.
os.environ.setdefault(
    "TOKEN",
    "000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
)
