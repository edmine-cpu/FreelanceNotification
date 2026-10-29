import asyncio
import logging
import signal
import httpx

from app.config import Settings
from app.ai import BidGenerator, AnthropicClient, OrderScreener
from app.ai.policy import AI_OWNER_ID, ai_allowed
from app.ai.store import AIStore
from app.ai.cases import ensure_cases_json
from app.ai.pricing import AtomicQuoteStore
from app.ai.prompt_store import upgrade_stock_prompts
from app.notifier.outcomes import OutcomeTracker
from app.notifier.public import PublicNotifier
from app.rates import RatesProvider
from app.source import FreelancehuntSource
from app.storage.users import UserContext, UserRegistry
from app.telegram import build_bot, build_dispatcher

log = logging.getLogger(__name__)


async def run(settings: Settings) -> None:
    registry = UserRegistry(settings)
    await registry.migrate_legacy()
    log.info("public mode: %d registered users", len(await registry.users()))
    bot = build_bot(settings.telegram_bot_token.get_secret_value())
    source = FreelancehuntSource(
        token=settings.freelancehunt_token.get_secret_value(),
        categories=[],
    )

    generators: dict[int, BidGenerator] = {}
    ai_store = AIStore(settings.state_file.parent / "ai.sqlite3")
    ai_store.recover_interrupted()
    http = httpx.AsyncClient()
    bid_client = None
    screener = None
    rates_provider = RatesProvider(fallback=settings.fallback_rates)

    async def alert_usage():
        for month, threshold, level, total in ai_store.claim_alerts(
            AI_OWNER_ID, settings.ai_monthly_alert_usd, settings.ai_cost_timezone
        ):
            try:
                await bot.send_message(
                    AI_OWNER_ID, f"Расходы AI за {month}: ${total:.4f}, достигнуто {level}% "
                    f"порога ${threshold}. AI продолжает работать.", parse_mode=None)
            except Exception:
                ai_store.release_alert(month, threshold, level)
                log.warning("could not deliver AI usage alert")

    if settings.ai_active:
        bid_client = AnthropicClient(
            settings.anthropic_api_key.get_secret_value(), settings.ai_bid_model,
            http=http, store=ai_store, timeout=settings.ai_bid_timeout_sec,
            max_tokens=settings.ai_bid_max_tokens, effort=settings.ai_bid_effort, on_usage=alert_usage)
        screen_client = AnthropicClient(
            settings.anthropic_api_key.get_secret_value(), settings.ai_screen_model,
            http=http, store=ai_store, timeout=settings.ai_screen_timeout_sec,
            max_tokens=settings.ai_screen_max_tokens, on_usage=alert_usage)
        screener = OrderScreener(screen_client, user_id=AI_OWNER_ID, store=ai_store,
                                 max_context_chars=settings.ai_max_context_chars)
        log.info("AI owner=%s screen=%s bid=%s", AI_OWNER_ID, settings.ai_screen_model, settings.ai_bid_model)
    else:
        log.info("AI disabled (no ANTHROPIC_API_KEY or AI_ENABLED=false)")

    def generator_for(user: UserContext) -> BidGenerator | None:
        if bid_client is None or not ai_allowed(user.user_id):
            return None
        if user.user_id not in generators:
            upgrade_stock_prompts(user.system_prompt_path, user.prompt_examples_path)
            ensure_cases_json(user.cases_path)
            generators[user.user_id] = BidGenerator(
                bid_client, user_id=user.user_id, ai_store=ai_store,
                examples_path=user.prompt_examples_path, system_prompt_path=user.system_prompt_path,
                profile_provider=user.store.profile, rates_provider=rates_provider,
                quote_store=AtomicQuoteStore(user.quote_path), max_context_chars=settings.ai_max_context_chars,
                cases_path=user.cases_path)
        return generators[user.user_id]

    dispatcher = build_dispatcher(settings, registry, generator_for, source, ai_store=ai_store)
    notifier = PublicNotifier(bot, registry, source, settings, generator_for, screener)

    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    polling_task = asyncio.create_task(
        dispatcher.start_polling(bot, handle_signals=False),
        name="updates-polling",
    )
    notifier_task = asyncio.create_task(notifier.run(stop_event), name="notifier")
    # Best-effort: a failing outcome check never stops the bot.
    tracker_task = asyncio.create_task(
        OutcomeTracker(bot, source, ai_store, AI_OWNER_ID).run(stop_event), name="bid-outcomes")

    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-wait")
    done, _ = await asyncio.wait(
        (polling_task, notifier_task, stop_task), return_when=asyncio.FIRST_COMPLETED
    )
    stop_event.set()
    log.info("shutdown requested")
    if not polling_task.done():
        await dispatcher.stop_polling()

    for task in (polling_task, notifier_task):
        try:
            await asyncio.wait_for(task, timeout=5)
        except asyncio.TimeoutError:
            task.cancel()
        except Exception:
            log.exception("task %s exited with error", task.get_name())

    tracker_task.cancel()
    await asyncio.gather(polling_task, notifier_task, tracker_task, return_exceptions=True)
    stop_task.cancel()

    pending = list(ai_store.flights.values())
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    await http.aclose()
    ai_store.close()
    await source.aclose()
    await bot.session.close()
    # Let Docker restart the service if either critical loop stopped by itself.
    if stop_task not in done:
        raise RuntimeError("a bot service task stopped unexpectedly")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Windows fallback — not actually used here, but keeps tests happy.
            signal.signal(sig, lambda *_: stop_event.set())
