from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from news_bot.cli import build_notify_message, run_doctor, run_notify, run_send_latest_ap
from news_bot.config import AppConfig
from news_bot.models import SourcePost
from news_bot.translate import TranslationError


def make_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@main",
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
        state_db_path=Path("data/test.sqlite3"),
        bootstrap_latest_only=True,
        initial_history_limit=5,
        fetch_limit=10,
        exclude_replies=False,
        exclude_reblogs=False,
        user_agent="test-agent",
        log_level="INFO",
        telegram_alert_chat_id="@ops",
    )


class RecordingSender:
    def __init__(self) -> None:
        self.calls: list[tuple[str | None, str]] = []
        self.posts: list[tuple[str, str, str | None]] = []

    def send_message(self, text: str, chat_id: str | None = None) -> None:
        self.calls.append((chat_id, text))

    def send_post(
        self,
        post: SourcePost,
        text: str,
        chat_id: str | None = None,
        media_caption: str | None = None,
    ) -> None:
        self.posts.append((post.id, text, chat_id))


class FakeAPSource:
    def __init__(self, posts: list[SourcePost]) -> None:
        self.posts = posts
        self.source_id = "rss:ap-world"
        self.source_name = "AP News"

    def fetch_posts(self, since_id: str | None = None, limit: int | None = None) -> list[SourcePost]:
        posts = self.posts
        if limit is not None:
            posts = posts[:limit]
        return posts

    def probe(self):  # pragma: no cover
        raise NotImplementedError


class FakeTranslator:
    def __init__(self, translated_text: str) -> None:
        self.translated_text = translated_text

    def translate(self, text: str) -> str:
        return self.translated_text


