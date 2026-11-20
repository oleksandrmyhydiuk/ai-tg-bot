# Configuration Reference

The bot is configured primarily via environment variables defined in a `.env` file or passed directly to the container environment.

---

## Environment Variables

| Variable | Default Value | Description |
|:---------|:--------------|:------------|
| `BOT_TOKEN` | *Required* | Telegram Bot API token obtained from [@BotFather](https://t.me/BotFather). |
| `GEMINI_API_KEY` | *Required* | Google Gemini API key obtained from [Google AI Studio](https://aistudio.google.com/). |
| `GEMINI_ASK_MODEL` | `gemini-3.8-flash` | Primary model for `/ask`, user pings, and web search queries. |
| `GEMINI_SUMMARY_MODEL` | `gemma-4-26b-a4b-it` | Primary model for generating hourly digests and period summaries. |
| `GEMINI_ENABLE_SEARCH` | `true` | Enables Google Search grounding tool for factual and web queries. |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/bot_database.db` | SQLAlchemy async database connection string. |
| `DIGEST_INTERVAL_MINUTES` | `60` | Background task execution interval in minutes for digests and cleanup. |
| `RETENTION_DAYS` | `7` | Retention window in days; records older than this are pruned. |
| `SUMMARY_RAW_MESSAGE_LIMIT` | `400` | Maximum raw messages to process directly in Pure Raw Mode. |
| `SUMMARY_RAW_CHAR_LIMIT` | `60000` | Character ceiling for raw transcript passed to LLM. |
| `SUMMARY_SHOW_CITATIONS` | `true` | Appends clickable links and message citations (`#1045`) to summary items. |
| `SUMMARY_ENABLE_SELF_CHECK` | `false` | Enables optional Pass-2 fact-checking auditor pass. |

---

## Fallback Chains

### Ask / Search Fallback Chain

When handling questions (`/ask` or direct mentions), the bot cascades through the following models if rate limits (429) or transient API errors occur:

1. `gemini-3.8-flash` (Primary)
2. `gemma-4-31b-it`
3. `gemma-4-26b-a4b-it`
4. `gemini-3.6-flash`
5. `gemini-3.1-flash-lite`
6. `gemini-3.5-flash-lite`

### Summary & Digest Fallback Chain

For structured JSON summaries and hourly background digests, models with generous free-tier quotas and structured output support are prioritized:

1. `gemma-4-26b-a4b-it` (Primary)
2. `gemma-4-31b-it`
3. `gemini-3.1-flash-lite`
4. `gemini-3.5-flash-lite`
5. `gemini-3.6-flash`
6. `gemini-3.8-flash`
