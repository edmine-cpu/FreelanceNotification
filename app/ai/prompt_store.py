import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .pricing import line_leaks, normalize_dashes

PROMPTS_DIR = Path(__file__).parent / "prompts"
EXAMPLES_FILE = PROMPTS_DIR / "bids_examples.json"
SYSTEM_PROMPT_FILE = PROMPTS_DIR / "system_prompt.md"
_LEGACY_STOCK_HASHES = {
    "7a6c669e85fd02e46058824e27d6b18cd36ffa82daf9188cb556204012890b0c",
    "fe3600ed7683a61fa8708308a334fc595214e1705e4dd09ba50efe37bbc2b3fc",  # three-line bids
}
# Raw sha256 of owner system prompts that shipped as stock and were never edited.
_LEGACY_SYSTEM_PROMPT_HASHES = {
    "7c01e48d8abe143829540fcbfdd79634f4726d5eeea8c2a4b1850fab7d4c5961",  # three-line bids
}
_PRICING_INPUT_KEYS = {
    "budget",
    "price",
    "deadline",
    "hours",
    "hourly_rate",
    "rates",
    "currency",
}


class PromptJsonError(ValueError):
    pass


def read_prompt_json(path: Path = EXAMPLES_FILE) -> str:
    data = sanitize_prompt_data(_load_json(path))
    return json.dumps(data, ensure_ascii=False, indent=2)


def ensure_prompt_json(path: Path, default_path: Path = EXAMPLES_FILE) -> None:
    if path.exists():
        existing = _load_json(path)
        if canonical_prompt_hash(existing) in _LEGACY_STOCK_HASHES:
            _atomic_write_json(path, sanitize_prompt_data(_load_json(default_path)))
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, sanitize_prompt_data(_load_json(default_path)))


def upgrade_stock_prompts(prompt_path: Path, examples_path: Path,
                          prompt_default: Path = SYSTEM_PROMPT_FILE,
                          examples_default: Path = EXAMPLES_FILE) -> None:
    """Move an owner who never edited the shipped prompt/examples to the current ones."""
    if prompt_path.exists() and hashlib.sha256(prompt_path.read_bytes()).hexdigest() in _LEGACY_SYSTEM_PROMPT_HASHES:
        tmp = prompt_path.with_name(f".{prompt_path.name}.tmp")
        tmp.write_bytes(prompt_default.read_bytes())
        os.replace(tmp, prompt_path)
    if examples_path.exists():
        ensure_prompt_json(examples_path, examples_default)


def write_prompt_json(raw: str, path: Path = EXAMPLES_FILE) -> None:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PromptJsonError(f"JSON не читается: {exc.msg} на строке {exc.lineno}") from None
    _validate_prompt_data(data)
    normalized = copy.deepcopy(data)
    for example in normalized["examples"]:
        example["output"] = normalize_dashes(example["output"])
    sanitized = sanitize_prompt_data(data)
    if sanitized != normalized:
        raise PromptJsonError(
            "Примеры должны описывать только стиль: убери budget/price/deadline/hours "
            "и строки с ценой или сроками."
        )
    _atomic_write_json(path, sanitized)


def sanitize_prompt_data(data: Any) -> dict[str, Any]:
    """Remove pricing signals without mutating a customized source file."""
    _validate_prompt_data(data)
    clean = copy.deepcopy(data)
    for example in clean["examples"]:
        payload = example["input"]
        for key in list(payload):
            if str(key).lower() in _PRICING_INPUT_KEYS:
                payload.pop(key, None)
        lines = []
        for line in normalize_dashes(example["output"]).splitlines():
            if not line_leaks(line, tokens=True):
                lines.append(line)
        example["output"] = "\n".join(lines).strip()
    return clean


def canonical_prompt_hash(data: Any) -> str:
    payload = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PromptJsonError(f"Не удалось прочитать JSON: {exc}") from None
    except json.JSONDecodeError as exc:
        raise PromptJsonError(f"Текущий JSON повреждён: {exc.msg} на строке {exc.lineno}") from None
    _validate_prompt_data(data)
    return data


def _validate_prompt_data(data: Any) -> None:
    if not isinstance(data, dict):
        raise PromptJsonError("Корень JSON должен быть объектом.")
    examples = data.get("examples")
    if not isinstance(examples, list):
        raise PromptJsonError('В JSON должен быть массив "examples".')
    for index, example in enumerate(examples, start=1):
        if not isinstance(example, dict):
            raise PromptJsonError(f"Пример #{index} должен быть объектом.")
        if not isinstance(example.get("input"), dict):
            raise PromptJsonError(f'У примера #{index} поле "input" должно быть объектом.')
        if not isinstance(example.get("output"), str):
            raise PromptJsonError(f'У примера #{index} поле "output" должно быть строкой.')


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
