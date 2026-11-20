# Architecture & Design

The bot follows a layered, modular asynchronous architecture powered by **aiogram 3**, **SQLAlchemy (Async)**, and Google's **GenAI SDK**.

---

## High-Level Architecture Diagram

```mermaid
graph TD
    User([Telegram User]) -->|Message / Command / Ping| Router[Aiogram Router]
    
    subgraph Handlers
        Router --> CmdHandler[commands.py]
        Router --> GroupHandler[group_events.py]
    end
    
    subgraph Storage & Context
        GroupHandler -->|Ingest Message, Quote, Reply| SQLite[(SQLite Database)]
        CmdHandler -->|Build Context Window| CtxBuilder[context_builder.py]
        SQLite -->|Hydrate Orphan Parents & Digests| CtxBuilder
    end
    
    subgraph AI Engine & Anti-Hallucination
        CtxBuilder -->|XML Transcript + Ground Truth| LLMClient[gemini_client.py]
        LLMClient -->|Gemini 3 / Gemma 4 Call| GeminiAPI[Google GenAI Cloud]
        GeminiAPI -->|Structured JSON Summary| Verifier[summary_verifier.py]
        Verifier -->|Prune Hallucinated Speakers & IDs| OutputFormatter[Clean Output]
    end
    
    OutputFormatter -->|Formatted Response| User
```

---

## Core Components

### 1. Ingestion Engine (`app/handlers/group_events.py`)
- Filters service messages (join/left events, pin notifications).
- Extracts rich reply metadata:
  - `reply_to_message_id`, `reply_to_author`, `reply_to_snippet`
  - Quoted text fragments (Telegram 2.0 quotes)
  - Topic/Thread IDs (`message_thread_id`)
  - Media placeholders (`[photo]`, `[voice]`, `[document: spec.pdf]`)
  - Forwarded message origins (`[forwarded: channel: TechNews]`)
- Tracks message edits (`edited_message`) by updating text and `edited_at` without resetting `is_processed`.

### 2. Context Builder (`app/services/context_builder.py`)
- **Raw-First Strategy**: If all messages within the requested time slice fit within `summary_raw_message_limit` and `summary_raw_char_limit`, the builder operates in **Pure Raw Mode**, avoiding lossy intermediate digests.
- **Orphan Hydration**: If a message in the window replies to an earlier message outside the window, the parent message is batch-fetched from the database and placed into a dedicated `[PRE-PERIOD CONTEXT]` block.
- **Namesake Disambiguation**: When different participants share the same display name, usernames (`@username`) or user IDs (`#12345`) are automatically appended.

### 3. Anti-Hallucination Verifier (`app/services/summary_verifier.py`)
- Employs a closed-world assumption:
  - `evidence_msg_ids`: Every topic and decision must cite concrete integer message IDs present in the transcript.
  - `valid_speakers`: Participants attributed to statements or decisions must exist in the chat window.
  - Fabricated claims, hallucinated participants, or phantom action items are purged before rendering.

### 4. Background Scheduler
- Runs continuously via an `asyncio.Task` inside the main process without heavy external dependencies like APScheduler.
- Aggregates unprocessed messages into hourly digests (`ChatDigest`) in batches of 200.
- Keeps SQLite connections open only during fast fetch and commit operations, leaving the database unlocked during LLM network calls.
- Prunes expired records based on `RETENTION_DAYS`.
