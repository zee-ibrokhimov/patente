from aiogram import Router

from bot.handlers import misc, onboarding, quiz


def build_router() -> Router:
    """Order matters: onboarding claims the language callbacks before misc's
    catch-all `s:` filters, and quiz's Simple handler only answers `unlock`."""
    router = Router(name="root")
    router.include_router(onboarding.router)
    router.include_router(misc.router)
    router.include_router(quiz.router)
    return router


__all__ = ["build_router"]
