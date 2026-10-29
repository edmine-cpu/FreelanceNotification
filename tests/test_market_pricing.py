import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.ai.policy import AI_OWNER_ID
from app.ai.pricing import BID_TIERS, PricingEngine, PricingQuote, load_price_table
from app.ai.store import AIStore
from app.notifier.outcomes import OutcomeTracker, outcome_for
from app.projects import Project
from app.telegram import keyboards

RATES = {"UAH": Decimal("41.5"), "EUR": Decimal("0.92"), "PLN": Decimal("4")}


def project(budget: str = "", pid: str = "1") -> Project:
    return Project(pid, f"https://freelancehunt.com/project/x/{pid}.html", "CRM", budget, "", "", "", 0)


class TablePricingTests(unittest.TestCase):
    def setUp(self):
        self.engine = PricingEngine()

    def quote(self, tier, budget=""):
        return self.engine.quote(project(budget), tier, RATES)

    def test_no_budget_uses_market_table(self):
        q = self.quote("crm:L")
        self.assertEqual((q.amount, q.currency, q.deadline, q.hours), (Decimal("12000"), "UAH", "5-7", None))
        self.assertEqual(self.quote("bot:S").deadline, "1-2")
        self.assertEqual(self.quote("ai:M").deadline, "2-4")

    def test_never_below_budget(self):
        self.assertEqual(self.quote("bot:S", "25000 UAH").amount, Decimal("25000"))
        self.assertEqual(self.quote("parsing:S", "1161 UAH").amount, Decimal("1161"))

    def test_close_to_budget_bids_exactly_budget(self):
        # bot:M = 3500 <= 1.5 * 3000, so the client's own number wins.
        self.assertEqual(self.quote("bot:M", "3000 UAH").amount, Decimal("3000"))

    def test_far_above_budget_is_capped(self):
        # crm:XL = 18000 > 1.5 * 3800; capped at 2.5 * 3800 = 9500.
        self.assertEqual(self.quote("crm:XL", "3800 UAH").amount, Decimal("9500"))
        # crm:L = 12000 between 1.5x and 2.5x of 5000 keeps the market price.
        self.assertEqual(self.quote("crm:L", "5000 UAH").amount, Decimal("12000"))

    def test_foreign_budget_currency(self):
        q = self.quote("bot:M", "50 USD")
        self.assertEqual(q.currency, "USD")
        self.assertEqual(q.amount, Decimal("90"))  # 3500 / 41.5 = 84.3 -> 90 (> 1.5x50 -> capped at 130)

    def test_quote_roundtrip_and_tiers(self):
        q = self.quote("integration:M")
        self.assertEqual(PricingQuote.from_dict(q.to_dict()), q)
        self.assertIn("crm:XL", BID_TIERS)
        self.assertNotIn("8", BID_TIERS)
        legacy = self.quote("8")
        self.assertEqual(legacy.hours, 8)

    def test_paragraphs_keep_questions_for_concrete_projects(self):
        from app.ai.pricing import render_bid
        prose = "Здравствуйте.\nСделаю бота записи на Python. Есть документация?\nПохожий бот уже работает у клиента."
        rendered = render_bid(prose, self.quote("bot:M"), "ru")
        self.assertEqual(rendered, "Здравствуйте.\n\nСделаю бота записи на Python. Есть документация?"
                                   "\n\nПохожий бот уже работает у клиента."
                                   "\n\nЦена 3500 грн, срок 2-3 дня, начну сегодня. Пишите")

    def test_ukrainian_quote_line_uses_termin(self):
        from app.ai.pricing import format_quote_line
        line = format_quote_line(self.quote("bot:M"), "ua")
        self.assertEqual(line, "Ціна 3500 грн, термін 2-3 дні, почну сьогодні. Пишіть")
        self.assertNotIn("строк", line)

    def test_table_file_is_complete(self):
        table = load_price_table()
        self.assertEqual(set(table["prices_uah"]), {t.split(":")[0] for t in BID_TIERS if t != "omit"})


class BidLogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = AIStore()
        self.engine = PricingEngine()

    def log(self, pid, tier, budget=""):
        p = project(budget, pid)
        self.store.log_bid(AI_OWNER_ID, p, self.engine.quote(p, tier, RATES).to_dict())

    def test_outcome_rules(self):
        self.assertEqual(outcome_for(14, "Edmine", "edmine"), "won")
        self.assertEqual(outcome_for(21, "other", "edmine"), "lost")
        self.assertEqual(outcome_for(11, None, "edmine"), "open")
        self.assertEqual(outcome_for(13, None, "edmine"), "closed")

    async def test_tracker_marks_outcomes_and_notifies_only_sent(self):
        self.log("1", "crm:L")
        self.log("2", "bot:M", "3000 UAH")
        self.log("3", "parsing:S")
        self.assertTrue(self.store.mark_bid_sent(AI_OWNER_ID, "1"))
        self.assertFalse(self.store.mark_bid_sent(AI_OWNER_ID, "404"))
        outcomes = {"1": (14, "edmine"), "2": (14, "rival"), "3": (11, None)}
        source = Mock(my_login=AsyncMock(return_value="edmine"),
                      project_outcome=AsyncMock(side_effect=lambda pid: outcomes[pid]))
        bot = AsyncMock()
        await OutcomeTracker(bot, source, self.store, AI_OWNER_ID).check_once()
        rows = {r["project_id"]: r["outcome"] for r in self.store.db.execute("SELECT * FROM bid_log")}
        self.assertEqual(rows, {"1": "won", "2": "lost", "3": "open"})
        bot.send_message.assert_awaited_once()
        self.assertIn("Тебя выбрали", bot.send_message.call_args.args[1])
        # Checked rows wait for the interval before the next API call.
        self.assertEqual(self.store.bids_to_check(AI_OWNER_ID), [])

    def test_stats_report(self):
        self.assertIn("нет", self.store.bid_stats(AI_OWNER_ID))
        self.log("1", "crm:L")
        self.log("2", "bot:M", "3000 UAH")
        self.store.mark_bid_sent(AI_OWNER_ID, "1")
        self.store.mark_bid_sent(AI_OWNER_ID, "2")
        self.store.set_bid_outcome(AI_OWNER_ID, "1", "won", "edmine")
        self.store.set_bid_outcome(AI_OWNER_ID, "2", "lost", "rival")
        report = self.store.bid_stats(AI_OWNER_ID)
        self.assertIn("Всего: 2 ставок, выиграно 1, другому 1", report)
        self.assertIn("винрейт 50%", report)
        self.assertIn("= бюджету: 1", report)
        self.assertIn("без бюджета: 1", report)
        with self.assertRaises(PermissionError):
            self.store.bid_stats(101)

    def test_sent_button_on_generated_bid(self):
        data = [b.callback_data for row in keyboards.regen_bid_keyboard("42", "v").inline_keyboard for b in row]
        self.assertIn("sent:42", data)


class CombinedGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_tier_prices_logs_and_limits_stages_to_requests(self):
        import json
        from app.ai.bid_generator import BID_SCHEMA, BidGenerator
        from app.ai.pricing import StaticRatesSource
        from app.llm import LLMResult

        class Client:
            calls = []

            async def generate(self, *, system_instruction, messages, **kwargs):
                self.calls.append((system_instruction, kwargs.get("schema")))
                return LLMResult(json.dumps({"scope_tier": "crm:L", "prose": "Привет, сделаю CRM с ролями"}))

        store = AIStore()
        client = Client()
        generator = BidGenerator(client, user_id=AI_OWNER_ID, ai_store=store,
                                 rates_provider=StaticRatesSource(RATES))
        result = await generator.generate_bid(project("3800 UAH", "77"))
        self.assertIn("Цена 9500 грн, срок 5-7 дней, начну сегодня. Пишите", result["rendered"])
        system, schema = client.calls[0]
        self.assertIn("Этапы работ описывай, только если заказчик сам их просит", system)
        self.assertIn("crm (CRM", system)
        self.assertEqual(schema["properties"]["scope_tier"]["enum"], BID_SCHEMA["properties"]["scope_tier"]["enum"])
        row = dict(store.db.execute("SELECT * FROM bid_log").fetchone())
        self.assertEqual((row["project_id"], row["tier"], row["amount"], row["sent"]), ("77", "crm:L", "9500", 0))


if __name__ == "__main__":
    unittest.main()
