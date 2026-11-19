import logging
import os
from pathlib import Path
import tempfile
from sqlalchemy import Connection, event, text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.config import settings
from app.database.models import Base

logger = logging.getLogger(__name__)

_CHAT_LOGS_NEW_COLUMNS: list[tuple[str, str]] = [
    ("reply_to_message_id", "BIGINT"),
    ("reply_to_user_id", "BIGINT"),
    ("reply_to_author", "VARCHAR(128)"),
    ("reply_to_snippet", "VARCHAR(240)"),
    ("quote_text", "VARCHAR(240)"),
    ("message_thread_id", "BIGINT"),
    ("media_type", "VARCHAR(32)"),
    ("forward_origin", "VARCHAR(160)"),
    ("sender_name", "VARCHAR(128)"),
    ("is_bot_message", "BOOLEAN DEFAULT 0"),
    ("edited_at", "DATETIME"),
]

_CHAT_DIGESTS_NEW_COLUMNS: list[tuple[str, str]] = [
    ("period_start", "DATETIME"),
    ("period_end", "DATETIME"),
    ("message_count", "INTEGER"),
    ("structured_json", "TEXT"),
]


def _resolve_database_url(raw_url: str) -> str:
    """
    Гарантує існування та доступність на запис директорії для SQLite
    (як у Docker, так і при локальному запуску з VS Code на Windows або без chown на Linux).
    """
    if not raw_url.startswith("sqlite"):
        return raw_url

    # Якщо на Windows у .env залишився Docker-шлях //app/data/..., автоматично замінюємо на відносний ./data/...
    if os.name == "nt" and "/app/data/" in raw_url:
        raw_url = "sqlite+aiosqlite:///./data/bot_database.db"

    parts = raw_url.split(":///", 1)
    if len(parts) == 2:
        raw_path_str = parts[1]
        if os.name == "nt" and raw_path_str.startswith("/") and not raw_path_str.startswith("./"):
            raw_path_str = raw_path_str.lstrip("/")
        db_path = Path(raw_path_str).resolve()
        parent_dir = db_path.parent

        try:
            parent_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            fallback_dir = Path(tempfile.gettempdir()) / "hybrid_bot_data"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            fallback_file = (fallback_dir / db_path.name).as_posix()
            logger.warning(
                "🚨 УВАГА: Не вдалося створити директорію БД %s (%s)! "
                "Використовується тимчасова директорія %s — дані можуть бути втрачені після перезавантаження!",
                parent_dir,
                e,
                fallback_file,
            )
            return f"sqlite+aiosqlite:///{fallback_file}"

        # Перевіряємо права на запис (наприклад, якщо на Linux забули зробити chown 10001:10001 ./data)
        if not os.access(parent_dir, os.W_OK):
            fallback_dir = Path(tempfile.gettempdir()) / "hybrid_bot_data"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            fallback_file = (fallback_dir / db_path.name).as_posix()
            logger.warning(
                "🚨 УВАГА: Немає прав на запис у %s (виконайте 'sudo chown -R 10001:10001 data'). "
                "Тимчасово використовується резервна БД у tempdir: %s — дані можуть не зберігатися між перезапусками!",
                parent_dir,
                fallback_file,
            )
            return f"sqlite+aiosqlite:///{fallback_file}"

    return raw_url


RESOLVED_DB_URL = _resolve_database_url(settings.database_url)

engine = create_async_engine(
    RESOLVED_DB_URL,
    echo=False,
    connect_args={"timeout": 30} if "sqlite" in RESOLVED_DB_URL else {},
)

# Безпечне налаштування PRAGMA для SQLite (якщо файлова система bind-mount не підтримує WAL, не падаємо)
if "sqlite" in RESOLVED_DB_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
        except Exception as e:
            logger.warning("WAL-режим не підтримується файловою системою, використовується стандартний журнал: %s", e)
        try:
            cursor.execute("PRAGMA busy_timeout=15000;")
            cursor.execute("PRAGMA foreign_keys=ON;")
        except Exception:
            pass
        finally:
            cursor.close()

async_session_factory = async_sessionmaker(
    engine, expire_on_commit=False, class_=AsyncSession
)


