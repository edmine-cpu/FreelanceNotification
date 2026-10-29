from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.projects import Category, Project

CALLBACK_LIST_PREFIX = "list:"
CALLBACK_RAW_PREFIX = "raw:"
CALLBACK_START = "start"
CALLBACK_NOOP = "noop"
CALLBACK_REGEN_PREFIX = "regen:"
CALLBACK_HIDE = "hide"
CALLBACK_SHOW_PREFIX = "show:"
CALLBACK_GEN_PREFIX = "gen:"
CALLBACK_SKIP_PREFIX = "ai_skip:"
CALLBACK_CANCEL_AI_PREFIX = "ai_cancel:"
CALLBACK_RETRY_AI_PREFIX = "ai_retry:"
CALLBACK_AI_USAGE = "ai_usage"
CALLBACK_BID_SENT_PREFIX = "sent:"
CALLBACK_SETTINGS = "settings"
CALLBACK_ADD_CATEGORY = "settings:add_category"
CALLBACK_CATEGORY_NAMES = "settings:category_names"
CALLBACK_EDIT_CATEGORY_NAME_PREFIX = "settings:edit_name:"
CALLBACK_PROMPT_JSON = "settings:prompt_json"
CALLBACK_PROMPT_EDIT = "settings:prompt_edit"
CALLBACK_NOTIFICATIONS = "settings:notifications"
CALLBACK_TOGGLE_MUTE_PREFIX = "settings:toggle_mute:"
CALLBACK_REMOVE_CATEGORIES = "settings:remove_categories"
CALLBACK_REMOVE_CATEGORY_PREFIX = "settings:remove_category:"
CALLBACK_PROFILE = "settings:profile"
CALLBACK_PROFILE_NAME = "settings:profile_name"
CALLBACK_PROFILE_PORTFOLIO = "settings:profile_portfolio"
CALLBACK_SYSTEM_PROMPT = "settings:system_prompt"
CALLBACK_SYSTEM_PROMPT_EDIT = "settings:system_prompt_edit"
CALLBACK_CASES = "settings:cases"
CALLBACK_CASES_EDIT = "settings:cases_edit"

# Filter key used in `list:<filter>:<page>` callbacks to mean "every category".
LIST_FILTER_ALL = "all"

MAX_BUTTON_TEXT = 60

HIDE_BUTTON_TEXT = "🙈 Скрыть"


def list_callback(filter_key: str, page: int) -> str:
    return f"{CALLBACK_LIST_PREFIX}{filter_key}:{page}"


def raw_callback(skill_id: int, page: int) -> str:
    return f"{CALLBACK_RAW_PREFIX}{skill_id}:{page}"


def start_menu_keyboard(categories: list[Category]) -> InlineKeyboardMarkup:
    # Per category, two buttons in one row: 📂 = filtered (passed the primary
    # check, from cache), 🔴 = unfiltered (live fetch, everything).
    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(
                text=f"📂 {_truncate(category.name)}",
                callback_data=list_callback(str(category.skill_id), 0),
            ),
            InlineKeyboardButton(
                text=f"🔴 {_truncate(category.name)}",
                callback_data=raw_callback(category.skill_id, 0),
            ),
        ]
        for category in categories
    ]
    rows.append(
        [InlineKeyboardButton(text="📋 Все", callback_data=list_callback(LIST_FILTER_ALL, 0))]
    )
    rows.append([InlineKeyboardButton(text="⚙️ Настройки", callback_data=CALLBACK_SETTINGS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_keyboard(*, ai_enabled: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить новую категорию по ID", callback_data=CALLBACK_ADD_CATEGORY)],
            [InlineKeyboardButton(text="🏷️ Имена категорий", callback_data=CALLBACK_CATEGORY_NAMES)],
            [InlineKeyboardButton(text="➖ Удалить категорию", callback_data=CALLBACK_REMOVE_CATEGORIES)],
            [InlineKeyboardButton(text="👤 Моё имя и портфолио", callback_data=CALLBACK_PROFILE)],
            *([[InlineKeyboardButton(text="📝 Мой промпт", callback_data=CALLBACK_SYSTEM_PROMPT)],
               [InlineKeyboardButton(text="📑 Примеры откликов (JSON)", callback_data=CALLBACK_PROMPT_JSON)],
               [InlineKeyboardButton(text="🗂 Мои кейсы (досье)", callback_data=CALLBACK_CASES)],
               [InlineKeyboardButton(text="Расходы AI", callback_data=CALLBACK_AI_USAGE)]] if ai_enabled else []),
            [InlineKeyboardButton(text="🔔 Уведомления категорий", callback_data=CALLBACK_NOTIFICATIONS)],
            [InlineKeyboardButton(text="🏠 В меню", callback_data=CALLBACK_START)],
        ]
    )


def category_notifications_keyboard(
    categories: list[Category],
    muted_skill_ids: set[int],
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for category in categories:
        muted = category.skill_id in muted_skill_ids
        icon = "🔕" if muted else "🔔"
        status = "выкл" if muted else "вкл"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {status} · {_truncate(category.name)}",
                    callback_data=f"{CALLBACK_TOGGLE_MUTE_PREFIX}{category.skill_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Настройки", callback_data=CALLBACK_SETTINGS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_names_keyboard(categories: list[Category]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for category in categories:
        label = _truncate(f"{category.skill_id} · {category.name}")
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✏️ {label}",
                    callback_data=f"{CALLBACK_EDIT_CATEGORY_NAME_PREFIX}{category.skill_id}",
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="⬅️ Настройки", callback_data=CALLBACK_SETTINGS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def category_names_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Имена категорий", callback_data=CALLBACK_CATEGORY_NAMES)]
        ]
    )


def remove_categories_keyboard(categories: list[Category]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"➖ {_truncate(category.name)}",
            callback_data=f"{CALLBACK_REMOVE_CATEGORY_PREFIX}{category.skill_id}",
        )]
        for category in categories
    ]
    rows.append([InlineKeyboardButton(text="⬅️ Настройки", callback_data=CALLBACK_SETTINGS)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def profile_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Моё имя", callback_data=CALLBACK_PROFILE_NAME)],
        [InlineKeyboardButton(text="🔗 Моё портфолио", callback_data=CALLBACK_PROFILE_PORTFOLIO)],
        [InlineKeyboardButton(text="⬅️ Настройки", callback_data=CALLBACK_SETTINGS)],
    ])


