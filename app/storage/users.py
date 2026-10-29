"""Persisted private workspaces for Telegram users and one-time owner import."""

import asyncio
import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings, parse_skill_ids
from app.ai.policy import ai_allowed

from .state import StateStore

PROMPTS_DIR = Path(__file__).resolve().parents[1] / "ai" / "prompts"
PUBLIC_SYSTEM_PROMPT = PROMPTS_DIR / "public_system_prompt.md"
LEGACY_SYSTEM_PROMPT = PROMPTS_DIR / "system_prompt.md"


@dataclass(frozen=True)
class UserContext:
    user_id: int
    store: StateStore
    prompt_examples_path: Path
    system_prompt_path: Path
    quote_path: Path
    is_legacy_owner: bool
    _base_settings: Settings = field(repr=False, compare=False)

    @property
    def cases_path(self) -> Path:
        return self.prompt_examples_path.parent / "cases.json"

    async def settings(self) -> Settings:
        """Build a request/poll snapshot without mutating application defaults."""
        skill_ids = await self.store.skill_ids(parse_skill_ids(self._base_settings.skill_ids))
        return self._base_settings.model_copy(
            deep=True,
            update={
                "telegram_chat_id": str(self.user_id),
                "skill_ids": ",".join(str(skill_id) for skill_id in skill_ids),
                "category_names": await self.store.category_names(),
                "state_file": self.prompt_examples_path.parent / "state.json",
                "prompt_examples_file": self.prompt_examples_path,
                "quote_file": self.quote_path,
                "primary_filter_enabled": ai_allowed(self.user_id),
                "ai_user_id": self.user_id,
            },
        )


class UserRegistry:
    def __init__(self, settings: Settings) -> None:
        # Keep pristine ENV defaults even if a caller modifies its own settings.
        self._settings = settings.model_copy(deep=True)
        self.root = settings.state_file.parent / "users"
        self._lock = asyncio.Lock()
        self._contexts: dict[int, UserContext] = {}
        self._migration_checked = False

    async def migrate_legacy(self) -> None:
        async with self._lock:
            await self._migrate_legacy_locked()

    async def _migrate_legacy_locked(self) -> None:
        if self._migration_checked:
            return
        try:
            owner_id = int(self._settings.telegram_chat_id)
        except (TypeError, ValueError):
            owner_id = 0
        if owner_id > 0 and not (self.root / str(owner_id)).exists():
            examples_path = (
                self._settings.prompt_examples_file
                or self._settings.state_file.parent / "bids_examples.json"
            )
            legacy_exists = any(
                path.exists()
                for path in (self._settings.state_file, examples_path, self._settings.quote_file)
            )
            if legacy_exists:
                await self._create_locked(owner_id, legacy=True)
        self._migration_checked = True

    async def get(self, user_id: int) -> UserContext:
        if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
            raise ValueError("a positive Telegram user ID is required")
        async with self._lock:
            await self._migrate_legacy_locked()
            if user_id not in self._contexts:
                if not (self.root / str(user_id)).exists():
                    await self._create_locked(user_id, legacy=False)
                self._contexts[user_id] = self._load_context(user_id)
            return self._contexts[user_id]

    async def users(self) -> list[UserContext]:
        async with self._lock:
            await self._migrate_legacy_locked()
            if self.root.exists():
                for directory in self.root.iterdir():
                    if not directory.is_dir() or not directory.name.isdecimal():
                        continue
                    user_id = int(directory.name)
                    if user_id <= 0 or directory.name != str(user_id):
                        continue
                    if user_id not in self._contexts:
                        self._contexts[user_id] = self._load_context(user_id)
            return [self._contexts[user_id] for user_id in sorted(self._contexts)]

    async def _create_locked(self, user_id: int, *, legacy: bool) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        directory = self.root / str(user_id)
        # Build completely before making a user visible to the notifier. A
        # crash cannot leave a registered user with missing/private templates.
        with tempfile.TemporaryDirectory(prefix=f".{user_id}-", dir=self.root) as tmp:
            staging = Path(tmp) / "user"
            staging.mkdir()
            state_path = staging / "state.json"
            if legacy and self._settings.state_file.exists():
                shutil.copyfile(self._settings.state_file, state_path)
            else:
                state_path.write_text(
                    json.dumps({
                        "skill_ids": parse_skill_ids(self._settings.skill_ids),
                        "profile": {"name": "", "portfolio_url": ""},
                        "active": True,
                    }),
                    encoding="utf-8",
                )
            store = StateStore(state_path, history_size=self._settings.history_size)
            if legacy:
                await store.set_profile(
                    name="Никита",
                    portfolio_url="https://freelancehunt.com/freelancer/edmine.html#portfolio",
                )
            await store.set_active(True)
            examples_path = (
                self._settings.prompt_examples_file
                or self._settings.state_file.parent / "bids_examples.json"
            )
            if legacy and examples_path.exists():
                shutil.copyfile(examples_path, staging / "bids_examples.json")
            else:
                (staging / "bids_examples.json").write_text('{"examples": []}', encoding="utf-8")
            if legacy and self._settings.quote_file.exists():
                shutil.copyfile(self._settings.quote_file, staging / "quotes.json")
            else:
                (staging / "quotes.json").write_text('{"quotes": {}}', encoding="utf-8")
            prompt_path = LEGACY_SYSTEM_PROMPT if legacy else PUBLIC_SYSTEM_PROMPT
            shutil.copyfile(prompt_path, staging / "system_prompt.md")
            (staging / "user.json").write_text(
                json.dumps({"user_id": user_id, "is_legacy_owner": legacy}), encoding="utf-8"
            )
            staging.rename(directory)

    def _load_context(self, user_id: int) -> UserContext:
        directory = self.root / str(user_id)
        metadata_path = directory / "user.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
        return UserContext(
            user_id=user_id,
            store=StateStore(directory / "state.json", history_size=self._settings.history_size),
            prompt_examples_path=directory / "bids_examples.json",
            system_prompt_path=directory / "system_prompt.md",
            quote_path=directory / "quotes.json",
            is_legacy_owner=metadata.get("is_legacy_owner") is True,
            _base_settings=self._settings,
        )
