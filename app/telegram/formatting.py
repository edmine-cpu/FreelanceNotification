import html

from app.projects import Project

MAX_DESC = 300


def _category_label(value: str) -> str:
    """Keep menu summaries within Telegram's UTF-16 text budget."""
    value = value or "пока не выбраны"
    size = 0
    for index, char in enumerate(value):
        size += 2 if ord(char) > 0xFFFF else 1
        if size > 1500:
            return value[:index].rstrip() + "…"
    return value


def format_project_notification(project: Project, maybe_note: str | None = None) -> str:
    """maybe_note is set when the AI filter is unsure; "" means no reason given."""
    parts = [f'🆕 <a href="{html.escape(project.url, quote=True)}"><b>{html.escape(project.title)}</b></a>']

    if project.budget:
        parts.append(f"💰 <b>{html.escape(project.budget)}</b>")

    if project.description:
        snippet = project.description
        if len(snippet) > MAX_DESC:
            snippet = snippet[:MAX_DESC].rstrip() + "…"
        parts.append(html.escape(snippet))

    time_bits: list[str] = []
    if project.relative_time:
        time_bits.append(html.escape(project.relative_time))
    if project.absolute_time and project.absolute_time != project.relative_time:
        time_bits.append(f"({html.escape(project.absolute_time)})")
    if time_bits:
        parts.append("🕒 " + " ".join(time_bits))

    if project.category_name:
        category = html.escape(project.category_name)
        if project.category_url:
            category = f'<a href="{html.escape(project.category_url, quote=True)}">{category}</a>'
        parts.append(f"📂 {category}")
    if maybe_note is not None:
        parts.append("🤔 <i>Под вопросом" + (f": {html.escape(maybe_note)}" if maybe_note else "") + "</i>")
    return "\n\n".join(parts)


def format_start_menu(category_label: str, *, ai_enabled: bool = False) -> str:
    return (
        "Привет! Я помогу следить за проектами на Freelancehunt.\n\n"
        f"Твои категории: <b>{html.escape(_category_label(category_label))}</b>.\n\n"
        "Открой ⚙️ Настройки: добавь категории по ID, укажи своё имя и ссылку на портфолио. "
        "Там же можно задать названия категорий и уведомления. "
        + ("Владельцу доступны промпт, примеры и /ai_usage. " if ai_enabled else "") +
        "Все эти настройки действуют только для тебя.\n\n"
        "📂 — проекты из истории; 🔴 — все проекты категории.\n"
        "/settings — настройки · /cancel — отменить ввод\n"
        "/stop — остановить уведомления · /start — возобновить"
    )


def format_settings_menu(category_label: str, muted_count: int, profile: dict | None = None) -> str:
    muted_line = f"\nОтключено уведомлений: <b>{muted_count}</b>" if muted_count else ""
    profile = profile or {}
    return (
        "<b>Мои настройки</b>\n\nВсе изменения действуют только для тебя.\n\n"
        f"Категории: {html.escape(_category_label(category_label))}{muted_line}\n"
        f"Имя: {html.escape(profile.get('name') or 'не указано')}\n"
        f"Портфолио: {html.escape(profile.get('portfolio_url') or 'не указано')}"
    )


def format_profile(profile: dict) -> str:
    return (
        "<b>Моё имя и портфолио</b>\n\n"
        f"Имя: {html.escape(profile.get('name') or 'не указано')}\n"
        f"Портфолио: {html.escape(profile.get('portfolio_url') or 'не указано')}\n\n"
        "ИИ использует эти данные только в твоих откликах. "
        "Выбери, что изменить. Для очистки поля отправь «-»."
    )


def format_remove_categories(category_label: str) -> str:
    return (
        "<b>Удалить категорию</b>\n\n"
        f"Твои категории: {html.escape(_category_label(category_label))}.\n"
        "Нажми на категорию, чтобы убрать её из своей подписки."
    )


def format_category_notifications(category_label: str) -> str:
    return (
        "<b>Уведомления категорий</b>\n\n"
        f"Категории: {html.escape(_category_label(category_label))}\n"
        "Нажми на категорию, чтобы включить или отключить уведомления."
    )


def format_category_names(category_label: str) -> str:
    return (
        "<b>Имена категорий</b>\n\n"
        f"Категории: {html.escape(_category_label(category_label))}\n"
        "Нажми на категорию, чтобы задать или изменить имя."
    )


def format_add_category_prompt() -> str:
    return (
        "<b>Добавить новую категорию</b>\n\n"
        "Отправь ID категории Freelancehunt одним сообщением. "
        "Например: 180 — разработка ботов, 99 — веб-программирование.\n"
        "Можно добавить до 30 категорий. /cancel — отменить ввод."
    )


def format_category_name_prompt(skill_id: int, current_name: str) -> str:
    return (
        "<b>Имя категории</b>\n\n"
        f"ID: <code>{skill_id}</code>\n"
        f"Сейчас: <b>{html.escape(current_name)}</b>\n\n"
        "Отправь новое имя одним сообщением."
    )


def format_prompt_edit_prompt() -> str:
    return (
        "<b>Мои примеры откликов (JSON)</b>\n\n"
        "Отправь новый JSON целиком одним сообщением. "
        "Изменятся только твои примеры. /cancel — отменить ввод."
    )


def format_cases_caption() -> str:
    return (
        "<b>Мои кейсы (досье)</b>\n\n"
        "Из этого файла AI берёт похожие кейсы, цифры, гарантию и бонус для ставок. "
        "Ничего вне файла он не придумывает. Исправь файл и загрузи его кнопкой ниже."
    )


def format_cases_edit_prompt() -> str:
    return (
        "<b>Мои кейсы (досье)</b>\n\n"
        "Отправь файл cases.json документом (или JSON текстом, если он короткий). "
        "Старый файл заменится целиком. /cancel — отменить ввод."
    )


def format_settings_notice(text: str) -> str:
    return f"<b>Настройки:</b>\n\n{html.escape(text)}"


def format_projects_page_header(category_label: str, page: int, total_pages: int, total: int) -> str:
    return (
        f"<b>Последние проекты</b> — {html.escape(_category_label(category_label))}\n"
        f"Всего: {total} · страница {page + 1}/{max(total_pages, 1)}"
    )


def format_empty_history(category_label: str) -> str:
    return (
        f"Пока в истории нет проектов из категорий <b>{html.escape(_category_label(category_label))}</b>.\n"
        "Подожди до следующей проверки — и они появятся здесь."
    )
