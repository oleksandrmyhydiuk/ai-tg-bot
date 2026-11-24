from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, UTC
import io
import os
from pathlib import Path
import socket
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_api_key")

from aiogram.enums import ChatMemberStatus
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database.engine import _migrate_sqlite_schema
from app.database.models import Base, ChatDigest, ChatLog, User, utc_now
from app.handlers import group_events
from app.main import AllowlistMiddleware
from app.modules import (
    admin_ops as admin_ops_mod,
    link_tldr as link_tldr_mod,
    privacy_export as privacy_export_mod,
    registry,
    stats as stats_mod,
    voice as voice_mod,
)
from app.services.gemini_client import GeminiService
from app.services.link_summarizer import (
    ExtractedLinkContent,
    LinkFetchError,
    validate_public_http_url,
)
from app.services.stats_renderer import (
    ParticipantStat,
    StatsCardData,
    render_stats_leaderboard,
)


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class _SyncSessionAdapter:
    """Synchronous SQLAlchemy adapter matching AsyncSession for in-memory SQLite tests."""

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

    async def get(self, entity, ident):
        return self._session.get(entity, ident)

    async def execute(self, statement, params=None):
        if params is not None:
            return self._session.execute(statement, params)
        return self._session.execute(statement)

    async def commit(self) -> None:
        self._session.commit()


def _make_test_session_factory():
    sync_engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(sync_engine)
    sync_maker = sessionmaker(bind=sync_engine, expire_on_commit=False)

    def _factory() -> _SyncSessionAdapter:
        return _SyncSessionAdapter(sync_maker)

    return sync_engine, _factory


def _make_message(
    *,
    chat_id: int = -1001234567890,
    chat_type: str = "supergroup",
    chat_title: str | None = "Тестовий чат",
    chat_username: str | None = "test_chat",
    message_id: int = 100,
    user_id: int | None = 42,
    username: str | None = "taras",
    full_name: str = "Тарас",
    is_bot: bool = False,
    text: str | None = "Привіт",
    caption: str | None = None,
    entities=None,
    caption_entities=None,
    reply_to_message=None,
    bot=None,
    sender_chat=None,
    document=None,
) -> SimpleNamespace:
    reply_msg = SimpleNamespace(
        chat=SimpleNamespace(
            id=chat_id,
            type=chat_type,
            title=chat_title,
            full_name=chat_title,
            username=chat_username,
        ),
        message_id=message_id + 1000,
        from_user=SimpleNamespace(
            id=99999, username="test_bot", full_name="Бот", is_bot=True
        ),
        text="",
        caption=None,
        edit_text=AsyncMock(),
        answer=AsyncMock(),
        delete=AsyncMock(),
    )
    return SimpleNamespace(
        chat=SimpleNamespace(
            id=chat_id,
            type=chat_type,
            title=chat_title,
            full_name=chat_title,
            username=chat_username,
        ),
        message_id=message_id,
        from_user=SimpleNamespace(
            id=user_id, username=username, full_name=full_name, is_bot=is_bot
        )
        if user_id is not None
        else None,
        sender_chat=sender_chat,
        text=text,
        caption=caption,
        entities=entities,
        caption_entities=caption_entities,
        reply_to_message=reply_to_message,
        quote=None,
        message_thread_id=None,
        is_topic_message=False,
        photo=None,
        video=None,
        voice=None,
        video_note=None,
        audio=None,
        document=document,
        sticker=None,
        poll=None,
        animation=None,
        location=None,
        forward_origin=None,
        forward_from_chat=None,
        forward_from=None,
        forward_sender_name=None,
        bot=bot,
        reply=AsyncMock(return_value=reply_msg),
        answer=AsyncMock(return_value=reply_msg),
        edit_text=AsyncMock(),
        reply_photo=AsyncMock(return_value=reply_msg),
        reply_document=AsyncMock(return_value=reply_msg),
        answer_document=AsyncMock(return_value=reply_msg),
    )


