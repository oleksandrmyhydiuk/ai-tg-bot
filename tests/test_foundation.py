from __future__ import annotations

import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import tempfile
import time
import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_api_key")

from sqlalchemy import create_engine, select, text

from app.config import settings
from app.database.engine import _migrate_sqlite_schema
from app.database.models import Base, ChatDigest, ChatLog, User, utc_now
from app.handlers import commands, group_events
from app.services import gemini_client
from app.services.context_builder import (
    build_summary_context,
)
from app.services.gemini_client import (
    ASK_FALLBACK_MODELS,
    SUMMARY_FALLBACK_MODELS,
    UNAVAILABLE_MODEL_TTL_SECONDS,
    GeminiService,
    process_hourly_digest_for_chat,
    strip_markdown,
)
from app.services.summary_verifier import (
    ChatSummarySchema,
    TopicItem,
    format_message_link,
    parse_summary_json,
    render_summary_text,
    verify_and_prune_summary,
)


class _AsyncCallableMock:
    def __init__(self, return_value=None) -> None:
        self.return_value = return_value
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.return_value

    def assert_awaited_once(self) -> None:
        assert len(self.calls) == 1, f"Expected 1 call, got {len(self.calls)}"

    def assert_awaited_once_with(self, *args, **kwargs) -> None:
        assert len(self.calls) == 1, f"Expected 1 call, got {len(self.calls)}"
        assert self.calls[0] == (args, kwargs), f"Expected {(args, kwargs)}, got {self.calls[0]}"

    def assert_not_awaited(self) -> None:
        assert len(self.calls) == 0, f"Expected 0 calls, got {len(self.calls)}"

    @property
    def call_args(self):
        return self.calls[-1] if self.calls else None


def run_coro(coro):
    try:
        while True:
            try:
                coro.send(None)
            except StopIteration as e:
                return e.value
    finally:
        coro.close()


class _SyncSessionAdapter:
    """
    Synchronous SQLAlchemy session adapter matching the AsyncSession interface (`async with`, `await session.execute(...)`, `await session.commit()`),
    avoiding background thread locking in SQLite during unit tests.
    """

    def __init__(self, sync_session_factory) -> None:
        self._factory = sync_session_factory
        self._session = None

    async def __aenter__(self) -> _SyncSessionAdapter:
        self._session = self._factory()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            self._session.close()

    def add(self, instance) -> None:
        self._session.add(instance)

    def add_all(self, instances) -> None:
        self._session.add_all(instances)

    async def execute(self, statement, params=None):
        if params is not None:
            return self._session.execute(statement, params)
        return self._session.execute(statement)

    async def commit(self) -> None:
        self._session.commit()


def _make_test_session_factory():
    from sqlalchemy.orm import sessionmaker

    sync_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(sync_engine)
    sync_maker = sessionmaker(bind=sync_engine, expire_on_commit=False)

    def _factory() -> _SyncSessionAdapter:
        return _SyncSessionAdapter(sync_maker)

    return sync_engine, _factory


