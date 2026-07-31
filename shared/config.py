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
    tribute_product_1m: str = ""
    tribute_product_3m: str = ""

    # Superseded by the 7-day trial (shared.constants.TRIAL_DAYS). Three explanations
    # sampled ONE feature; a week samples the product. Kept as a setting rather than
    # deleted so the old behaviour is one env var away.
    free_explanations: int = 0
    admin_chat_ids: str = ""
    support_contact: str = ""

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
