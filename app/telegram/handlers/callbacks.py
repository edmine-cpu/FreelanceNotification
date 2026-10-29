import logging
import os
import re
import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from app.config import Settings, parse_skill_ids
from app.ai import BidGenerator, BidGenerationError
from app.ai.cases import MAX_CASES_BYTES, CasesJsonError, write_cases_json
from app.ai.prompt_store import PromptJsonError, read_prompt_json, write_prompt_json
from app.ai.policy import ai_allowed, ACCESS_DENIED
from app.source import FreelancehuntSource
from app.storage import StateStore

from .. import formatting, keyboards
from ..views import (
    category_names_view,
    category_notifications_view,
    projects_page_view,
    settings_view,
    start_view,
)

router = Router(name="callbacks")
log = logging.getLogger(__name__)

_QUOTA_NOTICE = "Лимит запросов к ИИ исчерпан. Попробуйте позже."
_MAX_CATEGORY_NAME_LENGTH = 80
_MAX_CATEGORIES = 30
_MAX_SKILL_ID = 2147483647
_MAX_PROMPT_LENGTH = 3500


class SettingsFlow(StatesGroup):
    awaiting_category_id = State()
    awaiting_category_name = State()
    awaiting_prompt_json = State()
    awaiting_system_prompt = State()
    awaiting_profile_name = State()
    awaiting_profile_portfolio = State()
    awaiting_cases = State()


