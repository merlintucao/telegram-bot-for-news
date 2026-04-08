from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from news_bot.cli import run_status
from news_bot.config import AppConfig
from news_bot.filtering import build_post_filter
from news_bot.image_summary import ImageSummaryError
from news_bot.models import MediaAttachment, SourcePost
from news_bot.routing import build_router
from news_bot.service import NewsBotService, format_post_caption, format_post_message
from news_bot.source_types import SourceError
from news_bot.storage import StateStore
from news_bot.translate import TranslationError


def make_config(db_path: Path) -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("truthsocial_trump",),
        rss_feed_urls=(),
        truthsocial_handle="realDonaldTrump",
        truthsocial_account_id="123",
        truthsocial_base_url="https://truthsocial.com",
        truthsocial_cookies_file=None,
        truthsocial_reload_cookies=True,
        poll_interval_seconds=60,
        request_timeout_seconds=20,
        state_db_path=db_path,
        bootstrap_latest_only=True,
        initial_history_limit=5,
        fetch_limit=10,
        exclude_replies=False,
        exclude_reblogs=False,
        user_agent="test-agent",
        log_level="INFO",
        translation_retry_attempts=3,
        translation_retry_backoff_seconds=0,
        translation_failure_placeholder="Ban dich tam thoi chua san sang.",
    )


def make_post(post_id: str, text: str) -> SourcePost:
    return SourcePost(
        source_id="truthsocial:realDonaldTrump",
        source_name="Truth Social",
        id=post_id,
        account_handle="realDonaldTrump",
        created_at="2026-04-07T08:00:00Z",
        url=f"https://truthsocial.com/@realDonaldTrump/posts/{post_id}",
        body_text=text,
        is_reply=False,
        is_reblog=False,
        media_attachments=(),
        raw_payload={"id": post_id, "content": text},
    )


class FakeClient:
    def __init__(self, responses: list[list[SourcePost]], source_id: str = "truthsocial:realDonaldTrump", source_name: str = "Truth Social") -> None:
        self.responses = responses
        self.source_id = source_id
        self.source_name = source_name
        self.calls: list[tuple[str | None, int | None]] = []

    def fetch_posts(self, since_id: str | None = None, limit: int | None = None) -> list[SourcePost]:
        self.calls.append((since_id, limit))
        return self.responses.pop(0)


class FakeSender:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.posts: list[SourcePost] = []
        self.deliveries: list[tuple[str | None, str]] = []
        self.alerts: list[tuple[str | None, str]] = []

    def send_post(
        self,
        post: SourcePost,
        text: str,
        chat_id: str | None = None,
        media_caption: str | None = None,
    ) -> None:
        self.posts.append(post)
        self.messages.append(text)
        self.deliveries.append((chat_id, post.id))

    def send_message(self, text: str, chat_id: str | None = None) -> None:
        self.alerts.append((chat_id, text))


class FailingClient:
    def __init__(
        self,
        error: Exception,
        source_id: str = "truthsocial:realDonaldTrump",
        source_name: str = "Truth Social",
    ) -> None:
        self.error = error
        self.source_id = source_id
        self.source_name = source_name

    def fetch_posts(self, since_id: str | None = None, limit: int | None = None) -> list[SourcePost]:
        raise self.error


class FlakyClient:
    def __init__(
        self,
        failures_before_success: int,
        responses: list[list[SourcePost]],
        source_id: str = "truthsocial:realDonaldTrump",
        source_name: str = "Truth Social",
        error: Exception | None = None,
    ) -> None:
        self.failures_remaining = failures_before_success
        self.responses = responses
        self.source_id = source_id
        self.source_name = source_name
        self.error = error or SourceError("temporary failure")
        self.calls = 0

    def fetch_posts(self, since_id: str | None = None, limit: int | None = None) -> list[SourcePost]:
        self.calls += 1
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise self.error
        return self.responses.pop(0)


class FakeTranslator:
    def __init__(self, translated_text: str) -> None:
        self.translated_text = translated_text
        self.calls: list[str] = []

    def translate(self, text: str) -> str:
        self.calls.append(text)
        return self.translated_text


class FlakyTranslator:
    def __init__(self, failures_before_success: int, translated_text: str) -> None:
        self.failures_remaining = failures_before_success
        self.translated_text = translated_text
        self.calls: list[str] = []

    def translate(self, text: str) -> str:
        self.calls.append(text)
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise TranslationError("temporary translate failure")
        return self.translated_text


