import hashlib
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from app.ai.bid_generator import BidGenerator
from app.ai.cases import CASES_FILE, CasesJsonError, dossier_durations, ensure_cases_json, parse_cases
from app.ai.policy import AI_OWNER_ID
from app.ai.pricing import PricingEngine, StaticRatesSource, render_bid
from app.ai import prompt_store
from app.ai.prompt_store import SYSTEM_PROMPT_FILE, upgrade_stock_prompts
from app.ai.store import AIStore
from app.llm import LLMResult
from app.projects import Project
from app.telegram import keyboards

RATES = {"UAH": Decimal("41.5"), "EUR": Decimal("0.92"), "PLN": Decimal("4")}
CASES = {
    "profile_facts": ["21 проект, оценка 5.0"],
    "offer": {"guarantee": "90 дней бесплатно исправляю ошибки"},
    "cases": [{"id": "booking", "title": "Бот записи", "metric": "запись 24/7"}],
}


def project(description: str = "Бот записи") -> Project:
    return Project("7", "https://freelancehunt.com/project/x/7.html", "Бот", "", description, "", "", 0,
                   client_name="Олег")


def quote(tier: str = "bot:M"):
    return PricingEngine().quote(project(), tier, RATES)


class RenderTests(unittest.TestCase):
    def test_price_tokens_land_in_the_requested_section(self):
        prose = ("👋 Здравствуйте!\n\n3. Стоимость и сроки\n- Разработка: {price}, {deadline}, точнее после материалов\n"
                 "- Сделаю за 2 дня\n\n👉 Обсудим детали в переписке?")
        rendered = render_bid(prose, quote(), "ru")
        self.assertIn("3. Стоимость и сроки\n- Разработка: 3500 грн, 2-3 дня, точнее после материалов", rendered)
        self.assertNotIn("за 2 дня", rendered)
        self.assertNotIn("Цена 3500", rendered)
        self.assertTrue(rendered.endswith("👉 Обсудим детали в переписке?"))

    def test_vague_projects_drop_price_tokens(self):
        rendered = render_bid("Сделаю бота\nЦена: {price}", quote("omit"), "ru")
        self.assertNotIn("{price}", rendered)
        self.assertNotIn("Цена", rendered)

    def test_dossier_durations_survive_but_invented_terms_do_not(self):
        allowed = dossier_durations(parse_cases(json.dumps(CASES)))
        prose = "Гарантія 90 днів на виправлення\nСделаю за 4 дня\nСделаю за 3 дня"
        rendered = render_bid(prose, quote(), "ru", allowed_durations=allowed)
        self.assertIn("Гарантія 90 днів", rendered)
        self.assertNotIn("за 4 дня", rendered)
        self.assertNotIn("за 3 дня", rendered)

    def test_lists_stay_together_and_bold_is_removed(self):
        prose = "**Подход:**\n- база знаний\n- анкета\nВопрос в конце?"
        rendered = render_bid(prose, quote(), "ru")
        self.assertTrue(rendered.startswith("Подход:\n- база знаний\n- анкета\n\nВопрос в конце?"))


