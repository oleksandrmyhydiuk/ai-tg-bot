# AI Telegram Bot

[![CI](https://github.com/oleksandrmyhydiuk/ai-tg-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/oleksandrmyhydiuk/ai-tg-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![Security Scanned](https://img.shields.io/badge/Security-Bandit%20%7C%20pip--audit-green)](https://github.com/PyCQA/bandit)

A production-grade, asynchronous Telegram group and private assistant powered by Google's **Gemini 3** and **Gemma 4** Free-Tier APIs.

Features automated hourly background digests, on-demand time-sliced summaries (`/summary <hours>`), web-grounded Q&A with Google Search, image analysis via Gemini Vision, and a closed-world anti-hallucination verification engine.

---

## Key Highlights

- **Modular Per-Chat Architecture (`/start`, `/modules`, `/settings`)**: Interactive inline-keyboard settings (admin-only in groups) to enable or disable any of the 7 modules per chat, with dynamic `BotCommandScopeChat` command syncing and passive logging isolation.
- **Pure Free Tier Cloud Engine**: Relies 100% on free-tier Gemini and Gemma API models with automated fallback cascades (`gemini-3.8-flash`, `gemma-4-31b-it`, `gemma-4-26b-a4b-it`, `gemini-3.6-flash`, `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite`).
- **Closed-World Anti-Hallucination Pipeline**: `/summary` and `/tasks` validate every topic, decision, and open question against real message IDs and speaker names stored in SQLite.
- **Web-Grounded Q&A, Vision & Multi-Turn Replies (`/ask`)**: Answers questions with Google Search grounding (including source URLs), reply-chain history reconstruction, and image analysis up to 10 MB.
- **Media Downloader (`/dl` & Auto-Download)**: SSRF-safe `yt-dlp` + `ffmpeg` downloader for TikTok, Instagram, and YouTube videos up to Telegram's 50 MB limit.
- **Search, Analytics, Voice & Daily Digests**: Full-text history search (`/find`), activity analytics (`/stats`), Gemini audio transcription (`/transcribe`), and scheduled daily digests (`/digest`).
- **Docker & Security Hardened**: Runs as a non-root user (`botuser`, UID 10001) with `ffmpeg`, automated dependency auditing via `pip-audit`, Bandit SAST, and Ruff linting.

---

## Quick Navigation

- [Getting Started](getting-started.md): Installation, prerequisites, and initial setup.
- [Configuration](configuration.md): Complete list of environment variables and configuration options.
- [Architecture](architecture.md): Context builder, anti-hallucination engine, and fallback strategies.
- [Deployment](deployment.md): Running with Docker Compose and systemd.
- [CI/CD & Testing](cicd.md): Automated testing pipeline, security scanning, and documentation publishing.
- [API Reference](api/main.md): Auto-generated Python module and API reference.