@router.callback_query(F.data == keyboards.CALLBACK_NOOP)
async def handle_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_HIDE)
async def handle_hide(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    try:
        await callback.message.delete()
    except TelegramBadRequest:
        log.exception("delete message failed")
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_START)
async def handle_start_menu(callback: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.clear()
    text, markup = start_view(settings)
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_SETTINGS)
async def handle_settings_menu(
    callback: CallbackQuery,
    settings: Settings,
    store: StateStore,
    state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.clear()
    text, markup = settings_view(settings, await store.muted_skill_ids(), await store.profile())
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_ADD_CATEGORY)
async def handle_add_category_menu(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    if len(settings.categories) >= _MAX_CATEGORIES:
        await callback.answer("Можно добавить до 30 категорий. Сначала удали ненужную.", show_alert=True)
        return
    await state.set_state(SettingsFlow.awaiting_category_id)
    await _remember_menu_message(callback, state)
    await _safe_edit(callback, formatting.format_add_category_prompt(), keyboards.settings_back_keyboard())
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_PROMPT_JSON)
async def handle_prompt_json(
    callback: CallbackQuery,
    settings: Settings,
    prompt_examples_path: Path,
    state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.clear()
    try:
        text = read_prompt_json(prompt_examples_path)
    except PromptJsonError as exc:
        await _send_settings_flow_message(
            callback,
            formatting.format_settings_notice(str(exc)),
            keyboards.hide_keyboard(),
        )
        await callback.answer()
        return
    try:
        chunks = _telegram_chunks(text, _MAX_PROMPT_LENGTH)
        for index, chunk in enumerate(chunks):
            await callback.message.answer(
                chunk,
                reply_markup=keyboards.prompt_json_keyboard() if index == len(chunks) - 1 else None,
                parse_mode=None,
            )
    except TelegramAPIError:
        log.exception("failed to send prompt JSON")
        await callback.answer("Не удалось отправить JSON", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_CASES)
async def handle_cases(callback: CallbackQuery, settings: Settings, cases_path: Path, state: FSMContext) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.clear()
    try:
        raw = cases_path.read_bytes() if cases_path.exists() else b'{"profile_facts": [], "offer": {}, "cases": []}'
        await callback.message.answer_document(
            BufferedInputFile(raw, filename="cases.json"),
            caption=formatting.format_cases_caption(),
            reply_markup=keyboards.cases_keyboard(),
        )
    except (OSError, TelegramAPIError):
        log.exception("failed to send cases file")
        await callback.answer("Не удалось отправить файл кейсов", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_CASES_EDIT)
async def handle_cases_edit_menu(callback: CallbackQuery, settings: Settings, state: FSMContext) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.set_state(SettingsFlow.awaiting_cases)
    await _send_settings_flow_message(callback, formatting.format_cases_edit_prompt(), keyboards.hide_keyboard())
    await callback.answer()


@router.message(SettingsFlow.awaiting_cases)
async def handle_cases_message(
    message: Message,
    settings: Settings,
    bid_generator: BidGenerator | None,
    cases_path: Path,
    state: FSMContext,
) -> None:
    if not message.from_user or not ai_allowed(message.from_user.id):
        await state.clear()
        await message.answer(ACCESS_DENIED)
        return
    if not _is_allowed_chat(message.chat.id, settings):
        return
    try:
        if message.document is not None:
            if (message.document.file_size or 0) > MAX_CASES_BYTES:
                raise CasesJsonError("Файл кейсов слишком большой.")
            buffer = await message.bot.download(message.document)
            raw = buffer.read()
        elif (message.text or "").strip():
            raw = message.text
        else:
            raise CasesJsonError("Отправь файл cases.json документом.")
        data = write_cases_json(raw, cases_path)
    except CasesJsonError as exc:
        await message.answer(formatting.format_settings_notice(f"{exc}\n\nОтправь исправленный файл ещё раз."))
        return
    except TelegramAPIError:
        log.exception("failed to download cases file")
        await message.answer(formatting.format_settings_notice("Не удалось скачать файл. Отправь его ещё раз."))
        return
    if bid_generator is not None:
        bid_generator.reload_prompt()
    await state.clear()
    await message.answer(formatting.format_settings_notice(f"Кейсы обновлены: {len(data['cases'])} шт."))


@router.callback_query(F.data == keyboards.CALLBACK_CATEGORY_NAMES)
async def handle_category_names_menu(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.clear()
    text, markup = category_names_view(settings)
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith(keyboards.CALLBACK_EDIT_CATEGORY_NAME_PREFIX))
async def handle_category_name_edit_menu(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    skill_id = _parse_skill_id_suffix(callback.data or "", keyboards.CALLBACK_EDIT_CATEGORY_NAME_PREFIX)
    category = _find_category(settings, skill_id)
    if category is None:
        await callback.answer("Категория не найдена", show_alert=True)
        return
    await state.set_state(SettingsFlow.awaiting_category_name)
    await state.update_data(category_name_skill_id=skill_id)
    await _remember_menu_message(callback, state)
    await _safe_edit(
        callback,
        formatting.format_category_name_prompt(category.skill_id, category.name),
        keyboards.category_names_back_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_PROMPT_EDIT)
async def handle_prompt_edit_menu(
    callback: CallbackQuery,
    settings: Settings,
    state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.set_state(SettingsFlow.awaiting_prompt_json)
    await _remember_menu_message(callback, state)
    await _safe_edit(callback, formatting.format_prompt_edit_prompt(), keyboards.hide_keyboard())
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_NOTIFICATIONS)
async def handle_notifications_menu(
    callback: CallbackQuery,
    settings: Settings,
    store: StateStore,
    state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.clear()
    text, markup = category_notifications_view(settings, await store.muted_skill_ids())
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_REMOVE_CATEGORIES)
async def handle_remove_categories_menu(
    callback: CallbackQuery, settings: Settings, state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.clear()
    await _safe_edit(
        callback, formatting.format_remove_categories(settings.category_label),
        keyboards.remove_categories_keyboard(settings.categories),
    )
    await callback.answer()


@router.callback_query(F.data.startswith(keyboards.CALLBACK_REMOVE_CATEGORY_PREFIX))
async def handle_remove_category(
    callback: CallbackQuery, settings: Settings, store: StateStore, state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    skill_id = _parse_skill_id_suffix(callback.data or "", keyboards.CALLBACK_REMOVE_CATEGORY_PREFIX)
    if _find_category(settings, skill_id) is None:
        await callback.answer("Категория не найдена в твоих настройках", show_alert=True)
        return
    fallback = parse_skill_ids(settings.skill_ids)
    await store.remove_skill_id(skill_id, fallback)
    settings.skill_ids = ",".join(str(item) for item in await store.skill_ids(fallback))
    await state.clear()
    await _safe_edit(
        callback, formatting.format_remove_categories(settings.category_label),
        keyboards.remove_categories_keyboard(settings.categories),
    )
    await callback.answer("Категория удалена из твоей подписки")


@router.callback_query(F.data == keyboards.CALLBACK_PROFILE)
async def handle_profile_menu(
    callback: CallbackQuery, settings: Settings, store: StateStore, state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.clear()
    await _safe_edit(callback, formatting.format_profile(await store.profile()), keyboards.profile_keyboard())
    await callback.answer()


@router.callback_query(F.data.in_({keyboards.CALLBACK_PROFILE_NAME, keyboards.CALLBACK_PROFILE_PORTFOLIO}))
async def handle_profile_edit_menu(
    callback: CallbackQuery, settings: Settings, state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    is_name = callback.data == keyboards.CALLBACK_PROFILE_NAME
    await state.set_state(
        SettingsFlow.awaiting_profile_name if is_name else SettingsFlow.awaiting_profile_portfolio
    )
    await _remember_menu_message(callback, state)
    prompt = (
        "Отправь своё имя (до 80 символов)."
        if is_name else "Отправь ссылку на своё портфолио, начиная с https:// или http:// (до 500 символов)."
    )
    await _safe_edit(
        callback, formatting.format_settings_notice(f"{prompt}\n«-» — очистить поле. /cancel — отменить ввод."),
        keyboards.settings_back_keyboard(),
    )
    await callback.answer()


@router.message(SettingsFlow.awaiting_profile_name)
@router.message(SettingsFlow.awaiting_profile_portfolio)
async def handle_profile_message(
    message: Message, settings: Settings, store: StateStore, state: FSMContext,
) -> None:
    if not _is_allowed_chat(message.chat.id, settings):
        return
    is_name = await state.get_state() == SettingsFlow.awaiting_profile_name.state
    raw = (message.text or "").strip()
    try:
        value = _validate_profile_name(raw) if is_name else _validate_portfolio_url(raw)
    except ValueError as exc:
        await _edit_menu_from_state(
            message, state, formatting.format_settings_notice(str(exc)), keyboards.settings_back_keyboard(),
        )
        await _delete_user_message(message)
        return
    await store.set_profile(**({"name": value} if is_name else {"portfolio_url": value}))
    await _delete_user_message(message)
    await _edit_menu_from_state(
        message, state, formatting.format_profile(await store.profile()), keyboards.profile_keyboard(),
    )
    await state.clear()


@router.callback_query(F.data == keyboards.CALLBACK_SYSTEM_PROMPT)
async def handle_system_prompt(
    callback: CallbackQuery, settings: Settings, system_prompt_path: Path, state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.clear()
    try:
        prompt = system_prompt_path.read_text(encoding="utf-8").strip()
        chunks = _telegram_chunks("Мой промпт (действует только для меня):\n\n" + prompt, _MAX_PROMPT_LENGTH)
        for index, chunk in enumerate(chunks):
            await callback.message.answer(
                chunk, parse_mode=None,
                reply_markup=keyboards.system_prompt_keyboard() if index == len(chunks) - 1 else None,
            )
    except (OSError, TelegramAPIError):
        log.exception("failed to show personal prompt")
        await callback.answer("Не удалось показать промпт. Попробуй позже.", show_alert=True)
        return
    await callback.answer()


@router.callback_query(F.data == keyboards.CALLBACK_SYSTEM_PROMPT_EDIT)
async def handle_system_prompt_edit_menu(
    callback: CallbackQuery, settings: Settings, state: FSMContext,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    await state.set_state(SettingsFlow.awaiting_system_prompt)
    await _remember_menu_message(callback, state)
    await _safe_edit(
        callback,
        formatting.format_settings_notice(
            "Отправь свой промпт обычным текстом (до 3500 символов). "
            "Опиши опыт, тон, язык и правила своих откликов. "
            "Имя и портфолио берутся из твоего профиля. /cancel — отменить ввод."
        ),
        keyboards.settings_back_keyboard(),
    )
    await callback.answer()


@router.message(SettingsFlow.awaiting_system_prompt)
async def handle_system_prompt_message(
    message: Message, settings: Settings, bid_generator: BidGenerator | None,
    system_prompt_path: Path, state: FSMContext,
) -> None:
    if not message.from_user or not ai_allowed(message.from_user.id):
        await state.clear()
        await message.answer(ACCESS_DENIED)
        return
    if not _is_allowed_chat(message.chat.id, settings):
        return
    prompt = (message.text or "").strip()
    if not prompt or _telegram_length(prompt) > _MAX_PROMPT_LENGTH:
        await _edit_menu_from_state(
            message, state,
            formatting.format_settings_notice("Отправь непустой промпт обычным текстом, до 3500 символов."),
            keyboards.settings_back_keyboard(),
        )
        await _delete_user_message(message)
        return
    try:
        _write_personal_prompt(system_prompt_path, prompt)
        if bid_generator is not None:
            bid_generator.reload_prompt()
    except OSError:
        log.exception("failed to save personal prompt")
        await _edit_menu_from_state(
            message, state, formatting.format_settings_notice("Не удалось сохранить промпт. Попробуй ещё раз."),
            keyboards.settings_back_keyboard(),
        )
        return
    await _delete_user_message(message)
    await _edit_menu_from_state(
        message, state, formatting.format_settings_notice("Твой промпт сохранён и будет использован в следующем отклике."),
        keyboards.system_prompt_keyboard(),
    )
    await state.clear()


@router.callback_query(F.data.startswith(keyboards.CALLBACK_TOGGLE_MUTE_PREFIX))
async def handle_toggle_category_notifications(
    callback: CallbackQuery,
    settings: Settings,
    store: StateStore,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    skill_id = _parse_skill_id_suffix(callback.data or "", keyboards.CALLBACK_TOGGLE_MUTE_PREFIX)
    if _find_category(settings, skill_id) is None:
        await callback.answer("Категория не найдена в твоих настройках", show_alert=True)
        return
    muted = await store.toggle_muted_skill_id(skill_id)
    text, markup = category_notifications_view(settings, await store.muted_skill_ids())
    await _safe_edit(callback, text, markup)
    await callback.answer("Уведомления выключены" if muted else "Уведомления включены")


@router.message(SettingsFlow.awaiting_category_id)
async def handle_category_id_message(
    message: Message,
    settings: Settings,
    store: StateStore,
    state: FSMContext,
) -> None:
    if not _is_allowed_chat(message.chat.id, settings):
        await _delete_user_message(message)
        return
    raw = (message.text or "").strip()
    try:
        skill_id = int(raw)
    except ValueError:
        await _edit_menu_from_state(
            message,
            state,
            formatting.format_settings_notice("ID категории должен быть числом. Отправь ID ещё раз."),
            keyboards.settings_back_keyboard(),
        )
        await _delete_user_message(message)
        return
    if not 0 < skill_id <= _MAX_SKILL_ID:
        await _edit_menu_from_state(
            message,
            state,
            formatting.format_settings_notice(f"ID категории должен быть числом от 1 до {_MAX_SKILL_ID}."),
            keyboards.settings_back_keyboard(),
        )
        await _delete_user_message(message)
        return

    fallback = parse_skill_ids(settings.skill_ids)
    if skill_id not in fallback and len(fallback) >= _MAX_CATEGORIES:
        await _edit_menu_from_state(
            message, state,
            formatting.format_settings_notice("Можно добавить до 30 категорий. Сначала удали ненужную."),
            keyboards.settings_back_keyboard(),
        )
        await _delete_user_message(message)
        await state.clear()
        return
    added = await store.add_skill_id(skill_id, fallback)
    skill_ids = await store.skill_ids(fallback)
    settings.skill_ids = ",".join(str(item) for item in skill_ids)
    await _delete_user_message(message)

    if added:
        text = formatting.format_settings_notice(
            f"Категория #{skill_id} добавлена. Имя можно задать в разделе «Имена категорий»."
        )
    else:
        text = formatting.format_settings_notice(f"Категория #{skill_id} уже есть в списке.")
    await _edit_menu_from_state(message, state, text, keyboards.settings_keyboard(ai_enabled=ai_allowed(settings.ai_user_id)))
    await state.clear()


@router.message(SettingsFlow.awaiting_category_name)
async def handle_category_name_message(
    message: Message,
    settings: Settings,
    store: StateStore,
    state: FSMContext,
) -> None:
    if not _is_allowed_chat(message.chat.id, settings):
        await _delete_user_message(message)
        return

    data = await state.get_data()
    try:
        skill_id = int(data.get("category_name_skill_id", 0))
    except (TypeError, ValueError):
        skill_id = 0
    category = _find_category(settings, skill_id)
    if category is None:
        await _delete_user_message(message)
        await _edit_menu_from_state(
            message,
            state,
            formatting.format_settings_notice("Категория не найдена. Выбери её заново."),
            keyboards.category_names_keyboard(settings.categories),
        )
        await state.clear()
        return

    name = _normalize_category_name(message.text or "")
    if not name:
        await _edit_menu_from_state(
            message,
            state,
            formatting.format_settings_notice("Имя не должно быть пустым. Отправь имя ещё раз."),
            keyboards.category_names_back_keyboard(),
        )
        await _delete_user_message(message)
        return
    if len(name) > _MAX_CATEGORY_NAME_LENGTH:
        await _edit_menu_from_state(
            message,
            state,
            formatting.format_settings_notice(
                f"Имя слишком длинное: максимум {_MAX_CATEGORY_NAME_LENGTH} символов."
            ),
            keyboards.category_names_back_keyboard(),
        )
        await _delete_user_message(message)
        return

    await store.set_category_name(skill_id, name)
    settings.category_names = await store.category_names(settings.category_names)
    await _delete_user_message(message)
    text, markup = category_names_view(settings)
    await _edit_menu_from_state(message, state, text, markup)
    await state.clear()


@router.message(SettingsFlow.awaiting_prompt_json)
async def handle_prompt_json_message(
    message: Message,
    settings: Settings,
    bid_generator: BidGenerator | None,
    prompt_examples_path: Path,
    state: FSMContext,
) -> None:
    if not message.from_user or not ai_allowed(message.from_user.id):
        await state.clear()
        await message.answer(ACCESS_DENIED)
        return
    if not _is_allowed_chat(message.chat.id, settings):
        await _delete_user_message(message)
        return
    raw = message.text or ""
    if not raw.strip():
        await _edit_menu_from_state(
            message,
            state,
            formatting.format_settings_notice("Отправь JSON обычным текстовым сообщением."),
            keyboards.hide_keyboard(),
        )
        await _delete_user_message(message)
        return

    try:
        write_prompt_json(raw, prompt_examples_path)
    except PromptJsonError as exc:
        await _edit_menu_from_state(
            message,
            state,
            formatting.format_settings_notice(f"{exc}\n\nОтправь исправленный JSON ещё раз."),
            keyboards.hide_keyboard(),
        )
        await _delete_user_message(message)
        return

    if bid_generator is not None:
        bid_generator.reload_prompt()
    await _delete_user_message(message)
    await _edit_menu_from_state(
        message,
        state,
        formatting.format_settings_notice("Твои примеры откликов обновлены."),
        keyboards.hide_keyboard(),
    )
    await state.clear()


@router.callback_query(F.data.startswith(keyboards.CALLBACK_LIST_PREFIX))
async def handle_list_page(callback: CallbackQuery, settings: Settings, store: StateStore) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    filter_key, page = _parse_list_data(callback.data or "")
    if filter_key != keyboards.LIST_FILTER_ALL and filter_key not in {
        str(category.skill_id) for category in settings.categories
    }:
        await callback.answer("Категория не найдена в твоих настройках", show_alert=True)
        return
    projects = await store.recent_projects()
    # With AI off there's no primary check, so nothing is ever marked passed —
    # fall back to showing everything (passed_ids=None disables the filter).
    passed = await store.passed_ids() if settings.ai_active and settings.primary_filter_enabled else None
    text, markup = projects_page_view(projects, page, settings, filter_key, passed_ids=passed)
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith(keyboards.CALLBACK_RAW_PREFIX))
async def handle_raw_page(
    callback: CallbackQuery,
    settings: Settings,
    store: StateStore,
    source: FreelancehuntSource,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    skill_id, page = _parse_raw_data(callback.data or "")
    category = _find_category(settings, skill_id)
    if category is None:
        await callback.answer("Категория не найдена в твоих настройках", show_alert=True)
        return
    # Live, unfiltered view: fetch the category fresh on entry (page 0) and merge
    # new projects into the cache so Show/Generate can find them later. On a
    # fetch error, fall back to the cached history rather than failing the tap.
    try:
        fetched = await source.fetch_category(skill_id)
        fetched = [
            replace(project, category_name=category.name, category_url=category.listing_url)
            for project in fetched if project.skill_id == skill_id
        ]
        await store.add_projects(fetched)
        projects = fetched
    except Exception:
        log.exception("raw fetch failed for skill %s", skill_id)
        projects = [p for p in await store.recent_projects() if p.skill_id == skill_id]
    text, markup = projects_page_view(projects, page, settings, str(skill_id), unfiltered=True)
    await _safe_edit(callback, text, markup)
    await callback.answer()


@router.callback_query(F.data.startswith(keyboards.CALLBACK_SHOW_PREFIX))
async def handle_show_project(
    callback: CallbackQuery,
    settings: Settings,
    store: StateStore,
) -> None:
    if not await _ensure_callback_allowed(callback, settings):
        return
    project_id = (callback.data or "")[len(keyboards.CALLBACK_SHOW_PREFIX):]
    project = await store.find_project(project_id)
    if project is None or _find_category(settings, project.skill_id) is None:
        await callback.answer("Проект не найден в истории", show_alert=True)
        return

    text = formatting.format_project_notification(project)
    try:
        await callback.message.answer(
            text,
            reply_markup=keyboards.project_detail_keyboard(project, ai_enabled=ai_allowed(callback.from_user.id)),
        )
    except TelegramAPIError:
        log.exception("failed to send project detail for %s", project_id)
        await callback.answer("Не удалось отправить", show_alert=True)
        return
    await callback.answer()


async def _send_notice(callback: CallbackQuery, text: str) -> None:
    """Send a plain follow-up message; the callback query is already answered."""
    try:
        await callback.message.answer(text)
    except TelegramAPIError:
        log.exception("failed to send notice: %s", text)


async def _send_settings_flow_message(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup,
    *,
    parse_mode: str | None = "HTML",
) -> bool:
    try:
        await callback.message.answer(text, reply_markup=markup, parse_mode=parse_mode)
    except TelegramAPIError:
        log.exception("failed to send settings flow message")
        return False
    return True


async def _ensure_callback_allowed(callback: CallbackQuery, settings: Settings) -> bool:
    if callback.data in {keyboards.CALLBACK_PROMPT_JSON, keyboards.CALLBACK_PROMPT_EDIT,
                         keyboards.CALLBACK_SYSTEM_PROMPT, keyboards.CALLBACK_SYSTEM_PROMPT_EDIT,
                         keyboards.CALLBACK_CASES, keyboards.CALLBACK_CASES_EDIT} and not ai_allowed(callback.from_user.id):
        await callback.answer(ACCESS_DENIED, show_alert=True)
        return False
    if (
        callback.message is not None
        and _is_allowed_chat(callback.message.chat.id, settings)
        and _is_allowed_chat(callback.from_user.id, settings)
    ):
        return True
    await callback.answer("Нет доступа к настройкам", show_alert=True)
    return False


def _is_allowed_chat(chat_id: int, settings: Settings) -> bool:
    return str(chat_id) == str(settings.telegram_chat_id)


async def _safe_edit(
    callback: CallbackQuery,
    text: str,
    markup: InlineKeyboardMarkup | None,
    *,
    parse_mode: str | None = "HTML",
) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc):
            return
        log.exception("editMessageText failed")


async def _remember_menu_message(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(
        menu_chat_id=callback.message.chat.id,
        menu_message_id=callback.message.message_id,
    )


async def _edit_menu_from_state(
    message: Message,
    state: FSMContext,
    text: str,
    markup: InlineKeyboardMarkup,
    *,
    parse_mode: str | None = "HTML",
) -> None:
    data = await state.get_data()
    chat_id = data.get("menu_chat_id")
    message_id = data.get("menu_message_id")
    if chat_id is not None and message_id is not None:
        try:
            await message.bot.edit_message_text(
                chat_id=int(chat_id),
                message_id=int(message_id),
                text=text,
                reply_markup=markup,
                parse_mode=parse_mode,
            )
            return
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return
            log.exception("editMessageText failed for settings flow")
    try:
        await message.answer(text, reply_markup=markup, parse_mode=parse_mode)
    except TelegramAPIError:
        log.exception("failed to send settings flow message")


async def _delete_user_message(message: Message) -> None:
    try:
        await message.delete()
    except TelegramAPIError:
        log.exception("failed to delete user settings message")


def _parse_skill_id_suffix(data: str, prefix: str) -> int:
    if not data.startswith(prefix):
        return 0
    try:
        skill_id = int(data[len(prefix):])
        return skill_id if 0 < skill_id <= _MAX_SKILL_ID else 0
    except ValueError:
        return 0


def _parse_list_data(data: str) -> tuple[str, int]:
    """Parse a `list:<filter>:<page>` callback into (filter_key, page)."""
    body = data[len(keyboards.CALLBACK_LIST_PREFIX):]
    parts = body.split(":")
    filter_key = parts[0] if parts and parts[0] else keyboards.LIST_FILTER_ALL
    page = 0
    if len(parts) > 1:
        try:
            page = max(0, int(parts[1]))
        except ValueError:
            page = 0
    return filter_key, page


def _parse_raw_data(data: str) -> tuple[int, int]:
    """Parse a `raw:<skill_id>:<page>` callback into (skill_id, page)."""
    body = data[len(keyboards.CALLBACK_RAW_PREFIX):]
    parts = body.split(":")
    try:
        skill_id = int(parts[0])
    except (ValueError, IndexError):
        skill_id = 0
    page = 0
    if len(parts) > 1:
        try:
            page = max(0, int(parts[1]))
        except ValueError:
            page = 0
    return skill_id, page


def _find_category(settings: Settings, skill_id: int):
    if not 0 < skill_id <= _MAX_SKILL_ID:
        return None
    return next((category for category in settings.categories if category.skill_id == skill_id), None)


def _normalize_category_name(raw: str) -> str:
    return " ".join(raw.split())


def _validate_profile_name(raw: str) -> str:
    if raw == "-":
        return ""
    value = " ".join(raw.split())
    if not value or len(value) > 80 or any(ord(char) < 32 for char in value):
        raise ValueError("Отправь имя от 1 до 80 символов или «-», чтобы очистить поле.")
    return value


def _validate_portfolio_url(raw: str) -> str:
    if raw == "-":
        return ""
    error = "Нужна ссылка http:// или https:// до 500 символов, без логина и пароля. «-» — очистить поле."
    if not raw or len(raw) > 500 or any(char.isspace() or ord(char) < 32 or char in '<>"\\' for char in raw):
        raise ValueError(error)
    try:
        parsed = urlsplit(raw)
        if parsed.scheme.lower() not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
            raise ValueError(error)
        host = (parsed.hostname or "").encode("idna").decode("ascii")
        parsed.port  # Reject malformed or out-of-range ports.
        labels = host.rstrip(".").split(".")
        if len(host) > 253 or len(labels) < 2 or any(
            not re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?", label)
            for label in labels
        ):
            raise ValueError(error)
    except (ValueError, UnicodeError):
        raise ValueError(error) from None
    return raw


def _telegram_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _telegram_chunks(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    chunk: list[str] = []
    size = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > limit:
            chunks.append("".join(chunk))
            chunk = []
            size = 0
        chunk.append(char)
        size += width
    if chunk:
        chunks.append("".join(chunk))
    return chunks


def _write_personal_prompt(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(text + "\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
