import unittest

from app.ai.screener import ScreenResult, _parse_verdict
from app.projects import Project
from app.telegram import formatting


def project():
    return Project("1", "https://freelancehunt.com/project/x/1.html", "CRM", "", "", "", "", 0)


class ScreenTierTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(_parse_verdict('{"decision":"core","stack":"FastAPI","reason":"CRM"}'),
                         ScreenResult(True, "FastAPI", "core", "CRM"))
        self.assertEqual(_parse_verdict('{"decision":"maybe","stack":"","reason":"стек не указан"}'),
                         ScreenResult(True, "", "maybe", "стек не указан"))
        self.assertFalse(_parse_verdict('{"decision":"skip","stack":"PHP","reason":"плагин WP"}').allowed)

    def test_old_and_invalid_formats_rejected(self):
        for raw in ['{"decision":"allow","stack":""}', '{"decision":"core","stack":""}',
                    '{"decision":"core","stack":"","reason":"' + "x" * 201 + '"}']:
            with self.assertRaises(ValueError):
                _parse_verdict(raw)

    def test_maybe_note_in_notification(self):
        self.assertNotIn("🤔", formatting.format_project_notification(project()))
        text = formatting.format_project_notification(project(), maybe_note="<смешанный стек>")
        self.assertIn("🤔 <i>Под вопросом: &lt;смешанный стек&gt;</i>", text)
        self.assertIn("🤔 <i>Под вопросом</i>", formatting.format_project_notification(project(), maybe_note=""))


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_does_not_rescreen_stored_projects(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from app.notifier.public import PublicNotifier

        screener = Mock(screen=AsyncMock())
        registry = Mock(users=AsyncMock(return_value=[]))
        from pathlib import Path
        settings = SimpleNamespace(poll_interval=60, state_file=Path("/nonexistent/state.json"))
        notifier = PublicNotifier(AsyncMock(), registry, Mock(), settings, Mock(), screener)
        stop = asyncio.Event()
        stop.set()
        await notifier.run(stop)
        screener.screen.assert_not_awaited()


class AutoBidTests(unittest.IsolatedAsyncioTestCase):
    async def test_core_orders_get_a_ready_bid_reply(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from app.ai.policy import AI_OWNER_ID
        from app.notifier import loop as loop_module
        from app.notifier.loop import NotifierLoop

        for uid, tier, expected in [(AI_OWNER_ID, "core", True), (AI_OWNER_ID, "maybe", False), (101, "core", False)]:
            with self.subTest(uid=uid, tier=tier):
                bot = AsyncMock()
                bot.send_message.return_value = SimpleNamespace(message_id=555)
                generator = Mock(generate_bid=AsyncMock(return_value={"rendered": "Здравствуйте.", "version": "v1"}))
                screener = Mock(screen=AsyncMock(return_value=SimpleNamespace(allowed=True, tier=tier, reason="")))
                notifier = NotifierLoop(bot, Mock(), Mock(), SimpleNamespace(telegram_chat_id="1", ai_auto_bid=True),
                                        screener=screener, bid_generator=generator, user_id=uid)
                await notifier._passes_primary_check(project())
                self.assertTrue(await notifier._send_project(project()))
                await asyncio.gather(*list(loop_module._BACKGROUND))
                self.assertEqual(generator.generate_bid.await_count, int(expected))
                self.assertEqual(bot.send_message.await_count, 1 + int(expected))
                if expected:
                    reply = bot.send_message.await_args_list[1].kwargs
                    self.assertEqual((reply["text"], reply["reply_to_message_id"]), ("<pre>Здравствуйте.</pre>", 555))
                    buttons = [b.callback_data for row in reply["reply_markup"].inline_keyboard for b in row]
                    self.assertIn("sent:1", buttons)


if __name__ == "__main__":
    unittest.main()
