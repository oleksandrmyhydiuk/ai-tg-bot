from dataclasses import dataclass
import json
import logging
from aiogram import Bot
from aiogram.types import (
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from sqlalchemy import or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_upsert
from app.database.engine import async_session_factory
from app.database.models import ChatSettings, User, utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModuleSpec:
    id: str
    title: str
    description: str
    commands: tuple[tuple[str, str], ...]
    default_enabled: bool = True
    requires_logging: bool = False


CORE_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Налаштування модулів чату"),
    ("help", "Довідка по активних модулях"),
    ("clear", "Очистити історію чату (адміни)"),
)

MODULE_ORDER: tuple[str, ...] = (
    "summary",
    "ask",
    "media_dl",
    "search",
    "stats",
    "voice",
    "daily_digest",
    "link_tldr",
    "privacy_export",
    "admin_ops",
)

MODULES: dict[str, ModuleSpec] = {
    "summary": ModuleSpec(
        id="summary",
        title="Підсумки чату",
        description="Погодинні дайджести, /summary та /tasks із посиланнями на джерела",
        commands=(
            ("summary", "Підсумок чату за N год"),
            ("tasks", "Рішення, задачі та відкриті питання"),
        ),
        default_enabled=True,
        requires_logging=True,
    ),
    "ask": ModuleSpec(
        id="ask",
        title="AI-помічник (/ask)",
        description="Відповіді на запитання, фото, Google Search та діалог у reply",
        commands=(("ask", "Запитати ШІ (текст або фото)"),),
        default_enabled=True,
        requires_logging=False,
    ),
    "media_dl": ModuleSpec(
        id="media_dl",
        title="Завантаження відео",
        description="Автоматичне завантаження відео за посиланням (TikTok, YouTube, Instagram) без команд + /dl",
        commands=(("dl", "Завантажити відео за посиланням"),),
        default_enabled=True,
        requires_logging=False,
    ),
    "search": ModuleSpec(
        id="search",
        title="Пошук по чату",
        description="Швидкий пошук по збереженій історії повідомлень (/find)",
        commands=(("find", "Пошук по історії чату"),),
        default_enabled=True,
        requires_logging=True,
    ),
    "stats": ModuleSpec(
        id="stats",
        title="Статистика чату",
        description="Активність, топ учасників, пікові години та топ-reply (/stats)",
        commands=(("stats", "Статистика активності чату"),),
        default_enabled=True,
        requires_logging=True,
    ),
    "voice": ModuleSpec(
        id="voice",
        title="Голосові та документи",
        description="Транскрибація голосових/відеонотаток (/transcribe) та аналіз документів (/doc)",
        commands=(
            ("transcribe", "Розшифрувати голосове / кружечок"),
            ("doc", "Проаналізувати PDF/текстовий документ"),
        ),
        default_enabled=False,
        requires_logging=False,
    ),
    "daily_digest": ModuleSpec(
        id="daily_digest",
        title="Щоденний дайджест",
        description="Автоматичний щоденний підсумок чату за розкладом (/digest)",
        commands=(("digest", "Розклад щоденного дайджесту"),),
        default_enabled=False,
        requires_logging=True,
    ),
    "link_tldr": ModuleSpec(
        id="link_tldr",
        title="Саммарі посилань",
        description="Стислий переказ веб-сторінок та YouTube-відео (/tldr)",
        commands=(("tldr", "Короткий зміст посилання або YouTube"),),
        default_enabled=True,
        requires_logging=False,
    ),
    "privacy_export": ModuleSpec(
        id="privacy_export",
        title="Приватність і експорт",
        description="Відмова від логування (/optout, /optin) та експорт дайджестів (/export)",
        commands=(
            ("optout", "Не зберігати мої повідомлення"),
            ("optin", "Знову зберігати мої повідомлення"),
            ("export", "Експорт дайджестів (.md/.txt)"),
        ),
        default_enabled=True,
        requires_logging=False,
    ),
    "admin_ops": ModuleSpec(
        id="admin_ops",
        title="Моніторинг і квоти",
        description="Стан бота, БД та статистика моделей ШІ (/health, /quota)",
        commands=(
            ("health", "Стан бота та бази даних"),
            ("quota", "Статистика запитів до моделей ШІ"),
        ),
        default_enabled=False,
        requires_logging=False,
    ),
}