def _migrate_sqlite_schema(sync_conn: Connection) -> None:
    """
    Ідемпотентна міграція SQLite-схеми для існуючих баз даних:
    додає нові колонки у chat_logs та chat_digests, знімає NOT NULL з chat_logs.user_id за потреби
    та створює композитні індекси idx_chat_reply_msg і idx_chat_thread.
    """
    if "sqlite" not in RESOLVED_DB_URL:
        return

    log_info = sync_conn.execute(text("PRAGMA table_info(chat_logs)")).fetchall()
    if log_info:
        existing_log_cols = {row[1] for row in log_info}
        for col_name, col_type in _CHAT_LOGS_NEW_COLUMNS:
            if col_name not in existing_log_cols:
                logger.info("Міграція БД: додаю колонку chat_logs.%s (%s)", col_name, col_type)
                sync_conn.execute(
                    text(f"ALTER TABLE chat_logs ADD COLUMN {col_name} {col_type}")
                )

        # Якщо у старій таблиці chat_logs колонка user_id мала NOT NULL (row[3] == 1),
        # перебудовуємо таблицю, щоб анонімні адміни та пости каналів (user_id=NULL) не викликали помилку
        user_id_notnull = any(row[1] == "user_id" and row[3] == 1 for row in log_info)
        if user_id_notnull:
            logger.info("Міграція БД: оновлюю chat_logs.user_id до nullable=True")
            all_cols = (
                "chat_id, message_id, user_id, text, timestamp, is_processed, "
                "reply_to_message_id, reply_to_user_id, reply_to_author, reply_to_snippet, "
                "quote_text, message_thread_id, media_type, forward_origin, sender_name, "
                "is_bot_message, edited_at"
            )
            sync_conn.execute(
                text(
                    "CREATE TABLE chat_logs_new ("
                    "chat_id BIGINT NOT NULL, "
                    "message_id BIGINT NOT NULL, "
                    "user_id BIGINT REFERENCES users(user_id), "
                    "text TEXT NOT NULL, "
                    "timestamp DATETIME NOT NULL, "
                    "is_processed BOOLEAN NOT NULL DEFAULT 0, "
                    "reply_to_message_id BIGINT, "
                    "reply_to_user_id BIGINT, "
                    "reply_to_author VARCHAR(128), "
                    "reply_to_snippet VARCHAR(240), "
                    "quote_text VARCHAR(240), "
                    "message_thread_id BIGINT, "
                    "media_type VARCHAR(32), "
                    "forward_origin VARCHAR(160), "
                    "sender_name VARCHAR(128), "
                    "is_bot_message BOOLEAN NOT NULL DEFAULT 0, "
                    "edited_at DATETIME, "
                    "PRIMARY KEY (chat_id, message_id)"
                    ")"
                )
            )
            # Static schema migration column list, no user input
            sync_conn.execute(
                text(
                    f"INSERT INTO chat_logs_new ({all_cols}) "  # nosec B608
                    "SELECT chat_id, message_id, user_id, text, timestamp, is_processed, "
                    "reply_to_message_id, reply_to_user_id, reply_to_author, reply_to_snippet, "
                    "quote_text, message_thread_id, media_type, forward_origin, sender_name, "
                    "COALESCE(is_bot_message, 0), edited_at FROM chat_logs"
                )
            )
            sync_conn.execute(text("DROP TABLE chat_logs"))
            sync_conn.execute(text("ALTER TABLE chat_logs_new RENAME TO chat_logs"))
            sync_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_chat_logs_user_id "
                    "ON chat_logs (user_id)"
                )
            )
            sync_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_chat_logs_chat_timestamp "
                    "ON chat_logs (chat_id, timestamp)"
                )
            )
            sync_conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_chat_logs_unprocessed "
                    "ON chat_logs (chat_id, is_processed, timestamp)"
                )
            )

        sync_conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_chat_reply_msg "
                "ON chat_logs (chat_id, reply_to_message_id)"
            )
        )
        sync_conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS idx_chat_thread "
                "ON chat_logs (chat_id, message_thread_id)"
            )
        )

    digest_info = sync_conn.execute(text("PRAGMA table_info(chat_digests)")).fetchall()
    if digest_info:
        existing_digest_cols = {row[1] for row in digest_info}
        for col_name, col_type in _CHAT_DIGESTS_NEW_COLUMNS:
            if col_name not in existing_digest_cols:
                logger.info("Міграція БД: додаю колонку chat_digests.%s (%s)", col_name, col_type)
                sync_conn.execute(
                    text(f"ALTER TABLE chat_digests ADD COLUMN {col_name} {col_type}")
                )


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_sqlite_schema)