class TestSqliteSchemaMigration(unittest.TestCase):
    def test_migrate_legacy_sqlite_schema(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "legacy.db")
            sync_engine = create_engine(f"sqlite:///{db_file}")
            try:
                with sync_engine.begin() as conn:
                    conn.execute(
                        text(
                            "CREATE TABLE users ("
                            "user_id BIGINT NOT NULL PRIMARY KEY, "
                            "username VARCHAR(64), "
                            "full_name VARCHAR(128) NOT NULL, "
                            "updated_at DATETIME NOT NULL"
                            ")"
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE TABLE chat_logs ("
                            "chat_id BIGINT NOT NULL, "
                            "message_id BIGINT NOT NULL, "
                            "user_id BIGINT NOT NULL REFERENCES users(user_id), "
                            "text TEXT NOT NULL, "
                            "timestamp DATETIME NOT NULL, "
                            "is_processed BOOLEAN NOT NULL DEFAULT 0, "
                            "PRIMARY KEY (chat_id, message_id)"
                            ")"
                        )
                    )
                    conn.execute(
                        text(
                            "CREATE TABLE chat_digests ("
                            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                            "chat_id BIGINT NOT NULL, "
                            "digest_text TEXT NOT NULL, "
                            "keywords TEXT NOT NULL DEFAULT '[]', "
                            "created_at DATETIME NOT NULL"
                            ")"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO users (user_id, username, full_name, updated_at) "
                            "VALUES (10, 'ivan', 'Іван', '2026-11-19 10:00:00')"
                        )
                    )
                    conn.execute(
                        text(
                            "INSERT INTO chat_logs (chat_id, message_id, user_id, text, timestamp, is_processed) "
                            "VALUES (-100123, 1, 10, 'Legacy text', '2026-11-19 10:01:00', 1)"
                        )
                    )

                    _migrate_sqlite_schema(conn)
                    _migrate_sqlite_schema(conn)

                    log_cols = {
                        row[1]: row
                        for row in conn.execute(text("PRAGMA table_info(chat_logs)")).fetchall()
                    }
                    expected_log_cols = {
                        "reply_to_message_id",
                        "reply_to_user_id",
                        "reply_to_author",
                        "reply_to_snippet",
                        "quote_text",
                        "message_thread_id",
                        "media_type",
                        "forward_origin",
                        "sender_name",
                        "is_bot_message",
                        "edited_at",
                    }
                    self.assertTrue(expected_log_cols.issubset(set(log_cols.keys())))
                    self.assertEqual(log_cols["user_id"][3], 0)

                    conn.execute(
                        text(
                            "INSERT INTO chat_logs (chat_id, message_id, user_id, text, timestamp, is_processed, sender_name) "
                            "VALUES (-100123, 2, NULL, 'Anon admin post', '2026-11-19 10:02:00', 0, 'Анонімний адмін')"
                        )
                    )
                    rows = conn.execute(
                        text("SELECT message_id, user_id, text, is_processed FROM chat_logs ORDER BY message_id")
                    ).fetchall()
                    self.assertEqual(len(rows), 2)
                    self.assertEqual(rows[0][0], 1)
                    self.assertEqual(rows[0][3], 1)
                    self.assertIsNone(rows[1][1])

                    digest_cols = {
                        row[1]
                        for row in conn.execute(text("PRAGMA table_info(chat_digests)")).fetchall()
                    }
                    self.assertTrue(
                        {"period_start", "period_end", "message_count", "structured_json"}.issubset(
                            digest_cols
                        )
                    )
            finally:
                sync_engine.dispose()


class TestRichMessageIngestion(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        self.patcher_group = patch.object(
            group_events, "async_session_factory", self.session_factory
        )
        self.patcher_group.start()

    def tearDown(self) -> None:
        self.patcher_group.stop()
        self.sync_engine.dispose()

    def _make_msg(self, **kwargs) -> SimpleNamespace:
        defaults = {
            "chat": SimpleNamespace(id=-100555, type="supergroup", username=None),
            "message_id": 100,
            "from_user": SimpleNamespace(
                id=42, username="taras", full_name="Тарас", is_bot=False
            ),
            "sender_chat": None,
            "text": "Привіт усім",
            "caption": None,
            "reply_to_message": None,
            "quote": None,
            "message_thread_id": None,
            "is_topic_message": False,
            "photo": None,
            "video": None,
            "voice": None,
            "video_note": None,
            "audio": None,
            "document": None,
            "sticker": None,
            "poll": None,
            "animation": None,
            "location": None,
            "forward_origin": None,
            "forward_from_chat": None,
            "forward_from": None,
            "forward_sender_name": None,
        }
        defaults.update(kwargs)
        return SimpleNamespace(**defaults)

    def test_log_text_reply_quote_and_edit_preserves_is_processed(self) -> None:
        async def _run():
            parent_msg = self._make_msg(
                message_id=99,
                from_user=SimpleNamespace(
                    id=11, username="oksana", full_name="Оксана", is_bot=False
                ),
                text="Коли реліз нової версії?",
            )
            child_msg = self._make_msg(
                message_id=100,
                text="Сьогодні о 18:00!",
                reply_to_message=parent_msg,
                quote=SimpleNamespace(text="Коли реліз"),
                message_thread_id=777,
            )
            await group_events.log_text_message(child_msg)

            async with self.session_factory() as session:
                row = (
                    await session.execute(
                        select(ChatLog).where(
                            ChatLog.chat_id == -100555, ChatLog.message_id == 100
                        )
                    )
                ).scalar_one()
                self.assertEqual(row.reply_to_message_id, 99)
                self.assertEqual(row.reply_to_user_id, 11)
                self.assertEqual(row.reply_to_author, "Оксана")
                self.assertEqual(row.reply_to_snippet, "Коли реліз нової версії?")
                self.assertEqual(row.quote_text, "Коли реліз")
                self.assertEqual(row.message_thread_id, 777)
                self.assertFalse(row.is_processed)
                self.assertIsNone(row.edited_at)

                row.is_processed = True
                await session.commit()

            edited_msg = self._make_msg(
                message_id=100,
                text="Сьогодні о 19:00 (перенесли)!",
                reply_to_message=parent_msg,
                quote=SimpleNamespace(text="Коли реліз"),
                message_thread_id=777,
            )
            await group_events.log_edited_message(edited_msg)

            async with self.session_factory() as session:
                row_after = (
                    await session.execute(
                        select(ChatLog).where(
                            ChatLog.chat_id == -100555, ChatLog.message_id == 100
                        )
                    )
                ).scalar_one()
                self.assertEqual(row_after.text, "Сьогодні о 19:00 (перенесли)!")
                self.assertTrue(row_after.is_processed)
                self.assertIsNotNone(row_after.edited_at)

        run_coro(_run())

    def test_forum_topic_root_pseudo_reply_ignored(self) -> None:
        async def _run():
            forum_root = self._make_msg(
                message_id=50,
                text=None,
                forum_topic_created=SimpleNamespace(name="Backend"),
            )
            topic_msg = self._make_msg(
                message_id=101,
                text="Пишу просто в топік",
                reply_to_message=forum_root,
                message_thread_id=50,
                is_topic_message=True,
            )
            await group_events.log_text_message(topic_msg)

            async with self.session_factory() as session:
                row = (
                    await session.execute(
                        select(ChatLog).where(
                            ChatLog.chat_id == -100555, ChatLog.message_id == 101
                        )
                    )
                ).scalar_one()
                self.assertIsNone(row.reply_to_message_id)
                self.assertIsNone(row.reply_to_author)
                self.assertEqual(row.message_thread_id, 50)

        run_coro(_run())

    def test_anonymous_admin_and_captionless_media_and_bot_message(self) -> None:
        async def _run():
            anon_media_msg = self._make_msg(
                message_id=102,
                from_user=SimpleNamespace(
                    id=1087968824,
                    username="GroupAnonymousBot",
                    full_name="Group",
                    is_bot=True,
                ),
                sender_chat=SimpleNamespace(
                    id=-100555, title="DevOps Чат", username=None, type="supergroup"
                ),
                text=None,
                caption=None,
                voice=SimpleNamespace(file_id="v1"),
            )
            await group_events.log_text_message(anon_media_msg)

            bot_reply_msg = self._make_msg(
                message_id=103,
                from_user=SimpleNamespace(
                    id=99999, username="my_assistant_bot", full_name="AI Bot", is_bot=True
                ),
                text="Ось відповідь бота",
            )
            await group_events.log_bot_message(
                bot_reply_msg, reply_to_message=anon_media_msg
            )

            async with self.session_factory() as session:
                anon_row = (
                    await session.execute(
                        select(ChatLog).where(
                            ChatLog.chat_id == -100555, ChatLog.message_id == 102
                        )
                    )
                ).scalar_one()
                self.assertIsNone(anon_row.user_id)
                self.assertEqual(anon_row.sender_name, "DevOps Чат")
                self.assertEqual(anon_row.media_type, "voice")
                self.assertEqual(anon_row.text, "[голосове повідомлення]")

                bot_row = (
                    await session.execute(
                        select(ChatLog).where(
                            ChatLog.chat_id == -100555, ChatLog.message_id == 103
                        )
                    )
                ).scalar_one()
                self.assertTrue(bot_row.is_bot_message)
                self.assertEqual(bot_row.sender_name, "Бот")
                self.assertEqual(bot_row.reply_to_message_id, 102)
                self.assertEqual(bot_row.reply_to_author, "DevOps Чат")

        run_coro(_run())


class TestContextBuilder(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()

    def tearDown(self) -> None:
        self.sync_engine.dispose()

    def test_pure_raw_mode_orphan_hydration_and_speaker_disambiguation(self) -> None:
        async def _run():
            now = utc_now()
            chat_id = -100777
            async with self.session_factory() as session:
                u1 = User(user_id=1, username="alex_dev", full_name="Олександр", updated_at=now)
                u2 = User(user_id=2, username="alex_qa", full_name="Олександр", updated_at=now)
                session.add_all([u1, u2])

                orphan_log = ChatLog(
                    chat_id=chat_id,
                    message_id=10,
                    user_id=1,
                    text="Хто візьме на себе міграцію бази даних?",
                    timestamp=now - timedelta(hours=3),
                    is_processed=True,
                )
                recent_log = ChatLog(
                    chat_id=chat_id,
                    message_id=25,
                    user_id=2,
                    text="Я вже зробив міграцію і протестував.",
                    timestamp=now - timedelta(minutes=30),
                    is_processed=False,
                    reply_to_message_id=10,
                    reply_to_user_id=1,
                    reply_to_author="Олександр",
                    reply_to_snippet="Хто візьме на себе міграцію бази даних?",
                    quote_text="міграцію бази даних",
                )
                session.add_all([orphan_log, recent_log])
                await session.commit()

                cutoff = now - timedelta(hours=1)
                bundle = await build_summary_context(session, chat_id, cutoff, hours=1.0)

                self.assertEqual(bundle.digest_count, 0)
                self.assertEqual(bundle.message_count, 1)
                self.assertIn(10, bundle.valid_msg_ids)
                self.assertIn(25, bundle.valid_msg_ids)
                self.assertTrue(bundle.transcript_text.startswith("<chat_transcript>"))
                self.assertTrue(bundle.transcript_text.endswith("</chat_transcript>"))
                self.assertIn("[КОНТЕКСТ ДО ПЕРІОДУ", bundle.transcript_text)
                self.assertIn("[ПОВІДОМЛЕННЯ ЗА ПЕРІОД]", bundle.transcript_text)
                self.assertIn("Олександр (@alex_dev)", bundle.transcript_text)
                self.assertIn("Олександр (@alex_qa)", bundle.transcript_text)
                self.assertIn("↩ #10 Олександр (@alex_dev):", bundle.transcript_text)
                self.assertIn('цитата: "міграцію бази даних"', bundle.transcript_text)

        run_coro(_run())

    def test_digest_plus_tail_raw_mode(self) -> None:
        async def _run():
            now = utc_now()
            chat_id = -100888
            async with self.session_factory() as session:
                u1 = User(user_id=5, username="maria", full_name="Марія", updated_at=now)
                session.add(u1)

                digest = ChatDigest(
                    chat_id=chat_id,
                    digest_text="1. Головні теми:\n- Обговорення архітектури (Марія)",
                    created_at=now - timedelta(hours=2),
                    period_start=now - timedelta(hours=3),
                    period_end=now - timedelta(hours=2),
                    message_count=15,
                    structured_json='{"topics":[{"title":"Архітектура","summary":"Тест","participants":["Марія"],"evidence_msg_ids":[501]}],"decisions_and_outcomes":[]}',
                )
                processed_old_log = ChatLog(
                    chat_id=chat_id,
                    message_id=550,
                    user_id=5,
                    text="Старе повідомлення, вже у дайджесті.",
                    timestamp=now - timedelta(hours=2, minutes=30),
                    is_processed=True,
                )
                tail_log = ChatLog(
                    chat_id=chat_id,
                    message_id=600,
                    user_id=5,
                    text="Додала фінальні тести.",
                    timestamp=now - timedelta(minutes=20),
                    is_processed=False,
                )
                session.add_all([digest, processed_old_log, tail_log])
                await session.commit()

                with patch.object(settings, "summary_raw_message_limit", 1):
                    bundle = await build_summary_context(
                        session, chat_id, now - timedelta(hours=4), hours=4.0
                    )

                self.assertEqual(bundle.digest_count, 1)
                self.assertIn("[ПОПЕРЕДНІ ПОГОДИННІ ДАЙДЖЕСТИ ЗА ПЕРІОД]", bundle.transcript_text)
                self.assertIn("[СВІЖІ ПОВІДОМЛЕННЯ ЧАТУ]", bundle.transcript_text)
                self.assertIn(501, bundle.valid_msg_ids)
                self.assertIn(600, bundle.valid_msg_ids)
                self.assertIn("Марія", bundle.valid_speakers)

        run_coro(_run())


class TestSummaryVerifier(unittest.TestCase):
    def test_parse_summary_json_and_string_id_coercion(self) -> None:
        raw_llm_output = """```json
        {
          "is_low_signal": false,
          "topics": [
            {
              "title": "**Деплой на прод**",
              "summary": "Обговорили деплой сервісу",
              "participants": ["Тарас", "Вигаданий Ілон"],
              "evidence_msg_ids": ["#1045", "1046", 9999],
              "thread_id": "#12"
            },
            {
              "title": "Фантомна тема",
              "summary": "Цієї теми не існувало",
              "participants": ["Вигаданий Ілон"],
              "evidence_msg_ids": ["#8888"]
            }
          ],
          "decisions_and_outcomes": [
            {
              "text": "Деплоїмо о 20:00",
              "status": "confirmed",
              "participants": ["Тарас"],
              "evidence_msg_ids": ["#1046"]
            },
            {
              "text": "Повністю вигадане рішення",
              "status": "proposed",
              "participants": ["Вигаданий Ілон"],
              "evidence_msg_ids": [7777]
            }
          ],
          "unverified_or_unclear_context": []
        }
        ```"""
        parsed = parse_summary_json(raw_llm_output)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.topics[0].evidence_msg_ids, [1045, 1046, 9999])
        self.assertEqual(parsed.topics[0].thread_id, 12)

        verified = verify_and_prune_summary(
            parsed,
            valid_msg_ids={1045, 1046},
            valid_speakers={"Тарас", "Оксана"},
        )
        self.assertEqual(len(verified.topics), 1)
        self.assertEqual(verified.topics[0].evidence_msg_ids, [1045, 1046])
        self.assertEqual(verified.topics[0].participants, ["Тарас"])
        self.assertEqual(len(verified.decisions_and_outcomes), 1)
        self.assertEqual(verified.decisions_and_outcomes[0].evidence_msg_ids, [1046])

        rendered = render_summary_text(
            verified,
            chat_id=-1001234567890,
            chat_username=None,
            show_citations=True,
        )
        self.assertIn("1. Головні теми обговорення:", rendered)
        self.assertIn("Деплой на прод (Тарас): Обговорили деплой сервісу", rendered)
        self.assertIn("https://t.me/c/1234567890/1045", rendered)
        self.assertIn("2. Висновки, рішення та відкриті питання:", rendered)
        self.assertIn("[Підтверджено] Деплоїмо о 20:00 (Тарас)", rendered)
        self.assertNotIn("**", rendered)

    def test_format_message_link_variants(self) -> None:
        self.assertEqual(
            format_message_link(-1001234567890, 42, chat_username=None),
            "https://t.me/c/1234567890/42",
        )
        self.assertEqual(
            format_message_link(-1001234567890, 42, chat_username="@ukrainian_devs"),
            "https://t.me/ukrainian_devs/42",
        )
        self.assertEqual(
            format_message_link(123456, 42, chat_username=None),
            "#42",
        )


class TestGeminiClient(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        self.patcher_gemini = patch.object(
            gemini_client, "async_session_factory", self.session_factory
        )
        self.patcher_gemini.start()

    def tearDown(self) -> None:
        self.patcher_gemini.stop()
        self.sync_engine.dispose()

    def test_model_chains_and_strip_markdown_dunders(self) -> None:
        self.assertEqual(ASK_FALLBACK_MODELS[0], "gemini-3.8-flash")
        self.assertEqual(ASK_FALLBACK_MODELS[1], "gemma-4-31b-it")
        self.assertEqual(SUMMARY_FALLBACK_MODELS[0], "gemma-4-26b-a4b-it")
        self.assertEqual(SUMMARY_FALLBACK_MODELS[1], "gemma-4-31b-it")

        sample = "Метод `__init__` у файлі **main.py** та змінна `my_var_name` і __bold__."
        cleaned = strip_markdown(sample)
        self.assertIn("__init__", cleaned)
        self.assertIn("my_var_name", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertIn("bold", cleaned)

    def test_gemma4_native_config_no_429_retry_amplification_and_404_ttl(self) -> None:
        async def _run():
            service = GeminiService()
            calls: list[tuple[str, dict]] = []

            async def fake_generate_content(*, model: str, contents, config):
                cfg_dict = {
                    "system_instruction": getattr(config, "system_instruction", None),
                    "tools": getattr(config, "tools", None),
                }
                calls.append((model, cfg_dict))
                if model == "model-404":
                    raise RuntimeError("404 NOT_FOUND")
                if model == "model-429":
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")
                return SimpleNamespace(text="Успішна відповідь від Gemma 4", candidates=[])

            service.client = SimpleNamespace(
                aio=SimpleNamespace(
                    models=SimpleNamespace(generate_content=fake_generate_content)
                )
            )

            res = await service._generate_with_fallback(
                model_chain=["model-404", "model-429", "gemma-4-31b-it"],
                contents="Привіт",
                system_instruction="Системна інструкція",
                use_search=True,
            )
            self.assertEqual(res, "Успішна відповідь від Gemma 4")

            called_models = [c[0] for c in calls]
            self.assertEqual(called_models, ["model-404", "model-429", "gemma-4-31b-it"])

            gemma4_cfg = calls[2][1]
            self.assertEqual(gemma4_cfg["system_instruction"], "Системна інструкція")
            self.assertIsNotNone(gemma4_cfg["tools"])

            self.assertTrue(service._is_model_unavailable("model-404"))
            service._unavailable_models["model-404"] = (
                time.monotonic() - UNAVAILABLE_MODEL_TTL_SECONDS - 10
            )
            self.assertFalse(service._is_model_unavailable("model-404"))

        run_coro(_run())

    def test_hourly_digest_aborts_if_cleared_during_llm_flight(self) -> None:
        async def _run():
            now = utc_now()
            chat_id = -100999
            async with self.session_factory() as session:
                u = User(user_id=7, username="dmytro", full_name="Дмитро", updated_at=now)
                session.add(u)
                for mid in range(1, 6):
                    session.add(
                        ChatLog(
                            chat_id=chat_id,
                            message_id=mid,
                            user_id=7,
                            text=f"Повідомлення номер {mid} для тесту дайджесту",
                            timestamp=now - timedelta(minutes=10 - mid),
                            is_processed=False,
                        )
                    )
                await session.commit()

            async def fake_generate_verified_summary(bundle, **kwargs):
                async with self.session_factory() as clear_session:
                    await clear_session.execute(
                        text("DELETE FROM chat_logs WHERE chat_id = :cid"),
                        {"cid": chat_id},
                    )
                    await clear_session.commit()
                schema = ChatSummarySchema(
                    is_low_signal=False,
                    topics=[
                        TopicItem(
                            title="Тест",
                            summary="Тест",
                            participants=["Дмитро"],
                            evidence_msg_ids=[1, 2],
                        )
                    ],
                )
                return "Згенерований дайджест", schema

            with patch.object(
                gemini_client.gemini_service,
                "generate_verified_summary",
                side_effect=fake_generate_verified_summary,
            ):
                processed = await process_hourly_digest_for_chat(chat_id)
                self.assertFalse(processed)

            async with self.session_factory() as session:
                digests = (
                    await session.execute(
                        select(ChatDigest).where(ChatDigest.chat_id == chat_id)
                    )
                ).scalars().all()
                self.assertEqual(len(digests), 0)

        run_coro(_run())


class TestCommandsHandlers(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        self.patcher_commands = patch.object(
            commands, "async_session_factory", self.session_factory
        )
        self.patcher_commands.start()
        commands._user_last_request.clear()

    def tearDown(self) -> None:
        self.patcher_commands.stop()
        self.sync_engine.dispose()

    def test_safe_reply_and_edit_chunking_over_4000_chars(self) -> None:
        async def _run():
            para1 = "А" * 2500
            para2 = "Б" * 2500
            long_text = f"{para1}\n\n{para2}"

            msg = SimpleNamespace(
                reply=_AsyncCallableMock(return_value=SimpleNamespace(message_id=1)),
                answer=_AsyncCallableMock(return_value=SimpleNamespace(message_id=2)),
                edit_text=_AsyncCallableMock(),
            )

            await commands._safe_reply(msg, long_text)
            msg.reply.assert_awaited_once_with(para1)
            msg.answer.assert_awaited_once_with(para2)

            target_msg = SimpleNamespace(
                edit_text=_AsyncCallableMock(),
                answer=_AsyncCallableMock(),
            )
            await commands._safe_edit_text(target_msg, long_text)
            target_msg.edit_text.assert_awaited_once_with(para1)
            target_msg.answer.assert_awaited_once_with(para2)

        run_coro(_run())

    def test_rate_limit_checked_before_photo_download(self) -> None:
        async def _run():
            user_id = 555
            commands._user_last_request[user_id] = time.monotonic()

            msg = SimpleNamespace(
                chat=SimpleNamespace(id=-100111, type="supergroup"),
                message_id=50,
                from_user=SimpleNamespace(id=user_id, username="tester", full_name="Tester"),
                text=None,
                caption="/ask Що на фото?",
                reply_to_message=None,
                photo=[SimpleNamespace(file_id="p1", file_size=1024)],
                reply=_AsyncCallableMock(),
                answer=_AsyncCallableMock(),
            )
            bot = SimpleNamespace(
                get_me=_AsyncCallableMock(return_value=SimpleNamespace(username="test_bot")),
                download=_AsyncCallableMock(),
            )

            with patch.object(
                commands, "_extract_photo_bytes", new_callable=_AsyncCallableMock
            ) as mock_extract:
                await commands._process_ask_request(msg, query="Що на фото?", bot=bot)
                mock_extract.assert_not_awaited()
                msg.reply.assert_awaited_once()
                sent_text = msg.reply.call_args[0][0]
                self.assertIn("Зачекайте", sent_text)

        run_coro(_run())

    def test_anonymous_admin_allowed_in_clear(self) -> None:
        async def _run():
            chat_id = -100222
            async with self.session_factory() as session:
                session.add(
                    ChatLog(
                        chat_id=chat_id,
                        message_id=1,
                        user_id=None,
                        text="Тестове повідомлення",
                        timestamp=utc_now(),
                        is_processed=False,
                    )
                )
                await session.commit()

            anon_admin_msg = SimpleNamespace(
                chat=SimpleNamespace(id=chat_id, type="supergroup"),
                message_id=2,
                from_user=SimpleNamespace(
                    id=1087968824, username="GroupAnonymousBot", is_bot=True
                ),
                sender_chat=SimpleNamespace(id=chat_id, title="Supergroup"),
                reply=_AsyncCallableMock(),
                answer=_AsyncCallableMock(),
            )
            bot = SimpleNamespace(get_chat_member=_AsyncCallableMock())

            await commands.cmd_clear_memory(anon_admin_msg, bot)
            bot.get_chat_member.assert_not_awaited()
            anon_admin_msg.reply.assert_awaited_once()
            self.assertIn("повністю очищено", anon_admin_msg.reply.call_args[0][0])

            async with self.session_factory() as session:
                remaining = (
                    await session.execute(
                        select(ChatLog).where(ChatLog.chat_id == chat_id)
                    )
                ).scalars().all()
                self.assertEqual(len(remaining), 0)

        run_coro(_run())


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromModule(__import__(__name__))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    os._exit(0 if result.wasSuccessful() else 1)
