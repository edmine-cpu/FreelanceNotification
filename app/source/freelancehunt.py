import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.projects import Category, Project

log = logging.getLogger(__name__)

API_BASE = "https://api.freelancehunt.com/v2"
WEB_PROJECT_URL = "https://freelancehunt.com/project/{id}.html"


class FreelancehuntSource:
    """Fetches open projects for one or more skills from the FreelanceHunt API.

    Each watched category is queried separately and the fetched projects are
    tagged with the category they came from. Docs: https://apidocs.freelancehunt.com/
    """

    def __init__(
        self,
        token: str,
        categories: list[Category],
        page_size: int = 20,
        timeout: float = 30.0,
    ) -> None:
        self._categories = categories
        self._page_size = page_size
        self.successful_skill_ids: set[int] = set()
        self._request_slots = asyncio.Semaphore(5)
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                # Names come back as written on the site (Микола), not transliterated.
                "Accept-Language": "uk",
                "User-Agent": "freelancehunt-notifier/1.0 (+telegram-bot)",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_categories(self, categories: list[Category]) -> None:
        self._categories = list(categories)

    async def fetch_projects(self) -> list[Project]:
        categories = list(self._categories)
        results = await asyncio.gather(
            *(self._fetch_category(category) for category in categories),
            return_exceptions=True,
        )
        projects: list[Project] = []
        self.successful_skill_ids = set()
        for category, result in zip(categories, results):
            if isinstance(result, Exception):
                log.error(
                    "failed to fetch category %s (skill %d): %s",
                    category.name, category.skill_id, result,
                )
                continue
            self.successful_skill_ids.add(category.skill_id)
            projects.extend(result)
        return projects

    async def fetch_category(self, skill_id: int) -> list[Project]:
        """Fetch the current open projects for a single watched skill. Used by the
        /start menu's unfiltered (🔴) view for a live, on-demand refresh."""
        category = next((c for c in self._categories if c.skill_id == skill_id), None)
        if category is None:
            # User callbacks validate membership. A just-added category may not
            # be in the polling union until the next tick.
            from app.config import build_category
            category = build_category(skill_id)
        return await self._fetch_category(category)

    async def my_login(self) -> str:
        """Login of the account that owns the API token."""
        async with self._request_slots:
            resp = await self._client.get("/my/profile")
        resp.raise_for_status()
        return resp.json()["data"]["attributes"]["login"]

    async def project_status(self, project_id: str) -> int:
        """Status id of a project; 0 when it no longer exists."""
        async with self._request_slots:
            resp = await self._client.get(f"/projects/{project_id}")
        if resp.status_code == 404:
            return 0
        resp.raise_for_status()
        return int((resp.json()["data"]["attributes"].get("status") or {}).get("id") or 0)

    async def project_outcome(self, project_id: str) -> tuple[int, str | None]:
        """(status id, chosen freelancer login or None) for a project."""
        async with self._request_slots:
            resp = await self._client.get(f"/projects/{project_id}")
        resp.raise_for_status()
        attrs = resp.json()["data"]["attributes"]
        winner = attrs.get("freelancer") or {}
        return int((attrs.get("status") or {}).get("id") or 0), winner.get("login")

    async def _fetch_category(self, category: Category) -> list[Project]:
        params = {
            "filter[skill_id]": category.skill_id,
            "page[size]": self._page_size,
        }
        async with self._request_slots:
            resp = await self._client.get("/projects", params=params)
        if resp.status_code >= 400:
            raise RuntimeError(f"FreelanceHunt API {resp.status_code}: {resp.text[:200]}")
        data = resp.json().get("data", [])
        result: list[Project] = []
        for item in data:
            project = _to_project(item, category)
            if project is not None:
                result.append(project)
        return result


def _to_project(item: dict, category: Category) -> Project | None:
    try:
        project_id = str(item["id"])
        attrs = item.get("attributes", {})
        name = (attrs.get("name") or "").strip()
        if not name:
            return None

        description = (attrs.get("description") or "").strip()
        budget = _format_budget(attrs.get("budget"))

        published_ts, absolute_time = _format_published(attrs.get("published_at"))
        relative_time = _format_relative(published_ts) if published_ts else ""

        return Project(
            id=project_id,
            url=_extract_web_url(item, project_id),
            title=name,
            budget=budget,
            description=description,
            relative_time=relative_time,
            absolute_time=absolute_time,
            published_ts=published_ts,
            skill_id=category.skill_id,
            category_name=category.name,
            category_url=category.listing_url,
            client_name=((attrs.get("employer") or {}).get("first_name") or "").strip(),
        )
    except Exception:
        log.exception("failed to parse project item: %s", item)
        return None


def _extract_web_url(item: dict, project_id: str) -> str:
    self_link = (item.get("links") or {}).get("self")
    if isinstance(self_link, dict):
        web = self_link.get("web")
        if isinstance(web, str) and web:
            return web
    elif isinstance(self_link, str) and self_link:
        return self_link
    return WEB_PROJECT_URL.format(id=project_id)


def _format_budget(budget: dict | None) -> str:
    if not budget:
        return ""
    amount = budget.get("amount")
    currency = budget.get("currency") or ""
    if amount is None:
        return ""
    return f"{amount} {currency}".strip()


def _format_published(raw: str | None) -> tuple[int, str]:
    if not raw:
        return 0, ""
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return 0, ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp()), _format_absolute(dt)


_MONTHS_RU = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля",
    5: "мая", 6: "июня", 7: "июля", 8: "августа",
    9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _format_absolute(dt: datetime) -> str:
    return f"{dt.day} {_MONTHS_RU[dt.month]}, {dt.hour:02d}:{dt.minute:02d}"


def _format_relative(ts: int) -> str:
    now = int(datetime.now(timezone.utc).timestamp())
    diff = max(0, now - ts)
    if diff < 60:
        return "только что"
    if diff < 3600:
        return _plural(diff // 60, "минуту", "минуты", "минут") + " назад"
    if diff < 86400:
        return _plural(diff // 3600, "час", "часа", "часов") + " назад"
    return _plural(diff // 86400, "день", "дня", "дней") + " назад"


def _plural(n: int, one: str, few: str, many: str) -> str:
    mod10 = n % 10
    mod100 = n % 100
    if mod10 == 1 and mod100 != 11:
        word = one
    elif 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"
