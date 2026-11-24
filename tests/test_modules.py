from __future__ import annotations

import os
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BOT_TOKEN", "123456:TEST_TOKEN")
os.environ.setdefault("GEMINI_API_KEY", "test_gemini_api_key")

from aiogram.enums import ChatMemberStatus
from aiogram.types import BotCommandScopeChat, InlineKeyboardMarkup
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database.models import Base, ChatDigest, ChatLog, User, utc_now
from app.handlers import commands, group_events
from app.modules import (
    ask as ask_mod,
    core as core_mod,
    daily_digest as digest_mod,
    media_dl as media_dl_mod,
    registry,
    search as search_mod,
    stats as stats_mod,
    summary as summary_mod,
    voice as voice_mod,
)
from app.services import gemini_client
from app.services.gemini_client import GeminiService, run_daily_digest_tick
from app.services.media_downloader import (
    detect_platform,
    extract_auto_download_urls,
    extract_supported_urls,
    is_supported_video_url,
)
from app.services.summary_verifier import ChatSummarySchema, TopicItem


class _AsyncCallableMock:
    def __init__(self, return_value=None) -> None:
        self.return_value = return_value
        self.calls: list[tuple[tuple, dict]] = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if callable(self.return_value):
            return self.return_value(*args, **kwargs)
        return self.return_value

    def assert_awaited_once(self) -> None:
        assert len(self.calls) == 1, f"Expected 1 call, got {len(self.calls)}"

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
            except StopIteration as exc:
                return exc.value
    finally:
        coro.close()


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
    chat_username: str | None = None,
    message_id: int = 100,
    user_id: int = 42,
    username: str | None = "taras",
    full_name: str = "Тарас",
    is_bot: bool = False,
    text: str | None = "Привіт",
    caption: str | None = None,
    reply_to_message=None,
    bot=None,
    sender_chat=None,
    voice=None,
    audio=None,
    video_note=None,
    photo=None,
) -> SimpleNamespace:
    reply_msg = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, username=chat_username),
        message_id=message_id + 1000,
        from_user=SimpleNamespace(id=99999, username="test_bot", full_name="Бот", is_bot=True),
        text="",
        caption=None,
        edit_text=_AsyncCallableMock(),
        answer=_AsyncCallableMock(),
        delete=_AsyncCallableMock(),
    )
    return SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type=chat_type, username=chat_username),
        message_id=message_id,
        from_user=SimpleNamespace(
            id=user_id, username=username, full_name=full_name, is_bot=is_bot
        )
        if user_id is not None
        else None,
        sender_chat=sender_chat,
        text=text,
        caption=caption,
        reply_to_message=reply_to_message,
        quote=None,
        message_thread_id=None,
        is_topic_message=False,
        photo=photo,
        video=None,
        voice=voice,
        video_note=video_note,
        audio=audio,
        document=None,
        sticker=None,
        poll=None,
        animation=None,
        location=None,
        forward_origin=None,
        forward_from_chat=None,
        forward_from=None,
        forward_sender_name=None,
        bot=bot,
        reply=_AsyncCallableMock(return_value=reply_msg),
        answer=_AsyncCallableMock(return_value=reply_msg),
        edit_text=_AsyncCallableMock(),
        reply_video=_AsyncCallableMock(),
        answer_video=_AsyncCallableMock(),
    )


