"""Owner-only manual bids. Pending corrections and operation IDs survive restart."""
import logging
import json
import re
import time

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.ai import BidGenerator, BidGenerationError
from app.ai.policy import ACCESS_DENIED, ai_allowed
from app.ai.store import AIStore
from app.config import Settings
from app.llm import QuotaExceededError
from app.storage import StateStore
from .. import formatting, keyboards

router = Router(name="ai_actions")
log = logging.getLogger(__name__)
_MAX_CORRECTIONS = 2000
# Requests to edit money/time cannot be fulfilled by a prose-only operation.
_CHANGE = r"(?:пересч[иі]т|переоцен|зменш|збільш|измени|изменить|меняй|сниз|повыс|увелич|уменьш|поменя|зміни|оцени|оціни|постав|сделай|зроби|recalculate|change|reduce|increase|set)"
_PRICE = r"\b(?:цен[ауые]|ценник\w*|стоимост\w*|бюджет\w*|час(?:а|ов|ы)?|срок\w*|цін[ауиі]|вартіст\w*|годин\w*|термін\w*|price|cost|hours?|deadline)\b"
_REPRICE = re.compile(rf"(?is){_CHANGE}.{{0,60}}{_PRICE}|{_PRICE}.{{0,60}}{_CHANGE}|(?:дешевле|дороже|cheaper|more expensive)|{_CHANGE}.{{0,40}}(?:\d+\s*(?:USD|UAH|грн|доллар|долар|\$)|\$\s*\d+)")
_KEEP_PRICE = re.compile(rf"(?i)(?:не\s+(?:меняй|изменяй|пересчитывай|змінюй)\s+{_PRICE}|{_PRICE}\s+не\s+(?:меняй|изменяй|пересчитывай|змінюй))")


def _identity(event):
    message = event.message if isinstance(event, CallbackQuery) else event
    sender = event.from_user
    return bool(message and sender and not sender.is_bot and message.chat.type == "private"
                and message.chat.id == sender.id and ai_allowed(sender.id))


async def _guard(callback, bid_generator=None, *, need_generator=True):
    if not _identity(callback):
        await callback.answer(ACCESS_DENIED, show_alert=True)
        return False
    if need_generator and bid_generator is None:
        await callback.answer("AI выключен: проверьте ANTHROPIC_API_KEY и AI_ENABLED на сервере.", show_alert=True)
        return False
    return True


async def _find_project(store, settings, project_id):
    project = await store.find_project(project_id)
    return project if project and project.skill_id in {c.skill_id for c in settings.categories} else None


@router.callback_query(F.data.startswith(keyboards.CALLBACK_GEN_PREFIX))
async def handle_generate(callback: CallbackQuery, settings: Settings, store: StateStore,
                          bid_generator: BidGenerator | None, ai_store: AIStore, state: FSMContext):
    if not await _guard(callback, bid_generator):
        return
    await state.clear()
    project_id = callback.data[len(keyboards.CALLBACK_GEN_PREFIX):]
    project = await _find_project(store, settings, project_id)
    if project is None:
        await callback.answer("Проект не найден в истории", show_alert=True)
        return
    await callback.answer("Генерирую…")
    try:
        result = await bid_generator.generate_bid(project, operation_id=f"tg:{callback.from_user.id}:{callback.id}")
    except (BidGenerationError, QuotaExceededError) as exc:
        await callback.message.answer(str(exc), parse_mode=None)
        return
    try:
        await callback.message.reply(formatting.format_bid(result["rendered"]), parse_mode="HTML",
                                     reply_markup=keyboards.regen_bid_keyboard(
                                         project.id, result["version"], needs_case=result.get("needs_case", False)))
    except TelegramAPIError:
        log.warning("could not send cached bid for project %s", project.id)
        # The persisted result will be returned by the next Generate click.