def system_prompt_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить мой промпт", callback_data=CALLBACK_SYSTEM_PROMPT_EDIT)],
        [InlineKeyboardButton(text="⬅️ Настройки", callback_data=CALLBACK_SETTINGS)],
    ])


def prompt_json_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Изменить JSON", callback_data=CALLBACK_PROMPT_EDIT)],
            [InlineKeyboardButton(text=HIDE_BUTTON_TEXT, callback_data=CALLBACK_HIDE)],
        ]
    )


def cases_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Загрузить новый файл", callback_data=CALLBACK_CASES_EDIT)],
            [InlineKeyboardButton(text=HIDE_BUTTON_TEXT, callback_data=CALLBACK_HIDE)],
        ]
    )


def settings_back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Настройки", callback_data=CALLBACK_SETTINGS)]]
    )


def projects_page_keyboard(
    projects: list[Project],
    page: int,
    page_size: int,
    filter_key: str,
    raw: bool = False,
) -> InlineKeyboardMarkup:
    total = len(projects)
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    chunk = projects[start : start + page_size]

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=_truncate(p.title), callback_data=f"{CALLBACK_SHOW_PREFIX}{p.id}")]
        for p in chunk
    ]

    # In raw mode filter_key is the skill_id; navigation re-enters the live view.
    def nav(target_page: int) -> str:
        return raw_callback(int(filter_key), target_page) if raw else list_callback(filter_key, target_page)

    prev_cb = nav(page - 1) if page > 0 else CALLBACK_NOOP
    next_cb = nav(page + 1) if page < total_pages - 1 else CALLBACK_NOOP
    rows.append(
        [
            InlineKeyboardButton(text="« назад" if page > 0 else "·", callback_data=prev_cb),
            InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data=CALLBACK_NOOP),
            InlineKeyboardButton(text="далее »" if page < total_pages - 1 else "·", callback_data=next_cb),
        ]
    )
    rows.append([InlineKeyboardButton(text="🏠 В меню", callback_data=CALLBACK_START)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def empty_history_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🏠 В меню", callback_data=CALLBACK_START)]]
    )


def project_detail_keyboard(project: Project, *, ai_enabled: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔗 Открыть проект", url=project.url)],
            *([[InlineKeyboardButton(text="Сгенерировать ставку", callback_data=f"{CALLBACK_GEN_PREFIX}{project.id}")]] if ai_enabled else []),
            [InlineKeyboardButton(text=HIDE_BUTTON_TEXT, callback_data=CALLBACK_HIDE)],
        ]
    )


def notification_keyboard(project_id: str, *, ai_enabled: bool = False) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *([[InlineKeyboardButton(text="Сгенерировать ставку", callback_data=f"{CALLBACK_GEN_PREFIX}{project_id}")]] if ai_enabled else []),
            [InlineKeyboardButton(text=HIDE_BUTTON_TEXT, callback_data=CALLBACK_HIDE)],
        ]
    )


def regen_bid_keyboard(project_id: str, version: str = "", *, needs_case: bool = False) -> InlineKeyboardMarkup:
    warning = [[InlineKeyboardButton(text="⚠️ Нет похожего кейса: заполни строку в [ ]",
                                     callback_data=CALLBACK_NOOP)]] if needs_case else []
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *warning,
            [InlineKeyboardButton(text="Новый вариант", callback_data=f"{CALLBACK_REGEN_PREFIX}{project_id}:{version}")],
            [InlineKeyboardButton(text="📤 Отправил ставку", callback_data=f"{CALLBACK_BID_SENT_PREFIX}{project_id}")],
            [InlineKeyboardButton(text=HIDE_BUTTON_TEXT, callback_data=CALLBACK_HIDE)],
        ]
    )


def hide_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=HIDE_BUTTON_TEXT, callback_data=CALLBACK_HIDE)]]
    )


def _truncate(text: str) -> str:
    if len(text) <= MAX_BUTTON_TEXT:
        return text
    return text[: MAX_BUTTON_TEXT - 1].rstrip() + "…"


def revision_keyboard(operation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Пропустить", callback_data=CALLBACK_SKIP_PREFIX+operation_id)],
        [InlineKeyboardButton(text="Отмена", callback_data=CALLBACK_CANCEL_AI_PREFIX+operation_id)],
    ])


def retry_ai_keyboard(operation_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Повторить попытку", callback_data=CALLBACK_RETRY_AI_PREFIX+operation_id)]
    ])
