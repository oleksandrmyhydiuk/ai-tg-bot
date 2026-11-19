# AI Telegram Group & DM Assistant (Gemini API)

[![CI](https://github.com/oleksandrmyhydiuk/ai-tg-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/oleksandrmyhydiuk/ai-tg-bot/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

A lightweight, asynchronous Telegram bot for group chats and direct messages powered exclusively by the **Google Gemini API (Free Tier)**. It provides time-windowed chat summaries with anti-hallucination verification, web-grounded Q&A via Google Search, image analysis (Vision), and automated hourly digest caching backed by SQLite.

---

## Features

- **Time-Windowed Chat Summaries (`/summary [hours]`)**:
  - Summarizes discussions over any lookback window (defaults to `6` hours, up to `168` hours / 7 days).
  - Combines pre-computed hourly digests (`ChatDigest`) with fresh unprocessed messages (`ChatLog`) to minimize token usage and latency.
  - Supports `/summary 4`, `@bot summary 4`, and Ukrainian triggers (`суммарі 4`).
- **Anti-Hallucination & Grounded Context**:
  - Hydrates reply chains and speaker attribution so context is preserved accurately.
  - Enforces structured JSON extraction and optional message citation links (`SUMMARY_SHOW_CITATIONS`).
- **Web-Grounded Q&A & Vision (`/ask`, `@bot`, DMs)**:
  - Uses newer Gemini models (`gemini-3.8-flash` with automatic fallback chain) and **Google Search Grounding** for up-to-date answers.
  - Supports **reply-to-message** context (`@bot` in reply to a text or photo) and direct questions in private messages without slash commands.
  - Analyzes attached or replied-to images up to 10 MB.
- **Automated Background Maintenance**:
  - Built-in `asyncio` loop generates hourly digests and prunes records older than `RETENTION_DAYS` (default `7` days).
- **Production-Ready & Lightweight**:
  - Single-container Docker setup (~512 MB RAM limit), non-root `botuser` (UID `10001`), SQLite WAL mode with automatic legacy schema migration, and per-user rate limiting.

---

## Project Structure

```text
.
├── app/
│   ├── config.py                  # Pydantic settings with .env fallback
│   ├── main.py                    # Bot entry point, router setup & hourly asyncio loop
│   ├── database/
│   │   ├── engine.py              # Async SQLite engine, WAL pragmas & schema migrations
│   │   └── models.py              # SQLAlchemy ORM models (User, ChatLog, ChatDigest)
│   ├── handlers/
│   │   ├── commands.py            # /start, /help, /summary, /ask, /stats, /clear, @mentions, DMs
│   │   └── group_events.py        # Asynchronous message & edit logger
│   └── services/
│       ├── context_builder.py     # Hybrid digest + raw tail context assembly & hydration
│       ├── gemini_client.py       # Gemini API client, model fallback chain & hourly digest job
│       └── summary_verifier.py    # Structured JSON parsing, verification & citation rendering
├── tests/
│   └── test_foundation.py         # Unit test suite (handlers, DB migrations, context, verifier)
├── .github/workflows/ci.yml       # CI pipeline (Ruff, Bandit, pip-audit, unittest, Docker build)
├── Dockerfile                     # Multi-stage slim Python 3.11 image (non-root user)
├── docker-compose.yml             # Single-service Compose configuration
├── pyproject.toml                 # Ruff & Bandit configuration
├── requirements.txt               # Pinned runtime dependencies
├── SECURITY.md                    # Security policy & vulnerability reporting
└── LICENSE                        # MIT License
```

---

## Quick Start

### Prerequisites

- A Telegram Bot Token from [@BotFather](https://t.me/BotFather)
- A Google AI Studio API Key from [Google AI Studio](https://aistudio.google.com/apikey)
- **Important for group chats**: Grant the bot **Administrator** rights in your group OR disable Privacy Mode in `@BotFather` (`/setprivacy` -> `Disable`) so it can log regular messages for `/summary`.

### 1. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env` and set your `BOT_TOKEN` and `GEMINI_API_KEY`.

### 2. Run with Docker Compose (Recommended)

```bash
mkdir -p data
sudo chown -R 10001:10001 data   # Linux/ARM64 servers (matches container UID 10001)
docker compose up -d --build
```

Check logs:

```bash
docker compose logs -f bot
```

### 3. Run Locally (Python 3.11+)

```bash
python -m venv venv
# Windows: venv\Scripts\activate
# Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

---

## Bot Commands & Usage

| Command / Trigger | Description |
| --- | --- |
| `/start`, `/help` | Display the full command reference and usage guide |
| `/summary` | Summarize chat activity over the last 6 hours |
| `/summary <hours>` | Summarize chat activity over the last `N` hours (e.g. `/summary 4`, `/summary 24`) |
| `/ask <question>` | Ask Gemini with Google Search grounding (or analyze an attached/replied photo) |
| `@bot <question>` | Mention the bot in a group to ask a question or summarize (`@bot суммарі 4`) |
| Reply + `@bot` | Reply to any message or photo with `@bot` to explain or analyze it |
| `/stats` | Show 24h/total message counts, cached digests, and top-5 active users |
| `/clear` | Clear stored chat history and digests (restricted to group admins) |

---

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `BOT_TOKEN` | *Required* | Telegram bot token from `@BotFather` |
| `GEMINI_API_KEY` | *Required* | Google AI Studio API key |
| `GEMINI_ASK_MODEL` | `gemini-3.8-flash` | Primary model for `/ask`, `@bot` mentions, and Vision |
| `GEMINI_SUMMARY_MODEL` | `gemma-4-26b-a4b-it` | Primary model for `/summary` and hourly digests |
| `GEMINI_ENABLE_SEARCH` | `true` | Enable Google Search grounding for text queries |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/bot_database.db` | Async SQLAlchemy database URL |
| `DIGEST_INTERVAL_MINUTES` | `60` | Interval for background hourly digest & cleanup loop |
| `RETENTION_DAYS` | `7` | Number of days to retain logs and digests in SQLite |
| `SUMMARY_RAW_MESSAGE_LIMIT` | `400` | Max raw messages included in a summary context |
| `SUMMARY_RAW_CHAR_LIMIT` | `60000` | Max character budget for summary context |
| `SUMMARY_SHOW_CITATIONS` | `true` | Include message citation links in rendered summaries |
| `SUMMARY_ENABLE_SELF_CHECK` | `false` | Enable secondary verification pass on summaries |
| `LOG_LEVEL` | `INFO` | Application logging level |

---

## Testing & Code Quality

Run the unit test suite:

```bash
python -m unittest discover -s tests -v
```

Run linting and security checks locally:

```bash
pip install ruff bandit pip-audit
ruff check .
bandit -r app -c pyproject.toml
pip-audit -r requirements.txt
```

---

## Security

Please review [SECURITY.md](SECURITY.md) for our security practices and instructions on how to report vulnerabilities responsibly.

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.
