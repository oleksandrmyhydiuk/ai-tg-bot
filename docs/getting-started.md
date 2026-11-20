# Getting Started

This guide walks you through setting up and running the AI Telegram Bot locally or in a containerized environment.

---

## Prerequisites

- **Python 3.11** or **Python 3.12**
- A **Telegram Bot Token** from [@BotFather](https://t.me/BotFather)
- A **Google Gemini API Key** from [Google AI Studio](https://aistudio.google.com/)
- (Optional) **Docker** and **Docker Compose**

---

## 1. Clone the Repository

```bash
git clone https://github.com/oleksandrmyhydiuk/ai-tg-bot.git
cd ai-tg-bot
```

---

## 2. Environment Configuration

Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env`:

```ini
BOT_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
GEMINI_API_KEY=AIzaSyYourGeminiApiKeyHere

# Model configuration
GEMINI_ASK_MODEL=gemini-3.8-flash
GEMINI_SUMMARY_MODEL=gemma-4-26b-a4b-it
GEMINI_ENABLE_SEARCH=true

# Database storage path
DATABASE_URL=sqlite+aiosqlite:///./data/bot_database.db

# Processing thresholds
DIGEST_INTERVAL_MINUTES=60
RETENTION_DAYS=7
SUMMARY_RAW_MESSAGE_LIMIT=400
SUMMARY_RAW_CHAR_LIMIT=60000
SUMMARY_SHOW_CITATIONS=true
SUMMARY_ENABLE_SELF_CHECK=false
```

---

## 3. Local Installation & Run

Create a virtual environment and install dependencies:

```bash
python -m venv venv

# On Linux/macOS:
source venv/bin/activate

# On Windows (PowerShell):
.\venv\Scripts\Activate.ps1

# Install runtime dependencies
pip install -r requirements.txt
```

Launch the bot:

```bash
python -m app.main
```

---

## 4. Telegram Privacy Settings

For the bot to read messages in group chats and build hourly digests:

1. Open [@BotFather](https://t.me/BotFather).
2. Send `/setprivacy`.
3. Select your bot.
4. Choose **Disable**.
5. Add the bot to your group and grant it Administrator privileges (recommended for seamless operation).
