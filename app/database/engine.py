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

_CHAT_SETTINGS_NEW_COLUMNS: list[tuple[str, str]] = [
    ("enabled_modules", "TEXT NOT NULL DEFAULT '[]'"),
    ("daily_digest_time", "VARCHAR(5)"),
    ("daily_digest_last_sent", "VARCHAR(10)"),
    ("updated_at", "DATETIME"),
]


def _resolve_database_url(raw_url: str) -> str:
    """
    Ensures that the directory for SQLite exists and is writable
    (both in Docker and local execution from VS Code on Windows or non-chowned Linux mounts).
    """
    if not raw_url.startswith("sqlite"):
        return raw_url

    # If Windows .env contains Docker path //app/data/..., automatically normalize to relative ./data/...
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
                "WARNING: Failed to create database directory %s (%s)! "
                "Falling back to temporary directory %s - data may be lost after reboot!",
                parent_dir,
                e,
                fallback_file,
            )
            return f"sqlite+aiosqlite:///{fallback_file}"

        # Check write permissions (e.g. if Linux user forgot 'sudo chown 10001:10001 ./data')
        if not os.access(parent_dir, os.W_OK):
            fallback_dir = Path(tempfile.gettempdir()) / "hybrid_bot_data"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            fallback_file = (fallback_dir / db_path.name).as_posix()
            logger.warning(
                "WARNING: Directory %s is not writable (run 'sudo chown -R 10001:10001 data'). "
                "Temporarily falling back to backup DB in tempdir: %s - data might not persist across restarts!",
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

# Safe PRAGMA setup for SQLite (avoid crashing if filesystem bind-mount does not support WAL)
if "sqlite" in RESOLVED_DB_URL:
    @event.listens_for(engine.sync_engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL;")
            cursor.execute("PRAGMA synchronous=NORMAL;")
        except Exception as e:
            logger.warning("WAL mode not supported by filesystem, falling back to standard journal: %s", e)
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
    Idempotent SQLite schema migration for existing databases:
    adds new columns to chat_logs and chat_digests, drops NOT NULL from chat_logs.user_id if needed,
    and creates composite indexes idx_chat_reply_msg and idx_chat_thread.
    """
    if "sqlite" not in RESOLVED_DB_URL:
        return

    log_info = sync_conn.execute(text("PRAGMA table_info(chat_logs)")).fetchall()
    if log_info:
        existing_log_cols = {row[1] for row in log_info}
        for col_name, col_type in _CHAT_LOGS_NEW_COLUMNS:
            if col_name not in existing_log_cols:
                logger.info("DB Migration: adding column chat_logs.%s (%s)", col_name, col_type)
                sync_conn.execute(
                    text(f"ALTER TABLE chat_logs ADD COLUMN {col_name} {col_type}")
                )

        # If legacy chat_logs.user_id has NOT NULL (row[3] == 1),
        # rebuild table so anonymous admins and channel posts (user_id=NULL) do not trigger errors
        user_id_notnull = any(row[1] == "user_id" and row[3] == 1 for row in log_info)
        if user_id_notnull:
            logger.info("DB Migration: updating chat_logs.user_id to nullable=True")
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
                logger.info("DB Migration: adding column chat_digests.%s (%s)", col_name, col_type)
                sync_conn.execute(
                    text(f"ALTER TABLE chat_digests ADD COLUMN {col_name} {col_type}")
                )

    settings_info = sync_conn.execute(text("PRAGMA table_info(chat_settings)")).fetchall()
    if not settings_info:
        logger.info("DB Migration: creating table chat_settings")
        sync_conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS chat_settings ("
                "chat_id BIGINT NOT NULL PRIMARY KEY, "
                "enabled_modules TEXT NOT NULL, "
                "daily_digest_time VARCHAR(5), "
                "daily_digest_last_sent VARCHAR(10), "
                "updated_at DATETIME"
                ")"
            )
        )
    else:
        existing_settings_cols = {row[1] for row in settings_info}
        for col_name, col_type in _CHAT_SETTINGS_NEW_COLUMNS:
            if col_name not in existing_settings_cols:
                logger.info(
                    "DB Migration: adding column chat_settings.%s (%s)",
                    col_name,
                    col_type,
                )
                sync_conn.execute(
                    text(f"ALTER TABLE chat_settings ADD COLUMN {col_name} {col_type}")
                )


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_migrate_sqlite_schema)
