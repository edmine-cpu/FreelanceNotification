from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Literal, Protocol

from app.projects import Project

log = logging.getLogger(__name__)

# "omit", a legacy hour count ("8") or "<work type>:<size>" ("crm:L").
ScopeTier = str
Language = Literal["ru", "ua"]

ALLOWED_HOURS = (4, 8, 16, 24, 40, 80)
WORK_TYPES = ("bot", "parsing", "integration", "ai", "crm", "site", "fix", "other")
SIZES = ("S", "M", "L", "XL")
# Tiers the model may return now. Hour tiers stay valid for stored quotes.
BID_TIERS = {"omit", *(f"{kind}:{size}" for kind in WORK_TYPES for size in SIZES)}
ALLOWED_TIERS = BID_TIERS | {str(hours) for hours in ALLOWED_HOURS}
POLICY_VERSION = "quote-v2"
DEFAULT_HOURLY_RATE_USD = Decimal("8")
DEFAULT_RATES = {"UAH": Decimal("43"), "EUR": Decimal("0.92"), "PLN": Decimal("4")}
SCOPE_PROMPT_FILE = Path(__file__).parent / "prompts" / "scope_prompt.md"
PRICE_TABLE_FILE = Path(__file__).parent / "prompts" / "price_table.json"

_STEPS = {
    "UAH": Decimal("500"),
    "USD": Decimal("10"),
    "EUR": Decimal("10"),
    "PLN": Decimal("50"),
}
_CURRENCY_LABELS = {"UAH": "грн", "USD": "$", "EUR": "€", "PLN": "zł"}
_DASH_TRANSLATION = str.maketrans(
    {character: "-" for character in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"}
)
_CURRENCY_OR_PLACEHOLDER_RE = re.compile(
    r"(?i)(\{price\}|\{deadline\}|\b(?:UAH|USD|EUR|PLN)\b|"
    r"грн|доллар|долар|евро|євро|злот|zł|[$€])"
)
_QUOTE_LABEL_RE = re.compile(
    r"(?i)^\s*(?:ориентировоч|орієнтовн|цена\b|ціна\b|стоимость\b|"
    r"вартість\b|бюджет\b|срок\b|строк\b|термін\b|price\b|deadline\b)"
)
_DEADLINE_TOPIC_RE = re.compile(
    r"(?i)\b(?:срок|сроки|сроков|срокам|сроках|сроком|"
    r"строк|строки|строків|строкам|строках|строком|"
    r"термін|терміни|термінів|термінам|термінах|терміном|"
    r"deadline)\b"
)
_PRICING_DISCUSSION_RE = re.compile(
    r"(?i)\b(?:по|щодо)\s+(?:цене|ценам|стоимости|вартості|бюджету)\b|"
    r"\b(?:что\s+касается|що\s+стосується)\s+"
    r"(?:цены|цiни|ціни|стоимости|вартості|бюджета|бюджету)\b|"
    r"\b(?:обсудим|согласуем|уточним|обговоримо|узгодимо|уточнимо)\s+"
    r"(?:цену|цiну|ціну|стоимость|вартість|бюджет)\b|"
    r"\b(?:цену|цiну|ціну|стоимость|вартість|бюджет)\s+"
    r"(?:обсудим|согласуем|уточним|обговоримо|узгодимо|уточнимо)\b"
)
_NUMERIC_QUOTE_RE = re.compile(
    r"(?i)^\s*(?:(?:ориентировочно|орієнтовно|примерно|приблизно|"
    r"предлагаю|пропоную|ставка)\s*[:=-]?\s*)?\d+(?:[.,]\d+)?\s*$"
)
_NUMBERED_TIME_RE = re.compile(
    r"(?i)\d\s*(?:-|до\s*)?\s*\d*\s*(?:час|годин|дн|день|дня|дней|дні|днів|day)"
)
_TIME_UNIT_PATTERN = (
    r"(?:час|часа|часов|часу|часами|"
    r"год|года|годов|година|години|годину|годин|годинами|"
    r"день|дня|дней|дни|дні|днів|днями|"
    r"неделя|неделю|недели|недель|неделей|"
    r"тиждень|тижня|тижні|тижнів|тижнем|тижнями|"
    r"месяц|месяца|месяцы|месяцев|месяцем|месяцами|"
    r"місяць|місяця|місяці|місяців|місяцем|місяцями)"
)
_DURATION_PHRASE_RE = re.compile(
    rf"(?i)\b(?:за|на)\s+(?:(?:пару|кілька|несколько|один|одну|одна|"
    rf"два|две|дві|три|четыре|чотири|\d+)\s+)?{_TIME_UNIT_PATTERN}\b|"
    rf"\b(?:в\s+течение|на\s+протяжении|протягом|впродовж)\s+"
    rf"(?:(?:пары|кількох|нескольких|одного|однієї|\d+)\s+)?{_TIME_UNIT_PATTERN}\b"
)
_COMPLETION_TIME_RE = re.compile(
    rf"(?i)(?:сдела|выполн|заверш|закончи|управ|справ|реализ|разработ|"
    rf"займ[её]|потребу|викона|закінч|встиг|реаліз|розроб).{{0,80}}"
    rf"\b{_TIME_UNIT_PATTERN}\b|"
    rf"\b{_TIME_UNIT_PATTERN}\b.{{0,80}}(?:сдела|выполн|заверш|закончи|"
    rf"управ|справ|реализ|разработ|займ[её]|потребу|викона|закінч|встиг|"
    rf"реаліз|розроб)"
)
_QUESTION_RE = re.compile(r"\?")
_CLARIFICATION_RE = re.compile(
    r"(?i)(уточн|расскаж|опишит|пришлит|какие\s+|подробност|"
    r"уточнi|уточні|розкаж|опишi|опиші|надiшл|надішл|якi\s+|які\s+|детал)"
)
_CLOSING_RE = re.compile(
    r"(?i)^\s*(пиш|напиш|можем\s+обсуд|готов\s+обсуд|звертай|"
    r"пишi|пиші|обговор|готовий\s+обговор)"
)


class ScopeEstimator(Protocol):
    async def estimate(self, project: Project) -> ScopeTier:
        ...


class QuoteStore(Protocol):
    async def pricing_quote(self, project_id: str) -> dict | None:
        ...

    async def save_pricing_quote_if_absent(
        self, project_id: str, quote: dict
    ) -> dict:
        ...

    async def replace_pricing_quote(self, project_id: str, quote: dict) -> None:
        ...


class RatesSource(Protocol):
    async def get_rates(self) -> dict[str, float]:
        ...


@dataclass(frozen=True)
class PricingQuote:
    tier: ScopeTier
    hours: int | None
    amount: Decimal | None
    currency: str | None
    deadline: str | None
    fx_snapshot: dict[str, str]
    hourly_rate_usd: Decimal
    policy_version: str
    input_fingerprint: str

    @property
    def omitted(self) -> bool:
        return self.tier == "omit"

    def to_dict(self) -> dict:
        return {
            "tier": self.tier,
            "hours": self.hours,
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "deadline": self.deadline,
            "fx_snapshot": dict(self.fx_snapshot),
            "hourly_rate_usd": str(self.hourly_rate_usd),
            "policy_version": self.policy_version,
            "input_fingerprint": self.input_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "PricingQuote":
        tier = str(raw.get("tier", ""))
        if tier not in ALLOWED_TIERS:
            raise ValueError("invalid pricing tier")
        hours = int(tier) if tier.isdigit() else None
        raw_amount = raw.get("amount")
        amount = None if raw_amount is None else Decimal(str(raw_amount))
        currency = raw.get("currency")
        deadline = raw.get("deadline")
        if tier == "omit":
            if amount is not None or currency is not None or deadline is not None:
                raise ValueError("omitted quote contains pricing values")
        elif amount is None or currency not in _STEPS or not isinstance(deadline, str):
            raise ValueError("priced quote is incomplete")
        fx_snapshot = raw.get("fx_snapshot")
        if not isinstance(fx_snapshot, dict):
            raise ValueError("invalid FX snapshot")
        return cls(
            tier=tier,  # type: ignore[arg-type]
            hours=hours,
            amount=amount,
            currency=currency,
            deadline=deadline,
            fx_snapshot={str(key): str(value) for key, value in fx_snapshot.items()},
            hourly_rate_usd=Decimal(str(raw.get("hourly_rate_usd"))),
            policy_version=str(raw.get("policy_version") or ""),
            input_fingerprint=str(raw.get("input_fingerprint") or ""),
        )


class InMemoryQuoteStore:
    def __init__(self) -> None:
        self._quotes: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def pricing_quote(self, project_id: str) -> dict | None:
        async with self._lock:
            quote = self._quotes.get(project_id)
            return copy.deepcopy(quote) if quote is not None else None

    async def save_pricing_quote_if_absent(
        self, project_id: str, quote: dict
    ) -> dict:
        async with self._lock:
            stored = self._quotes.setdefault(project_id, copy.deepcopy(quote))
            return copy.deepcopy(stored)

    async def replace_pricing_quote(self, project_id: str, quote: dict) -> None:
        async with self._lock:
            self._quotes[project_id] = copy.deepcopy(quote)


class AtomicQuoteStore:
    """Dedicated, rollback-safe quote persistence with atomic file replacement."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._quotes: dict[str, dict] = {}
        self._loaded = False
        self._lock = asyncio.Lock()

    async def pricing_quote(self, project_id: str) -> dict | None:
        async with self._lock:
            await self._ensure_loaded_locked()
            quote = self._quotes.get(project_id)
            return copy.deepcopy(quote) if quote is not None else None

    async def save_pricing_quote_if_absent(
        self, project_id: str, quote: dict
    ) -> dict:
        async with self._lock:
            await self._ensure_loaded_locked()
            stored = self._quotes.get(project_id)
            if stored is None:
                self._quotes[project_id] = copy.deepcopy(quote)
                await self._persist_locked()
                stored = quote
            return copy.deepcopy(stored)

    async def replace_pricing_quote(self, project_id: str, quote: dict) -> None:
        async with self._lock:
            await self._ensure_loaded_locked()
            self._quotes[project_id] = copy.deepcopy(quote)
            await self._persist_locked()

    async def _ensure_loaded_locked(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            data = json.loads(raw)
            quotes = data.get("quotes") if isinstance(data, dict) else None
            if not isinstance(quotes, dict):
                raise ValueError("quotes root is missing")
            self._quotes = {
                str(project_id): copy.deepcopy(quote)
                for project_id, quote in quotes.items()
                if isinstance(quote, dict)
            }
        except Exception:
            log.exception("failed to load pricing quotes from %s", self._path)
            self._quotes = {}

    async def _persist_locked(self) -> None:
        payload = json.dumps(
            {"quotes": self._quotes}, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        # The file is deliberately small and the async lock already serializes
        # writers. A direct atomic replace is more reliable here than handing an
        # fsync operation to a worker thread during event-loop shutdown.
        _atomic_write(self._path, payload)


class PricingEngine:
    def __init__(self, hourly_rate_usd: Decimal = DEFAULT_HOURLY_RATE_USD,
                 table: dict | None = None) -> None:
        if hourly_rate_usd <= 0:
            raise ValueError("hourly rate must be positive")
        self._hourly_rate_usd = Decimal(hourly_rate_usd)
        self._table = table if table is not None else load_price_table()

    @property
    def hourly_rate_usd(self) -> Decimal:
        return self._hourly_rate_usd

    def quote(
        self,
        project: Project,
        tier: ScopeTier,
        rates: dict[str, float | Decimal],
    ) -> PricingQuote:
        fingerprint = input_fingerprint(project)
        fx = _fx_snapshot(rates)
        if tier == "omit":
            return PricingQuote(
                tier=tier,
                hours=None,
                amount=None,
                currency=None,
                deadline=None,
                fx_snapshot=fx,
                hourly_rate_usd=self._hourly_rate_usd,
                policy_version=POLICY_VERSION,
                input_fingerprint=fingerprint,
            )

        if not tier.isdigit():
            return self._table_quote(project, tier, fx, fingerprint)
        hours = int(tier)
        budget_amount, budget_currency = parse_budget(project.budget)
        currency = budget_currency if budget_currency in _STEPS else "UAH"
        rate = Decimal("1") if currency == "USD" else Decimal(fx[currency])
        raw = Decimal(hours) * self._hourly_rate_usd * rate
        rounded_raw = round_up(raw, _STEPS[currency])
        exact_floor = (
            budget_amount
            if budget_amount is not None and budget_currency == currency
            else Decimal("0")
        )
        amount = max(rounded_raw, exact_floor)
        base_days = max(1, (hours + 7) // 8)
        return PricingQuote(
            tier=tier,
            hours=hours,
            amount=amount,
            currency=currency,
            deadline=f"{base_days}-{base_days + 1}",
            fx_snapshot=fx,
            hourly_rate_usd=self._hourly_rate_usd,
            policy_version=POLICY_VERSION,
            input_fingerprint=fingerprint,
        )


    def _table_quote(self, project: Project, tier: str, fx: dict[str, str],
                     fingerprint: str) -> PricingQuote:
        """Market price for the work type and size, anchored to the client's
        budget: never below it, equal to it when we are close, capped above."""
        kind, size = tier.split(":", 1)
        budget_amount, budget_currency = parse_budget(project.budget)
        currency = budget_currency if budget_currency in _STEPS else "UAH"
        price_uah = Decimal(str(self._table["prices_uah"][kind][size]))
        rate = Decimal(fx[currency]) / Decimal(fx["UAH"])
        amount = round_up(price_uah * rate, _STEPS[currency])
        if budget_amount is not None and budget_currency == currency:
            rules = self._table["budget"]
            if amount <= budget_amount * Decimal(str(rules["match_up_to"])):
                amount = budget_amount
            else:
                cap = round_up(budget_amount * Decimal(str(rules["max_over"])), _STEPS[currency])
                amount = max(min(amount, cap), budget_amount)
        deadlines = self._table["deadlines"]
        deadline = deadlines.get(kind, deadlines["default"])[size]
        return PricingQuote(
            tier=tier,
            hours=None,
            amount=amount,
            currency=currency,
            deadline=deadline,
            fx_snapshot=fx,
            hourly_rate_usd=self._hourly_rate_usd,
            policy_version=POLICY_VERSION,
            input_fingerprint=fingerprint,
        )


def load_price_table(path: Path = PRICE_TABLE_FILE) -> dict:
    table = json.loads(path.read_text(encoding="utf-8"))
    for kind in WORK_TYPES:
        for size in SIZES:
            if Decimal(str(table["prices_uah"][kind][size])) <= 0:
                raise ValueError(f"price for {kind}:{size} must be positive")
            table["deadlines"].get(kind, table["deadlines"]["default"])[size].split("-", 1)
    return table


class QuoteService:
    def __init__(
        self,
        estimator: ScopeEstimator | None,
        engine: PricingEngine,
        store: QuoteStore,
        rates: RatesSource,
    ) -> None:
        self._estimator = estimator
        self._engine = engine
        self._store = store
        self._rates = rates
        self._locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()

    async def get_or_create(self, project: Project) -> PricingQuote:
        lock = await self._project_lock(project.id)
        async with lock:
            stored = await self._store.pricing_quote(project.id)
            if stored is not None:
                try:
                    quote = PricingQuote.from_dict(stored)
                    if quote.input_fingerprint == input_fingerprint(project):
                        return quote
                except (InvalidOperation, TypeError, ValueError):
                    pass

            if self._estimator is None:
                raise ValueError("a scope tier must come from the combined bid response")
            tier = await self._estimator.estimate(project)
            rates = await self._rates.get_rates()
            quote = self._engine.quote(project, tier, rates)
            if stored is None:
                saved = await self._store.save_pricing_quote_if_absent(
                    project.id, quote.to_dict()
                )
                return PricingQuote.from_dict(saved)
            await self._store.replace_pricing_quote(project.id, quote.to_dict())
            return quote

    async def existing(self, project: Project) -> PricingQuote | None:
        stored = await self._store.pricing_quote(project.id)
        if stored:
            try:
                quote = PricingQuote.from_dict(stored)
                if quote.input_fingerprint == input_fingerprint(project):
                    return quote
            except (InvalidOperation, TypeError, ValueError):
                pass
        return None

    async def save_tier(self, project: Project, tier: ScopeTier) -> PricingQuote:
        async with await self._project_lock(project.id):
            existing = await self.existing(project)
            if existing:
                return existing
            quote = self._engine.quote(project, tier, await self._rates.get_rates())
            await self._store.replace_pricing_quote(project.id, quote.to_dict())
            return quote

    async def _project_lock(self, project_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._locks.setdefault(project_id, asyncio.Lock())


class StaticRatesSource:
    def __init__(self, rates: dict[str, float | Decimal] | None = None) -> None:
        self._rates = rates or DEFAULT_RATES

    async def get_rates(self) -> dict[str, float | Decimal]:
        return dict(self._rates)


def round_up(value: Decimal, step: Decimal) -> Decimal:
    return (value / step).to_integral_value(rounding=ROUND_CEILING) * step


def parse_budget(raw: str) -> tuple[Decimal | None, str]:
    text = raw.strip().replace("\u00a0", " ")
    if not text:
        return None, ""
    match = re.fullmatch(r"(.+?)\s+([A-Za-z]{3})", text)
    if match:
        amount_text, currency = match.groups()
    else:
        amount_text, currency = text, ""
    normalized = amount_text.replace(" ", "").replace("_", "").replace(",", ".")
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None, ""
    if amount <= 0:
        return None, ""
    return amount, currency.upper()


def input_fingerprint(project: Project) -> str:
    payload = json.dumps(
        {
            "title": project.title.strip(),
            "description": project.description.strip(),
            "budget": project.budget.strip(),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalize_dashes(text: str) -> str:
    normalized = text.translate(_DASH_TRANSLATION)
    while "--" in normalized:
        normalized = normalized.replace("--", "-")
    return normalized


def sanitize_prose(text: str, *, vague: bool, tokens: dict[str, str] | None = None,
                   allowed_durations: frozenset[tuple[str, str]] = frozenset()) -> str:
    """Keep the model's lines minus pricing leaks (and, for vague projects,
    questions). Blank lines the model wrote separate paragraphs; without any,
    every kept line is its own paragraph and list items stay together."""
    source = normalize_dashes(text).replace("**", "").strip()
    paragraphs = any(not line.strip() for line in source.splitlines())
    out: list[str] = []
    previous, gap = "", False
    for raw_line in source.splitlines():
        line = raw_line.strip()
        if not line:
            gap = True
            continue
        if vague:
            line = _without_questions(line)
        if not line or line_leaks(line, allowed_durations=allowed_durations, tokens=tokens is not None):
            continue
        if vague and (_CLARIFICATION_RE.search(line) or _CLOSING_RE.search(line)):
            continue
        line = fill_tokens(line, tokens or {})
        if out:
            together = not gap if paragraphs else bool(
                _LIST_ITEM_RE.match(line) and (_LIST_ITEM_RE.match(previous) or previous.endswith(":")))
            out.append("\n" if together else "\n\n")
        out.append(line)
        previous, gap = line, False
    return "".join(out)


PRICE_TOKENS = ("{price}", "{deadline}")
_LIST_ITEM_RE = re.compile(r"^(?:-|•|\d{1,2}[.)])\s")
_LIST_NUMBER_RE = re.compile(r"^\d{1,2}[.)]\s+")
_DIGITS_RE = re.compile(r"\d")
_TIME_UNIT_RE = re.compile(rf"(?i)\b{_TIME_UNIT_PATTERN}\b")
_DURATION_RE = re.compile(rf"(?i)\b(\d+)\s*({_TIME_UNIT_PATTERN})\b")
_UNIT_KINDS = (("дн", "day"), ("ден", "day"), ("недел", "week"), ("тиж", "week"),
               ("месяц", "month"), ("місяц", "month"), ("час", "hour"), ("год", "god"))


def _unit_kind(unit: str) -> str:
    return next((kind for prefix, kind in _UNIT_KINDS if unit.lower().startswith(prefix)), unit.lower())


def durations(text: str) -> frozenset[tuple[str, str]]:
    """(number, unit kind) pairs in trusted text: "90 дней" also allows "90 днів"."""
    return frozenset((number, _unit_kind(unit)) for number, unit in _DURATION_RE.findall(text))


def line_leaks(line: str, *, allowed_durations: frozenset[tuple[str, str]] = frozenset(),
               tokens: bool = False) -> bool:
    """contains_quote_leakage plus the sanctioned exceptions: {price}/{deadline}
    placeholders the program fills, numbered headings without figures
    ("3. Стоимость и сроки") and durations taken from the author's dossier."""
    if any(token in line for token in PRICE_TOKENS):
        if not tokens:
            return True
        rest = _LIST_NUMBER_RE.sub("", fill_tokens(line, dict.fromkeys(PRICE_TOKENS, "")))
        return bool(_DIGITS_RE.search(rest) or _CURRENCY_OR_PLACEHOLDER_RE.search(rest))
    body = _LIST_NUMBER_RE.sub("", line)
    if (body != line and len(body) <= 60 and not _DIGITS_RE.search(body)
            and not _CURRENCY_OR_PLACEHOLDER_RE.search(body) and not _TIME_UNIT_RE.search(body)):
        return bool(_PRICING_DISCUSSION_RE.search(body))
    if allowed_durations:
        line = _DURATION_RE.sub(
            lambda match: "" if (match[1], _unit_kind(match[2])) in allowed_durations else match[0], line)
    return contains_quote_leakage(line)


def fill_tokens(text: str, tokens: dict[str, str]) -> str:
    for token, value in tokens.items():
        text = text.replace(token, value)
    return text


def quote_tokens(quote: PricingQuote, language: Language) -> dict[str, str]:
    if quote.omitted or quote.amount is None or quote.currency is None or quote.deadline is None:
        return {}
    start, end = (int(item) for item in quote.deadline.split("-", 1))
    return {"{price}": f"{_format_decimal(quote.amount)} {_CURRENCY_LABELS[quote.currency]}",
            "{deadline}": f"{quote.deadline} {_calendar_days(start, end, language)}"}


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+")
_QUESTION_LABEL_RE = re.compile(r"(?i)^\s*(?:вопрос|питання|question)\s*:")


def _without_questions(line: str) -> str:
    """Drop question sentences (and "Вопрос:" lines), keeping the rest of the line."""
    sentences = _SENTENCE_SPLIT_RE.split(line)
    return " ".join(
        sentence for sentence in sentences
        if sentence and not _QUESTION_RE.search(sentence) and not _QUESTION_LABEL_RE.match(sentence)
    ).strip()


def contains_quote_leakage(line: str) -> bool:
    return bool(
        _CURRENCY_OR_PLACEHOLDER_RE.search(line)
        or _QUOTE_LABEL_RE.search(line)
        or _DEADLINE_TOPIC_RE.search(line)
        or _PRICING_DISCUSSION_RE.search(line)
        or _NUMERIC_QUOTE_RE.search(line)
        or _NUMBERED_TIME_RE.search(line)
        or _DURATION_PHRASE_RE.search(line)
        or _COMPLETION_TIME_RE.search(line)
    )


def render_bid(prose: str, quote: PricingQuote, language: Language, *,
               allowed_durations: frozenset[tuple[str, str]] = frozenset()) -> str:
    tokens = quote_tokens(quote, language)
    body = sanitize_prose(prose, vague=quote.omitted, tokens=tokens or None, allowed_durations=allowed_durations)
    if not body:
        raise ValueError("model prose is empty after pricing sanitization")
    if quote.omitted:
        closing = "Пишите, обсудим детали" if language == "ru" else "Пишіть, обговоримо деталі"
        return normalize_dashes(f"{body}\n\n{closing}")
    if any(value in body for value in tokens.values()):
        return normalize_dashes(body)  # the quote already sits where the client asked for it
    return normalize_dashes(f"{body}\n\n{format_quote_line(quote, language)}")


def format_quote_line(quote: PricingQuote, language: Language) -> str:
    if quote.omitted or quote.amount is None or quote.currency is None or quote.deadline is None:
        raise ValueError("cannot render omitted quote")
    amount = _format_decimal(quote.amount)
    currency = _CURRENCY_LABELS[quote.currency]
    start, end = (int(item) for item in quote.deadline.split("-", 1))
    days = _calendar_days(start, end, language)
    if language == "ru":
        return f"Цена {amount} {currency}, срок {quote.deadline} {days}, начну сегодня. Пишите"
    return f"Ціна {amount} {currency}, термін {quote.deadline} {days}, почну сьогодні. Пишіть"


def _fx_snapshot(rates: dict[str, float | Decimal]) -> dict[str, str]:
    snapshot = {"USD": "1"}
    for currency in ("UAH", "EUR", "PLN"):
        value = rates.get(currency, DEFAULT_RATES[currency])
        decimal = Decimal(str(value))
        if decimal <= 0:
            decimal = DEFAULT_RATES[currency]
        snapshot[currency] = str(decimal)
    return snapshot


def _format_decimal(value: Decimal) -> str:
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _calendar_days(start: int, end: int, language: Language) -> str:
    few = end % 10 in (2, 3, 4) and end % 100 not in (12, 13, 14)
    if language == "ua":
        return "дні" if few else "днів"
    return "дня" if few else "дней"


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # Windows cannot open directories through os.open; the file itself
        # was still flushed before its atomic replacement above.
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