DEFAULT_ENABLED_MODULES: frozenset[str] = frozenset(
    spec.id for spec in MODULES.values() if spec.default_enabled
)

_modules_cache: dict[int, set[str]] = {}
_optout_cache: dict[int, bool] = {}


def clear_modules_cache(chat_id: int | None = None) -> None:
    """Clears the in-memory per-chat enabled modules cache (all or a specific chat)."""
    if chat_id is None:
        _modules_cache.clear()
    else:
        _modules_cache.pop(chat_id, None)


def clear_optout_cache(user_id: int | None = None) -> None:
    """Clears the in-memory per-user opt-out cache (all or a specific user)."""
    if user_id is None:
        _optout_cache.clear()
    else:
        _optout_cache.pop(user_id, None)


async def is_user_opted_out(user_id: int | None) -> bool:
    """Returns True if the user has opted out of chat message logging."""
    if user_id is None:
        return False

    cached = _optout_cache.get(user_id)
    if cached is not None:
        return cached

    try:
        async with async_session_factory() as session:
            row = await session.get(User, user_id)
    except Exception as e:
        logger.warning("Failed to load opt-out state for user %s: %s", user_id, e)
        return False

    opted_out = bool(row.is_opted_out) if row is not None else False
    _optout_cache[user_id] = opted_out
    return opted_out


async def set_user_opted_out(user_id: int, opted_out: bool) -> None:
    """Upserts the User row with the given opt-out preference and updates the in-memory cache."""
    flag = bool(opted_out)
    now_utc = utc_now()
    async with async_session_factory() as session:
        stmt = (
            sqlite_upsert(User)
            .values(
                user_id=user_id,
                username=None,
                full_name=f"User {user_id}",
                is_opted_out=flag,
                updated_at=now_utc,
            )
            .on_conflict_do_update(
                index_elements=[User.user_id],
                set_={
                    "is_opted_out": flag,
                    "updated_at": now_utc,
                },
            )
        )
        await session.execute(stmt)
        await session.commit()

    _optout_cache[user_id] = flag


def _serialize_modules(enabled: set[str] | frozenset[str]) -> str:
    ordered = [mid for mid in MODULE_ORDER if mid in enabled]
    return json.dumps(ordered, ensure_ascii=False)


def _deserialize_modules(raw: str | None) -> set[str]:
    if raw is None:
        return set(DEFAULT_ENABLED_MODULES)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return {mid for mid in parsed if isinstance(mid, str) and mid in MODULES}
    except (ValueError, TypeError) as e:
        logger.warning("Invalid enabled_modules JSON (%r): %s", raw, e)
    return set(DEFAULT_ENABLED_MODULES)


async def get_enabled_modules(chat_id: int) -> set[str]:
    cached = _modules_cache.get(chat_id)
    if cached is not None:
        return set(cached)

    try:
        async with async_session_factory() as session:
            row = await session.get(ChatSettings, chat_id)
    except Exception as e:
        logger.warning("Failed to load ChatSettings for chat %s: %s", chat_id, e)
        return set(DEFAULT_ENABLED_MODULES)

    if row is None:
        enabled = set(DEFAULT_ENABLED_MODULES)
    else:
        enabled = _deserialize_modules(row.enabled_modules)

    _modules_cache[chat_id] = set(enabled)
    return set(enabled)


async def is_module_enabled(chat_id: int, module_id: str) -> bool:
    if module_id not in MODULES:
        return False
    enabled = await get_enabled_modules(chat_id)
    return module_id in enabled


async def chat_requires_logging(chat_id: int) -> bool:
    enabled = await get_enabled_modules(chat_id)
    return any(MODULES[mid].requires_logging for mid in enabled if mid in MODULES)


