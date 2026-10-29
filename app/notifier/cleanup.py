"""Remove projects that stopped accepting bids from every user's list."""
import asyncio
import logging
import time

from app.source import FreelancehuntSource
from app.storage.users import UserContext

from .outcomes import STATUS_OPEN

log = logging.getLogger(__name__)


class ClosedProjectsCleaner:
    def __init__(self, source: FreelancehuntSource, max_checks: int = 40) -> None:
        self._source = source
        self._max_checks = max_checks
        self._checked: dict[str, float] = {}

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
        removed = 0
        for user in users:
            if closed & histories[user.user_id]:
                removed += await user.store.remove_projects(closed)
        if removed:
            log.info("removed %d closed projects from lists", removed)
        return removed