class TestVisualStatsLeaderboard(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        registry.clear_modules_cache()
        self.patchers = [
            patch.object(registry, "async_session_factory", self.session_factory),
            patch.object(stats_mod, "async_session_factory", self.session_factory),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        registry.clear_modules_cache()
        self.sync_engine.dispose()

    def test_render_stats_leaderboard_pillow_or_clean_runtime_error(self) -> None:
        try:
            from PIL import Image  # type: ignore[import-not-found]
        except ImportError:
            Image = None

        avatar_bytes: bytes | None = None
        if Image is not None:
            buf = io.BytesIO()
            Image.new("RGB", (64, 64), (56, 189, 248)).save(buf, format="PNG")
            avatar_bytes = buf.getvalue()

        card = StatsCardData(
            chat_title="Команда розробки",
            period_label="24 год.",
            total_period_messages=42,
            total_retained_messages=150,
            digest_count=4,
            peak_hour_label="19:00–20:00 (15)",
            participants=[
                ParticipantStat(
                    user_id=101,
                    display_name="Олена Коваль",
                    message_count=25,
                    avatar_bytes=avatar_bytes,
                ),
                ParticipantStat(
                    user_id=102,
                    display_name="Тарас Шевченко",
                    message_count=17,
                    avatar_bytes=None,
                ),
            ],
        )

        if Image is not None:
            png_bytes = render_stats_leaderboard(card)
            self.assertTrue(png_bytes.startswith(PNG_SIGNATURE))
            with Image.open(io.BytesIO(png_bytes)) as rendered_img:
                width, height = rendered_img.size
            self.assertEqual(width, 960)
            self.assertGreater(height, 200)
        else:
            with self.assertRaises(RuntimeError) as ctx:
                render_stats_leaderboard(card)
            self.assertIn("Pillow", str(ctx.exception))

    def test_stats_photo_with_top3_links_and_text_fallback(self) -> None:
        async def _run():
            chat_id = -100555666777
            now = utc_now()
            async with self.session_factory() as session:
                session.add_all(
                    [
                        User(user_id=1, username="olena", full_name="Олена"),
                        User(user_id=2, username="ivan", full_name="Іван"),
                    ]
                )
                session.add_all(
                    [
                        ChatLog(
                            chat_id=chat_id,
                            message_id=10,
                            user_id=1,
                            text="Головна ідея для релізу",
                            timestamp=now - timedelta(hours=2),
                            is_bot_message=False,
                        ),
                        ChatLog(
                            chat_id=chat_id,
                            message_id=11,
                            user_id=2,
                            text="Підтримую!",
                            reply_to_message_id=10,
                            timestamp=now - timedelta(hours=1, minutes=50),
                            is_bot_message=False,
                        ),
                        ChatLog(
                            chat_id=chat_id,
                            message_id=12,
                            user_id=1,
                            text="Ок, беремо в роботу",
                            reply_to_message_id=10,
                            timestamp=now - timedelta(hours=1, minutes=40),
                            is_bot_message=False,
                        ),
                        ChatDigest(
                            chat_id=chat_id,
                            digest_text="Обговорили реліз.",
                            created_at=now - timedelta(hours=1),
                        ),
                    ]
                )
                await session.commit()

            fake_png = PNG_SIGNATURE + b"\x00\x00\x00\rIHDRfake-png-payload"
            msg_photo = _make_message(
                chat_id=chat_id,
                chat_username="dev_group",
                text="/stats 24h",
                bot=None,
            )
            cmd = SimpleNamespace(args="24h")

            with patch.object(
                stats_mod, "render_stats_leaderboard", return_value=fake_png
            ) as mock_render:
                await stats_mod.cmd_stats(msg_photo, cmd)

            mock_render.assert_called_once()
            msg_photo.reply_photo.assert_awaited_once()
            msg_photo.reply.assert_not_awaited()
            photo_arg = msg_photo.reply_photo.call_args.args[0]
            caption_arg = msg_photo.reply_photo.call_args.kwargs["caption"]
            self.assertIsInstance(photo_arg, BufferedInputFile)
            self.assertEqual(photo_arg.data, fake_png)
            self.assertIn("https://t.me/dev_group/10", caption_arg)
            self.assertIn("Топ-3 найбільш обговорюваних", caption_arg)

            # Fallback to plain-text stats when rendering raises an error
            msg_fallback = _make_message(
                chat_id=chat_id,
                chat_username="dev_group",
                text="/stats 24h",
                bot=None,
            )
            with patch.object(
                stats_mod,
                "render_stats_leaderboard",
                side_effect=RuntimeError("Pillow is not installed"),
            ):
                await stats_mod.cmd_stats(msg_fallback, cmd)

            msg_fallback.reply_photo.assert_not_awaited()
            msg_fallback.reply.assert_awaited_once()
            fallback_text = msg_fallback.reply.call_args.args[0]
            self.assertIn("Статистика чату", fallback_text)
            self.assertIn("Олена", fallback_text)
            self.assertIn("https://t.me/dev_group/10", fallback_text)

        asyncio.run(_run())


class TestLinkTldrAndSSRF(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        registry.clear_modules_cache()
        self.patchers = [
            patch.object(registry, "async_session_factory", self.session_factory),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        registry.clear_modules_cache()
        self.sync_engine.dispose()

    def test_validate_public_http_url_ssrf_protection(self) -> None:
        blocked_urls = [
            "http://127.0.0.1",
            "http://127.0.0.1:8080/admin",
            "http://localhost",
            "http://localhost/secret",
            "http://169.254.169.254",
            "http://169.254.169.254/latest/meta-data/",
            "file:///etc/passwd",
            "https://u:p@example.com",
            "http://10.0.0.1",
            "http://192.168.1.1",
            "http://[::1]/",
            "ftp://example.com/file.txt",
        ]
        for bad_url in blocked_urls:
            with self.subTest(url=bad_url):
                with self.assertRaises(LinkFetchError):
                    validate_public_http_url(bad_url)

        # DNS rebinding / hostname resolving to private IP must also be rejected
        with patch.object(
            socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))],
        ):
            with self.assertRaises(LinkFetchError):
                validate_public_http_url("https://internal.example.test/page")

        # Valid public IP literal and public DNS resolution must be accepted
        self.assertEqual(
            validate_public_http_url("https://93.184.216.34/article"),
            "https://93.184.216.34/article",
        )
        with patch.object(
            socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
        ):
            self.assertEqual(
                validate_public_http_url("https://example.com/article"),
                "https://example.com/article",
            )

    def test_tldr_module_gating_rate_limit_and_summary_flow(self) -> None:
        async def _run():
            chat_id = -100777888999

            # 1. Module disabled -> polite notice
            await registry.set_module_enabled(chat_id, "link_tldr", False)
            msg_disabled = _make_message(chat_id=chat_id, text="/tldr https://example.com")
            with patch.object(link_tldr_mod, "fetch_link_content", new=AsyncMock()) as mock_fetch:
                await link_tldr_mod.cmd_tldr(
                    msg_disabled, SimpleNamespace(args="https://example.com")
                )
                mock_fetch.assert_not_awaited()
            msg_disabled.reply.assert_awaited_once()
            self.assertIn("вимкнено", msg_disabled.reply.call_args.args[0].lower())

            # 2. Module enabled + rate limited -> wait notice before fetching
            await registry.set_module_enabled(chat_id, "link_tldr", True)
            msg_rl = _make_message(chat_id=chat_id, text="/tldr https://example.com")
            with (
                patch.object(link_tldr_mod, "_check_rate_limit", return_value=7.5),
                patch.object(link_tldr_mod, "fetch_link_content", new=AsyncMock()) as mock_fetch,
            ):
                await link_tldr_mod.cmd_tldr(
                    msg_rl, SimpleNamespace(args="https://example.com")
                )
                mock_fetch.assert_not_awaited()
            msg_rl.reply.assert_awaited_once()
            self.assertIn("Зачекайте", msg_rl.reply.call_args.args[0])

            # 3. Full happy path: fetch_link_content -> gemini_service.summarize_external_content
            msg_ok = _make_message(chat_id=chat_id, text="/tldr https://example.com/news")
            extracted = ExtractedLinkContent(
                url="https://example.com/news",
                title="Новини науки",
                text="Вчені зробили важливе відкриття у сфері батарей.",
                source_type="webpage",
            )
            with (
                patch.object(link_tldr_mod, "_check_rate_limit", return_value=0.0),
                patch.object(
                    link_tldr_mod,
                    "fetch_link_content",
                    new=AsyncMock(return_value=extracted),
                ) as mock_fetch,
                patch.object(
                    link_tldr_mod.gemini_service,
                    "summarize_external_content",
                    new=AsyncMock(return_value="• Відкрито новий тип батарей."),
                ) as mock_sum,
            ):
                await link_tldr_mod.cmd_tldr(
                    msg_ok, SimpleNamespace(args="https://example.com/news")
                )

            mock_fetch.assert_awaited_once_with("https://example.com/news")
            mock_sum.assert_awaited_once_with(
                "Новини науки",
                "https://example.com/news",
                "Вчені зробили важливе відкриття у сфері батарей.",
            )
            status_msg = msg_ok.reply.return_value
            status_msg.edit_text.assert_awaited_once()
            final_text = status_msg.edit_text.call_args.args[0]
            self.assertIn("TL;DR: Новини науки", final_text)
            self.assertIn("Відкрито новий тип батарей", final_text)
            self.assertIn("https://example.com/news", final_text)

        asyncio.run(_run())


class TestDocumentAnalysis(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        registry.clear_modules_cache()
        self.patchers = [
            patch.object(registry, "async_session_factory", self.session_factory),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        registry.clear_modules_cache()
        self.sync_engine.dispose()

    def test_doc_validation_rate_limit_and_gemini_analysis(self) -> None:
        async def _run():
            chat_id = -100888999000
            await registry.set_module_enabled(chat_id, "voice", True)

            # 1. Rejects files > 15 MB before download
            big_doc = SimpleNamespace(
                file_id="big_pdf",
                file_name="huge.pdf",
                mime_type="application/pdf",
                file_size=16 * 1024 * 1024,
            )
            bot = SimpleNamespace(download=AsyncMock())
            msg_big = _make_message(
                chat_id=chat_id,
                text="/doc",
                reply_to_message=_make_message(chat_id=chat_id, document=big_doc),
            )
            await voice_mod.cmd_doc(msg_big, bot, SimpleNamespace(args=""))
            bot.download.assert_not_awaited()
            msg_big.reply.assert_awaited_once()
            self.assertIn("15 МБ", msg_big.reply.call_args.args[0])

            # 2. Rejects unsupported MIME types before download
            bad_doc = SimpleNamespace(
                file_id="bad_exe",
                file_name="installer.exe",
                mime_type="application/x-msdownload",
                file_size=1024,
            )
            msg_bad = _make_message(
                chat_id=chat_id,
                text="/doc",
                reply_to_message=_make_message(chat_id=chat_id, document=bad_doc),
            )
            await voice_mod.cmd_doc(msg_bad, bot, SimpleNamespace(args=""))
            bot.download.assert_not_awaited()
            msg_bad.reply.assert_awaited_once()
            self.assertIn("Непідтримуваний формат", msg_bad.reply.call_args.args[0])

            # 3. Checks rate limit BEFORE downloading file
            ok_pdf = SimpleNamespace(
                file_id="pdf_1",
                file_name="report.pdf",
                mime_type="application/pdf",
                file_size=2048,
            )
            msg_rl = _make_message(
                chat_id=chat_id,
                text="/doc",
                reply_to_message=_make_message(chat_id=chat_id, document=ok_pdf),
            )
            with patch.object(voice_mod, "_check_rate_limit", return_value=4.0):
                await voice_mod.cmd_doc(msg_rl, bot, SimpleNamespace(args=""))
            bot.download.assert_not_awaited()
            msg_rl.reply.assert_awaited_once()
            self.assertIn("Зачекайте", msg_rl.reply.call_args.args[0])

            # 4. Passes PDF bytes to gemini_service.analyze_document
            pdf_payload = b"%PDF-1.4 sample quarterly report"

            async def _fake_download_pdf(file_id, destination):
                destination.write(pdf_payload)

            bot_pdf = SimpleNamespace(download=AsyncMock(side_effect=_fake_download_pdf))
            msg_pdf = _make_message(
                chat_id=chat_id,
                text="/doc Який дохід?",
                reply_to_message=_make_message(chat_id=chat_id, document=ok_pdf),
            )
            with (
                patch.object(voice_mod, "_check_rate_limit", return_value=0.0),
                patch.object(
                    voice_mod.gemini_service,
                    "analyze_document",
                    new=AsyncMock(return_value="Дохід зріс на 20%."),
                ) as mock_analyze,
            ):
                await voice_mod.cmd_doc(msg_pdf, bot_pdf, SimpleNamespace(args="Який дохід?"))

            bot_pdf.download.assert_awaited_once()
            mock_analyze.assert_awaited_once_with(
                pdf_payload, "application/pdf", "report.pdf", "Який дохід?"
            )
            pdf_status = msg_pdf.reply.return_value
            pdf_status.edit_text.assert_awaited_once()
            self.assertIn("Дохід зріс на 20%.", pdf_status.edit_text.call_args.args[0])

            # 5. Passes TXT bytes (directly attached document) to gemini_service.analyze_document
            txt_doc = SimpleNamespace(
                file_id="txt_1",
                file_name="notes.txt",
                mime_type="text/plain",
                file_size=128,
            )
            txt_payload = "Нотатки наради: запуск у п'ятницю.".encode()

            async def _fake_download_txt(file_id, destination):
                destination.write(txt_payload)

            bot_txt = SimpleNamespace(download=AsyncMock(side_effect=_fake_download_txt))
            msg_txt = _make_message(
                chat_id=chat_id,
                text="/doc",
                document=txt_doc,
            )
            with (
                patch.object(voice_mod, "_check_rate_limit", return_value=0.0),
                patch.object(
                    voice_mod.gemini_service,
                    "analyze_document",
                    new=AsyncMock(return_value="Запуск заплановано на п'ятницю."),
                ) as mock_analyze_txt,
            ):
                await voice_mod.cmd_doc(msg_txt, bot_txt, SimpleNamespace(args=""))

            mock_analyze_txt.assert_awaited_once_with(
                txt_payload, "text/plain", "notes.txt", ""
            )

        asyncio.run(_run())


class TestPrivacyOptOutAndExport(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        registry.clear_modules_cache()
        registry.clear_optout_cache()
        self.patchers = [
            patch.object(registry, "async_session_factory", self.session_factory),
            patch.object(group_events, "async_session_factory", self.session_factory),
            patch.object(privacy_export_mod, "async_session_factory", self.session_factory),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        registry.clear_modules_cache()
        registry.clear_optout_cache()
        self.sync_engine.dispose()

    def test_optout_skips_logging_and_optin_reenables_logging(self) -> None:
        async def _run():
            chat_id = -10011223344
            user_id = 505

            # Opt out
            msg_out = _make_message(chat_id=chat_id, user_id=user_id, text="/optout")
            await privacy_export_mod.cmd_optout(msg_out)
            msg_out.reply.assert_awaited_once()

            async with self.session_factory() as session:
                user_row = await session.get(User, user_id)
                self.assertIsNotNone(user_row)
                self.assertTrue(user_row.is_opted_out)

            # Message from opted-out user MUST NOT be logged
            msg_skipped = _make_message(
                chat_id=chat_id,
                message_id=201,
                user_id=user_id,
                text="Секретне повідомлення",
            )
            await group_events.log_text_message(msg_skipped)

            async with self.session_factory() as session:
                rows = (
                    await session.execute(
                        select(ChatLog).where(ChatLog.chat_id == chat_id)
                    )
                ).scalars().all()
                self.assertEqual(len(rows), 0)

            # Opt back in
            msg_in = _make_message(chat_id=chat_id, user_id=user_id, text="/optin")
            await privacy_export_mod.cmd_optin(msg_in)
            msg_in.reply.assert_awaited_once()

            async with self.session_factory() as session:
                user_row = await session.get(User, user_id)
                self.assertFalse(user_row.is_opted_out)

            # Message from opted-in user MUST now be logged
            msg_logged = _make_message(
                chat_id=chat_id,
                message_id=202,
                user_id=user_id,
                text="Звичайне повідомлення після /optin",
            )
            await group_events.log_text_message(msg_logged)

            async with self.session_factory() as session:
                rows = (
                    await session.execute(
                        select(ChatLog).where(ChatLog.chat_id == chat_id)
                    )
                ).scalars().all()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].message_id, 202)
                self.assertEqual(rows[0].text, "Звичайне повідомлення після /optin")

        asyncio.run(_run())

    def test_export_md_and_txt_admin_only_and_digest_content(self) -> None:
        async def _run():
            chat_id = -10022334455
            t1 = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
            t2 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
            async with self.session_factory() as session:
                session.add_all(
                    [
                        ChatDigest(
                            chat_id=chat_id,
                            digest_text="Дайджест 1: обговорили бекенд.",
                            period_start=t1,
                            period_end=t1 + timedelta(hours=1),
                            message_count=12,
                            created_at=t1 + timedelta(hours=1),
                        ),
                        ChatDigest(
                            chat_id=chat_id,
                            digest_text="Дайджест 2: узгодили деплой.",
                            period_start=t2,
                            period_end=t2 + timedelta(hours=1),
                            message_count=8,
                            created_at=t2 + timedelta(hours=1),
                        ),
                    ]
                )
                await session.commit()

            # Non-admin in supergroup -> rejected
            non_admin_bot = SimpleNamespace(
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status=ChatMemberStatus.MEMBER)
                )
            )
            msg_denied = _make_message(
                chat_id=chat_id, chat_type="supergroup", user_id=77, text="/export md"
            )
            await privacy_export_mod.cmd_export(
                msg_denied, non_admin_bot, SimpleNamespace(args="md")
            )
            msg_denied.reply_document.assert_not_awaited()
            msg_denied.reply.assert_awaited_once()
            self.assertIn("адміністратори", msg_denied.reply.call_args.args[0].lower())

            # Admin in supergroup -> /export md
            admin_bot = SimpleNamespace(
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
                )
            )
            msg_md = _make_message(
                chat_id=chat_id, chat_type="supergroup", user_id=1, text="/export md"
            )
            await privacy_export_mod.cmd_export(
                msg_md, admin_bot, SimpleNamespace(args="md")
            )
            msg_md.reply_document.assert_awaited_once()
            md_doc = msg_md.reply_document.call_args.args[0]
            self.assertIsInstance(md_doc, BufferedInputFile)
            self.assertTrue(md_doc.filename.endswith(".md"))
            md_text = md_doc.data.decode("utf-8")
            self.assertIn("Дайджест 1: обговорили бекенд.", md_text)
            self.assertIn("Дайджест 2: узгодили деплой.", md_text)
            self.assertLess(
                md_text.index("Дайджест 1"),
                md_text.index("Дайджест 2"),
            )

            # Admin in supergroup -> /export txt
            msg_txt = _make_message(
                chat_id=chat_id, chat_type="supergroup", user_id=1, text="/export txt"
            )
            await privacy_export_mod.cmd_export(
                msg_txt, admin_bot, SimpleNamespace(args="txt")
            )
            msg_txt.reply_document.assert_awaited_once()
            txt_doc = msg_txt.reply_document.call_args.args[0]
            self.assertIsInstance(txt_doc, BufferedInputFile)
            self.assertTrue(txt_doc.filename.endswith(".txt"))
            txt_text = txt_doc.data.decode("utf-8")
            self.assertIn("Дайджест 1: обговорили бекенд.", txt_text)
            self.assertIn("Дайджест 2: узгодили деплой.", txt_text)

        asyncio.run(_run())

    def test_sqlite_migration_adds_users_is_opted_out(self) -> None:
        legacy_engine = create_engine("sqlite:///:memory:")
        try:
            with legacy_engine.begin() as conn:
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
                        "INSERT INTO users (user_id, username, full_name, updated_at) "
                        "VALUES (10, 'legacy_u', 'Legacy User', '2026-01-01 00:00:00')"
                    )
                )

                _migrate_sqlite_schema(conn)
                # Idempotency check: running migration a second time must succeed cleanly
                _migrate_sqlite_schema(conn)

                cols = {
                    row[1]
                    for row in conn.execute(text("PRAGMA table_info(users)")).fetchall()
                }
                self.assertIn("is_opted_out", cols)

                val = conn.execute(
                    text("SELECT is_opted_out FROM users WHERE user_id = 10")
                ).scalar_one()
                self.assertEqual(val, 0)
        finally:
            legacy_engine.dispose()