async def set_module_enabled(chat_id: int, module_id: str, enabled: bool) -> set[str]:
    if module_id not in MODULES:
        raise ValueError(f"Unknown module id: {module_id}")

    async with async_session_factory() as session:
        row = await session.get(ChatSettings, chat_id)
        if row is None:
            current = set(DEFAULT_ENABLED_MODULES)
            if enabled:
                current.add(module_id)
            else:
                current.discard(module_id)
            row = ChatSettings(
                chat_id=chat_id,
                enabled_modules=_serialize_modules(current),
                updated_at=utc_now(),
            )
            session.add(row)
        else:
            current = _deserialize_modules(row.enabled_modules)
            if enabled:
                current.add(module_id)
            else:
                current.discard(module_id)
            row.enabled_modules = _serialize_modules(current)
            row.updated_at = utc_now()
        await session.commit()

    _modules_cache[chat_id] = set(current)
    return set(current)


async def toggle_module(chat_id: int, module_id: str) -> tuple[bool, set[str]]:
    if module_id not in MODULES:
        raise ValueError(f"Unknown module id: {module_id}")

    async with async_session_factory() as session:
        row = await session.get(ChatSettings, chat_id)
        if row is None:
            current = set(DEFAULT_ENABLED_MODULES)
            new_state = module_id not in current
            if new_state:
                current.add(module_id)
            else:
                current.discard(module_id)
            row = ChatSettings(
                chat_id=chat_id,
                enabled_modules=_serialize_modules(current),
                updated_at=utc_now(),
            )
            session.add(row)
        else:
            current = _deserialize_modules(row.enabled_modules)
            new_state = module_id not in current
            if new_state:
                current.add(module_id)
            else:
                current.discard(module_id)
            row.enabled_modules = _serialize_modules(current)
            row.updated_at = utc_now()
        await session.commit()

    _modules_cache[chat_id] = set(current)
    return new_state, set(current)


async def set_all_modules(chat_id: int, enabled: bool) -> set[str]:
    new_set = set(MODULES.keys()) if enabled else set()

    async with async_session_factory() as session:
        row = await session.get(ChatSettings, chat_id)
        if row is None:
            row = ChatSettings(
                chat_id=chat_id,
                enabled_modules=_serialize_modules(new_set),
                updated_at=utc_now(),
            )
            session.add(row)
        else:
            row.enabled_modules = _serialize_modules(new_set)
            row.updated_at = utc_now()
        await session.commit()

    _modules_cache[chat_id] = set(new_set)
    return set(new_set)


async def get_daily_digest_time(chat_id: int) -> str | None:
    async with async_session_factory() as session:
        row = await session.get(ChatSettings, chat_id)
        return row.daily_digest_time if row is not None else None


async def set_daily_digest_time(chat_id: int, hhmm: str | None) -> None:
    async with async_session_factory() as session:
        row = await session.get(ChatSettings, chat_id)
        if row is None:
            row = ChatSettings(
                chat_id=chat_id,
                enabled_modules=_serialize_modules(DEFAULT_ENABLED_MODULES),
                daily_digest_time=hhmm,
                updated_at=utc_now(),
            )
            session.add(row)
        else:
            row.daily_digest_time = hhmm
            row.updated_at = utc_now()
        await session.commit()


async def mark_daily_digest_sent(chat_id: int, date_str: str) -> None:
    async with async_session_factory() as session:
        row = await session.get(ChatSettings, chat_id)
        if row is None:
            row = ChatSettings(
                chat_id=chat_id,
                enabled_modules=_serialize_modules(DEFAULT_ENABLED_MODULES),
                daily_digest_last_sent=date_str,
                updated_at=utc_now(),
            )
            session.add(row)
        else:
            row.daily_digest_last_sent = date_str
            row.updated_at = utc_now()
        await session.commit()


