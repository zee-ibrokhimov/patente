"""Callback payloads.

Telegram caps callback_data at 64 bytes, so these carry ids and nothing else —
never text, never state. Everything else is re-fetched from the API, which also
means a button still works after a bot restart.
"""

from aiogram.filters.callback_data import CallbackData


class Answer(CallbackData, prefix="a"):
    qid: int
    value: bool


class NextQuestion(CallbackData, prefix="n"):
    exclude: int = 0


class ReportBad(CallbackData, prefix="r"):
    qid: int


class SetLanguage(CallbackData, prefix="l"):
    code: str


class Simple(CallbackData, prefix="s"):
    action: str  # toggle_translations | unlock | delete_yes | delete_no | settings