class CLITests(unittest.TestCase):
    def test_run_doctor_allows_missing_cookies_in_auto_mode(self) -> None:
        config = make_config()
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
            truthsocial_account_id="107780257626128497",
            truthsocial_base_url=config.truthsocial_base_url,
            truthsocial_cookies_file=None,
            truthsocial_reload_cookies=config.truthsocial_reload_cookies,
            poll_interval_seconds=config.poll_interval_seconds,
            request_timeout_seconds=config.request_timeout_seconds,
            state_db_path=config.state_db_path,
            bootstrap_latest_only=config.bootstrap_latest_only,
            initial_history_limit=config.initial_history_limit,
            fetch_limit=config.fetch_limit,
            exclude_replies=config.exclude_replies,
            exclude_reblogs=config.exclude_reblogs,
            user_agent=config.user_agent,
            log_level=config.log_level,
            telegram_alert_chat_id=config.telegram_alert_chat_id,
            truthsocial_auth_mode="auto",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_doctor(config, skip_network=True)

        self.assertEqual(exit_code, 0)
        self.assertIn("Truth Social access mode: auto", output.getvalue())
        self.assertIn("optional in public/auto mode", output.getvalue())

    def test_run_doctor_requires_cookies_in_cookies_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            config = make_config()
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
                truthsocial_cookies_file=Path(tmpdir) / "missing.json",
                truthsocial_reload_cookies=config.truthsocial_reload_cookies,
                poll_interval_seconds=config.poll_interval_seconds,
                request_timeout_seconds=config.request_timeout_seconds,
                state_db_path=config.state_db_path,
                bootstrap_latest_only=config.bootstrap_latest_only,
                initial_history_limit=config.initial_history_limit,
                fetch_limit=config.fetch_limit,
                exclude_replies=config.exclude_replies,
                exclude_reblogs=config.exclude_reblogs,
                user_agent=config.user_agent,
                log_level=config.log_level,
                telegram_alert_chat_id=config.telegram_alert_chat_id,
                truthsocial_auth_mode="cookies",
            )
            output = io.StringIO()

            with redirect_stdout(output):
                exit_code = run_doctor(config, skip_network=True)

        self.assertEqual(exit_code, 1)
        self.assertIn("required in cookies mode", output.getvalue())

    def test_run_doctor_reports_x_cookie_requirements(self) -> None:
        config = make_config()
        config = AppConfig(
            telegram_bot_token=config.telegram_bot_token,
            telegram_chat_id=config.telegram_chat_id,
            source_chat_routes=config.source_chat_routes,
            source_keyword_filters=config.source_keyword_filters,
            source_category_filters=config.source_category_filters,
            enabled_sources=("x_kobeissi_letter",),
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
            bootstrap_latest_only=config.bootstrap_latest_only,
            initial_history_limit=config.initial_history_limit,
            fetch_limit=config.fetch_limit,
            exclude_replies=config.exclude_replies,
            exclude_reblogs=config.exclude_reblogs,
            user_agent=config.user_agent,
            log_level=config.log_level,
            telegram_alert_chat_id=config.telegram_alert_chat_id,
            x_auth_mode="cookies",
            x_cookies_file=None,
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_doctor(config, skip_network=True)

        self.assertEqual(exit_code, 1)
        self.assertIn("X auth mode: cookies", output.getvalue())
        self.assertIn("X cookies: missing (required in cookies mode)", output.getvalue())

    def test_build_notify_message_uses_custom_text_when_present(self) -> None:
        self.assertEqual(
            build_notify_message("main", "custom ping"),
            "custom ping",
        )

    def test_build_notify_message_for_routed_target_includes_sources(self) -> None:
        self.assertEqual(
            build_notify_message(
                "routed",
                source_ids=("truthsocial:realDonaldTrump", "rss:ap"),
            ),
            "Telegram routed chat test from news_bot.\nSources: truthsocial:realDonaldTrump, rss:ap",
        )

    def test_run_notify_sends_to_main_and_alert_chats(self) -> None:
        config = make_config()
        sender = RecordingSender()
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_notify(config, target="both", sender=sender)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            sender.calls,
            [
                ("@main", "Telegram main chat test from news_bot."),
                ("@ops", "Telegram alert chat test from news_bot."),
            ],
        )
        self.assertIn("Sent main test message", output.getvalue())
        self.assertIn("Sent alert test message", output.getvalue())

    def test_run_notify_fails_when_target_chat_is_missing(self) -> None:
        config = make_config()
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
            bootstrap_latest_only=config.bootstrap_latest_only,
            initial_history_limit=config.initial_history_limit,
            fetch_limit=config.fetch_limit,
            exclude_replies=config.exclude_replies,
            exclude_reblogs=config.exclude_reblogs,
            user_agent=config.user_agent,
            log_level=config.log_level,
            telegram_alert_chat_id="",
        )
        sender = RecordingSender()
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_notify(config, target="alert", sender=sender)

        self.assertEqual(exit_code, 1)
        self.assertEqual(sender.calls, [])
        self.assertIn("No Telegram destinations configured", output.getvalue())

    def test_run_notify_can_send_to_routed_destinations(self) -> None:
        config = make_config()
        config = AppConfig(
            telegram_bot_token=config.telegram_bot_token,
            telegram_chat_id="",
            source_chat_routes=("truthsocial:*=@truths",),
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
            bootstrap_latest_only=config.bootstrap_latest_only,
            initial_history_limit=config.initial_history_limit,
            fetch_limit=config.fetch_limit,
            exclude_replies=config.exclude_replies,
            exclude_reblogs=config.exclude_reblogs,
            user_agent=config.user_agent,
            log_level=config.log_level,
            telegram_alert_chat_id=config.telegram_alert_chat_id,
        )
        sender = RecordingSender()
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_notify(config, target="routed", sender=sender)

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            sender.calls,
            [
                (
                    "@truths",
                    "Telegram routed chat test from news_bot.\nSource: truthsocial:realDonaldTrump",
                ),
            ],
        )
        self.assertIn("Sent routed test message to @truths", output.getvalue())

    def test_run_notify_can_filter_routed_destinations_by_source_pattern(self) -> None:
        config = make_config()
        config = AppConfig(
            telegram_bot_token=config.telegram_bot_token,
            telegram_chat_id="@default",
            source_chat_routes=(
                "truthsocial:*=@truths",
                "rss:*=@rss",
            ),
            source_keyword_filters=config.source_keyword_filters,
            source_category_filters=config.source_category_filters,
            enabled_sources=("truthsocial_trump", "rss"),
            rss_feed_urls=("https://example.com/feed.xml",),
            truthsocial_fallback_feed_urls=config.truthsocial_fallback_feed_urls,
            truthsocial_handle=config.truthsocial_handle,
            truthsocial_account_id=config.truthsocial_account_id,
            truthsocial_base_url=config.truthsocial_base_url,
            truthsocial_cookies_file=config.truthsocial_cookies_file,
            truthsocial_reload_cookies=config.truthsocial_reload_cookies,
            poll_interval_seconds=config.poll_interval_seconds,
            request_timeout_seconds=config.request_timeout_seconds,
            state_db_path=config.state_db_path,
            bootstrap_latest_only=config.bootstrap_latest_only,
            initial_history_limit=config.initial_history_limit,
            fetch_limit=config.fetch_limit,
            exclude_replies=config.exclude_replies,
            exclude_reblogs=config.exclude_reblogs,
            user_agent=config.user_agent,
            log_level=config.log_level,
            telegram_alert_chat_id=config.telegram_alert_chat_id,
        )
        sender = RecordingSender()
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_notify(
                config,
                target="routed",
                source_pattern="rss:*",
                sender=sender,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            sender.calls,
            [
                (
                    "@rss",
                    "Telegram routed chat test from news_bot.\nSource: rss:example-com-feed-xml",
                ),
            ],
        )
        self.assertIn("for rss:example-com-feed-xml", output.getvalue())

    def test_run_send_latest_ap_dry_run_prints_message(self) -> None:
        config = make_config()
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
            bootstrap_latest_only=config.bootstrap_latest_only,
            initial_history_limit=config.initial_history_limit,
            fetch_limit=config.fetch_limit,
            exclude_replies=config.exclude_replies,
            exclude_reblogs=config.exclude_reblogs,
            user_agent=config.user_agent,
            log_level=config.log_level,
            telegram_alert_chat_id=config.telegram_alert_chat_id,
        )
        source = FakeAPSource(
            [
                SourcePost(
                    source_id="rss:ap-world",
                    source_name="AP News",
                    id="ap-1",
                    account_handle="AP News",
                    created_at="2026-04-07T08:00:00Z",
                    url="https://apnews.com/article/test-story",
                    body_text="Israeli Prime Minister Benjamin Netanyahu says he has authorized direct negotiations with Lebanon as soon as possible.",
                    is_reply=False,
                    is_reblog=False,
                    media_attachments=(),
                    raw_payload={"id": "ap-1"},
                )
            ]
        )
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_send_latest_ap(
                config,
                dry_run=True,
                source=source,
                sender=RecordingSender(),
                translator=FakeTranslator(
                    "Thủ tướng Israel Benjamin Netanyahu cho biết ông đã cho phép đàm phán trực tiếp với Lebanon."
                ),
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("AP News", output.getvalue())
        self.assertNotIn("Link:", output.getvalue())
        self.assertIn("Thủ tướng Israel Benjamin Netanyahu", output.getvalue())

    def test_run_send_latest_ap_sends_post(self) -> None:
        config = make_config()
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
            bootstrap_latest_only=config.bootstrap_latest_only,
            initial_history_limit=config.initial_history_limit,
            fetch_limit=config.fetch_limit,
            exclude_replies=config.exclude_replies,
            exclude_reblogs=config.exclude_reblogs,
            user_agent=config.user_agent,
            log_level=config.log_level,
            telegram_alert_chat_id=config.telegram_alert_chat_id,
        )
        source = FakeAPSource(
            [
                SourcePost(
                    source_id="rss:ap-world",
                    source_name="AP News",
                    id="ap-1",
                    account_handle="AP News",
                    created_at="2026-04-07T08:00:00Z",
                    url="https://apnews.com/article/test-story",
                    body_text="Israeli Prime Minister Benjamin Netanyahu says he has authorized direct negotiations with Lebanon as soon as possible.",
                    is_reply=False,
                    is_reblog=False,
                    media_attachments=(),
                    raw_payload={"id": "ap-1"},
                )
            ]
        )
        sender = RecordingSender()
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_send_latest_ap(
                config,
                dry_run=False,
                source=source,
                sender=sender,
                translator=FakeTranslator(
                    "Thủ tướng Israel Benjamin Netanyahu cho biết ông đã cho phép đàm phán trực tiếp với Lebanon."
                ),
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(sender.posts), 1)
        self.assertEqual(sender.posts[0][0], "ap-1")
        self.assertIn("Sent latest AP story", output.getvalue())


if __name__ == "__main__":
    unittest.main()
