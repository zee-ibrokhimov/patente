"""The core loop: question -> Vero/Falso -> verdict + explanation -> next."""

from __future__ import annotations

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from bot import quiz_flow, render
from bot.api_client import ApiClient
from bot.callbacks import Answer, NextQuestion, ReportBad, Simple
from bot.i18n import t

router = Router(name="quiz")


@router.message(Command("quiz"))
async def quiz(message: Message, lang: str, api: ApiClient, bot: Bot):
    await quiz_flow.send_question(bot, api, message.chat.id, lang)


@router.callback_query(Answer.filter())
async def answer(
    query: CallbackQuery, callback_data: Answer, lang: str, api: ApiClient
):
    outcome = await api.answer(query.from_user.id, callback_data.qid, callback_data.value)

    # The statement is already on screen; reuse it rather than re-fetching, which
    # would also advance the Leitner schedule for a question being re-rendered.
    shown = query.message.caption or query.message.text or ""
    question = {"id": callback_data.qid, "statement_it": shown.split("\n\n")[0]}

    await quiz_flow.show_result(query.message, question, outcome, lang)
    await query.answer()


@router.callback_query(NextQuestion.filter())
async def next_question(
    query: CallbackQuery, callback_data: NextQuestion, lang: str, api: ApiClient, bot: Bot
):
    # Drop the buttons from the answered message so the history stays tappable-free.
    try:
        await query.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.answer()
    await quiz_flow.send_question(
        bot, api, query.message.chat.id, lang, exclude_id=callback_data.exclude or None
    )


@router.callback_query(ReportBad.filter())
async def report(query: CallbackQuery, callback_data: ReportBad, lang: str, api: ApiClient):
    """The report button. You are the first heavy user of it (plan §3.3)."""
    await api.report(query.from_user.id, callback_data.qid)
    await query.answer(t(lang, "report_thanks"), show_alert=True)


@router.callback_query(Simple.filter())
async def unlock(query: CallbackQuery, callback_data: Simple, lang: str):
    if callback_data.action != "unlock":
        return
    # Payments land at build step 9. Saying so plainly beats a dead button.
    await query.answer(t(lang, "payments_not_live"), show_alert=True)


@router.message(Command("stats"))
async def stats(message: Message, lang: str, api: ApiClient):
    data = await api.stats(message.from_user.id)
    await message.answer(render.stats(data, lang))
