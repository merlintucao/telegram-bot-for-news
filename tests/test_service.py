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
from news_bot.service import (
    NewsBotService,
    _summarize_caption,
    format_post_caption,
    format_post_message,
)
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
        truthsocial_fallback_feed_urls=(),
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


class EchoTranslator:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def translate(self, text: str) -> str:
        self.calls.append(text)
        return text


class UnchangedThenTranslatedTranslator:
    def __init__(self, translated_text: str) -> None:
        self.translated_text = translated_text
        self.calls: list[str] = []
        self.return_original = True

    def translate(self, text: str) -> str:
        self.calls.append(text)
        if self.return_original:
            self.return_original = False
            return text
        return self.translated_text


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
            "🚨 BREAKING from Donald Trump\nPosted: 15:00 07/04/2026",
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
        self.assertIn("Ông Donald Trump cho biết xin chao the gioi.", message)
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
        self.assertIn("Ông Donald Trump cho biết xin chao the gioi.", caption)
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

        self.assertNotIn("Bai dang kem 1 hinh anh.", message)
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
            self.assertNotIn("Bai dang kem 1 hinh anh.", sender.messages[0])

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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
            self.assertNotIn("Bai dang co kem video hoac tep media.", sender.messages[0])

    def test_format_post_message_does_not_include_link_context(self) -> None:
        post = make_post(
            "101",
            "Two more major pharmaceutical companies to launch products through TrumpRx: https://justthenews.com/story",
        )

        message = format_post_message(post)

        self.assertIn("Ông Donald Trump cho biết Two more major pharmaceutical companies to launch products through TrumpRx.", message)
        self.assertNotIn("Link summary:", message)

    def test_format_post_message_skips_tco_fallback_link_summary(self) -> None:
        post = SourcePost(
            source_id="x:kobeissiletter",
            source_name="The Kobeissi Letter",
            id="story-x-tco-1",
            account_handle="KobeissiLetter",
            created_at="2026-04-12T02:39:00Z",
            url="https://x.com/KobeissiLetter/status/999",
            body_text="Bitcoin is dropping sharply after US-Iran talks failed. https://t.co/example",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-x-tco-1", "text": "Bitcoin is dropping sharply after US-Iran talks failed. https://t.co/example"},
        )

        message = format_post_message(
            post,
            translated_text="Bitcoin giảm mạnh do các cuộc đàm phán Mỹ-Iran thất bại.",
        )

        self.assertNotIn("Link summary:", message)

    def test_format_post_message_skips_trivial_rt_summary_and_link(self) -> None:
        post = SourcePost(
            source_id="truthsocial:realDonaldTrump",
            source_name="Truth Social",
            id="story-trump-rt-1",
            account_handle="realDonaldTrump",
            created_at="2026-04-12T04:53:00Z",
            url="https://truthsocial.com/@realDonaldTrump/posts/999",
            body_text="RT https://example.com/story",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={
                "id": "story-trump-rt-1",
                "card": {"title": "RT"},
            },
        )

        message = format_post_message(
            post,
            translated_text="RT",
        )

        self.assertNotIn("Ông Donald Trump cho biết RT.", message)
        self.assertNotIn("Link summary: RT.", message)

    def test_format_post_message_preserves_rt_pcr_term(self) -> None:
        post = SourcePost(
            source_id="truthsocial:realDonaldTrump",
            source_name="Truth Social",
            id="story-trump-rt-pcr-1",
            account_handle="realDonaldTrump",
            created_at="2026-04-12T12:53:12.256Z",
            url="https://truthsocial.com/@realDonaldTrump/posts/116391830634836371",
            body_text=(
                "RT-PCR demand is rising as countries expand testing capacity."
            ),
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-trump-rt-pcr-1"},
        )

        message = format_post_message(
            post,
            translated_text="RT-PCR tăng mạnh khi các quốc gia mở rộng năng lực xét nghiệm.",
        )

        self.assertIn("Ông Donald Trump cho biết RT-PCR tăng mạnh khi các quốc gia mở rộng năng lực xét nghiệm.", message)

    def test_format_post_message_uses_card_description_when_title_is_junk(self) -> None:
        post = SourcePost(
            source_id="truthsocial:realDonaldTrump",
            source_name="Truth Social",
            id="story-trump-card-1",
            account_handle="realDonaldTrump",
            created_at="2026-04-12T04:53:00Z",
            url="https://truthsocial.com/@realDonaldTrump/posts/1000",
            body_text="RT https://example.com/story",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={
                "id": "story-trump-card-1",
                "card": {
                    "title": "RT",
                    "description": "Trump signals a possible naval blockade if Iran refuses terms",
                },
            },
        )

        message = format_post_message(
            post,
            translated_text="RT",
        )

        self.assertNotIn("Link summary:", message)

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

        self.assertIn("Ông Donald Trump cho biết Một ngày trọng đại cho hòa bình thế giới.", message)
        self.assertIn("Iran muốn điều đó xảy ra, họ đã chịu đủ rồi.", message)
        self.assertNotIn("Ông cũng nói", message)
        self.assertIn("Ông nhấn mạnh Hoa Kỳ sẽ hỗ trợ tình trạng ùn tắc giao thông tại eo biển Hormuz.", message)
        self.assertNotIn("Iran có thể bắt đầu quá trình tái thiết.", message)

    def test_format_post_message_keeps_warning_and_context_for_major_claim(self) -> None:
        post = make_post(
            "101",
            "A whole civilization will die tonight, never to be brought back again. I don't want that to happen, but it probably will. "
            "However, now that we have Complete and Total Regime Change, where different, smarter, and less radicalized minds prevail, "
            "maybe something revolutionarily wonderful can happen.",
        )

        message = format_post_message(
            post,
            translated_text=(
                "Cả một nền văn minh sẽ chết tối nay, không bao giờ có thể quay trở lại được nữa. "
                "Tôi không muốn điều đó xảy ra, nhưng có lẽ nó sẽ xảy ra. "
                "Tuy nhiên, giờ đây khi đã có thay đổi chế độ hoàn toàn, với những tư duy khác biệt, thông minh hơn và bớt cực đoan hơn, "
                "có thể sẽ có điều tích cực mang tính bước ngoặt xảy ra."
            ),
        )

        self.assertIn("Ông Donald Trump cho biết Cả một nền văn minh sẽ chết tối nay, không bao giờ có thể quay trở lại được nữa.", message)
        self.assertIn("giờ đây khi đã có thay đổi chế độ hoàn toàn", message)
        self.assertNotIn("Ông cũng nói", message)

    def test_format_post_message_keeps_action_threat_and_terms_for_military_post(self) -> None:
        post = make_post(
            "101",
            (
                "All U.S. Ships, Aircraft, and Military Personnel, with additional Ammunition, Weaponry, and anything else that is appropriate and necessary, "
                "will remain in place in, and around, Iran, until such time as the REAL AGREEMENT reached is fully complied with. "
                "If for any reason it is not, then the Shootin' Starts, bigger, and better, and stronger than anyone has ever seen before. "
                "It was agreed, a long time ago - NO NUCLEAR WEAPONS and, the Strait of Hormuz WILL BE OPEN & SAFE."
            ),
        )

        message = format_post_message(
            post,
            translated_text=(
                "Tất cả tàu, máy bay và quân nhân Mỹ, cùng với thêm đạn dược, vũ khí và mọi thứ cần thiết, sẽ tiếp tục ở trong và xung quanh Iran "
                "cho đến khi thỏa thuận thực sự đạt được được tuân thủ đầy đủ. "
                "Nếu vì bất kỳ lý do nào điều đó không xảy ra, thì tiếng súng sẽ bắt đầu trở lại với quy mô lớn hơn và mạnh hơn trước. "
                "Điều này đã được thống nhất từ lâu - không có vũ khí hạt nhân và eo biển Hormuz sẽ mở và an toàn."
            ),
        )

        self.assertIn("Ông Donald Trump cho biết lực lượng và khí tài Mỹ sẽ tiếp tục hiện diện quanh Iran cho đến khi thỏa thuận được tuân thủ đầy đủ.", message)
        self.assertIn("Ông cảnh báo Nếu vì bất kỳ lý do nào điều đó không xảy ra, thì tiếng súng sẽ bắt đầu trở lại với quy mô lớn hơn và mạnh hơn trước.", message)
        self.assertIn("Ông nhấn mạnh không có vũ khí hạt nhân; eo biển Hormuz phải luôn mở và an toàn.", message)

    def test_format_post_message_rewrites_machine_translated_military_post_into_natural_vietnamese(self) -> None:
        post = make_post(
            "101",
            (
                "All U.S. Ships, Aircraft, and Military Personnel, with additional Ammunition, Weaponry, and anything else that is appropriate and necessary for the lethal prosecution and destruction of an already substantially degraded Enemy, will remain in place in, and around, Iran, until such time as the REAL AGREEMENT reached is fully complied with. "
                "If for any reason it is not, which is highly unlikely, then the “Shootin’ Starts,” bigger, and better, and stronger than anyone has ever seen before. "
                "It was agreed, a long time ago, and despite all of the fake rhetoric to the contrary - NO NUCLEAR WEAPONS and, the Strait of Hormuz WILL BE OPEN & SAFE. "
                "In the meantime our great Military is Loading Up and Resting, looking forward, actually, to its next Conquest. AMERICA IS BACK!"
            ),
        )

        message = format_post_message(
            post,
            translated_text=(
                "Tất cả các Tàu, Máy bay và Nhân viên Quân sự của Hoa Kỳ, cùng với Đạn dược, Vũ khí bổ sung và bất kỳ thứ gì khác phù hợp và cần thiết cho việc truy tố và tiêu diệt Kẻ thù vốn đã suy thoái đáng kể, sẽ vẫn tồn tại trong và xung quanh Iran, cho đến khi THỎA THUẬN THỰC SỰ đạt được được tuân thủ đầy đủ. "
                "Nếu vì bất kỳ lý do gì mà điều đó không xảy ra, điều này rất khó xảy ra, thì “Shootin' Starts”, lớn hơn, tốt hơn và mạnh mẽ hơn bất kỳ ai từng thấy trước đây. "
                "Nó đã được đồng ý từ lâu, và bất chấp tất cả những lời hoa mỹ giả tạo ngược lại - KHÔNG CÓ VŨ KHÍ HẠT NHÂN và eo biển Hormuz SẼ MỞ & AN TOÀN. "
                "Trong khi chờ đợi, Quân đội vĩ đại của chúng ta đang chuẩn bị và nghỉ ngơi, thực sự đang mong chờ Cuộc chinh phục tiếp theo. MỸ ĐÃ TRỞ LẠI!"
            ),
        )

        self.assertIn("Ông Donald Trump cho biết lực lượng và khí tài Mỹ sẽ tiếp tục hiện diện quanh Iran cho đến khi thỏa thuận được tuân thủ đầy đủ.", message)
        self.assertIn("Ông cảnh báo nếu thỏa thuận không được tuân thủ, giao tranh sẽ bùng phát trở lại ở quy mô lớn hơn.", message)
        self.assertIn("Ông nhấn mạnh không có vũ khí hạt nhân; eo biển Hormuz phải luôn mở và an toàn.", message)
        self.assertNotIn("Tất cả các Tàu, Máy bay", message)
        self.assertNotIn("MỸ ĐÃ TRỞ LẠI", message)

    def test_format_post_message_keeps_earlier_threat_when_main_claim_comes_later(self) -> None:
        post = make_post(
            "101",
            (
                "If Iran violates the deal, fighting will resume immediately. "
                "The United States will keep forces around Iran until the agreement is fully complied with. "
                "No nuclear weapons will be allowed."
            ),
        )

        message = format_post_message(
            post,
            translated_text=(
                "Nếu Iran vi phạm thỏa thuận, giao tranh sẽ bùng phát trở lại ngay lập tức. "
                "Mỹ sẽ duy trì lực lượng quanh Iran cho đến khi thỏa thuận được tuân thủ đầy đủ. "
                "Không có vũ khí hạt nhân."
            ),
        )

        self.assertIn("Ông Donald Trump cho biết Mỹ sẽ duy trì lực lượng quanh Iran cho đến khi thỏa thuận được tuân thủ đầy đủ.", message)
        self.assertIn("Ông cảnh báo Nếu Iran vi phạm thỏa thuận, giao tranh sẽ bùng phát trở lại ngay lập tức.", message)
        self.assertIn("Ông nhấn mạnh Không có vũ khí hạt nhân.", message)

    def test_format_post_message_for_trump_numbered_list_keeps_multiple_items(self) -> None:
        post = make_post(
            "101",
            (
                "BREAKING: Initial details are emerging as the US and Iran conduct their first direct meeting since 1979.\n\n"
                "Details include:\n\n"
                "1. The Strait of Hormuz remains a point of serious disagreement\n\n"
                "2. US military says 2 US warships have transited the Strait of Hormuz today\n\n"
                "3. Talks are expected to continue tonight and may extend into tomorrow"
            ),
        )

        message = format_post_message(
            post,
            translated_text=(
                "THÔNG TIN NÓNG: Các chi tiết ban đầu đang xuất hiện khi Mỹ và Iran tiến hành cuộc gặp trực tiếp đầu tiên kể từ năm 1979.\n\n"
                "Chi tiết bao gồm:\n\n"
                "1. Eo biển Hormuz vẫn là một điểm bất đồng nghiêm trọng\n\n"
                "2. Quân đội Mỹ cho biết 2 tàu chiến Mỹ đã đi qua eo biển Hormuz hôm nay\n\n"
                "3. Các cuộc đàm phán dự kiến sẽ tiếp tục tối nay và có thể kéo dài sang ngày mai"
            ),
        )

        self.assertIn("Ông Donald Trump cho biết Các chi tiết ban đầu đang xuất hiện khi Mỹ và Iran tiến hành cuộc gặp trực tiếp đầu tiên kể từ năm 1979.", message)
        self.assertIn("Các điểm chính:", message)
        self.assertIn("Eo biển Hormuz vẫn là một điểm bất đồng nghiêm trọng", message)
        self.assertIn("2 tàu chiến Mỹ đã đi qua eo biển Hormuz hôm nay", message)
        self.assertIn("có thể kéo dài sang ngày mai", message)

    def test_format_post_message_keeps_meaningful_quoted_terms(self) -> None:
        post = make_post(
            "101",
            'He praised the so-called "Big Beautiful Bill" and said it would cut taxes for workers.',
        )

        message = format_post_message(
            post,
            translated_text='Ông ca ngợi cái gọi là "Big Beautiful Bill" và nói dự luật này sẽ giảm thuế cho người lao động.',
        )

        self.assertIn('Big Beautiful Bill', message)
        self.assertNotIn('cái gọi là và', message)

    def test_format_post_message_does_not_end_with_broken_trump_fragment(self) -> None:
        post = make_post(
            "116382331013274742",
            (
                "I just met with Senators Lindsey Graham and John Barrasso to discuss funding our great ICE Agents and Border Patrol. "
                "Reconciliation is MOVING ALONG, and we are moving QUICKLY and FOCUSED on keeping our Border SAFE, while getting funding for B."
            ),
        )

        message = format_post_message(
            post,
            translated_text=(
                "Tôi vừa gặp Thượng nghị sĩ Lindsey Graham và John Barrasso để nói về việc tài trợ cho các Đặc vụ ICE vĩ đại và Đội tuần tra Biên giới của chúng ta. "
                "Quá trình hòa giải đang tiến hành, và chúng ta đang tiến hành nhanh chóng và tập trung vào việc giữ an toàn cho biên giới, đồng thời nhận tài trợ cho B."
            ),
        )

        self.assertNotIn("Ông cũng nói", message)
        self.assertNotIn("đồng thời nhận tài trợ cho B.", message)
        self.assertFalse(message.rstrip().endswith(" B."))

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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
            self.assertIn("Ông Donald Trump cho biết xin chao the gioi.", sender.messages[0])
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
            self.assertNotIn("Bai dang co kem hinh anh lien quan.", sender.messages[0])
            self.assertNotIn("hello world", sender.messages[0])
            self.assertNotIn("The post includes 1 image.", sender.messages[0])

    def test_unchanged_english_translation_uses_vietnamese_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=("ap_world_rss",),
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
                source_id="rss:ap-world",
                source_name="AP News",
                id="ap-english-1",
                account_handle="AP News",
                created_at="2026-04-07T08:00:00Z",
                url="https://apnews.com/article/test-story",
                body_text="Israeli Prime Minister Benjamin Netanyahu said he authorized direct negotiations with Lebanon.",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "ap-english-1"},
            )
            client = FakeClient([[post]], source_id="rss:ap-world", source_name="AP News")
            sender = FakeSender()
            translator = EchoTranslator()
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
            self.assertEqual(len(translator.calls), 2)
            self.assertIn("Ban dich tam thoi chua san sang.", sender.messages[0])
            self.assertNotIn("Israeli Prime Minister", sender.messages[0])

    def test_unchanged_english_translation_retries_before_using_vietnamese_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=("ap_world_rss",),
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
                source_id="rss:ap-world",
                source_name="AP News",
                id="ap-english-2",
                account_handle="AP News",
                created_at="2026-04-07T08:00:00Z",
                url="https://apnews.com/article/test-story",
                body_text="Israeli Prime Minister Benjamin Netanyahu said he authorized direct negotiations with Lebanon.",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "ap-english-2"},
            )
            client = FakeClient([[post]], source_id="rss:ap-world", source_name="AP News")
            sender = FakeSender()
            translator = UnchangedThenTranslatedTranslator(
                "Thủ tướng Israel Benjamin Netanyahu cho biết ông đã cho phép đàm phán trực tiếp với Lebanon."
            )
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
            self.assertEqual(len(translator.calls), 2)
            self.assertIn("Thủ tướng Israel Benjamin Netanyahu", sender.messages[0])
            self.assertNotIn("Ban dich tam thoi chua san sang.", sender.messages[0])

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

    def test_format_post_message_for_reuters_story_matches_wire_style(self) -> None:
        post = SourcePost(
            source_id="rss:reuters",
            source_name="Reuters",
            id="story-1",
            account_handle="Reuters",
            created_at="2026-04-07T08:00:00Z",
            url="https://news.google.com/rss/articles/test-1",
            body_text="US fourth-quarter GDP growth revised lower to a 0.5% rate",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-1", "source": "Reuters"},
        )

        message = format_post_message(
            post,
            translated_text="Tăng trưởng GDP quý 4 của Mỹ được điều chỉnh giảm xuống mức 0,5%.",
        )

        self.assertFalse(message.startswith("Reuters"))
        self.assertNotIn("Posted:", message)
        self.assertNotIn("Link:", message)
        self.assertIn("Tăng trưởng GDP quý 4 của Mỹ được điều chỉnh giảm xuống mức 0,5%.", message)
        self.assertIn("\n\nTheo Reuters", message)

    def test_format_post_message_for_ap_story_includes_link_and_translated_summary(self) -> None:
        post = SourcePost(
            source_id="rss:ap-world",
            source_name="AP News",
            id="story-ap-1",
            account_handle="AP News",
            created_at="2026-04-07T08:00:00Z",
            url="https://apnews.com/article/test-story",
            body_text=(
                "Netanyahu authorizes direct talks with Lebanon ‘as soon as possible’\n\n"
                "Israeli Prime Minister Benjamin Netanyahu says he has authorized direct negotiations with Lebanon as soon as possible."
            ),
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-ap-1"},
        )

        message = format_post_message(
            post,
            translated_text=(
                "Thủ tướng Israel Benjamin Netanyahu cho biết ông đã cho phép đàm phán trực tiếp với Lebanon sớm nhất có thể."
            ),
        )

        self.assertFalse(message.startswith("AP News"))
        self.assertNotIn("Posted:", message)
        self.assertNotIn("Link:", message)
        self.assertIn(
            "Thủ tướng Israel Benjamin Netanyahu cho biết ông đã cho phép đàm phán trực tiếp với Lebanon sớm nhất có thể.\n\nTheo AP News",
            message,
        )
        self.assertIn(
            "Thủ tướng Israel Benjamin Netanyahu cho biết ông đã cho phép đàm phán trực tiếp với Lebanon sớm nhất có thể.",
            message,
        )

    def test_format_post_message_for_reuters_story_adds_terminal_period(self) -> None:
        post = SourcePost(
            source_id="rss:reuters",
            source_name="Reuters",
            id="story-2",
            account_handle="Reuters",
            created_at="2026-04-07T08:00:00Z",
            url="https://news.google.com/rss/articles/test-2",
            body_text="US fourth-quarter GDP growth revised lower",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-2", "source": "Reuters"},
        )

        message = format_post_message(
            post,
            translated_text="Tăng trưởng GDP quý 4 của Mỹ được điều chỉnh giảm",
        )

        self.assertIn(
            "Tăng trưởng GDP quý 4 của Mỹ được điều chỉnh giảm.\n\nTheo Reuters",
            message,
        )

    def test_format_post_message_for_ap_story_adds_terminal_period(self) -> None:
        post = SourcePost(
            source_id="rss:ap-world",
            source_name="AP News",
            id="story-ap-2",
            account_handle="AP News",
            created_at="2026-04-07T08:00:00Z",
            url="https://apnews.com/article/test-story-2",
            body_text="Netanyahu authorizes direct talks with Lebanon",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-ap-2"},
        )

        message = format_post_message(
            post,
            translated_text="Thủ tướng Israel Benjamin Netanyahu cho biết ông đã cho phép đàm phán trực tiếp với Lebanon sớm nhất có thể",
        )

        self.assertIn(
            "Thủ tướng Israel Benjamin Netanyahu cho biết ông đã cho phép đàm phán trực tiếp với Lebanon sớm nhất có thể.\n\nTheo AP News",
            message,
        )

    def test_format_post_message_for_ft_story_matches_ap_style(self) -> None:
        post = SourcePost(
            source_id="rss:ft",
            source_name="FT",
            id="story-ft-1",
            account_handle="FT",
            created_at="2026-04-07T08:00:00Z",
            url="https://www.ft.com/content/test-story",
            body_text="Stocks rise on hopes for truce after Israeli strikes on country have threatened to derail planned peace talks",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-ft-1"},
        )

        message = format_post_message(
            post,
            translated_text="Chứng khoán tăng nhờ kỳ vọng ngừng bắn sau khi các cuộc không kích của Israel đe dọa làm chệch hướng các cuộc đàm phán hòa bình.",
        )

        self.assertFalse(message.startswith("FT"))
        self.assertNotIn("Posted:", message)
        self.assertNotIn("Link:", message)
        self.assertIn(
            "Chứng khoán tăng nhờ kỳ vọng ngừng bắn sau khi các cuộc không kích của Israel đe dọa làm chệch hướng các cuộc đàm phán hòa bình.\n\nTheo FT",
            message,
        )
        self.assertIn(
            "Chứng khoán tăng nhờ kỳ vọng ngừng bắn sau khi các cuộc không kích của Israel đe dọa làm chệch hướng các cuộc đàm phán hòa bình.",
            message,
        )

    def test_format_post_message_for_ft_story_adds_terminal_period(self) -> None:
        post = SourcePost(
            source_id="rss:ft",
            source_name="FT",
            id="story-ft-2",
            account_handle="FT",
            created_at="2026-04-07T08:00:00Z",
            url="https://www.ft.com/content/test-story-2",
            body_text="Saudi Arabia and Qatar both suffer significant hits to production capacity in war",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-ft-2"},
        )

        message = format_post_message(
            post,
            translated_text="Ả Rập Saudi và Qatar đều bị thiệt hại đáng kể về năng lực sản xuất trong cuộc chiến Mỹ-Israel chống lại Iran",
        )

        self.assertIn(
            "Ả Rập Saudi và Qatar đều bị thiệt hại đáng kể về năng lực sản xuất trong cuộc chiến Mỹ-Israel chống lại Iran.\n\nTheo FT",
            message,
        )

    def test_format_post_message_for_x_story_includes_time_without_link(self) -> None:
        post = SourcePost(
            source_id="x:kobeissiletter",
            source_name="The Kobeissi Letter",
            id="story-x-1",
            account_handle="KobeissiLetter",
            created_at="2026-04-11T08:15:00Z",
            url="https://x.com/KobeissiLetter/status/123",
            body_text="Markets are rallying after a key inflation print.",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"id": "story-x-1", "text": "Markets are rallying after a key inflation print."},
        )

        message = format_post_message(
            post,
            translated_text="Thi truong tang sau du lieu lam phat moi.",
        )

        self.assertTrue(message.startswith("Thi truong tang sau du lieu lam phat moi."))
        self.assertIn(
            "Thi truong tang sau du lieu lam phat moi.\n\nTheo The Kobeissi Letter\nPosted: 15:15 11/04/2026",
            message,
        )
        self.assertIn("Posted: 15:15 11/04/2026", message)
        self.assertNotIn("Link:", message)
        self.assertIn("Thi truong tang sau du lieu lam phat moi.", message)

    def test_summarize_caption_for_x_keeps_supporting_fact_without_prefix(self) -> None:
        text = (
            "Gold is reshaping the global financial system: Central bank gold holdings surpassed "
            "valuation-adjusted US Dollar reserve assets for the first time on record. "
            "Official gold reserve assets are up to a record $3.87 trillion, about $140 billion "
            "above valuation-adjusted US Dollar reserve assets at $3.73 trillion. "
            "Since 2022, gold reserve assets have tripled while USD reserve assets have declined."
        )

        summary = _summarize_caption(
            text,
            limit=260,
            source_id="x:kobeissiletter",
            max_sentences=3,
        )

        self.assertIn("Central bank gold holdings surpassed valuation-adjusted US Dollar reserve assets", summary)
        self.assertIn("$3.87 trillion", summary)
        self.assertNotIn("Kobeissi Letter cho biết", summary)

    def test_summarize_caption_for_x_strips_breaking_prefix(self) -> None:
        summary = _summarize_caption(
            "BREAKING: Iran is struggling to fully reopen the Strait of Hormuz.",
            limit=160,
            source_id="x:kobeissiletter",
            max_sentences=2,
        )

        self.assertNotIn("BREAKING:", summary)
        self.assertTrue(summary.startswith("Iran is struggling"))

    def test_summarize_caption_for_x_merges_numbered_list_fragments(self) -> None:
        summary = _summarize_caption(
            (
                "Initial details are emerging as the US and Iran hold direct talks. "
                "Details include: 1. Iran says negotiators will meet again next week."
            ),
            limit=220,
            source_id="x:kobeissiletter",
            max_sentences=2,
        )

        self.assertNotIn("Details include: 1.", summary)
        self.assertIn("Details include: Iran says negotiators will meet again next week.", summary)

    def test_summarize_caption_for_x_keeps_leading_numeric_fact(self) -> None:
        summary = _summarize_caption(
            "1.5% inflation triggered a sharp move higher in bond yields today.",
            limit=160,
            source_id="x:kobeissiletter",
            max_sentences=2,
        )

        self.assertIn("1.5% inflation", summary)

    def test_summarize_caption_for_x_keeps_short_one_word_alert(self) -> None:
        summary = _summarize_caption(
            "Bitcoin",
            limit=80,
            source_id="x:kobeissiletter",
            max_sentences=1,
        )

        self.assertEqual("Bitcoin.", summary)

    def test_summarize_caption_for_x_keeps_short_two_letter_alert(self) -> None:
        summary = _summarize_caption(
            "US",
            limit=80,
            source_id="x:kobeissiletter",
            max_sentences=1,
        )

        self.assertEqual("US.", summary)

    def test_summarize_caption_for_x_avoids_dangling_clause_truncation(self) -> None:
        summary = _summarize_caption(
            (
                "Giá trị tài sản ròng của hộ gia đình Hoa Kỳ đã tăng +2,2 nghìn tỷ USD trong quý 4 năm 2025, "
                "lên mức kỷ lục 184,1 nghìn tỷ USD. Điều này chủ yếu được thúc đẩy bởi lượng cổ phiếu nắm giữ "
                "đã tăng +1,6 nghìn tỷ USD khi thị trường tiếp tục đi lên."
            ),
            limit=170,
            source_id="x:kobeissiletter",
            max_sentences=2,
        )

        self.assertIn("Giá trị tài sản ròng của hộ gia đình Hoa Kỳ đã tăng +2,2 nghìn tỷ USD", summary)
        self.assertIn("184,1 nghìn tỷ USD", summary)
        self.assertNotIn("khi...", summary)
        self.assertNotIn("...", summary)

    def test_summarize_caption_for_x_can_trim_non_material_trailing_clause(self) -> None:
        summary = _summarize_caption(
            (
                "Lượng cổ phiếu nắm giữ của hộ gia đình Hoa Kỳ đã tăng mạnh trong quý 4 năm 2025, "
                "khi thị trường tiếp tục đi lên và tâm lý nhà đầu tư được cải thiện."
            ),
            limit=100,
            source_id="x:kobeissiletter",
            max_sentences=1,
        )

        self.assertIn("Lượng cổ phiếu nắm giữ của hộ gia đình Hoa Kỳ đã tăng mạnh trong quý 4 năm 2025.", summary)
        self.assertNotIn("khi thị trường", summary)

    def test_summarize_caption_for_x_numbered_lists_include_multiple_items(self) -> None:
        summary = _summarize_caption(
            (
                "BREAKING: Initial details are emerging as the US and Iran conduct their first direct "
                "meeting since 1979.\n\n"
                "Details include:\n\n"
                "1. The Strait of Hormuz remains a point of serious disagreement\n\n"
                "2. US military says 2 US warships have transited the Strait of Hormuz today\n\n"
                "3. Talks are expected to continue tonight and may extend into tomorrow\n\n"
                "We expect to receive much more detail in the coming hours."
            ),
            limit=320,
            source_id="x:kobeissiletter",
            max_sentences=3,
        )

        self.assertIn("first direct meeting since 1979", summary)
        self.assertIn("Strait of Hormuz remains a point of serious disagreement", summary)
        self.assertIn("2 US warships have transited the Strait of Hormuz today", summary)
        self.assertIn("Talks are expected to continue tonight and may extend into tomorrow", summary)
        self.assertNotIn("We expect to receive much more detail", summary)

    def test_summarize_caption_for_x_weekly_list_keeps_later_items_and_tail(self) -> None:
        summary = _summarize_caption(
            (
                "Key Events This Week:\n\n"
                "1. Markets React to Failed Negotiations and Hormuz \"Blockade\" - Today, 6 PM ET\n\n"
                "2. March Existing Home Sales data - Monday\n\n"
                "3. March PPI Inflation data - Tuesday\n\n"
                "4. Philly Fed Manufacturing Index - Thursday\n\n"
                "5. Initial Jobless Claims data - Thursday\n\n"
                "6. 10 Fed Speaker Events This Week\n\n"
                "Today marks day 44 of the Iran War."
            ),
            limit=220,
            source_id="x:kobeissiletter",
            max_sentences=2,
        )

        self.assertIn("Markets React to Failed Negotiations", summary)
        self.assertIn("March PPI Inflation data - Tuesday", summary)
        self.assertIn("Initial Jobless Claims data - Thursday", summary)
        self.assertIn("10 Fed Speaker Events This Week", summary)
        self.assertIn("Today marks day 44 of the Iran War", summary)

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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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

    def test_reuters_story_uses_default_group_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=("reuters_rss",),
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
            reuters_post = SourcePost(
                source_id="rss:reuters",
                source_name="Reuters",
                id="reuters-101",
                account_handle="Reuters",
                created_at="2026-04-07T09:00:00Z",
                url="https://news.google.com/rss/articles/reuters-101",
                body_text="US fourth-quarter GDP growth revised lower to a 0.5% rate",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "reuters-101", "source": "Reuters"},
            )
            client = FakeClient([[reuters_post], []], source_id="rss:reuters", source_name="Reuters")
            sender = FakeSender()
            translator = FakeTranslator("Tăng trưởng GDP quý 4 của Mỹ được điều chỉnh giảm xuống mức 0,5%.")
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                translator=translator,
            )

            first = service.run_once()
            second = service.run_once()
            source_status = store.get_source_statuses(filtered_limit=1)[0]

            self.assertEqual(first.sent_count, 1)
            self.assertEqual(sender.deliveries, [("@chat", "reuters-101")])
            self.assertEqual(second.sent_count, 0)
            self.assertEqual(source_status.source_key, "rss:reuters")
            self.assertEqual(source_status.checkpoint_id, "reuters-101")

    def test_ap_story_uses_default_group_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=("ap_world_rss",),
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
            ap_post = SourcePost(
                source_id="rss:ap-world",
                source_name="AP News",
                id="ap-101",
                account_handle="AP News",
                created_at="2026-04-07T09:00:00Z",
                url="https://apnews.com/article/test-story",
                body_text="Netanyahu authorizes direct talks with Lebanon ‘as soon as possible’",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "ap-101"},
            )
            client = FakeClient([[ap_post], []], source_id="rss:ap-world", source_name="AP News")
            sender = FakeSender()
            translator = FakeTranslator(
                "Thủ tướng Israel Benjamin Netanyahu cho biết ông đã cho phép đàm phán trực tiếp với Lebanon sớm nhất có thể."
            )
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                translator=translator,
            )

            first = service.run_once()
            second = service.run_once()
            source_status = store.get_source_statuses(filtered_limit=1)[0]

            self.assertEqual(first.sent_count, 1)
            self.assertEqual(sender.deliveries, [("@chat", "ap-101")])
            self.assertEqual(second.sent_count, 0)
            self.assertEqual(source_status.source_key, "rss:ap-world")
            self.assertEqual(source_status.checkpoint_id, "ap-101")

    def test_ft_story_uses_default_group_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.sqlite3"
            config = make_config(db_path)
            config = AppConfig(
                telegram_bot_token=config.telegram_bot_token,
                telegram_chat_id=config.telegram_chat_id,
                source_chat_routes=config.source_chat_routes,
                source_keyword_filters=config.source_keyword_filters,
                source_category_filters=config.source_category_filters,
                enabled_sources=("ft_rss",),
                rss_feed_urls=config.rss_feed_urls,
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
            ft_post = SourcePost(
                source_id="rss:ft",
                source_name="FT",
                id="ft-101",
                account_handle="FT",
                created_at="2026-04-07T09:00:00Z",
                url="https://www.ft.com/content/test-story",
                body_text="Stocks rise on hopes for truce after Israeli strikes on country have threatened to derail planned peace talks",
                is_reply=False,
                is_reblog=False,
                media_attachments=(),
                raw_payload={"id": "ft-101"},
            )
            client = FakeClient([[ft_post], []], source_id="rss:ft", source_name="FT")
            sender = FakeSender()
            translator = FakeTranslator(
                "Chứng khoán tăng nhờ kỳ vọng ngừng bắn sau khi các cuộc không kích của Israel đe dọa làm chệch hướng các cuộc đàm phán hòa bình."
            )
            service = NewsBotService(
                config,
                store,
                [client],
                build_router(config.telegram_chat_id, config.source_chat_routes),
                build_post_filter(config.source_keyword_filters, config.source_category_filters),
                sender,
                translator=translator,
            )

            first = service.run_once()
            second = service.run_once()
            source_status = store.get_source_statuses(filtered_limit=1)[0]

            self.assertEqual(first.sent_count, 1)
            self.assertEqual(sender.deliveries, [("@chat", "ft-101")])
            self.assertEqual(second.sent_count, 0)
            self.assertEqual(source_status.source_key, "rss:ft")
            self.assertEqual(source_status.checkpoint_id, "ft-101")

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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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

    def test_rt_prefixed_truthsocial_post_is_not_filtered_by_text(self) -> None:
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
            client = FakeClient([[make_post("101", "RT: forwarded content")]])
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
            self.assertEqual(summary.filtered_count, 0)
            self.assertEqual(sender.deliveries, [("@chat", "101")])
            self.assertEqual(store.get_last_status_id(client.source_id), "101")

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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
                truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
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