class TestAdminOpsTelemetryRpmAndAllowlist(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        registry.clear_modules_cache()
        self.patchers = [
            patch.object(registry, "async_session_factory", self.session_factory),
            patch.object(admin_ops_mod, "async_session_factory", self.session_factory),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        registry.clear_modules_cache()
        self.sync_engine.dispose()

    def test_gemini_telemetry_records_success_429_404_and_avg_latency(self) -> None:
        async def _run():
            svc = GeminiService(api_key="test_key")

            async def _fake_generate(model, contents, config):
                if model == "m-404":
                    raise RuntimeError("404 NOT_FOUND")
                if model == "m-429":
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")
                return SimpleNamespace(text="Успішна відповідь")

            with (
                patch.object(settings, "global_rpm_limit", 0),
                patch.object(
                    svc.client.aio.models,
                    "generate_content",
                    new=AsyncMock(side_effect=_fake_generate),
                ),
            ):
                res1 = await svc._generate_with_fallback(
                    model_chain=["m-404", "m-429", "m-ok"],
                    contents="Привіт",
                )
                res2 = await svc._generate_with_fallback(
                    model_chain=["m-ok"],
                    contents="Ще раз",
                )

            self.assertEqual(res1, "Успішна відповідь")
            self.assertEqual(res2, "Успішна відповідь")

            snap = svc.get_telemetry_snapshot()
            self.assertEqual(snap["m-404"]["not_found_404"], 1)
            self.assertEqual(snap["m-404"]["success"], 0)
            self.assertEqual(snap["m-429"]["rate_limited_429"], 1)
            self.assertEqual(snap["m-429"]["success"], 0)
            self.assertEqual(snap["m-ok"]["success"], 2)
            self.assertIsInstance(snap["m-ok"]["avg_latency_ms"], float)
            self.assertGreaterEqual(snap["m-ok"]["avg_latency_ms"], 0.0)

        asyncio.run(_run())

    def test_global_rpm_guard_rejects_when_limit_exceeded(self) -> None:
        async def _run():
            svc = GeminiService(api_key="test_key")
            mock_gen = AsyncMock(return_value=SimpleNamespace(text="OK"))

            with (
                patch.object(settings, "global_rpm_limit", 2),
                patch.object(svc.client.aio.models, "generate_content", new=mock_gen),
            ):
                await svc._generate_with_fallback(["m-ok"], "q1")
                await svc._generate_with_fallback(["m-ok"], "q2")
                with self.assertRaises(RuntimeError) as ctx:
                    await svc._generate_with_fallback(["m-ok"], "q3")
                self.assertIn("глобальний ліміт", str(ctx.exception).lower())

                # High-level ask() returns polite warning without hitting API again
                ask_resp = await svc.ask("q4")
                self.assertIn("глобальний ліміт", ask_resp.lower())

            self.assertEqual(mock_gen.await_count, 2)

        asyncio.run(_run())

    def test_health_and_quota_authorization(self) -> None:
        async def _run():
            chat_id = -10033445566
            fake_snapshot = {
                "gemini-3.8-flash": {
                    "success": 5,
                    "rate_limited_429": 1,
                    "not_found_404": 0,
                    "other_errors": 0,
                    "avg_latency_ms": 123.4,
                }
            }

            non_admin_bot = SimpleNamespace(
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status=ChatMemberStatus.MEMBER)
                )
            )
            admin_bot = SimpleNamespace(
                get_chat_member=AsyncMock(
                    return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
                )
            )

            # 1. Unauthorized regular user -> rejected
            msg_unauth_h = _make_message(
                chat_id=chat_id, user_id=111, text="/health", bot=non_admin_bot
            )
            msg_unauth_q = _make_message(
                chat_id=chat_id, user_id=111, text="/quota", bot=non_admin_bot
            )
            with patch.object(settings, "admin_user_ids", "999"):
                await admin_ops_mod.cmd_health(msg_unauth_h)
                await admin_ops_mod.cmd_quota(msg_unauth_q)
            self.assertIn("⛔", msg_unauth_h.reply.call_args.args[0])
            self.assertIn("⛔", msg_unauth_q.reply.call_args.args[0])

            # 2. Global ADMIN_USER_IDS user -> allowed even when admin_ops module is disabled
            msg_admin_h = _make_message(
                chat_id=chat_id, user_id=999, text="/health", bot=non_admin_bot
            )
            msg_admin_q = _make_message(
                chat_id=chat_id, user_id=999, text="/quota", bot=non_admin_bot
            )
            with (
                patch.object(settings, "admin_user_ids", "999"),
                patch.object(
                    admin_ops_mod.gemini_service,
                    "get_telemetry_snapshot",
                    return_value=fake_snapshot,
                ),
            ):
                await admin_ops_mod.cmd_health(msg_admin_h)
                await admin_ops_mod.cmd_quota(msg_admin_q)

            health_out = msg_admin_h.reply.call_args.args[0]
            quota_out = msg_admin_q.reply.call_args.args[0]
            self.assertIn("Стан бота (/health)", health_out)
            self.assertIn("База даних: OK", health_out)
            self.assertIn("gemini-3.8-flash", quota_out)
            self.assertIn("успішно=5", quota_out)
            self.assertIn("429=1", quota_out)

            # 3. Chat admin with admin_ops module enabled -> allowed
            await registry.set_module_enabled(chat_id, "admin_ops", True)
            msg_chat_admin = _make_message(
                chat_id=chat_id, user_id=222, text="/health", bot=admin_bot
            )
            with patch.object(settings, "admin_user_ids", ""):
                await admin_ops_mod.cmd_health(msg_chat_admin)
            self.assertIn("Стан бота (/health)", msg_chat_admin.reply.call_args.args[0])

        asyncio.run(_run())

    def test_allowlist_middleware_empty_vs_configured_and_admin_bypass(self) -> None:
        async def _run():
            mw = AllowlistMiddleware()

            def _make_msg_event(chat_id: int, user_id: int) -> MagicMock:
                ev = MagicMock(spec=Message)
                ev.chat = SimpleNamespace(id=chat_id)
                ev.from_user = SimpleNamespace(id=user_id)
                return ev

            def _make_cb_event(chat_id: int, user_id: int) -> MagicMock:
                ev = MagicMock(spec=CallbackQuery)
                ev.message = SimpleNamespace(chat=SimpleNamespace(id=chat_id))
                ev.from_user = SimpleNamespace(id=user_id)
                return ev

            # 1. Empty ALLOWED_CHAT_IDS -> allows everything
            handler = AsyncMock(return_value="handled")
            with (
                patch.object(settings, "allowed_chat_ids", ""),
                patch.object(settings, "admin_user_ids", ""),
            ):
                res = await mw(handler, _make_msg_event(-100999, 123), {})
            self.assertEqual(res, "handled")
            handler.assert_awaited_once()

            # 2. Configured ALLOWED_CHAT_IDS -> blocks non-listed chats, allows listed & ADMIN_USER_IDS
            with (
                patch.object(settings, "allowed_chat_ids", "-100111, -100222"),
                patch.object(settings, "admin_user_ids", "999"),
            ):
                # Listed chat -> allowed
                h_listed = AsyncMock(return_value="ok_listed")
                self.assertEqual(
                    await mw(h_listed, _make_msg_event(-100111, 123), {}),
                    "ok_listed",
                )
                h_listed.assert_awaited_once()

                # Non-listed chat, normal user (Message & CallbackQuery) -> blocked
                h_blocked_msg = AsyncMock(return_value="should_not_run")
                self.assertIsNone(
                    await mw(h_blocked_msg, _make_msg_event(-100999, 123), {})
                )
                h_blocked_msg.assert_not_awaited()

                h_blocked_cb = AsyncMock(return_value="should_not_run")
                self.assertIsNone(
                    await mw(h_blocked_cb, _make_cb_event(-100999, 123), {})
                )
                h_blocked_cb.assert_not_awaited()

                # Non-listed chat, global admin user (Message & CallbackQuery) -> allowed
                h_admin_msg = AsyncMock(return_value="ok_admin_msg")
                self.assertEqual(
                    await mw(h_admin_msg, _make_msg_event(-100999, 999), {}),
                    "ok_admin_msg",
                )
                h_admin_msg.assert_awaited_once()

                h_admin_cb = AsyncMock(return_value="ok_admin_cb")
                self.assertEqual(
                    await mw(h_admin_cb, _make_cb_event(-100999, 999), {}),
                    "ok_admin_cb",
                )
                h_admin_cb.assert_awaited_once()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