@router.callback_query(F.data.startswith(keyboards.CALLBACK_REGEN_PREFIX))
async def handle_regen(callback: CallbackQuery, settings: Settings, store: StateStore,
                       bid_generator: BidGenerator | None, ai_store: AIStore, state: FSMContext):
    if not await _guard(callback, bid_generator):
        return
    await state.clear()
    project_id, _, version = callback.data[len(keyboards.CALLBACK_REGEN_PREFIX):].partition(":")
    previous = ai_store.bid(callback.from_user.id, project_id, version)
    project = await _find_project(store, settings, project_id)
    if not previous or project is None:
        await callback.answer("Старая ставка. Сначала нажмите «Сгенерировать ставку» у проекта.", show_alert=True)
        return
    revision = ai_store.begin_revision(callback.from_user.id, callback.id, project_id, previous,
                                       callback.message.message_id)
    if revision["status"] != "waiting" or revision["expires"] <= time.time():
        await callback.answer("Эта операция уже завершена или устарела.")
        return
    await callback.answer()
    await callback.message.answer("Напишите корректировки для AI", reply_markup=keyboards.revision_keyboard(revision["id"]))


@router.callback_query(F.data.startswith(keyboards.CALLBACK_CANCEL_AI_PREFIX))
async def cancel_revision(callback: CallbackQuery, ai_store: AIStore):
    if not await _guard(callback, need_generator=False):
        return
    operation_id = callback.data[len(keyboards.CALLBACK_CANCEL_AI_PREFIX):]
    ai_store.cancel_revision(callback.from_user.id, operation_id)
    await callback.answer("Отменено")


@router.callback_query(F.data.startswith(keyboards.CALLBACK_SKIP_PREFIX))
async def skip_revision(callback: CallbackQuery, settings: Settings, store: StateStore,
                        bid_generator: BidGenerator | None, ai_store: AIStore):
    if not await _guard(callback, bid_generator):
        return
    operation_id = callback.data[len(keyboards.CALLBACK_SKIP_PREFIX):]
    previous = ai_store.revision(callback.from_user.id, operation_id)
    if previous and previous["status"] in {"failed", "delivery_failed"}:
        await callback.answer()
        await callback.message.answer(
            "Предыдущая операция прервалась. Корректировки сохранены; повтор запускается только по кнопке.",
            reply_markup=keyboards.retry_ai_keyboard(operation_id))
        return
    revision = ai_store.consume_revision(callback.from_user.id, operation_id, "")
    if not revision:
        await callback.answer("Эта кнопка устарела или операция уже выполняется.")
        return
    await callback.answer("Генерирую новый вариант…")
    await _run_revision(callback.message, settings, store, bid_generator, ai_store, revision)


@router.callback_query(F.data.startswith(keyboards.CALLBACK_RETRY_AI_PREFIX))
async def retry_revision(callback: CallbackQuery, settings: Settings, store: StateStore,
                         bid_generator: BidGenerator | None, ai_store: AIStore):
    if not await _guard(callback, bid_generator):
        return
    previous = ai_store.revision(callback.from_user.id, callback.data[len(keyboards.CALLBACK_RETRY_AI_PREFIX):])
    if not previous or previous["status"] not in {"failed", "delivery_failed"}:
        await callback.answer("Повтор недоступен; откройте ставку заново.")
        return
    await callback.answer("Повторяю…")
    if previous["result"]:
        await _deliver(callback.message, ai_store, previous, json.loads(previous["result"]))
        return
    retry = ai_store.begin_revision(callback.from_user.id, callback.id, previous["project_id"],
                                   previous["previous"], previous["target"])
    retry = ai_store.consume_revision(callback.from_user.id, retry["id"], previous["corrections"] or "")
    if retry:
        await _run_revision(callback.message, settings, store, bid_generator, ai_store, retry)


@router.message(Command("ai_usage"))
async def usage_command(message: Message, settings: Settings, ai_store: AIStore, state: FSMContext):
    await state.clear()
    if not _identity(message):
        await message.answer(ACCESS_DENIED)
        return
    await message.answer(ai_store.report(message.from_user.id, settings.ai_cost_timezone), parse_mode=None)


@router.callback_query(F.data == keyboards.CALLBACK_AI_USAGE)
async def usage_callback(callback: CallbackQuery, settings: Settings, ai_store: AIStore):
    if not await _guard(callback, need_generator=False):
        return
    await callback.answer()
    await callback.message.answer(ai_store.report(callback.from_user.id, settings.ai_cost_timezone), parse_mode=None)