class FailingTranslator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def translate(self, text: str) -> str:
        self.calls.append(text)
        raise TranslationError("translate unavailable")


class FakeImageSummarizer:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.calls: list[list[str]] = []

    def summarize_images(self, image_urls: list[str]) -> str:
        self.calls.append(list(image_urls))
        return self.summary


class FailingImageSummarizer:
    def summarize_images(self, image_urls: list[str]) -> str:
        raise ImageSummaryError("vision unavailable")


class ServiceTests(unittest.TestCase):
    def test_bootstrap_records_latest_without_sending(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            store = StateStore(db_path)
            client = FakeClient([[make_post("200", "latest")]])
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                sleep_fn=lambda seconds: None,
            )

            summary = service.run_once()

            self.assertTrue(summary.bootstrapped)
            self.assertEqual(summary.sent_count, 0)
            self.assertEqual(store.get_last_status_id(client.source_id), "200")
            self.assertEqual(sender.messages, [])

    def test_new_posts_are_sent_in_oldest_first_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            store = StateStore(db_path)
            store.update_checkpoint("truthsocial:realDonaldTrump", "100")
            client = FakeClient([[make_post("102", "second"), make_post("101", "first")]])
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
            )

            summary = service.run_once()

            self.assertFalse(summary.bootstrapped)
            self.assertEqual(summary.sent_count, 2)
            self.assertIn("first", sender.messages[0])
            self.assertIn("second", sender.messages[1])
            self.assertEqual(sender.posts[0].id, "101")
            self.assertEqual(sender.posts[1].id, "102")
            self.assertEqual(sender.deliveries[0][0], "@chat")
            self.assertEqual(store.get_last_status_id(client.source_id), "102")

    def test_first_run_dry_run_does_not_write_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            store = StateStore(db_path)
            client = FakeClient([[make_post("101", "preview")]])
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
            )

            summary = service.run_once(dry_run=True)

            self.assertEqual(summary.fetched_count, 1)
            self.assertIsNone(store.get_last_status_id(client.source_id))
            self.assertEqual(sender.messages, [])

    def test_format_post_message_uses_summary_without_original_link(self) -> None:
        message = format_post_message(make_post("101", "hello world"))
        self.assertTrue(message.startswith("🚨 BREAKING from Donald Trump"))
        self.assertIn(
            "🚨 BREAKING from Donald Trump\n\nPosted: 15:00 07/04/2026",
            message,
        )
        self.assertIn("hello world", message)
        self.assertNotIn("Link:", message)

    def test_format_post_message_includes_vietnamese_translation(self) -> None:
        message = format_post_message(
            make_post("101", "hello world"),
            translated_text="xin chao the gioi",
        )

        self.assertIn("🚨 BREAKING from Donald Trump", message)
        self.assertIn("Ông Donald Trump cho rằng xin chao the gioi.", message)
        self.assertNotIn("hello world", message)
        self.assertNotIn("Link:", message)
        self.assertNotIn("Vietnamese caption:", message)
        self.assertNotIn("Original caption:", message)

    def test_format_post_caption_is_compact_and_summary_only(self) -> None:
        caption = format_post_caption(
            make_post("101", "hello world"),
            translated_text="xin chao the gioi",
        )

        self.assertIn("Posted: 15:00 07/04/2026", caption)
        self.assertIn("Ông Donald Trump cho rằng xin chao the gioi.", caption)
        self.assertNotIn("Link:", caption)

    def test_format_post_message_uses_vietnam_time(self) -> None:
        message = format_post_message(make_post("101", "hello world"))

        self.assertIn("Posted: 15:00 07/04/2026", message)

    def test_format_post_message_summarizes_media_without_listing_urls(self) -> None:
        post = SourcePost(
            source_id="truthsocial:realDonaldTrump",
            source_name="Truth Social",
            id="101",
            account_handle="realDonaldTrump",
            created_at="2026-04-07T08:00:00Z",
            url="https://truthsocial.com/@realDonaldTrump/posts/101",
            body_text="hello world",
            is_reply=False,
            is_reblog=False,
            media_attachments=(
                MediaAttachment(kind="image", url="https://cdn.example.com/a.jpg"),
            ),
            raw_payload={"id": "101"},
        )

        message = format_post_message(post)

        self.assertIn("The post includes 1 image.", message)
        self.assertNotIn("https://cdn.example.com/a.jpg", message)

    def test_format_post_message_uses_attachment_descriptions_for_media_summary(self) -> None:
        post = SourcePost(
            source_id="truthsocial:realDonaldTrump",
            source_name="Truth Social",
            id="101",
            account_handle="realDonaldTrump",
            created_at="2026-04-07T08:00:00Z",
            url="https://truthsocial.com/@realDonaldTrump/posts/101",
            body_text="hello world",
            is_reply=False,
            is_reblog=False,
            media_attachments=(
                MediaAttachment(
                    kind="image",
                    url="https://cdn.example.com/a.jpg",
                    description="Portrait artwork with a U.S. flag over Trump's face",
                ),
            ),
            raw_payload={"id": "101"},
        )

        message = format_post_message(post)

        self.assertIn("Image summary: Portrait artwork with a U.S. flag over Trump's face", message)

    def test_image_summary_is_added_from_image_summarizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
                translation_retry_attempts=config.translation_retry_attempts,
                translation_retry_backoff_seconds=0,
                translation_failure_placeholder=config.translation_failure_placeholder,
            )
            store = StateStore(db_path)
            post = SourcePost(
                source_id="truthsocial:realDonaldTrump",
                source_name="Truth Social",
                id="101",
                account_handle="realDonaldTrump",
                created_at="2026-04-07T08:00:00Z",
                url="https://truthsocial.com/@realDonaldTrump/posts/101",
                body_text="hello world",
                is_reply=False,
                is_reblog=False,
                media_attachments=(
                    MediaAttachment(
                        kind="image",
                        url="https://cdn.example.com/a.jpg",
                        preview_url="https://cdn.example.com/a-preview.jpg",
                    ),
                ),
                raw_payload={"id": "101"},
            )
            client = FakeClient([[post]])
            sender = FakeSender()
            translator = FakeTranslator("xin chao the gioi")
            image_summarizer = FakeImageSummarizer("Buc anh cho thay chan dung co quoc ky My.")
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                sleep_fn=lambda seconds: None,
                translator=translator,
                image_summarizer=image_summarizer,
            )

            summary = service.run_once()

            self.assertEqual(summary.sent_count, 1)
            self.assertEqual(
                image_summarizer.calls,
                [["https://cdn.example.com/a-preview.jpg"]],
            )
            self.assertIn("Hinh anh cho thay: Buc anh cho thay chan dung co quoc ky My.", sender.messages[0])
            self.assertNotIn("The post includes 1 image.", sender.messages[0])

    def test_video_does_not_use_image_summarizer(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
                translation_retry_attempts=1,
                translation_retry_backoff_seconds=0,
                translation_failure_placeholder=config.translation_failure_placeholder,
            )
            store = StateStore(db_path)
            post = SourcePost(
                source_id="truthsocial:realDonaldTrump",
                source_name="Truth Social",
                id="101",
                account_handle="realDonaldTrump",
                created_at="2026-04-07T08:00:00Z",
                url="https://truthsocial.com/@realDonaldTrump/posts/101",
                body_text="hello world",
                is_reply=False,
                is_reblog=False,
                media_attachments=(
                    MediaAttachment(kind="video", url="https://cdn.example.com/a.mp4"),
                ),
                raw_payload={"id": "101"},
            )
            client = FakeClient([[post]])
            sender = FakeSender()
            image_summarizer = FakeImageSummarizer("Khong duoc dung")
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                sleep_fn=lambda seconds: None,
                translator=FailingTranslator(),
                image_summarizer=image_summarizer,
            )

            summary = service.run_once()

            self.assertEqual(summary.sent_count, 1)
            self.assertEqual(image_summarizer.calls, [])
            self.assertIn("Bai dang co kem video hoac tep media.", sender.messages[0])

    def test_format_post_message_summarizes_link_context(self) -> None:
        post = make_post(
            "101",
            "Two more major pharmaceutical companies to launch products through TrumpRx: https://justthenews.com/story",
        )

        message = format_post_message(post)

        self.assertIn("Ông Donald Trump cho rằng Two more major pharmaceutical companies to launch products through TrumpRx.", message)
        self.assertIn("Link summary: Two more major pharmaceutical companies to launch products through TrumpRx:", message)

    def test_format_post_message_keeps_multiple_sentences_for_long_story(self) -> None:
        post = make_post(
            "101",
            (
                "A big day for World Peace! Iran wants it to happen, they've had enough! "
                "The United States of America will be helping with the traffic buildup in the Strait of Hormuz. "
                "Iran can start the reconstruction process. "
                "This could be the Golden Age of the Middle East!"
            ),
        )

        message = format_post_message(
            post,
            translated_text=(
                "Một ngày trọng đại cho hòa bình thế giới! Iran muốn điều đó xảy ra, họ đã chịu đủ rồi! "
                "Hoa Kỳ sẽ hỗ trợ tình trạng ùn tắc giao thông tại eo biển Hormuz. "
                "Iran có thể bắt đầu quá trình tái thiết. "
                "Đây có thể là thời kỳ hoàng kim của Trung Đông!"
            ),
        )

        self.assertIn("Ông Donald Trump cho rằng Một ngày trọng đại cho hòa bình thế giới.", message)
        self.assertIn("Iran muốn điều đó xảy ra, họ đã chịu đủ rồi!", message)
        self.assertIn("Hoa Kỳ sẽ hỗ trợ tình trạng ùn tắc giao thông tại eo biển Hormuz.", message)
        self.assertNotIn("Iran có thể bắt đầu quá trình tái thiết.", message)

    def test_translation_is_applied_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
                translation_retry_attempts=config.translation_retry_attempts,
                translation_retry_backoff_seconds=config.translation_retry_backoff_seconds,
                translation_failure_placeholder=config.translation_failure_placeholder,
            )
            store = StateStore(db_path)
            client = FakeClient([[make_post("102", "hello world")]])
            sender = FakeSender()
            translator = FakeTranslator("xin chao the gioi")
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                translator=translator,
            )

            summary = service.run_once()

            self.assertEqual(summary.sent_count, 1)
            self.assertEqual(translator.calls, ["hello world"])
            self.assertIn("🚨 BREAKING from Donald Trump", sender.messages[0])
            self.assertIn("Ông Donald Trump cho rằng xin chao the gioi.", sender.messages[0])
            self.assertNotIn("hello world", sender.messages[0])

    def test_translation_retries_before_delivery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
                translation_retry_attempts=3,
                translation_retry_backoff_seconds=0,
                translation_failure_placeholder=config.translation_failure_placeholder,
            )
            store = StateStore(db_path)
            client = FakeClient([[make_post("102", "hello world")]])
            sender = FakeSender()
            translator = FlakyTranslator(2, "xin chao the gioi")
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                sleep_fn=lambda seconds: None,
                translator=translator,
            )

            summary = service.run_once()

            self.assertEqual(summary.sent_count, 1)
            self.assertEqual(len(translator.calls), 3)
            self.assertIn("xin chao the gioi", sender.messages[0])

    def test_translation_failure_uses_vietnamese_placeholder_and_hides_english(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
                translation_retry_attempts=2,
                translation_retry_backoff_seconds=0,
                translation_failure_placeholder="Ban dich tam thoi chua san sang.",
            )
            store = StateStore(db_path)
            post = SourcePost(
                source_id="truthsocial:realDonaldTrump",
                source_name="Truth Social",
                id="101",
                account_handle="realDonaldTrump",
                created_at="2026-04-07T08:00:00Z",
                url="https://truthsocial.com/@realDonaldTrump/posts/101",
                body_text="hello world https://example.com/story",
                is_reply=False,
                is_reblog=False,
                media_attachments=(
                    MediaAttachment(kind="image", url="https://cdn.example.com/a.jpg"),
                ),
                raw_payload={"id": "101"},
            )
            client = FakeClient([[post]])
            sender = FakeSender()
            translator = FailingTranslator()
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                sleep_fn=lambda seconds: None,
                translator=translator,
            )

            summary = service.run_once()

            self.assertEqual(summary.sent_count, 1)
            self.assertIn("Ban dich tam thoi chua san sang.", sender.messages[0])
            self.assertIn("Bai dang co kem lien ket lien quan.", sender.messages[0])
            self.assertIn("Bai dang co kem hinh anh lien quan.", sender.messages[0])
            self.assertNotIn("hello world", sender.messages[0])
            self.assertNotIn("The post includes 1 image.", sender.messages[0])

    def test_multiple_sources_are_processed_independently(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            store = StateStore(db_path)
            trump = FakeClient(
                [[make_post("201", "trump update")]],
                source_id="truthsocial:realDonaldTrump",
                source_name="Truth Social",
            )
            newswire_post = SourcePost(
                source_id="rss:ap",
                source_name="AP News",
                id="301",
                account_handle="ap",
                created_at="2026-04-07T09:00:00Z",
                url="https://example.com/ap/301",
                body_text="wire story",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "301"},
            )
            newswire = FakeClient([[newswire_post]], source_id="rss:ap", source_name="AP News")
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [trump, newswire],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
            )

            summary = service.run_once(dry_run=True)

            self.assertEqual(summary.fetched_count, 2)
            self.assertEqual(summary.sources_processed, 2)
            self.assertEqual(sender.messages, [])

    def test_format_post_message_for_rss_story_avoids_duplicate_source_name(self) -> None:
        post = SourcePost(
            source_id="rss:example",
            source_name="Example News",
            id="story-1",
            account_handle="Example News",
            created_at="2026-04-07T08:00:00Z",
            url="https://example.com/story-1",
            body_text="Headline",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-1"},
        )

        message = format_post_message(post)

        self.assertTrue(message.startswith("Example News story"))
        self.assertNotIn("from Example News", message)

    def test_source_routes_can_override_and_broadcast(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=(
                    "truthsocial:*=@truths_only",
                    "rss:ap=@ap_main|@ap_backup",
                ),
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
            )
            store = StateStore(db_path)
            trump = FakeClient([[make_post("201", "trump update")]])
            ap_post = SourcePost(
                source_id="rss:ap",
                source_name="AP News",
                id="story-301",
                account_handle="AP News",
                created_at="2026-04-07T09:00:00Z",
                url="https://example.com/ap/301",
                body_text="wire story",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "story-301"},
            )
            ap = FakeClient([[ap_post]], source_id="rss:ap", source_name="AP News")
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [trump, ap],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
            )

            summary = service.run_once()

            self.assertEqual(summary.sent_count, 2)
            self.assertEqual(
                sender.deliveries,
                [
                    ("@truths_only", "201"),
                    ("@ap_main", "story-301"),
                    ("@ap_backup", "story-301"),
                ],
            )

    def test_keyword_filter_only_delivers_matching_posts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=("truthsocial:*=trade|border",),
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
            )
            store = StateStore(db_path)
            client = FakeClient(
                [[make_post("101", "hello world"), make_post("102", "trade update")]]
            )
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
            )

            summary = service.run_once()

            self.assertEqual(summary.sent_count, 1)
            self.assertEqual(summary.filtered_count, 1)
            self.assertEqual(sender.deliveries, [("@chat", "102")])
            self.assertEqual(store.get_last_status_id(client.source_id), "102")

    def test_category_filter_matches_rss_categories(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=("rss:*=politics|world",),
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
            )
            store = StateStore(db_path)
            wire_post = SourcePost(
                source_id="rss:ap",
                source_name="AP News",
                id="story-1",
                account_handle="AP News",
                created_at="2026-04-07T08:00:00Z",
                url="https://example.com/story-1",
                body_text="Political story",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "story-1"},
                categories=("Politics",),
            )
            sports_post = SourcePost(
                source_id="rss:ap",
                source_name="AP News",
                id="story-2",
                account_handle="AP News",
                created_at="2026-04-07T09:00:00Z",
                url="https://example.com/story-2",
                body_text="Sports story",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "story-2"},
                categories=("Sports",),
            )
            client = FakeClient([[wire_post, sports_post]], source_id="rss:ap", source_name="AP News")
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
            )

            summary = service.run_once()

            self.assertEqual(summary.sent_count, 1)
            self.assertEqual(summary.filtered_count, 1)
            self.assertEqual(sender.deliveries, [("@chat", "story-1")])
            self.assertEqual(store.get_last_status_id(client.source_id), "story-2")

    def test_status_snapshot_includes_recent_run_and_filtered_event(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=("truthsocial:*=trade",),
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
            )
            store = StateStore(db_path)
            client = FakeClient(
                [[make_post("101", "hello world"), make_post("102", "trade update")]]
            )
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
            )

            summary = service.run_once()
            recent_runs = store.get_recent_runs(limit=1)
            source_statuses = store.get_source_statuses(filtered_limit=2)

            self.assertEqual(summary.sent_count, 1)
            self.assertEqual(summary.filtered_count, 1)
            self.assertEqual(len(recent_runs), 1)
            self.assertEqual(recent_runs[0].status, "ok")
            self.assertEqual(recent_runs[0].sent_count, 1)
            self.assertEqual(recent_runs[0].filtered_count, 1)
            self.assertEqual(summary.failed_sources, 0)
            self.assertEqual(len(source_statuses), 1)
            self.assertEqual(source_statuses[0].checkpoint_id, "102")
            self.assertEqual(source_statuses[0].last_delivered.status_id, "102")
            self.assertIsNone(source_statuses[0].last_error)
            self.assertEqual(source_statuses[0].consecutive_failures, 0)
            self.assertIsNotNone(source_statuses[0].last_success_at)
            self.assertEqual(source_statuses[0].recent_filtered[0].status_id, "101")

    def test_source_error_is_recorded_in_status_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
            )
            store = StateStore(db_path)
            client = FailingClient(SourceError("upstream unavailable"))
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
            )

            summary = service.run_once()

            recent_runs = store.get_recent_runs(limit=1)
            source_statuses = store.get_source_statuses(filtered_limit=1)

            self.assertEqual(summary.failed_sources, 1)
            self.assertEqual(summary.sources_processed, 0)
            self.assertEqual(len(recent_runs), 1)
            self.assertEqual(recent_runs[0].status, "error")
            self.assertIn("truthsocial:realDonaldTrump", recent_runs[0].error_message)
            self.assertIn("SourceError: upstream unavailable", recent_runs[0].error_message)
            self.assertEqual(len(source_statuses), 1)
            self.assertEqual(source_statuses[0].source_key, client.source_id)
            self.assertIsNone(source_statuses[0].checkpoint_id)
            self.assertIsNotNone(source_statuses[0].last_error)
            self.assertEqual(source_statuses[0].last_error.event_type, "error")
            self.assertIn("SourceError: upstream unavailable", source_statuses[0].last_error.detail)
            self.assertEqual(source_statuses[0].consecutive_failures, 1)
            self.assertIsNone(source_statuses[0].last_success_at)
            self.assertIsNone(source_statuses[0].last_alerted_at)

    def test_source_retry_recovers_without_counting_failure(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
                source_retry_attempts=3,
                source_retry_backoff_seconds=1,
            )
            store = StateStore(db_path)
            client = FlakyClient(
                failures_before_success=1,
                responses=[[make_post("101", "recovered")]],
            )
            sender = FakeSender()
            slept: list[float] = []
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                sleep_fn=slept.append,
            )

            summary = service.run_once()
            recent_runs = store.get_recent_runs(limit=1)
            source_statuses = store.get_source_statuses(filtered_limit=1)

            self.assertEqual(client.calls, 2)
            self.assertEqual(slept, [1.0])
            self.assertEqual(summary.sent_count, 1)
            self.assertEqual(summary.failed_sources, 0)
            self.assertEqual(recent_runs[0].status, "ok")
            self.assertIsNone(source_statuses[0].last_error)
            self.assertEqual(source_statuses[0].consecutive_failures, 0)

    def test_continue_on_source_error_keeps_other_sources_running(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=("truthsocial_trump", "rss"),
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
                continue_on_source_error=True,
                source_retry_attempts=2,
                source_retry_backoff_seconds=0,
            )
            store = StateStore(db_path)
            failing = FailingClient(SourceError("truthsocial unavailable"))
            rss_post = SourcePost(
                source_id="rss:ap",
                source_name="AP News",
                id="story-9",
                account_handle="AP News",
                created_at="2026-04-07T09:00:00Z",
                url="https://example.com/ap/9",
                body_text="wire story",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "story-9"},
            )
            healthy = FakeClient([[rss_post]], source_id="rss:ap", source_name="AP News")
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [failing, healthy],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                sleep_fn=lambda seconds: None,
            )

            summary = service.run_once()
            recent_runs = store.get_recent_runs(limit=1)
            source_statuses = {status.source_key: status for status in store.get_source_statuses(filtered_limit=1)}

            self.assertEqual(summary.failed_sources, 1)
            self.assertEqual(summary.sources_processed, 1)
            self.assertEqual(summary.sent_count, 1)
            self.assertEqual(sender.deliveries, [("@chat", "story-9")])
            self.assertEqual(recent_runs[0].status, "degraded")
            self.assertIn("truthsocial:realDonaldTrump", recent_runs[0].error_message)
            self.assertEqual(source_statuses["truthsocial:realDonaldTrump"].consecutive_failures, 1)
            self.assertEqual(source_statuses["rss:ap"].consecutive_failures, 0)

    def test_failure_alert_triggers_once_per_streak_and_resets_after_success(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
                telegram_alert_chat_id="@ops",
                source_failure_alert_threshold=2,
                continue_on_source_error=False,
            )
            store = StateStore(db_path)
            sender = FakeSender()
            failing_service = NewsBotService(
                config,
                store,
                [FailingClient(SourceError("upstream unavailable"))],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                sleep_fn=lambda seconds: None,
            )

            with self.assertRaisesRegex(SourceError, "upstream unavailable"):
                failing_service.run_once()
            self.assertEqual(sender.alerts, [])

            with self.assertRaisesRegex(SourceError, "upstream unavailable"):
                failing_service.run_once()
            self.assertEqual(len(sender.alerts), 1)
            self.assertEqual(sender.alerts[0][0], "@ops")
            self.assertIn("Consecutive failures: 2", sender.alerts[0][1])

            with self.assertRaisesRegex(SourceError, "upstream unavailable"):
                failing_service.run_once()
            self.assertEqual(len(sender.alerts), 1)
            self.assertEqual(store.get_source_statuses(filtered_limit=1)[0].consecutive_failures, 3)

            success_service = NewsBotService(
                config,
                store,
                [FakeClient([[]])],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                sleep_fn=lambda seconds: None,
            )
            success_service.run_once()
            status_after_success = store.get_source_statuses(filtered_limit=1)[0]
            self.assertEqual(len(sender.alerts), 2)
            self.assertEqual(sender.alerts[1][0], "@ops")
            self.assertIn("Source recovered", sender.alerts[1][1])
            self.assertIn("Recovered after failures: 3", sender.alerts[1][1])
            self.assertEqual(status_after_success.consecutive_failures, 0)
            self.assertIsNotNone(status_after_success.last_success_at)
            self.assertIsNone(status_after_success.last_alerted_at)

            with self.assertRaisesRegex(SourceError, "upstream unavailable"):
                failing_service.run_once()
            with self.assertRaisesRegex(SourceError, "upstream unavailable"):
                failing_service.run_once()

            self.assertEqual(len(sender.alerts), 3)
            self.assertIn("Consecutive failures: 2", sender.alerts[2][1])

    def test_status_json_output_is_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=("truthsocial:*=trade",),
                source_category_filters=config.source_category_filters,
                enabled_sources=config.enabled_sources,
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_handle=config.truthsocial_handle,
                truthsocial_account_id=config.truthsocial_account_id,
                truthsocial_base_url=config.truthsocial_base_url,
                truthsocial_cookies_file=config.truthsocial_cookies_file,
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=False,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
            )
            store = StateStore(db_path)
            client = FakeClient(
                [[make_post("101", "hello world"), make_post("102", "trade update")]]
            )
            sender = FakeSender()
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
            )
            service.run_once()

            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = run_status(config, limit=2, as_json=True)

            payload = json.loads(output.getvalue())

            self.assertEqual(exit_code, 0)
            self.assertEqual(payload["runs"][0]["status"], "ok")
            self.assertEqual(payload["runs"][0]["sent_count"], 1)
            self.assertEqual(payload["sources"][0]["source_key"], "truthsocial:realDonaldTrump")
            self.assertEqual(payload["sources"][0]["checkpoint_id"], "102")
            self.assertEqual(payload["sources"][0]["last_delivered"]["status_id"], "102")
            self.assertIsNone(payload["sources"][0]["last_error"])
            self.assertEqual(payload["sources"][0]["consecutive_failures"], 0)
            self.assertIsNotNone(payload["sources"][0]["last_success_at"])
            self.assertIsNone(payload["sources"][0]["last_alerted_at"])
            self.assertEqual(payload["sources"][0]["recent_filtered"][0]["status_id"], "101")


if __name__ == "__main__":
    unittest.main()
