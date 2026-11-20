import os
from pathlib import Path

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class Settings(BaseSettings):
        bot_token: str = ""
        gemini_api_key: str = ""
        gemini_ask_model: str = "gemini-3.8-flash"
        gemini_summary_model: str = "gemma-4-26b-a4b-it"
        gemini_enable_search: bool = True
        database_url: str = "sqlite+aiosqlite:///./data/bot_database.db"
        digest_interval_minutes: int = 60
        retention_days: int = 7
        summary_raw_message_limit: int = 400
        summary_raw_char_limit: int = 60000
        summary_show_citations: bool = True
        summary_enable_self_check: bool = False

        model_config = SettingsConfigDict(
            env_file=".env",
            env_file_encoding="utf-8",
            extra="ignore",
        )

    settings = Settings()

except ImportError:
    from pydantic import BaseModel

    def _load_dotenv_fallback() -> None:
        env_path = Path(__file__).resolve().parent.parent / ".env"
        if not env_path.exists():
            return
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip("'\""))

    _load_dotenv_fallback()

    class Settings(BaseModel):  # type: ignore[no-redef]
        bot_token: str = os.environ.get("BOT_TOKEN", "")
        gemini_api_key: str = os.environ.get("GEMINI_API_KEY", "")
        gemini_ask_model: str = os.environ.get("GEMINI_ASK_MODEL", "gemini-3.8-flash")
        gemini_summary_model: str = os.environ.get("GEMINI_SUMMARY_MODEL", "gemma-4-26b-a4b-it")
        gemini_enable_search: bool = os.environ.get("GEMINI_ENABLE_SEARCH", "true").lower() == "true"
        database_url: str = os.environ.get("DATABASE_URL", "sqlite+aiosqlite:///./data/bot_database.db")
        digest_interval_minutes: int = int(os.environ.get("DIGEST_INTERVAL_MINUTES", "60"))
        retention_days: int = int(os.environ.get("RETENTION_DAYS", "7"))
        summary_raw_message_limit: int = int(os.environ.get("SUMMARY_RAW_MESSAGE_LIMIT", "400"))
        summary_raw_char_limit: int = int(os.environ.get("SUMMARY_RAW_CHAR_LIMIT", "60000"))
        summary_show_citations: bool = os.environ.get("SUMMARY_SHOW_CITATIONS", "true").lower() == "true"
        summary_enable_self_check: bool = os.environ.get("SUMMARY_ENABLE_SELF_CHECK", "false").lower() == "true"

    settings = Settings()
