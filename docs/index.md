# AI Telegram Bot

[![CI](https://github.com/oleksandrmyhydiuk/ai-tg-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/oleksandrmyhydiuk/ai-tg-bot/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue)](https://www.python.org/)
[![Security Scanned](https://img.shields.io/badge/Security-Bandit%20%7C%20pip--audit-green)](https://github.com/PyCQA/bandit)

A production-grade, asynchronous Telegram group and private assistant powered by Google's **Gemini 3** and **Gemma 4** Free-Tier APIs.

Features automated hourly background digests, on-demand time-sliced summaries (`/summary <hours>`), web-grounded Q&A with Google Search, image analysis via Gemini Vision, and a closed-world anti-hallucination verification engine.

---

## Key Highlights

- **Pure Free Tier Cloud Engine**: Relies 100% on free-tier Gemini and Gemma API models with automated fallback cascades (`gemini-3.8-flash`, `gemma-4-31b-it`, `gemma-4-26b-a4b-it`, `gemini-3.6-flash`, `gemini-3.1-flash-lite`, `gemini-3.5-flash-lite`).
- **Closed-World Anti-Hallucination Pipeline**: Summaries validate every topic and decision against real message IDs and speaker names stored in the database. Hallucinated facts, fabricated participants, or phantom promises are pruned automatically.
- **Hourly Background Digest & Periodic Cleanup**: Continuous background task collects and summarizes chat messages every hour without holding SQLite transactions open, keeping subsequent `/summary` queries lightning-fast and quota-friendly.
- **Rich Context Ingestion**: Captures message replies (`↩ #1045 Author: "..."`), Telegram 2.0 quote fragments, topic roots, forwarded channels, and media placeholders.
- **Per-Chat Command Scoping**: Dynamically registers native bot command menus for individual chats and groups.
- **Docker & Security Hardened**: Runs as a non-root user (`botuser`, UID 10001), automated dependency auditing via `pip-audit`, Bandit static security analysis, and Ruff linting.

---

## Quick Navigation

- [Getting Started](getting-started.md): Installation, prerequisites, and initial setup.
- [Configuration](configuration.md): Complete list of environment variables and configuration options.
- [Architecture](architecture.md): Context builder, anti-hallucination engine, and fallback strategies.
- [Deployment](deployment.md): Running with Docker Compose and systemd.
- [CI/CD & Testing](cicd.md): Automated testing pipeline, security scanning, and documentation publishing.
- [API Reference](api/main.md): Auto-generated Python module and API reference.
