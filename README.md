# AI Telegram Group & DM Assistant (Gemini API)

[![CI](https://github.com/oleksandrmyhydiuk/ai-tg-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/oleksandrmyhydiuk/ai-tg-bot/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Security Scanned](https://img.shields.io/badge/Security-Bandit%20%7C%20pip--audit-green)](https://github.com/PyCQA/bandit)

A lightweight, asynchronous Telegram bot for group chats and direct messages powered exclusively by the **Google Gemini API (Free Tier)**. It provides time-windowed chat summaries with anti-hallucination verification, web-grounded Q&A via Google Search, image analysis (Vision), and automated hourly digest caching backed by SQLite.

---

## Features

- **Modular Per-Chat Configuration (`/start`, `/modules`, `/settings`)**:
  - Interactive inline keyboard (admin-only in groups) lets each chat toggle individual feature modules on or off.
  - Dynamically syncs per-chat Telegram command menus via `BotCommandScopeChat` and skips passive message logging when no logging-dependent module is active.
- **Time-Windowed Summaries & Action Items (`/summary [hours]`, `/tasks`)**:
  - Summarizes discussions over any lookback window (defaults to `6` hours, up to `168` hours / 7 days) by combining cached hourly digests (`ChatDigest`) with fresh messages (`ChatLog`).
  - `/tasks` extracts confirmed decisions, proposed action items, and open questions with message citations.
  - Supports `/summary 4`, `@bot summary 4`, and Ukrainian triggers (`суммарі 4`).
- **Anti-Hallucination & Grounded Context**:
  - Hydrates orphan reply chains and disambiguates namesake speakers.
  - Enforces structured JSON extraction, message ID verification, and clickable citation links (`SUMMARY_SHOW_CITATIONS`).
- **Web-Grounded Q&A, Multi-Turn Replies & Vision (`/ask`, `@bot`, DMs)**:
  - Uses newer Gemini models (`gemini-3.8-flash` with automatic fallback chain) and **Google Search Grounding** (with source URLs).
  - Supports multi-turn reply-chain context (`@bot` in reply to a message or photo) and direct questions in private messages.
  - Analyzes attached or replied-to images up to 10 MB.
- **Video Downloader (`/dl` & Auto-Download)**:
  - Downloads TikTok, Instagram, and YouTube videos automatically when links are posted or via `/dl <url>` using `yt-dlp` and `ffmpeg` (with SSRF validation, 50 MB Telegram cap, and per-user cooldown).
- **Chat Search & Activity Analytics (`/find`, `/stats`)**:
  - `/find <query>` searches stored chat history with clickable message links.
  - `/stats [hours|24h|7d|all]` reports message volume, top active participants, peak activity hour (Europe/Kyiv), and most-replied messages.
- **Voice Transcription & Scheduled Daily Digests (`/transcribe`, `/digest`)** *(opt-in per chat)*:
  - `/transcribe` transcribes replied voice messages, audio files, or video notes (up to 20 MB) via Gemini.
  - `/digest on [HH:MM]` schedules automated daily summaries at a configured Europe/Kyiv time.
- **Production-Ready & Lightweight**:
  - Single-container Docker setup (~512 MB RAM limit) with `ffmpeg`, non-root `botuser` (UID `10001`), SQLite WAL mode with idempotent schema migrations, and per-user rate limiting.

---

## Project Structure

```text
.
├── app/
│   ├── config.py                  # Pydantic settings with .env fallback
│   ├── main.py                    # Bot entry point, modular router setup & background loops
│   ├── database/
│   │   ├── engine.py              # Async SQLite engine, WAL pragmas & schema migrations
│   │   └── models.py              # SQLAlchemy ORM models (User, ChatLog, ChatDigest, ChatSettings)
│   ├── handlers/
│   │   ├── commands.py            # Shared AI ask/summary helpers & legacy compatibility exports
│   │   └── group_events.py        # Asynchronous message & edit logger (respects per-chat module state)
│   ├── modules/
│   │   ├── registry.py            # Module registry, in-memory cache, BotCommandScopeChat & inline UI
│   │   ├── core.py                # /start, /modules, /settings, /help, /clear & admin callbacks
│   │   ├── summary.py             # /summary, /tasks & @bot summary triggers
│   │   ├── ask.py                 # /ask, @bot mentions, multi-turn reply chains & DM handler
│   │   ├── media_dl.py            # /dl & automatic TikTok/Instagram/YouTube video downloads
│   │   ├── search.py              # /find full-text chat history search
│   │   ├── stats.py               # /stats activity analytics & leaderboards
│   │   ├── voice.py               # /transcribe for voice, audio & video notes
│   │   └── daily_digest.py        # /digest daily schedule management
│   └── services/
│       ├── context_builder.py     # Hybrid digest + raw tail context assembly & hydration
│       ├── gemini_client.py       # Gemini API client, fallback chains, audio & digest jobs
│       ├── media_downloader.py    # SSRF-safe yt-dlp video downloader
│       └── summary_verifier.py    # Structured JSON parsing, verification & citation rendering
├── docs/                          # Material for MkDocs source documentation & API reference
├── tests/
│   ├── test_foundation.py         # Core foundation tests (handlers, DB migrations, context, verifier)
│   └── test_modules.py            # Modular features, isolation, SSRF & media downloader tests
├── .github/workflows/ci.yml       # CI pipeline (Ruff, Bandit, pip-audit, pytest, Docker, GH Pages)
├── Dockerfile                     # Multi-stage slim Python 3.11 image with ffmpeg (non-root user)
├── docker-compose.yml             # Single-service Compose configuration
├── mkdocs.yml                     # MkDocs Material configuration
├── pyproject.toml                 # Ruff, Bandit & pytest configuration
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

| Command / Trigger | Module | Description |
| --- | --- | --- |
| `/start`, `/modules`, `/settings` | `core` | Open interactive per-chat module settings (admin-only in groups) |
| `/help` | `core` | Display help tailored to the modules enabled in the current chat |
| `/clear` | `core` | Clear stored chat history and digests (restricted to group admins) |
| `/summary [hours]` | `summary` | Summarize chat activity over the last `N` hours (default `6`, up to `168`) |
| `/tasks [hours]` | `summary` | List confirmed decisions, proposed action items, and open questions |
| `/ask <question>` | `ask` | Ask Gemini with Google Search grounding (or analyze an attached/replied photo) |
| `@bot <question>` / Reply + `@bot` | `ask` / `summary` | Mention the bot to ask in multi-turn reply context or summarize (`@bot суммарі 4`) |
| `/dl <url>` (or bare link) | `media_dl` | Download video from TikTok, Instagram, or YouTube (up to 50 MB) |
| `/find <query>` | `search` | Search stored chat history with clickable message links |
| `/stats [period]` | `stats` | Show activity stats, top users, peak Kyiv hour, and most-replied messages (`24h`, `7d`, `all`, or hours) |
| `/transcribe` | `voice` | Reply to a voice message, audio file, or video note to transcribe it *(disabled by default)* |
| `/digest [on HH:MM \| off \| status]` | `daily_digest` | Configure automated daily chat digests in Europe/Kyiv time *(disabled by default)* |

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

Run the automated test suite:

```bash
pytest tests -v
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
