"""Resolve Telegram sender identity before handlers can touch private data."""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.storage.users import UserContext, UserRegistry
from app.ai.policy import ai_allowed
from app.ai.store import AIStore


class UserContextMiddleware(BaseMiddleware):
    def __init__(
        self,
        registry: UserRegistry,
        generator_factory: Callable[[UserContext], Any],
        ai_store: AIStore | None = None,
    ) -> None:
        self._ai_store = ai_store
        self._registry = registry
        self._generator_factory = generator_factory

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        if isinstance(event, Message):
            message, sender = event, event.from_user
        elif isinstance(event, CallbackQuery):
            message, sender = event.message, event.from_user
            if message is not None and not isinstance(message, Message):
                await event.answer("Сообщение устарело. Открой свежий /start.", show_alert=True)
                return None
        else:
            return None
        if (
            message is None
            or sender is None
            or sender.is_bot
            or message.chat.type != ChatType.PRIVATE
            or message.chat.id != sender.id
        ):
            if isinstance(event, CallbackQuery):
                await event.answer("Настройки доступны только в личном чате с ботом.", show_alert=True)
            return None
        context = await self._registry.get(sender.id)
        if self._ai_store is not None:
            keep = isinstance(event, CallbackQuery) and (event.data or "").startswith(("regen:", "ai_skip:", "ai_cancel:", "ai_retry:"))
            if (isinstance(event, CallbackQuery) and not keep) or (isinstance(event, Message) and (event.text or "").startswith("/")):
                self._ai_store.cancel_waiting(sender.id)
                if data.get("state") is not None:
                    await data["state"].clear()
                    data["raw_state"] = None
        generator = self._generator_factory(context) if ai_allowed(sender.id) else None
        if inspect.isawaitable(generator):
            generator = await generator
        data.update(
            settings=await context.settings(),
            store=context.store,
            user_context=context,
            prompt_examples_path=context.prompt_examples_path,
            system_prompt_path=context.system_prompt_path,
            cases_path=context.cases_path,
            bid_generator=generator,
        )
        return await handler(event, data)
