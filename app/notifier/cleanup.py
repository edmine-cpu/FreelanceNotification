"""Remove projects that stopped accepting bids from every user's list."""
import asyncio
import json
import logging
import os
import time
from pathlib import Path

from app.source import FreelancehuntSource
from app.storage.users import UserContext

from .outcomes import STATUS_OPEN

log = logging.getLogger(__name__)


class ClosedProjectsCleaner:
    def __init__(self, source: FreelancehuntSource, path: Path | None = None, *,
                 max_checks: int = 100, interval: float = 900) -> None:
        self._source = source
        self._path = path
        self._max_checks = max_checks
        self._interval = interval
        self._last_run = time.monotonic()  # the first run waits a full interval after start
        self._checked: dict[str, float] = self._load()

    async def maybe_clean(self, users: list[UserContext], open_ids: set[str]) -> int:
        if time.monotonic() - self._last_run < self._interval:
            return 0
        self._last_run = time.monotonic()
        return await self.clean(users, open_ids)

    async def clean(self, users: list[UserContext], open_ids: set[str]) -> int:
        """Check the least recently checked history projects (the current open
        feed needs no check) and drop the closed ones everywhere."""
        histories = {user.user_id: set(await user.store.history_ids()) for user in users}
        candidates = set().union(*histories.values()) - open_ids if histories else set()
        self._checked = {pid: ts for pid, ts in self._checked.items() if pid in candidates}
        batch = sorted(candidates, key=lambda pid: self._checked.get(pid, 0.0))[: self._max_checks]
        statuses = await asyncio.gather(*(self._source.project_status(pid) for pid in batch),
                                        return_exceptions=True)
        now = time.time()
        closed = set()
        for project_id, status in zip(batch, statuses):
            if isinstance(status, Exception):
                log.debug("status check failed for project %s: %s", project_id, status)
                continue
            self._checked[project_id] = now
            if status != STATUS_OPEN:
                closed.add(project_id)
        self._save()
        removed = 0
        for user in users:
            if closed & histories[user.user_id]:
                removed += await user.store.remove_projects(closed)
        if removed:
            log.info("removed %d closed projects from lists", removed)
        return removed

    # Check times survive restarts, so a deploy continues the rotation instead of starting over.
    def _load(self) -> dict[str, float]:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8")) if self._path and self._path.exists() else {}
            return {str(pid): float(ts) for pid, ts in data.items()}
        except (OSError, ValueError, AttributeError):
            log.warning("closed projects check times unreadable, starting over")
            return {}

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            tmp = self._path.with_name(f".{self._path.name}.tmp")
            tmp.write_text(json.dumps(self._checked), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError:
            log.exception("failed to save closed projects check times")
