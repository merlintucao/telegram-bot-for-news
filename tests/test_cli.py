from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from news_bot.cli import build_notify_message, run_doctor, run_notify
from news_bot.config import AppConfig


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

    def send_message(self, text: str, chat_id: str | None = None) -> None:
        self.calls.append((chat_id, text))


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


if __name__ == "__main__":
    unittest.main()
