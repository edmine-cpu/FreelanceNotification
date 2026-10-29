import asyncio
import json
import logging
from dataclasses import replace
from pathlib import Path

import aiofiles

from app.projects import Project

log = logging.getLogger(__name__)


class StateStore:
    """Async, lock-protected persisted state.

    Holds the set of project IDs already announced, a rolling history of the
    most recent projects (used by the /start menu), and a per-skill publish
    watermark. A category is considered "first seen" until it has a watermark
    entry — see ``has_watermark``.
    """

    def __init__(self, path: Path, history_size: int = 50, seen_size: int = 500) -> None:
        self._path = path
        self._history_size = history_size
        self._seen_size = seen_size
        self._lock = asyncio.Lock()
        self._seen_ids: list[str] = []
        # IDs of projects that passed the primary check and were notified. Kept
        # separately from Project (which add_projects overwrites on re-fetch) so
        # the verdict survives. Powers the filtered (📂) view in the /start menu.
        self._passed_ids: list[str] = []
        self._projects: list[Project] = []
        # Projects that stopped accepting bids: dropped from history for good.
        self._closed_ids: list[str] = []
        # Watermark per skill_id: the publish timestamp of the last announced
        # project in that category. Absence of an entry means "not yet seen".
        self._watermarks: dict[str, int] = {}
        self._skill_ids: list[int] | None = None
        self._muted_skill_ids: list[int] = []
        self._category_names: dict[int, str] = {}
        self._profile: dict[str, str] = {"name": "", "portfolio_url": ""}
        self._active = True
        self._load_sync()

    def _load_sync(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            log.exception("failed to read state from %s, starting fresh", self._path)
            return
        self._seen_ids = [str(x) for x in data.get("seen_ids", [])]
        self._passed_ids = [str(x) for x in data.get("passed_ids", [])]
        self._projects = [Project.from_dict(p) for p in data.get("projects", [])]
        self._closed_ids = [str(x) for x in data.get("closed_ids", [])]
        raw_watermark = data.get("last_published_ts", {})
        if isinstance(raw_watermark, dict):
            self._watermarks = {str(k): int(v) for k, v in raw_watermark.items()}
        # Older single-int formats are intentionally dropped: every category is
        # then treated as first-seen and its current backlog is suppressed
        # (guarded further by seen_ids) instead of being re-announced.
        raw_skill_ids = data.get("skill_ids")
        if isinstance(raw_skill_ids, list):
            self._skill_ids = _dedupe_ints(raw_skill_ids)
        self._muted_skill_ids = _dedupe_ints(data.get("muted_skill_ids", []))
        self._category_names = _clean_category_names(data.get("category_names", {}))
        raw_profile = data.get("profile", {})
        if isinstance(raw_profile, dict):
            self._profile = {
                key: value.strip() if isinstance(value := raw_profile.get(key), str) else ""
                for key in ("name", "portfolio_url")
            }
        self._active = data.get("active", True) is not False
        self._apply_category_names_to_projects()

    async def _persist_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "seen_ids": self._seen_ids,
            "passed_ids": self._passed_ids,
            "projects": [p.to_dict() for p in self._projects],
            "closed_ids": self._closed_ids,
            "last_published_ts": self._watermarks,
            "skill_ids": self._skill_ids,
            "muted_skill_ids": self._muted_skill_ids,
            "category_names": self._category_names,
            "profile": self._profile,
            "active": self._active,
        }
        tmp = self._path.with_suffix(".tmp")
        async with aiofiles.open(tmp, "w", encoding="utf-8") as f:
            await f.write(json.dumps(payload, ensure_ascii=False))
        tmp.replace(self._path)

    def has_watermark(self, skill_id: int) -> bool:
        return str(skill_id) in self._watermarks

    def last_published_ts(self, skill_id: int) -> int:
        return self._watermarks.get(str(skill_id), 0)

    async def is_seen(self, project_id: str) -> bool:
        async with self._lock:
            return project_id in self._seen_ids

    async def mark_seen(self, project_ids: list[str]) -> None:
        if not project_ids:
            return
        async with self._lock:
            existing = set(self._seen_ids)
            for project_id in project_ids:
                if project_id not in existing:
                    self._seen_ids.append(project_id)
                    existing.add(project_id)
            # Keep the most recently seen IDs (insertion order = recency).
            self._seen_ids = self._seen_ids[-self._seen_size:]
            await self._persist_locked()

    async def mark_passed(self, project_ids: list[str]) -> None:
        """Record projects that passed the primary check and were notified, so
        the filtered (📂) /start view can show only them."""
        if not project_ids:
            return
        async with self._lock:
            existing = set(self._passed_ids)
            for project_id in project_ids:
                if project_id not in existing:
                    self._passed_ids.append(project_id)
                    existing.add(project_id)
            self._passed_ids = self._passed_ids[-self._seen_size:]
            await self._persist_locked()

    async def set_passed(self, project_ids: list[str]) -> None:
        """Replace the whole passed set (used by the startup re-filter, which
        recomputes it from scratch each launch)."""
        async with self._lock:
            seen: set[str] = set()
            ordered: list[str] = []
            for project_id in project_ids:
                if project_id not in seen:
                    seen.add(project_id)
                    ordered.append(project_id)
            self._passed_ids = ordered[-self._seen_size:]
            await self._persist_locked()

    async def passed_ids(self) -> set[str]:
        async with self._lock:
            return set(self._passed_ids)

    async def skill_ids(self, fallback: list[int]) -> list[int]:
        async with self._lock:
            return list(self._skill_ids if self._skill_ids is not None else fallback)

    async def add_skill_id(self, skill_id: int, fallback: list[int]) -> bool:
        """Persist a watched skill id. Returns False when it already exists."""
        if skill_id <= 0:
            raise ValueError("skill_id must be positive")
        async with self._lock:
            current = list(self._skill_ids if self._skill_ids is not None else fallback)
            if skill_id in current:
                return False
            current.append(skill_id)
            self._skill_ids = current
            await self._persist_locked()
            return True

    async def remove_skill_id(self, skill_id: int, fallback: list[int]) -> bool:
        """Stop watching a category, including when it is the last category."""
        async with self._lock:
            current = list(self._skill_ids if self._skill_ids is not None else fallback)
            if skill_id not in current:
                return False
            self._skill_ids = [value for value in current if value != skill_id]
            self._muted_skill_ids = [value for value in self._muted_skill_ids if value != skill_id]
            self._category_names.pop(skill_id, None)
            self._watermarks.pop(str(skill_id), None)
            self._projects = [project for project in self._projects if project.skill_id != skill_id]
            await self._persist_locked()
            return True

    async def profile(self) -> dict[str, str]:
        async with self._lock:
            return dict(self._profile)

    async def set_profile(
        self, name: str | None = None, portfolio_url: str | None = None
    ) -> None:
        async with self._lock:
            if name is not None:
                self._profile["name"] = name.strip()
            if portfolio_url is not None:
                self._profile["portfolio_url"] = portfolio_url.strip()
            await self._persist_locked()

    async def set_active(self, active: bool) -> None:
        async with self._lock:
            if self._active == active and self._path.exists():
                return
            self._active = active
            await self._persist_locked()

    async def is_active(self) -> bool:
        async with self._lock:
            return self._active

    async def category_names(self, fallback: dict[int, str] | None = None) -> dict[int, str]:
        async with self._lock:
            names = _clean_category_names(fallback or {})
            names.update(self._category_names)
            return dict(names)

    async def set_category_name(self, skill_id: int, name: str) -> None:
        clean_name = _normalize_category_name(name)
        if skill_id <= 0 or not clean_name:
            raise ValueError("category name requires a positive skill_id and non-empty name")
        async with self._lock:
            self._category_names[skill_id] = clean_name
            self._apply_category_names_to_projects()
            await self._persist_locked()

    async def muted_skill_ids(self) -> set[int]:
        async with self._lock:
            return set(self._muted_skill_ids)

    async def toggle_muted_skill_id(self, skill_id: int) -> bool:
        """Toggle category notifications. Returns True when muted after toggle."""
        async with self._lock:
            if skill_id in self._muted_skill_ids:
                self._muted_skill_ids = [x for x in self._muted_skill_ids if x != skill_id]
                muted = False
            else:
                self._muted_skill_ids.append(skill_id)
                muted = True
            await self._persist_locked()
            return muted

    async def add_projects(self, projects: list[Project]) -> None:
        if not projects:
            return
        async with self._lock:
            merged: dict[str, Project] = {p.id: p for p in self._projects}
            closed = set(self._closed_ids)
            for p in projects:
                if p.id not in closed:
                    merged[p.id] = p
            # Keep the most recent history_size projects PER category, so a busy
            # category can't evict another's history out of the /start menu.
            per_skill: dict[int, list[Project]] = {}
            for p in merged.values():
                per_skill.setdefault(p.skill_id, []).append(p)
            kept: list[Project] = []
            for group in per_skill.values():
                group.sort(key=lambda p: p.published_ts, reverse=True)
                kept.extend(group[: self._history_size])
            self._projects = sorted(kept, key=lambda p: p.published_ts, reverse=True)
            self._apply_category_names_to_projects()
            await self._persist_locked()

    async def history_ids(self) -> list[str]:
        async with self._lock:
            return [project.id for project in self._projects]

    async def remove_projects(self, project_ids: set[str]) -> int:
        """Drop closed projects from the history so lists show only open ones."""
        async with self._lock:
            kept = [project for project in self._projects if project.id not in project_ids]
            removed = len(self._projects) - len(kept)
            if not removed:
                return 0
            self._projects = kept
            known = set(self._closed_ids)
            self._closed_ids += [project_id for project_id in project_ids if project_id not in known]
            self._closed_ids = self._closed_ids[-self._seen_size:]
            await self._persist_locked()
            return removed

    async def recent_projects(self) -> list[Project]:
        async with self._lock:
            return [project for project in self._projects if self._watches(project.skill_id)]

    async def find_project(self, project_id: str) -> Project | None:
        async with self._lock:
            for p in self._projects:
                if p.id == project_id and self._watches(p.skill_id):
                    return p
            return None

    def _watches(self, skill_id: int) -> bool:
        return self._skill_ids is None or skill_id in self._skill_ids

    async def update_last_published_ts(self, skill_id: int, ts: int) -> None:
        key = str(skill_id)
        async with self._lock:
            # Seed the entry on first sight even if ts == 0, so the category is
            # no longer treated as first-seen on the next tick.
            if key not in self._watermarks or ts > self._watermarks[key]:
                self._watermarks[key] = ts
                await self._persist_locked()

    def _apply_category_names_to_projects(self) -> None:
        if not self._category_names or not self._projects:
            return
        updated: list[Project] = []
        changed = False
        for project in self._projects:
            category_name = self._category_names.get(project.skill_id)
            if category_name and project.category_name != category_name:
                updated.append(replace(project, category_name=category_name))
                changed = True
            else:
                updated.append(project)
        if changed:
            self._projects = updated


def _dedupe_ints(values: object) -> list[int]:
    if not isinstance(values, list):
        return []
    result: list[int] = []
    seen: set[int] = set()
    for value in values:
        try:
            item = int(value)
        except (TypeError, ValueError):
            continue
        if item > 0 and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _clean_category_names(values: object) -> dict[int, str]:
    if not isinstance(values, dict):
        return {}
    result: dict[int, str] = {}
    for raw_skill_id, raw_name in values.items():
        try:
            skill_id = int(raw_skill_id)
        except (TypeError, ValueError):
            continue
        name = _normalize_category_name(raw_name)
        if skill_id > 0 and name:
            result[skill_id] = name
    return result


def _normalize_category_name(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())
