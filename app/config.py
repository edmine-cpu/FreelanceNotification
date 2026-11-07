import re
from pathlib import Path
from decimal import Decimal
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.projects import Category

LISTING_URL_TEMPLATE = "https://freelancehunt.com/projects?skills[]={skill_id}"

# Human-readable names for known skills. Unknown IDs fall back to
# "Категория #<id>", so adding a category is just a new ID in SKILL_IDS.
SKILL_NAMES: dict[int, str] = {
    99: "Веб-программирование",
    180: "Разработка ботов",
    22: "Python",
    28: "Javascript и Typescript",
    169: "Парсинг данных",
    178: "Обработка данных",
    86: "Базы данных и SQL",
    175: "AI и машинное обучение",
    189: "Автоматизация управления предприятием",
    150: "Управление клиентами и CRM",
    129: "Интеграция платежных систем",
    199: "BI и аналитика данных",
    68: "Интернет-магазины и электронная коммерция",
    181: "DevOps",
}


def parse_skill_ids(raw: str) -> list[int]:
    """Parse a comma/space/semicolon-separated list of skill IDs, preserving
    order and dropping duplicates. Raises ValueError on a non-integer token."""
    result: list[int] = []
    seen: set[int] = set()
    for token in re.split(r"[,;\s]+", raw.strip()):
        if not token:
            continue
        try:
            skill_id = int(token)
        except ValueError:
            raise ValueError(
                f"invalid SKILL_IDS entry {token!r}: expected integers like '99,180'"
            ) from None
        if skill_id not in seen:
            seen.add(skill_id)
            result.append(skill_id)
    return result


def build_category(skill_id: int, category_names: dict[int, str] | None = None) -> Category:
    custom_name = ""
    if category_names:
        custom_name = (category_names.get(skill_id) or "").strip()
    return Category(
        skill_id=skill_id,
        name=custom_name or SKILL_NAMES.get(skill_id, f"Категория #{skill_id}"),
        listing_url=LISTING_URL_TEMPLATE.format(skill_id=skill_id),
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    telegram_bot_token: SecretStr
    # Optional legacy owner's private chat, used only for one-time migration.
    telegram_chat_id: str = ""
    freelancehunt_token: SecretStr

    # Comma-separated list of FreelanceHunt skill IDs to watch, e.g. "99,180".
    # Kept as a string (not list[int]) to avoid pydantic-settings JSON-decoding
    # of complex env values; parsed into categories below.
    skill_ids: str = "180"
    # Runtime overrides loaded from StateStore. This is intentionally not part of
    # SKILL_NAMES so users can rename unknown categories from Telegram settings.
    category_names: dict[int, str] = Field(default_factory=dict)
    poll_interval: int = 60
    state_file: Path = Path("/data/state.json")
    prompt_examples_file: Path | None = None
    send_existing_on_first_run: bool = False
    page_size: int = 5
    history_size: int = 50
    # Kept separate from general bot state so every prose regeneration can
    # reuse the exact same hours, price, FX snapshot, and deadline.
    quote_file: Path = Path("/data/quotes.json")
    # Фолбэк-курсы конвертации цены (1$ = N валюты): используются, только когда
    # API курсов недоступен. Живые курсы тянутся из open.er-api.com. Правятся в .env.
    usd_uah_rate: float = 43.0
    usd_eur_rate: float = 0.92
    usd_pln_rate: float = 4.0

    anthropic_api_key: SecretStr = Field(default=SecretStr(""))
    ai_enabled: bool = True
    ai_screen_model: Literal["claude-haiku-4-5-20251001"] = "claude-haiku-4-5-20251001"
    ai_bid_model: Literal["claude-opus-5-5"] = "claude-opus-5-5"
    ai_screen_max_tokens: int = Field(default=256, ge=64, le=2048)
    ai_bid_max_tokens: int = Field(default=2048, ge=256, le=16384)
    ai_bid_effort: Literal["low", "medium"] = "low"
    ai_screen_timeout_sec: float = Field(default=30, ge=1, le=120)
    ai_bid_timeout_sec: float = Field(default=120, ge=1, le=240)
    ai_monthly_alert_usd: Decimal = Field(default=Decimal("20"), ge=0)
    ai_cost_timezone: str = "Europe/Kyiv"
    ai_max_context_chars: int = Field(default=60000, ge=4000)
    # Only populated by a verified UserContext, never ENV/user preferences.
    ai_user_id: int | None = Field(default=None, exclude=True)
    primary_filter_enabled: bool = False
    # Reply to each "core" order notification with a ready bid (owner only).
    # Off by default: bids are generated only by the button.
    ai_auto_bid: bool = False

    @field_validator("ai_user_id", mode="before")
    @classmethod
    def _ignore_unverified_ai_user(cls, value) -> None:
        # UserContext sets its verified snapshot using model_copy(update=...).
        return None

    @field_validator("ai_cost_timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @field_validator("skill_ids")
    @classmethod
    def _check_skill_ids(cls, value: str) -> str:
        if not parse_skill_ids(value):
            raise ValueError("SKILL_IDS must contain at least one integer skill ID, e.g. '99,180'")
        return value

    @property
    def categories(self) -> list[Category]:
        return [
            build_category(skill_id, self.category_names)
            for skill_id in parse_skill_ids(self.skill_ids)
        ]

    @property
    def category_label(self) -> str:
        return ", ".join(category.name for category in self.categories)

    @property
    def ai_active(self) -> bool:
        return self.ai_enabled and bool(self.anthropic_api_key.get_secret_value())

    @property
    def fallback_rates(self) -> dict[str, float]:
        """USD-based rates used when the live rates API is unavailable."""
        return {"UAH": self.usd_uah_rate, "EUR": self.usd_eur_rate, "PLN": self.usd_pln_rate}
