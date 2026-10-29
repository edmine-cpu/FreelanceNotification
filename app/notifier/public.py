"""Fetch each category once, then apply each subscriber's own delivery state."""

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import replace

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from app.ai import BidGenerator, OrderScreener
from app.ai.policy import ai_allowed
from app.config import Settings, build_category
from app.source import FreelancehuntSource
from app.storage.users import UserContext, UserRegistry

from .cleanup import ClosedProjectsCleaner
from .loop import NotifierLoop

log = logging.getLogger(__name__)


class PacedBot:
    """Pace background messages across users and within each private chat."""

    def __init__(self, bot: Bot) -> None:
        self._bot = bot
        self._lock = asyncio.Lock()
        self._next_global = 0.0
        self._next_chat: dict[str, float] = {}

    async def send_message(self, **kwargs):
        chat_id = str(kwargs["chat_id"])
        async with self._lock:
            now = time.monotonic()
            scheduled = max(now, self._next_global, self._next_chat.get(chat_id, 0.0))
            self._next_global = scheduled + 0.05
            self._next_chat[chat_id] = scheduled + 1.05
        await asyncio.sleep(max(0.0, scheduled - time.monotonic()))
        try:
            return await self._bot.send_message(**kwargs)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after)
            return await self._bot.send_message(**kwargs)


class PublicNotifier:
    def __init__(
        self,
        bot: Bot,
        registry: UserRegistry,
        source: FreelancehuntSource,
        settings: Settings,
        generator_factory: Callable[[UserContext], BidGenerator | None],
        legacy_screener: OrderScreener | None = None,
    ) -> None:
        self._bot = PacedBot(bot)
        self._registry = registry
        self._source = source
        self._settings = settings
        self._generator_factory = generator_factory
        self._legacy_screener = legacy_screener
        self._slots = asyncio.Semaphore(8)
        self._cleaner = ClosedProjectsCleaner(source, settings.state_file.parent / "closed_checks.json")

    async def run(self, stop_event: asyncio.Event) -> None:
        log.info("starting public notifier, interval=%ss", self._settings.poll_interval)
        while not stop_event.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("public notifier tick failed")
            try:
                await asyncio.wait_for(stop_event.wait(), self._settings.poll_interval)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        users = [u for u in await self._registry.users() if await u.store.is_active()]
        snapshots = [(user, await user.settings()) for user in users]
        skill_ids = {
            category.skill_id
            for _, settings in snapshots
            for category in settings.categories
        }
        self._source.set_categories([build_category(sid) for sid in sorted(skill_ids)])
        if not skill_ids:
            return
        projects = await self._source.fetch_projects()
        successful_skills = set(self._source.successful_skill_ids)

        async def deliver(user: UserContext, settings: Settings) -> None:
            async with self._slots:
                try:
                    if not await user.store.is_active():
                        return
                    # Refresh after waiting for another subscriber, so settings
                    # edited during a slow AI request take effect this tick.
                    settings = await user.settings()
                    categories = {c.skill_id: c for c in settings.categories}
                    selected = [
                        replace(p, category_name=categories[p.skill_id].name)
                        for p in projects if p.skill_id in categories
                    ]
                    owner = ai_allowed(user.user_id)
                    notifier = NotifierLoop(
                        self._bot, user.store, self._source, settings,
                        screener=self._legacy_screener if owner else None,
                        bid_generator_factory=(lambda user=user: self._generator_factory(user)) if owner else None,
                        user_id=user.user_id,
                    )
                    await notifier._tick(selected, successful_skills & categories.keys())
                except Exception:
                    # One invalid profile or failed recipient cannot stop other
                    # subscribers, and that user's watermark remains retryable.
                    log.exception("notification tick failed for user %s", user.user_id)

        await asyncio.gather(*(deliver(user, settings) for user, settings in snapshots))
        try:
            await self._cleaner.maybe_clean(users, {project.id for project in projects})
        except Exception:
            log.exception("closed projects cleanup failed")
