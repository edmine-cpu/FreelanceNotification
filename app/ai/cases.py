"""The author's dossier: profile facts, offer and real cases a bid may cite."""
import json
import shutil
from pathlib import Path
from typing import Any

from .pricing import durations
from .prompt_store import _atomic_write_json

CASES_FILE = Path(__file__).parent / "prompts" / "cases.json"
MAX_CASES_BYTES = 200_000
_CASE_FIELDS = ("title", "tags", "did", "result", "metric", "stack", "proof", "link")


class CasesJsonError(ValueError):
    pass


def empty_cases() -> dict[str, Any]:
    return {"profile_facts": [], "offer": {}, "cases": []}


def ensure_cases_json(path: Path, default_path: Path = CASES_FILE) -> None:
    if not path.exists() and default_path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(default_path, path)


def load_cases(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return empty_cases()
    return parse_cases(path.read_bytes())


def parse_cases(raw: bytes | str) -> dict[str, Any]:
    if len(raw) > MAX_CASES_BYTES:
        raise CasesJsonError("Файл кейсов слишком большой.")
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CasesJsonError(f"JSON не читается: {exc}") from None
    _validate(data)
    return data


def write_cases_json(raw: bytes | str, path: Path) -> dict[str, Any]:
    data = parse_cases(raw)
    _atomic_write_json(path, data)
    return data


def case_ids(data: dict[str, Any]) -> set[str]:
    return {case["id"] for case in data["cases"]}


def dossier_durations(data: dict[str, Any]) -> frozenset[tuple[str, str]]:
    """Durations the bid may state because the author wrote them (guarantee)."""
    return durations(json.dumps(data, ensure_ascii=False))


def dossier_text(data: dict[str, Any]) -> str:
    lines = ["Досье автора - единственный источник фактов о его опыте, цифрах и ссылках:"]
    if data["profile_facts"]:
        lines.append("Факты профиля:")
        lines += [f"- {fact}" for fact in data["profile_facts"]]
    offer = {key: value for key, value in data["offer"].items() if value}
    labels = {"guarantee": "Гарантия", "free_bonus": "Бесплатный бонус", "handover": "Передача проекта"}
    for key, value in offer.items():
        lines.append(f"{labels.get(key, key)}: {value}")
    if not offer.get("free_bonus"):
        lines.append("Бесплатный бонус не задан: не обещай бесплатных услуг.")
    lines.append("Кейсы (id: описание):")
    for case in data["cases"]:
        parts = [case.get("title", "")]
        for field, label in (("did", "сделано"), ("result", "результат"), ("metric", "цифра"),
                             ("stack", "стек"), ("proof", "доказательство"), ("link", "ссылка")):
            if case.get(field):
                parts.append(f"{label}: {case[field]}")
        if case.get("tags"):
            parts.append("теги: " + ", ".join(case["tags"]))
        lines.append(f"- {case['id']}: " + " | ".join(parts))
    return "\n".join(lines)


def _validate(data: Any) -> None:
    if not isinstance(data, dict):
        raise CasesJsonError("Корень JSON должен быть объектом.")
    facts, offer, cases = data.get("profile_facts", []), data.get("offer", {}), data.get("cases")
    data.setdefault("profile_facts", facts)
    data.setdefault("offer", offer)
    if not isinstance(facts, list) or not all(isinstance(item, str) for item in facts):
        raise CasesJsonError('"profile_facts" должен быть списком строк.')
    if not isinstance(offer, dict) or not all(isinstance(value, str) for value in offer.values()):
        raise CasesJsonError('"offer" должен быть объектом со строковыми значениями.')
    if not isinstance(cases, list):
        raise CasesJsonError('В JSON должен быть массив "cases".')
    seen: set[str] = set()
    for index, case in enumerate(cases, start=1):
        if not isinstance(case, dict):
            raise CasesJsonError(f"Кейс #{index} должен быть объектом.")
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip() or case_id in seen:
            raise CasesJsonError(f'У кейса #{index} нужен уникальный непустой "id".')
        seen.add(case_id)
        for field in _CASE_FIELDS:
            value = case.get(field, [] if field == "tags" else "")
            valid = isinstance(value, list) and all(isinstance(item, str) for item in value) \
                if field == "tags" else isinstance(value, str)
            if not valid:
                raise CasesJsonError(f'У кейса "{case_id}" поле "{field}" заполнено неверно.')
