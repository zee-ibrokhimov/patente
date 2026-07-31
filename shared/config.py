"""Central configuration. Everything reads settings from here, nothing reads os.environ."""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent

# Content pipeline paths
QUESTIONS_DIR = ROOT / "questions"
CONTENT_OUT = ROOT / "content" / "out"
IMAGES_DIR = CONTENT_OUT / "images"
QUESTIONS_JSON = CONTENT_OUT / "questions.json"

# The ministerial listato currently in use. Bump when the Ministry reissues it;
# `source_version` on every question row is what makes a diff-based reseed possible.
SOURCE_PDF = QUESTIONS_DIR / "domande AB italiano 23 04 2025.pdf"
SOURCE_VERSION = "2025-04-23"

# Hard invariants for this edition of the listato. extract.py refuses to write
# output that violates them — a silent one-row misalignment between statement and
# answer key would poison translations, explanations and the paywall alike.
EXPECTED_STATEMENTS = 7106
EXPECTED_QUESITI = 715
EXPECTED_TOPICS = 25


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: str = "dev"
    bot_token_dev: str = ""
    bot_token_prod: str = ""
    bot_username: str = ""

    api_base_url: str = "http://127.0.0.1:8000"
    database_url: str = "sqlite+aiosqlite:///./patente.db"

    # Where the Mini App is served. Drilling lives there rather than in chat, so this
    # is how the bot hands a user over. Telegram requires https and refuses to attach
    # a web_app button to anything else; left empty the bot still works and simply
    # omits the button, rather than failing at send time on every /start.
    webapp_url: str = ""

    openai_api_key: str = ""
    openai_model: str = "gpt-4o"

    # Plan §4.4 expected translation to be the one place a cheaper model pays off:
    # literal rendering of one sentence, no legal reasoning. **Measured, it is not.**
    # gpt-4o-mini renders "il segnale raffigurato" as "сигнал" — a signal, not a road
    # sign — while gpt-4o and gpt-5-mini both say "знак" correctly. A glossary in the
    # prompt fixed the English and not the Russian, so it is the model.
    #
    # There is also nothing to save: the whole bank is roughly EUR 15 at gpt-4o, against
    # EUR 75 for the explanations. Left empty deliberately, so this falls back to
    # `openai_model` — set it only to go *up*.
    openai_translate_model: str = ""

    tribute_api_key: str = ""
    tribute_webhook_secret: str = ""
    # One product id per tier. `tribute_products` below derives the mapping from these,
    # so adding a tier cannot silently miss one — see api/services/purchases.tier_for.
    # ONE checkout link for the whole subscription. This is what Tribute actually
    # gives you: a subscription is a single object carrying several periods, and the
    # buyer picks the period on Tribute's own page. Prefer the t.me form
    # (https://t.me/tribute/app?startapp=...) over web.tribute.tg — it opens inside
    # Telegram instead of throwing the user into a browser mid-purchase.
    tribute_link: str = ""

    # Per-language checkout links. A Tribute subscription carries its own name and
    # description, so selling to a Russian speaker and an Italian one from the same
    # object means one of them reads the pitch in a language they did not choose — at
    # the exact moment they are deciding whether to pay.
    #
    # One subscription per language, each with its own link. Missing ones fall back to
    # `tribute_link`, so languages can be added as they are created without any code
    # change or redeploy.
    tribute_link_ru: str = ""
    tribute_link_it: str = ""
    tribute_link_en: str = ""
    tribute_link_uz: str = ""

    # Per-tier links, for the other shape: separate products, each with its own URL.
    # Unused when `tribute_link` is set.
    #
    # Separate from the product ids below because they answer different questions: a
    # LINK is how someone starts paying, an ID is how an incoming webhook is matched to
    # a tier. A subscription webhook carries no product id at all (the tier comes from
    # `period`), so the ids are needed only for one-off digital products.
    tribute_link_1m: str = ""
    tribute_link_3m: str = ""
    tribute_link_6m: str = ""

    tribute_product_1m: str = ""
    tribute_product_3m: str = ""
    tribute_product_6m: str = ""

    # Superseded by the 7-day trial (shared.constants.TRIAL_DAYS). Three explanations
    # sampled ONE feature; a week samples the product. Kept as a setting rather than
    # deleted so the old behaviour is one env var away.
    free_explanations: int = 0
    admin_chat_ids: str = ""

    # The Telegram channel Tribute sells access to. Membership of it is a second,
    # independent record of who has paid — see api/services/channel.py. Empty disables
    # every channel check, so the app runs unchanged without one.
    premium_channel_id: str = ""
    support_contact: str = ""

    def checkout_url(self, lang: str) -> str:
        """Where to send this user to pay. Their language if it exists, else the default.

        Returns "" when nothing is configured, which is what stops the Buy button being
        drawn at all.
        """
        per_language = {
            "ru": self.tribute_link_ru,
            "it": self.tribute_link_it,
            "en": self.tribute_link_en,
            "uz": self.tribute_link_uz,
        }
        return (per_language.get(lang) or "").strip() or self.tribute_link.strip()

    @property
    def any_checkout_link(self) -> bool:
        return bool(
            self.tribute_link.strip()
            or self.tribute_link_ru.strip() or self.tribute_link_it.strip()
            or self.tribute_link_en.strip() or self.tribute_link_uz.strip()
        )

    @property
    def can_sell(self) -> bool:
        """Whether a Buy button can lead anywhere at all.

        The webhook secret is part of the test on purpose: verify() fails closed without
        it, so selling first would mean taking money and rejecting every delivery that
        says so.
        """
        return bool(self.tribute_webhook_secret and (self.any_checkout_link or self.tribute_links))

    @property
    def tribute_links(self) -> dict[str, str]:
        """tier -> checkout URL, for whichever tiers have one configured."""
        # Imported inside the method, matching tribute_products below: shared.constants
        # is imported by modules that also read settings, and a module-level import here
        # closes that loop.
        from shared.constants import TIER_1M, TIER_3M, TIER_6M

        pairs = {
            TIER_1M: self.tribute_link_1m,
            TIER_3M: self.tribute_link_3m,
            TIER_6M: self.tribute_link_6m,
        }
        return {tier: url.strip() for tier, url in pairs.items() if url.strip()}

    @property
    def tribute_products(self) -> dict[str, str]:
        """{product id: tier}, skipping tiers with no id configured yet.

        Derived rather than hand-written: the previous version had an if-chain naming
        1m and 3m, so adding the 6-month tier meant a EUR 10.99 purchase fell through to
        "unrecognised" and granted 30 days. The customer pays for six months and gets
        one, and the only sign is a log line nobody reads.
        """
        from shared.constants import TIER_1M, TIER_3M, TIER_6M

        pairs = {
            self.tribute_product_1m: TIER_1M,
            self.tribute_product_3m: TIER_3M,
            self.tribute_product_6m: TIER_6M,
        }
        return {pid: tier for pid, tier in pairs.items() if pid}

    @property
    def translate_model(self) -> str:
        return self.openai_translate_model or self.openai_model

    @property
    def bot_token(self) -> str:
        return self.bot_token_prod if self.env == "prod" else self.bot_token_dev

    @property
    def admin_ids(self) -> list[int]:
        return [int(x) for x in self.admin_chat_ids.split(",") if x.strip()]


settings = Settings()