async def get_chats_due_for_daily_digest(current_hhmm: str, today_str: str) -> list[int]:
    async with async_session_factory() as session:
        stmt = select(ChatSettings).where(
            ChatSettings.daily_digest_time == current_hhmm,
            or_(
                ChatSettings.daily_digest_last_sent.is_(None),
                ChatSettings.daily_digest_last_sent != today_str,
            ),
        )
        rows = (await session.execute(stmt)).scalars().all()

    due_chat_ids: list[int] = []
    for row in rows:
        enabled = _deserialize_modules(row.enabled_modules)
        if "daily_digest" in enabled:
            due_chat_ids.append(row.chat_id)
    return due_chat_ids


def build_bot_commands_for_modules(enabled_modules: set[str]) -> list[BotCommand]:
    commands: list[BotCommand] = []
    seen: set[str] = set()

    for cmd, desc in CORE_COMMANDS:
        if cmd not in seen:
            commands.append(BotCommand(command=cmd, description=desc))
            seen.add(cmd)

    for module_id in MODULE_ORDER:
        if module_id not in enabled_modules:
            continue
        spec = MODULES[module_id]
        for cmd, desc in spec.commands:
            if cmd not in seen:
                commands.append(BotCommand(command=cmd, description=desc))
                seen.add(cmd)

    return commands


async def sync_chat_commands(
    bot: Bot, chat_id: int, enabled_modules: set[str] | None = None
) -> None:
    enabled = (
        await get_enabled_modules(chat_id)
        if enabled_modules is None
        else set(enabled_modules)
    )
    bot_commands = build_bot_commands_for_modules(enabled)
    try:
        await bot.set_my_commands(
            bot_commands,
            scope=BotCommandScopeChat(chat_id=chat_id),
        )
    except Exception as e:
        logger.warning(
            "Failed to sync Telegram commands for chat %s: %s", chat_id, e
        )


def build_modules_keyboard(enabled_modules: set[str]) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for module_id in MODULE_ORDER:
        spec = MODULES[module_id]
        icon = "✅" if module_id in enabled_modules else "❌"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"{icon} {spec.title}",
                    callback_data=f"mod:toggle:{spec.id}",
                )
            ]
        )

    rows.append(
        [
            InlineKeyboardButton(text="✅ Увімкнути все", callback_data="mod:all:on"),
            InlineKeyboardButton(text="❌ Вимкнути все", callback_data="mod:all:off"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_start_text(enabled_modules: set[str], is_group: bool) -> str:
    active_count = sum(1 for mid in MODULE_ORDER if mid in enabled_modules)
    lines = [
        "🤖 Налаштування модулів бота",
        "",
        f"Активно модулів: {active_count} із {len(MODULE_ORDER)}",
        "",
    ]
    for module_id in MODULE_ORDER:
        spec = MODULES[module_id]
        icon = "✅" if module_id in enabled_modules else "❌"
        lines.append(f"{icon} {spec.title} — {spec.description}")

    lines.append("")
    if is_group:
        lines.append("⚙️ Керувати модулями можуть лише адміністратори чату.")
    else:
        lines.append("⚙️ Натисніть на кнопку нижче, щоб увімкнути або вимкнути модуль.")
    lines.append("📖 Список активних команд: /help")
    return "\n".join(lines)


def build_help_text(enabled_modules: set[str]) -> str:
    lines = [
        "📖 Довідка по активних модулях",
        "",
        "📌 Базові команди:",
    ]
    for cmd, desc in CORE_COMMANDS:
        lines.append(f"• /{cmd} — {desc}")

    active_specs = [MODULES[mid] for mid in MODULE_ORDER if mid in enabled_modules]
    if active_specs:
        lines.append("")
        lines.append("🧩 Активні модулі:")
        for spec in active_specs:
            lines.append(f"\n• {spec.title} — {spec.description}")
            for cmd, desc in spec.commands:
                lines.append(f"  /{cmd} — {desc}")
    else:
        lines.append("")
        lines.append(
            "ℹ️ Наразі жоден додатковий модуль не увімкнено. "
            "Використайте /start, щоб обрати потрібні функції."
        )

    lines.append("")
    lines.append("⚙️ Змінити налаштування модулів: /start")
    return "\n".join(lines)