class TestPerChatIsolationAndCoreUI(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        registry.clear_modules_cache()
        self.patchers = [
            patch.object(registry, "async_session_factory", self.session_factory),
            patch.object(core_mod, "async_session_factory", self.session_factory),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        registry.clear_modules_cache()
        self.sync_engine.dispose()

    def test_per_chat_isolation_persistence_and_command_sync(self) -> None:
        async def _run():
            chat_a = -1001111111
            chat_b = -1002222222

            # Chat A: only summary + media_dl
            await registry.set_all_modules(chat_a, False)
            await registry.set_module_enabled(chat_a, "summary", True)
            await registry.set_module_enabled(chat_a, "media_dl", True)

            # Chat B: only ask + voice
            await registry.set_all_modules(chat_b, False)
            await registry.set_module_enabled(chat_b, "ask", True)
            await registry.set_module_enabled(chat_b, "voice", True)

            self.assertEqual(await registry.get_enabled_modules(chat_a), {"summary", "media_dl"})
            self.assertEqual(await registry.get_enabled_modules(chat_b), {"ask", "voice"})

            # Persist across cache clear
            registry.clear_modules_cache()
            enabled_a = await registry.get_enabled_modules(chat_a)
            enabled_b = await registry.get_enabled_modules(chat_b)
            self.assertEqual(enabled_a, {"summary", "media_dl"})
            self.assertEqual(enabled_b, {"ask", "voice"})

            # Verify build_bot_commands_for_modules
            cmds_a = [c.command for c in registry.build_bot_commands_for_modules(enabled_a)]
            cmds_b = [c.command for c in registry.build_bot_commands_for_modules(enabled_b)]
            core_names = [c for c, _ in registry.CORE_COMMANDS]
            self.assertEqual(cmds_a, core_names + ["summary", "tasks", "dl"])
            self.assertEqual(cmds_b, core_names + ["ask", "transcribe", "doc"])

            # Verify sync_chat_commands passes BotCommandScopeChat(chat_id=...)
            bot = SimpleNamespace(set_my_commands=_AsyncCallableMock())
            await registry.sync_chat_commands(bot, chat_a)
            await registry.sync_chat_commands(bot, chat_b)
            self.assertEqual(len(bot.set_my_commands.calls), 2)

            call_a_args, call_a_kwargs = bot.set_my_commands.calls[0]
            self.assertEqual([c.command for c in call_a_args[0]], core_names + ["summary", "tasks", "dl"])
            self.assertIsInstance(call_a_kwargs["scope"], BotCommandScopeChat)
            self.assertEqual(call_a_kwargs["scope"].chat_id, chat_a)

            call_b_args, call_b_kwargs = bot.set_my_commands.calls[1]
            self.assertEqual([c.command for c in call_b_args[0]], core_names + ["ask", "transcribe", "doc"])
            self.assertIsInstance(call_b_kwargs["scope"], BotCommandScopeChat)
            self.assertEqual(call_b_kwargs["scope"].chat_id, chat_b)

            # Verify build_help_text shows only enabled modules
            help_a = registry.build_help_text(enabled_a)
            self.assertIn("/summary", help_a)
            self.assertIn("/tasks", help_a)
            self.assertIn("/dl", help_a)
            self.assertNotIn("/ask", help_a)
            self.assertNotIn("/transcribe", help_a)
            self.assertNotIn("/find", help_a)
            self.assertNotIn("/stats", help_a)

            help_b = registry.build_help_text(enabled_b)
            self.assertIn("/ask", help_b)
            self.assertIn("/transcribe", help_b)
            self.assertNotIn("/summary", help_b)
            self.assertNotIn("/dl", help_b)

        run_coro(_run())

    def test_start_ui_keyboard_and_admin_gating_on_callbacks(self) -> None:
        async def _run():
            group_chat_id = -1003333333
            dm_chat_id = 77777

            # Setup custom enabled state for group
            await registry.set_all_modules(group_chat_id, False)
            await registry.set_module_enabled(group_chat_id, "summary", True)

            bot = SimpleNamespace(
                set_my_commands=_AsyncCallableMock(),
                get_chat_member=_AsyncCallableMock(
                    return_value=SimpleNamespace(status=ChatMemberStatus.MEMBER)
                ),
            )
            start_msg = _make_message(
                chat_id=group_chat_id,
                chat_type="supergroup",
                text="/start",
                bot=bot,
            )
            await core_mod.cmd_start_modules(start_msg)

            start_msg.reply.assert_awaited_once()
            reply_args, reply_kwargs = start_msg.reply.call_args
            self.assertIn("Налаштування модулів", reply_args[0])
            kb = reply_kwargs["reply_markup"]
            self.assertIsInstance(kb, InlineKeyboardMarkup)

            all_buttons = [btn for row in kb.inline_keyboard for btn in row]
            callbacks = [btn.callback_data for btn in all_buttons]
            self.assertIn("mod:toggle:summary", callbacks)
            self.assertIn("mod:toggle:voice", callbacks)
            self.assertIn("mod:all:on", callbacks)
            self.assertIn("mod:all:off", callbacks)

            summary_btn = next(b for b in all_buttons if b.callback_data == "mod:toggle:summary")
            voice_btn = next(b for b in all_buttons if b.callback_data == "mod:toggle:voice")
            self.assertTrue(summary_btn.text.startswith("✅"))
            self.assertTrue(voice_btn.text.startswith("❌"))

            # Non-admin in group -> rejected on mod: callback
            non_admin_cb = SimpleNamespace(
                data="mod:toggle:voice",
                from_user=SimpleNamespace(id=500),
                bot=bot,
                message=_make_message(chat_id=group_chat_id, chat_type="supergroup"),
                answer=_AsyncCallableMock(),
            )
            await core_mod.cb_toggle_modules(non_admin_cb)
            non_admin_cb.answer.assert_awaited_once()
            self.assertTrue(non_admin_cb.answer.call_args[1].get("show_alert"))
            self.assertNotIn("voice", await registry.get_enabled_modules(group_chat_id))

            # Admin in group -> succeeds
            admin_bot = SimpleNamespace(
                set_my_commands=_AsyncCallableMock(),
                get_chat_member=_AsyncCallableMock(
                    return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
                ),
            )
            admin_cb = SimpleNamespace(
                data="mod:toggle:voice",
                from_user=SimpleNamespace(id=1),
                bot=admin_bot,
                message=_make_message(chat_id=group_chat_id, chat_type="supergroup"),
                answer=_AsyncCallableMock(),
            )
            await core_mod.cb_toggle_modules(admin_cb)
            self.assertIn("voice", await registry.get_enabled_modules(group_chat_id))
            admin_bot.set_my_commands.assert_awaited_once()
            admin_cb.message.edit_text.assert_awaited_once()

            # DM user -> succeeds without get_chat_member
            dm_bot = SimpleNamespace(
                set_my_commands=_AsyncCallableMock(),
                get_chat_member=_AsyncCallableMock(),
            )
            dm_cb = SimpleNamespace(
                data="mod:all:off",
                from_user=SimpleNamespace(id=dm_chat_id),
                bot=dm_bot,
                message=_make_message(chat_id=dm_chat_id, chat_type="private"),
                answer=_AsyncCallableMock(),
            )
            await core_mod.cb_toggle_modules(dm_cb)
            self.assertEqual(await registry.get_enabled_modules(dm_chat_id), set())
            dm_bot.get_chat_member.assert_not_awaited()
            dm_bot.set_my_commands.assert_awaited_once()

            # mod:all:on in DM
            dm_cb_on = SimpleNamespace(
                data="mod:all:on",
                from_user=SimpleNamespace(id=dm_chat_id),
                bot=dm_bot,
                message=_make_message(chat_id=dm_chat_id, chat_type="private"),
                answer=_AsyncCallableMock(),
            )
            await core_mod.cb_toggle_modules(dm_cb_on)
            self.assertEqual(
                await registry.get_enabled_modules(dm_chat_id),
                set(registry.MODULES.keys()),
            )

        run_coro(_run())


class TestModuleGatingAndLoggingIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        registry.clear_modules_cache()
        commands._user_last_request.clear()
        media_dl_mod._user_last_download.clear()
        self.patchers = [
            patch.object(registry, "async_session_factory", self.session_factory),
            patch.object(group_events, "async_session_factory", self.session_factory),
            patch.object(summary_mod, "async_session_factory", self.session_factory),
            patch.object(ask_mod, "async_session_factory", self.session_factory),
            patch.object(search_mod, "async_session_factory", self.session_factory),
            patch.object(stats_mod, "async_session_factory", self.session_factory),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        registry.clear_modules_cache()
        self.sync_engine.dispose()

    def test_chat_requires_logging_and_log_text_message_skip(self) -> None:
        async def _run():
            chat_id = -1004444444

            # Enable only non-logging modules (ask, media_dl, voice)
            await registry.set_all_modules(chat_id, False)
            await registry.set_module_enabled(chat_id, "ask", True)
            await registry.set_module_enabled(chat_id, "media_dl", True)
            await registry.set_module_enabled(chat_id, "voice", True)

            self.assertFalse(await registry.chat_requires_logging(chat_id))

            msg1 = _make_message(chat_id=chat_id, message_id=10, text="Не повинно зберігатися")
            await group_events.log_text_message(msg1)

            async with self.session_factory() as session:
                rows = (
                    await session.execute(select(ChatLog).where(ChatLog.chat_id == chat_id))
                ).scalars().all()
                self.assertEqual(len(rows), 0)

            # Enabling any logging module (e.g. search) re-enables logging
            await registry.set_module_enabled(chat_id, "search", True)
            self.assertTrue(await registry.chat_requires_logging(chat_id))

            msg2 = _make_message(chat_id=chat_id, message_id=11, text="Тепер зберігається")
            await group_events.log_text_message(msg2)

            async with self.session_factory() as session:
                rows = (
                    await session.execute(select(ChatLog).where(ChatLog.chat_id == chat_id))
                ).scalars().all()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0].message_id, 11)

        run_coro(_run())

    def test_all_commands_return_disabled_notice_without_running_logic(self) -> None:
        async def _run():
            chat_id = -1005555555
            await registry.set_all_modules(chat_id, False)

            bot = SimpleNamespace(
                download=_AsyncCallableMock(),
                get_chat_member=_AsyncCallableMock(),
            )
            fake_ask = _AsyncCallableMock()
            fake_summary = _AsyncCallableMock()
            fake_transcribe = _AsyncCallableMock()
            fake_download = _AsyncCallableMock()

            with (
                patch.object(ask_mod.gemini_service, "ask", fake_ask),
                patch.object(summary_mod.gemini_service, "generate_verified_summary", fake_summary),
                patch.object(voice_mod.gemini_service, "transcribe_media", fake_transcribe),
                patch.object(media_dl_mod, "download_video", fake_download),
            ):
                # /summary
                m_sum = _make_message(chat_id=chat_id, text="/summary 4")
                await summary_mod.cmd_summary(m_sum, SimpleNamespace(args="4"))
                m_sum.reply.assert_awaited_once()
                self.assertIn("вимкнено", m_sum.reply.call_args[0][0].lower())

                # /tasks
                m_tasks = _make_message(chat_id=chat_id, text="/tasks 24")
                await summary_mod.cmd_tasks(m_tasks, SimpleNamespace(args="24"))
                m_tasks.reply.assert_awaited_once()
                self.assertIn("вимкнено", m_tasks.reply.call_args[0][0].lower())

                # /ask
                m_ask = _make_message(chat_id=chat_id, text="/ask Що нового?")
                await ask_mod.cmd_ask(m_ask, bot, SimpleNamespace(args="Що нового?"))
                m_ask.reply.assert_awaited_once()
                self.assertIn("вимкнено", m_ask.reply.call_args[0][0].lower())

                # /dl
                m_dl = _make_message(chat_id=chat_id, text="/dl https://www.tiktok.com/@u/video/123")
                await media_dl_mod.cmd_download_video(
                    m_dl, SimpleNamespace(args="https://www.tiktok.com/@u/video/123")
                )
                m_dl.reply.assert_awaited_once()
                self.assertIn("вимкнено", m_dl.reply.call_args[0][0].lower())

                # /find
                m_find = _make_message(chat_id=chat_id, text="/find реліз")
                await search_mod.cmd_find(m_find, SimpleNamespace(args="реліз"))
                m_find.reply.assert_awaited_once()
                self.assertIn("вимкнено", m_find.reply.call_args[0][0].lower())

                # /stats
                m_stats = _make_message(chat_id=chat_id, text="/stats 24h")
                await stats_mod.cmd_stats(m_stats, SimpleNamespace(args="24h"))
                m_stats.reply.assert_awaited_once()
                self.assertIn("вимкнено", m_stats.reply.call_args[0][0].lower())

                # /transcribe
                voice_reply = _make_message(
                    chat_id=chat_id,
                    voice=SimpleNamespace(file_id="v1", mime_type="audio/ogg", file_size=1024),
                )
                m_voice = _make_message(
                    chat_id=chat_id, text="/transcribe", reply_to_message=voice_reply
                )
                await voice_mod.cmd_transcribe(m_voice, bot)
                m_voice.reply.assert_awaited_once()
                self.assertIn("вимкнено", m_voice.reply.call_args[0][0].lower())

                # /digest
                m_digest = _make_message(chat_id=chat_id, text="/digest on 21:00")
                await digest_mod.cmd_digest(m_digest, SimpleNamespace(args="on 21:00"), bot)
                m_digest.reply.assert_awaited_once()
                self.assertIn("вимкнено", m_digest.reply.call_args[0][0].lower())

                fake_ask.assert_not_awaited()
                fake_summary.assert_not_awaited()
                fake_transcribe.assert_not_awaited()
                fake_download.assert_not_awaited()
                bot.download.assert_not_awaited()

        run_coro(_run())


class TestMediaDownloaderSSRFAndParsing(unittest.TestCase):
    def test_valid_platform_urls_accepted(self) -> None:
        valid_cases = [
            ("https://www.tiktok.com/@creator/video/7123456789012345678", "tiktok"),
            ("https://tiktok.com/@creator/video/7123456789012345678", "tiktok"),
            ("https://vm.tiktok.com/ZM6abc123/", "tiktok"),
            ("https://www.instagram.com/reel/Cx12345abc/", "instagram"),
            ("https://instagram.com/reels/Cx12345abc/", "instagram"),
            ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
            ("https://www.youtube.com/shorts/abc123XYZ", "youtube"),
            ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube"),
        ]
        for url, expected in valid_cases:
            with self.subTest(url=url):
                self.assertEqual(detect_platform(url), expected)
                self.assertTrue(is_supported_video_url(url))

    def test_ssrf_spoofed_and_non_video_urls_rejected(self) -> None:
        rejected_urls = [
            "http://localhost/video",
            "http://127.0.0.1/watch?v=1",
            "http://169.254.169.254/latest/meta-data/",
            "http://[::1]/shorts/123",
            "file:///etc/passwd",
            "ftp://tiktok.com/video/123",
            "https://tiktok.com.evil.com/@u/video/123",
            "https://evil-youtube.com/watch?v=123",
            "https://user:pass@tiktok.com/@u/video/123",
            "https://www.tiktok.com:8080/@u/video/123",
            "https://www.instagram.com/some_profile/",
            "https://www.instagram.com/",
            "https://www.youtube.com/channel/UC12345",
            "https://www.youtube.com/@somechannel",
            "https://youtu.be/",
        ]
        for url in rejected_urls:
            with self.subTest(url=url):
                self.assertIsNone(detect_platform(url))
                self.assertFalse(is_supported_video_url(url))

    def test_extract_auto_download_urls_filters_watch_links(self) -> None:
        sample_text = (
            "Дивіться: https://vm.tiktok.com/ZM6xyz/ "
            "і рілс https://www.instagram.com/reel/AbCd123/, "
            "шортс https://www.youtube.com/shorts/ShOrt999 "
            "але не довге відео https://www.youtube.com/watch?v=LongVid123 "
            "і не https://youtu.be/LongVid456 "
            "і не http://127.0.0.1/secret"
        )
        auto_urls = extract_auto_download_urls(sample_text)
        self.assertEqual(
            auto_urls,
            [
                "https://vm.tiktok.com/ZM6xyz/",
                "https://www.instagram.com/reel/AbCd123/",
                "https://www.youtube.com/shorts/ShOrt999",
            ],
        )

        # extract_supported_urls should still include watch & youtu.be for explicit /dl
        supported_urls = extract_supported_urls(sample_text)
        self.assertIn("https://www.youtube.com/watch?v=LongVid123", supported_urls)
        self.assertIn("https://youtu.be/LongVid456", supported_urls)
        self.assertNotIn("http://127.0.0.1/secret", supported_urls)


class TestNewModulesFunctionality(unittest.TestCase):
    def setUp(self) -> None:
        self.sync_engine, self.session_factory = _make_test_session_factory()
        registry.clear_modules_cache()
        commands._user_last_request.clear()
        self.patchers = [
            patch.object(registry, "async_session_factory", self.session_factory),
            patch.object(search_mod, "async_session_factory", self.session_factory),
            patch.object(stats_mod, "async_session_factory", self.session_factory),
            patch.object(ask_mod, "async_session_factory", self.session_factory),
            patch.object(gemini_client, "async_session_factory", self.session_factory),
            patch.object(group_events, "async_session_factory", self.session_factory),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self) -> None:
        for p in self.patchers:
            p.stop()
        registry.clear_modules_cache()
        self.sync_engine.dispose()

    def test_find_returns_matching_messages_with_clickable_links(self) -> None:
        async def _run():
            chat_id = -1001234567890
            now = utc_now()
            async with self.session_factory() as session:
                session.add(User(user_id=1, username="oksana", full_name="Оксана", updated_at=now))
                session.add_all(
                    [
                        ChatLog(
                            chat_id=chat_id,
                            message_id=201,
                            user_id=1,
                            text="Плануємо деплой бекенду завтра о 10:00",
                            timestamp=now - timedelta(minutes=20),
                        ),
                        ChatLog(
                            chat_id=chat_id,
                            message_id=202,
                            user_id=1,
                            text="Звичайна розмова про каву",
                            timestamp=now - timedelta(minutes=10),
                        ),
                    ]
                )
                await session.commit()

            msg = _make_message(chat_id=chat_id, text="/find деплой бекенду")
            await search_mod.cmd_find(msg, SimpleNamespace(args="деплой бекенду"))

            msg.reply.assert_awaited_once()
            reply_text = msg.reply.call_args[0][0]
            self.assertIn("знайдено 1", reply_text)
            self.assertIn("Оксана", reply_text)
            self.assertIn("деплой бекенду", reply_text)
            self.assertIn("https://t.me/c/1234567890/201", reply_text)
            self.assertNotIn("каву", reply_text)

        run_coro(_run())

    def test_stats_period_top_users_kyiv_peak_hour_and_top_replied(self) -> None:
        async def _run():
            chat_id = -1009876543210
            # Use fixed UTC timestamp 10:15 UTC -> 12:15 Kyiv (in Nov, UTC+2)
            base_now = datetime(2026, 11, 21, 10, 30, 0)
            peak_utc = datetime(2026, 11, 21, 10, 15, 0)
            other_utc = datetime(2026, 11, 21, 8, 5, 0)

            async with self.session_factory() as session:
                session.add_all(
                    [
                        User(user_id=10, username="ivan", full_name="Іван", updated_at=base_now),
                        User(user_id=20, username="olena", full_name="Олена", updated_at=base_now),
                    ]
                )
                session.add_all(
                    [
                        # Root message at 08:05 UTC (10:05 Kyiv)
                        ChatLog(
                            chat_id=chat_id,
                            message_id=300,
                            user_id=10,
                            text="Головне питання про реліз",
                            timestamp=other_utc,
                        ),
                        # 3 replies to #300 at 10:15 UTC (12:15 Kyiv) -> peak hour 12:00-13:00 Kyiv
                        ChatLog(
                            chat_id=chat_id,
                            message_id=301,
                            user_id=20,
                            text="Підтримую!",
                            timestamp=peak_utc,
                            reply_to_message_id=300,
                        ),
                        ChatLog(
                            chat_id=chat_id,
                            message_id=302,
                            user_id=10,
                            text="Ок, готуємось",
                            timestamp=peak_utc + timedelta(minutes=1),
                            reply_to_message_id=300,
                        ),
                        ChatLog(
                            chat_id=chat_id,
                            message_id=303,
                            user_id=10,
                            text="Перевірив тести",
                            timestamp=peak_utc + timedelta(minutes=2),
                            reply_to_message_id=300,
                        ),
                    ]
                )
                session.add(
                    ChatDigest(
                        chat_id=chat_id,
                        digest_text="Дайджест",
                        created_at=base_now,
                    )
                )
                await session.commit()

            with patch.object(stats_mod, "utc_now", return_value=base_now):
                msg = _make_message(chat_id=chat_id, text="/stats 24h")
                await stats_mod.cmd_stats(msg, SimpleNamespace(args="24h"))

            msg.reply.assert_awaited_once()
            out = msg.reply.call_args[0][0]
            self.assertIn("Повідомлень за період: 4", out)
            self.assertIn("Погодинних дайджестів у кеші: 1", out)
            self.assertIn("12:00–13:00 Київ (3 повід.)", out)
            self.assertIn("1. Іван (@ivan) — 3 повід.", out)
            self.assertIn("2. Олена (@olena) — 1 повід.", out)
            self.assertIn("Головне питання про реліз", out)
            self.assertIn("(3 відп.)", out)
            self.assertIn("https://t.me/c/9876543210/300", out)

        run_coro(_run())

    def test_daily_digest_schedule_and_single_daily_trigger(self) -> None:
        async def _run():
            chat_id = -1007778889
            await registry.set_module_enabled(chat_id, "daily_digest", True)

            admin_bot = SimpleNamespace(
                get_chat_member=_AsyncCallableMock(
                    return_value=SimpleNamespace(status=ChatMemberStatus.ADMINISTRATOR)
                ),
                send_message=_AsyncCallableMock(),
            )

            # /digest on 21:00
            m_on = _make_message(chat_id=chat_id, text="/digest on 21:00")
            await digest_mod.cmd_digest(m_on, SimpleNamespace(args="on 21:00"), admin_bot)
            self.assertEqual(await registry.get_daily_digest_time(chat_id), "21:00")

            # Add recent chat message so tick sends digest
            # 2026-11-21 19:00 UTC == 2026-11-21 21:00 Europe/Kyiv (UTC+2)
            fixed_utc = datetime(2026, 11, 21, 19, 0, 0)
            async with self.session_factory() as session:
                session.add(User(user_id=30, username="andrii", full_name="Андрій", updated_at=fixed_utc))
                session.add(
                    ChatLog(
                        chat_id=chat_id,
                        message_id=401,
                        user_id=30,
                        text="Зробили фінальний білд на прод",
                        timestamp=fixed_utc - timedelta(hours=1),
                    )
                )
                await session.commit()

            # Check due before tick
            due_before = await registry.get_chats_due_for_daily_digest("21:00", "2026-11-21")
            self.assertEqual(due_before, [chat_id])

            async def fake_verified_summary(bundle, **kwargs):
                schema = ChatSummarySchema(
                    is_low_signal=False,
                    topics=[
                        TopicItem(
                            title="Реліз",
                            summary="Зробили фінальний білд",
                            participants=["Андрій"],
                            evidence_msg_ids=[401],
                        )
                    ],
                )
                return "1. Головні теми:\n• Реліз (Андрій)", schema

            with (
                patch.object(gemini_client, "utc_now", return_value=fixed_utc),
                patch.object(
                    gemini_client.gemini_service,
                    "generate_verified_summary",
                    side_effect=fake_verified_summary,
                ),
            ):
                # First tick at 21:00 Kyiv -> sends digest
                await run_daily_digest_tick(admin_bot)
                admin_bot.send_message.assert_awaited_once()
                sent_kwargs = admin_bot.send_message.call_args[1]
                self.assertEqual(sent_kwargs["chat_id"], chat_id)
                self.assertIn("Щоденний дайджест", sent_kwargs["text"])

                # Second tick same day -> does NOT send again
                await run_daily_digest_tick(admin_bot)
                self.assertEqual(len(admin_bot.send_message.calls), 1)

            # Next day -> due again
            due_next_day = await registry.get_chats_due_for_daily_digest("21:00", "2026-11-22")
            self.assertEqual(due_next_day, [chat_id])

            # /digest off clears schedule
            m_off = _make_message(chat_id=chat_id, text="/digest off")
            await digest_mod.cmd_digest(m_off, SimpleNamespace(args="off"), admin_bot)
            self.assertIsNone(await registry.get_daily_digest_time(chat_id))
            self.assertEqual(
                await registry.get_chats_due_for_daily_digest("21:00", "2026-11-22"),
                [],
            )

        run_coro(_run())

    def test_gemini_ask_appends_grounding_sources(self) -> None:
        async def _run():
            service = GeminiService()

            chunks = [
                SimpleNamespace(web=SimpleNamespace(title="Python Docs", uri="https://docs.python.org/3/")),
                SimpleNamespace(web=SimpleNamespace(title="Duplicate", uri="https://docs.python.org/3/")),
                SimpleNamespace(web=SimpleNamespace(title="PEP 8", uri="https://peps.python.org/pep-0008/")),
                SimpleNamespace(web=SimpleNamespace(title=None, uri="https://realpython.com/")),
                SimpleNamespace(web=SimpleNamespace(title="Fourth Ignored", uri="https://example.com/4")),
            ]
            fake_response = SimpleNamespace(
                text="Python 3.12 підтримує нові можливості типізації.",
                candidates=[
                    SimpleNamespace(
                        grounding_metadata=SimpleNamespace(grounding_chunks=chunks)
                    )
                ],
            )

            async def fake_generate_content(*, model: str, contents, config):
                return fake_response

            service.client = SimpleNamespace(
                aio=SimpleNamespace(
                    models=SimpleNamespace(generate_content=fake_generate_content)
                )
            )

            answer = await service.ask("Що нового в Python 3.12?")
            self.assertIn("Джерела:", answer)
            self.assertIn("Python Docs — https://docs.python.org/3/", answer)
            self.assertIn("PEP 8 — https://peps.python.org/pep-0008/", answer)
            self.assertIn("• https://realpython.com/", answer)
            # Deduplicated and capped at 3 sources
            self.assertEqual(answer.count("https://docs.python.org/3/"), 1)
            self.assertNotIn("https://example.com/4", answer)

        run_coro(_run())

    def test_ask_multi_turn_reply_chain_pulls_ancestors_from_chat_log(self) -> None:
        async def _run():
            chat_id = -1006667778
            now = utc_now()
            async with self.session_factory() as session:
                session.add(User(user_id=50, username="sofia", full_name="Софія", updated_at=now))
                session.add_all(
                    [
                        ChatLog(
                            chat_id=chat_id,
                            message_id=501,
                            user_id=50,
                            text="Поясни, що таке asyncio?",
                            timestamp=now - timedelta(minutes=5),
                            sender_name="Софія",
                            is_bot_message=False,
                        ),
                        ChatLog(
                            chat_id=chat_id,
                            message_id=502,
                            user_id=None,
                            text="asyncio — це бібліотека для асинхронного коду в Python.",
                            timestamp=now - timedelta(minutes=4),
                            reply_to_message_id=501,
                            sender_name="Бот",
                            is_bot_message=True,
                        ),
                        ChatLog(
                            chat_id=chat_id,
                            message_id=503,
                            user_id=50,
                            text="А як створити задачу?",
                            timestamp=now - timedelta(minutes=3),
                            reply_to_message_id=502,
                            sender_name="Софія",
                            is_bot_message=False,
                        ),
                    ]
                )
                await session.commit()

            replied_msg = _make_message(
                chat_id=chat_id,
                message_id=503,
                user_id=50,
                full_name="Софія",
                text="А як створити задачу?",
            )
            current_msg = _make_message(
                chat_id=chat_id,
                message_id=504,
                user_id=50,
                full_name="Софія",
                text="/ask Наведи короткий приклад",
                reply_to_message=replied_msg,
            )

            full_query = await ask_mod._build_query_with_reply_chain(
                "Наведи короткий приклад", current_msg
            )
            self.assertIn("Контекст попереднього діалогу:", full_query)
            self.assertIn("Користувач (Софія): Поясни, що таке asyncio?", full_query)
            self.assertIn("Бот: asyncio — це бібліотека для асинхронного коду в Python.", full_query)
            self.assertIn("Користувач (Софія): А як створити задачу?", full_query)
            self.assertIn("Поточний запит користувача:\nНаведи короткий приклад", full_query)
            # Chronological order check
            idx1 = full_query.index("Поясни, що таке asyncio?")
            idx2 = full_query.index("бібліотека для асинхронного коду")
            idx3 = full_query.index("А як створити задачу?")
            self.assertLess(idx1, idx2)
            self.assertLess(idx2, idx3)

        run_coro(_run())

    def test_auto_media_filter_and_handler_downloads_without_command(self) -> None:
        async def _run():
            chat_id = -100777888999
            await registry.set_module_enabled(chat_id, "media_dl", True)
            media_dl_mod._user_last_download.clear()

            filt = media_dl_mod.AutoMediaUrlFilter()

            for url in (
                "https://vm.tiktok.com/ZM6xyz/",
                "https://www.instagram.com/reel/AbCd123/",
                "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "https://youtu.be/dQw4w9WgXcQ",
            ):
                msg = _make_message(chat_id=chat_id, text=f"Гляньте {url}")
                msg.entities = []
                msg.caption_entities = []
                self.assertTrue(await filt(msg), f"AutoMediaUrlFilter should match {url}")

            # Masked text_link entity without raw URL in text
            ent_msg = _make_message(chat_id=chat_id, text="Клік сюди")
            ent_msg.entities = [SimpleNamespace(type="text_link", url="https://youtu.be/dQw4w9WgXcQ")]
            ent_msg.caption_entities = []
            self.assertTrue(await filt(ent_msg))

            # Disabled chat must NOT match
            await registry.set_module_enabled(chat_id, "media_dl", False)
            msg_disabled = _make_message(chat_id=chat_id, text="https://youtu.be/dQw4w9WgXcQ")
            msg_disabled.entities = []
            msg_disabled.caption_entities = []
            self.assertFalse(await filt(msg_disabled))

            # Re-enable and verify handler downloads automatically without command
            await registry.set_module_enabled(chat_id, "media_dl", True)
            bot = SimpleNamespace(send_chat_action=AsyncMock())
            msg_dl = _make_message(
                chat_id=chat_id,
                user_id=99,
                text="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                bot=bot,
            )
            msg_dl.entities = []
            msg_dl.caption_entities = []

            fake_media = SimpleNamespace(
                file_path="dummy.mp4",
                title="Test Video",
                duration=12,
                platform="youtube",
                cleanup=AsyncMock(),
            )
            cleanup_called = False

            def _cleanup():
                nonlocal cleanup_called
                cleanup_called = True

            fake_media.cleanup = _cleanup

            with (
                patch.object(media_dl_mod, "download_video", new=AsyncMock(return_value=fake_media)) as mock_dl,
                patch.object(media_dl_mod, "_send_video_safely", new=AsyncMock()) as mock_send,
                patch.object(media_dl_mod, "log_text_message", new=AsyncMock()) as mock_log,
            ):
                await media_dl_mod.handle_auto_media_download(msg_dl)
                mock_log.assert_awaited_once_with(msg_dl)
                mock_dl.assert_awaited_once_with("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
                mock_send.assert_awaited_once_with(msg_dl, fake_media)
                self.assertTrue(cleanup_called)

        run_coro(_run())


if __name__ == "__main__":
    unittest.main()