@router.callback_query(F.data.startswith(keyboards.CALLBACK_BID_SENT_PREFIX))
async def bid_sent(callback: CallbackQuery, ai_store: AIStore):
    if not await _guard(callback, need_generator=False):
        return
    project_id = callback.data[len(keyboards.CALLBACK_BID_SENT_PREFIX):]
    if ai_store.mark_bid_sent(callback.from_user.id, project_id):
        await callback.answer("Отмечено. Сообщу, когда заказчик выберет исполнителя.")
    else:
        await callback.answer("Ставка не найдена в журнале", show_alert=True)


@router.message(Command("bid_stats"))
async def bid_stats_command(message: Message, ai_store: AIStore, state: FSMContext):
    await state.clear()
    if not _identity(message):
        await message.answer(ACCESS_DENIED)
        return
    await message.answer(ai_store.bid_stats(message.from_user.id), parse_mode=None)


@router.message()
async def correction_message(message: Message, settings: Settings, store: StateStore,
                             bid_generator: BidGenerator | None, ai_store: AIStore):
    if not _identity(message):
        return
    pending = ai_store.waiting_revision(message.from_user.id)
    if not pending:
        return
    if pending["expires"] <= time.time():
        ai_store.cancel_revision(message.from_user.id, pending["id"])
        await message.answer("Время ожидания истекло. Нажмите «Новый вариант» снова.")
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        ai_store.cancel_waiting(message.from_user.id)
        return
    if not text or len(text) > _MAX_CORRECTIONS:
        await message.answer("Отправьте корректировки обычным непустым текстом, до 2000 символов, или нажмите «Пропустить».")
        return
    if _REPRICE.search(_KEEP_PRICE.sub("", text)):
        await message.answer("Эта операция меняет только текст. Цена, часы, курс и сроки остаются прежними. Напишите пожелания к тексту или отмените операцию.")
        return
    if bid_generator is None:
        ai_store.cancel_waiting(message.from_user.id)
        await message.answer("AI выключен. После настройки ключа нажмите «Новый вариант» снова.")
        return
    revision = ai_store.consume_revision(message.from_user.id, pending["id"], text, event=f"message:{message.message_id}")
    if revision:
        await _run_revision(message, settings, store, bid_generator, ai_store, revision)


async def _run_revision(message, settings, store, generator, ai_store, revision):
    try:
        project = await _find_project(store, settings, revision["project_id"])
        if project is None:
            raise BidGenerationError("Проект не найден в вашей истории.")
        result = await generator.generate_bid(project, operation_id=revision["id"],
                    previous=revision["previous"], corrections=revision["corrections"] or "")
    except (BidGenerationError, QuotaExceededError) as exc:
        ai_store.finish_revision(revision["id"], "failed")
        await message.answer(str(exc), parse_mode=None, reply_markup=keyboards.retry_ai_keyboard(revision["id"]))
        return
    await _deliver(message, ai_store, revision, result)


async def _deliver(message, ai_store, revision, result):
    saved = json.dumps(result, ensure_ascii=False)
    # Persist before any Telegram side effect. A delivery retry costs no tokens.
    ai_store.finish_revision(revision["id"], "success", saved)
    try:
        await message.bot.edit_message_text(chat_id=revision["user_id"], message_id=revision["target"],
            text=formatting.format_bid(result["rendered"]), parse_mode="HTML",
            reply_markup=keyboards.regen_bid_keyboard(
                revision["project_id"], result["version"], needs_case=result.get("needs_case", False)))
    except TelegramAPIError as exc:
        if isinstance(exc, TelegramBadRequest) and "message is not modified" in str(exc):
            return
        ai_store.finish_revision(revision["id"], "delivery_failed", saved)
        await message.answer("Новый вариант сохранён, но сообщение не обновилось. Повторная отправка не вызывает AI.",
                             reply_markup=keyboards.retry_ai_keyboard(revision["id"]))
