"""Callback payloads.

Telegram caps callback_data at 64 bytes, so these carry ids and nothing else —
never text, never state. Everything else is re-fetched from the API, which also
means a button still works after a bot restart.

The drilling callbacks (answer, next, why, report) went with the study loop when it
moved to the Mini App: a question rendered in chat needed a button per action, while
the app calls the API directly.
"""

from aiogram.filters.callback_data import CallbackData


class SetLanguage(CallbackData, prefix="l"):
    code: str


class Simple(CallbackData, prefix="s"):
    action: str  # toggle_translations | subscribe | delete_yes | delete_no | language
