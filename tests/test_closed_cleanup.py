import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from app.notifier.cleanup import ClosedProjectsCleaner
from app.projects import Project
from app.storage import StateStore


def project(pid: str, ts: int = 1) -> Project:
    return Project(pid, f"https://freelancehunt.com/project/x/{pid}.html", "Бот", "", "", "", "", ts, skill_id=180)


class Source:
    def __init__(self, statuses):
        self.statuses, self.calls = statuses, []

    async def project_status(self, project_id):
        self.calls.append(project_id)
        status = self.statuses[project_id]
        if isinstance(status, Exception):
            raise status
        return status


class ClosedProjectsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "state.json"
        self.store = StateStore(self.path)
        await self.store.add_projects([project("open", 3), project("done", 2), project("gone", 1), project("err", 0)])
        self.user = SimpleNamespace(user_id=1, store=self.store)

    async def test_closed_and_deleted_projects_leave_the_list_for_good(self):
        source = Source({"done": 13, "gone": 0, "err": RuntimeError("429")})
        removed = await ClosedProjectsCleaner(source).clean([self.user], open_ids={"open"})
        self.assertEqual(removed, 2)
        self.assertNotIn("open", source.calls)  # the current open feed needs no request
        self.assertEqual([p.id for p in await self.store.recent_projects()], ["open", "err"])
        await self.store.add_projects([project("done", 2)])
        self.assertNotIn("done", await StateStore(self.path).history_ids())

    async def test_checks_are_capped_and_rotate(self):
        source = Source({"open": 11, "done": 11, "gone": 11, "err": 11})
        cleaner = ClosedProjectsCleaner(source, max_checks=2)
        await cleaner.clean([self.user], open_ids=set())
        await cleaner.clean([self.user], open_ids=set())
        self.assertEqual(sorted(source.calls), ["done", "err", "gone", "open"])
        self.assertEqual(len(await self.store.history_ids()), 4)


if __name__ == "__main__":
    unittest.main()