class CasesFileTests(unittest.TestCase):
    def test_validation_rejects_duplicate_ids(self):
        bad = {"cases": [{"id": "a", "title": "x"}, {"id": "a", "title": "y"}]}
        with self.assertRaises(CasesJsonError):
            parse_cases(json.dumps(bad))

    def test_stock_dossier_is_valid_and_copied_once(self):
        self.assertTrue(parse_cases(CASES_FILE.read_bytes())["cases"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            ensure_cases_json(path)
            self.assertEqual(path.read_bytes(), CASES_FILE.read_bytes())
            path.write_text('{"cases": []}')
            ensure_cases_json(path)
            self.assertEqual(path.read_text(), '{"cases": []}')

    def test_unedited_stock_dossier_is_upgraded(self):
        from app.ai import cases as cases_module
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cases.json"
            path.write_text('{"cases": []}')
            stock = {hashlib.sha256(path.read_bytes()).hexdigest()}
            with patch.object(cases_module, "_LEGACY_CASES_HASHES", stock):
                ensure_cases_json(path)
            self.assertEqual(path.read_bytes(), CASES_FILE.read_bytes())
            self.assertNotIn(b"t.me", path.read_bytes())

    def test_api_names_are_requested_in_ukrainian(self):
        from app.source import FreelancehuntSource
        source = FreelancehuntSource("token", [])
        self.assertEqual(source._client.headers["Accept-Language"], "uk")

    def test_unedited_stock_prompt_is_upgraded_custom_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            old, custom, examples = Path(tmp) / "old.md", Path(tmp) / "custom.md", Path(tmp) / "ex.json"
            old.write_text("Старый стоковый промпт")
            custom.write_text("Мой стиль")
            examples.write_text(json.dumps({"examples": []}))
            stock = {hashlib.sha256(old.read_bytes()).hexdigest()}
            with patch.object(prompt_store, "_LEGACY_SYSTEM_PROMPT_HASHES", stock):
                upgrade_stock_prompts(old, examples)
                upgrade_stock_prompts(custom, examples)
            self.assertEqual(old.read_bytes(), SYSTEM_PROMPT_FILE.read_bytes())
            self.assertEqual(custom.read_text(), "Мой стиль")
            self.assertEqual(json.loads(examples.read_text()), {"examples": []})

    def test_missing_case_warning_button(self):
        rows = keyboards.regen_bid_keyboard("7", "v", needs_case=True).inline_keyboard
        self.assertIn("заполни строку", rows[0][0].text)
        self.assertNotIn("заполни", keyboards.regen_bid_keyboard("7", "v").inline_keyboard[0][0].text)


class Client:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    async def generate(self, *, system_instruction, messages, **kwargs):
        self.calls.append({"system": system_instruction, "messages": messages, "schema": kwargs.get("schema")})
        return LLMResult(json.dumps(self.responses.pop(0)))


class GeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, *responses):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        prompt, cases = Path(self.tmp.name) / "prompt.md", Path(self.tmp.name) / "cases.json"
        prompt.write_text("Стиль")
        cases.write_text(json.dumps(CASES, ensure_ascii=False))
        self.client = Client(*responses)
        generator = BidGenerator(self.client, user_id=AI_OWNER_ID, ai_store=AIStore(), system_prompt_path=prompt,
                                 rates_provider=StaticRatesSource(RATES), cases_path=cases)
        return await generator.generate_bid(project("В отклике укажите примеры работ"))

    @staticmethod
    def answer(prose, case_id="booking", slot="", covered="Делал бота записи"):
        return {"scope_tier": "bot:M", "requirements": [
            {"need": "примеры работ", "type": "answer", "covered_by": covered}],
            "case_id": case_id, "case_slot": slot, "prose": prose}

    async def test_dossier_and_client_name_reach_the_model(self):
        result = await self.generate(self.answer("👋 Здравствуйте, Олег!\nДелал бота записи: запись 24/7"))
        call = self.client.calls[0]
        self.assertIn("- booking: Бот записи", call["system"])
        self.assertEqual(json.loads(call["messages"][-1].text)["client_name"], "Олег")
        self.assertEqual(list(call["schema"]["properties"])[-1], "prose")
        self.assertFalse(result["needs_case"])
        self.assertEqual(len(self.client.calls), 1)

    async def test_missing_case_becomes_a_bracketed_slot(self):
        result = await self.generate(self.answer(
            "👋 Здравствуйте!\n\n{case}\n\nДелал бота записи похожим способом", case_id="",
            slot="Делал похожий сервис для ..."))
        self.assertIn("👋 Здравствуйте!\n\n[Делал похожий сервис для ...]\n\nДелал бота", result["rendered"])
        self.assertTrue(result["needs_case"])

    async def test_telegram_and_email_contacts_never_reach_the_bid(self):
        result = await self.generate(self.answer(
            "👋 Здравствуйте!\nДелал бота записи. Работает: @FortuneXStarsBot.\nДемо: https://t.me/demo_bot\n"
            "Пишите в Telegram @nikita_dev или на mail@example.com"))
        for contact in ("@", "t.me", "Демо", "Работает", "Пишите в Telegram", "example.com"):
            self.assertNotIn(contact, result["rendered"])
        self.assertIn("Делал бота записи.", result["rendered"])

    async def test_uncovered_answer_and_unknown_case_trigger_one_correction(self):
        result = await self.generate(
            self.answer("Сделаю бота", case_id="invented"),
            self.answer("Сделаю бота", covered="примеры: бот записи"),
            self.answer("Сделаю бота. Делал бота записи"))
        self.assertEqual(len(self.client.calls), 2)
        feedback = self.client.calls[1]["messages"][-1].text
        self.assertIn("invented", feedback)
        self.assertIn("Цена 3500 грн", result["rendered"])
        self.assertNotIn("Делал бота записи", result["rendered"])


if __name__ == "__main__":
    unittest.main()
