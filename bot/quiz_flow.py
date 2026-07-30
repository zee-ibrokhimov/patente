"""Sending and updating a question message.

Shared by /start, /quiz and the Next button so the three cannot drift apart.

Figure handling implements the file_id cache (plan §6.4): a figure's bytes are
uploaded to Telegram exactly once, the returned file_id is handed back to the API,
and every later send of that figure is by id. 409 figures serve 3946 statements,
so this is the difference between one upload each and one per user per question.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.types import BufferedInputFile, Message

from bot import keyboards, render
from bot.render import CAPTION_LIMIT
from bot.api_client import ApiClient, ApiError
from bot.i18n import t

log = logging.getLogger(__name__)


async def send_question(
    bot: Bot, api: ApiClient, chat_id: int, lang: str, exclude_id: int | None = None
) -> Message | None:
    try:
        question = await api.next_question(chat_id, exclude_id=exclude_id)
    except ApiError as exc:
        if exc.status == 404:
            await bot.send_message(chat_id, t(lang, "no_questions"))
            return None
        raise

    caption = render.question(question, lang)
    markup = keyboards.answer_buttons(question["id"], lang)

    if not question.get("image"):
        return await bot.send_message(chat_id, caption, reply_markup=markup)

    name = question["image"].rsplit("/", 1)[-1]
    if question.get("image_file_id"):
        return await bot.send_photo(
            chat_id, question["image_file_id"], caption=caption, reply_markup=markup
        )

    data = await api.figure_bytes(name)
    message = await bot.send_photo(
        chat_id, BufferedInputFile(data, filename=name), caption=caption, reply_markup=markup
    )
    try:
        await api.cache_file_id(name, message.photo[-1].file_id)
    except ApiError:
        # Worth a re-upload next time, not worth failing the user's question.
        log.warning("could not cache file_id for %s", name, exc_info=True)
    return message


async def append_explanation(
    message: Message, result: dict, lang: str, question: dict
) -> None:
    """Add the explanation to the answered message, in place.

    Editing rather than sending a second bubble keeps the statement, the verdict and the
    reasoning together — which is the thing being sold, and it reads badly split across
    two messages.
    """
    body = message.caption or message.text or ""
    limit = CAPTION_LIMIT if message.photo else render.MESSAGE_LIMIT
    text = render.with_explanation(body, result["explanation"], limit=limit)
    markup = keyboards.after_answer(question["id"], lang, explained=True)
    if message.photo:
        await message.edit_caption(caption=text, reply_markup=markup)
    else:
        await message.edit_text(text, reply_markup=markup)


async def show_result(message: Message, question: dict, outcome: dict, lang: str) -> None:
    """Rewrite the question message in place with the verdict.

    Editing rather than sending keeps the statement and the verdict together in
    one bubble, and removing the Vero/Falso keyboard is what stops a double tap
    recording a second answer.
    """
    state = outcome["explanation_state"]
    markup = keyboards.after_answer(
        question["id"], lang,
        locked=state == "locked",
        explained=state == "shown",
        # Warming has not landed for this cluster, so the explanation exists only as an
        # offer. The button is the fallback path — normally the text is already here.
        offered=state == "available",
    )

    if message.photo:
        text = render.result(question, outcome, lang, limit=render.CAPTION_LIMIT)
        await message.edit_caption(caption=text, reply_markup=markup)
    else:
        text = render.result(question, outcome, lang, limit=render.MESSAGE_LIMIT)
        await message.edit_text(text, reply_markup=markup)
