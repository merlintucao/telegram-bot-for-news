from __future__ import annotations

import unittest
from pathlib import Path

from news_bot.ap import APWorldRSSSource
from news_bot.config import AppConfig, DEFAULT_AP_WORLD_RSS_URL, DEFAULT_FT_RSS_URL, DEFAULT_REUTERS_RSS_URL
from news_bot.ft import FTRSSSource
from news_bot.reuters import ReutersRSSSource
from news_bot.sources import build_sources
from news_bot.x import XKobeissiLetterSource


def make_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("truthsocial_trump", "reuters_rss", "ap_world_rss", "ft_rss", "x_kobeissi_letter"),
        rss_feed_urls=(),
        truthsocial_fallback_feed_urls=(),
        truthsocial_handle="realDonaldTrump",
        truthsocial_account_id="107780257626128497",
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
        reuters_rss_url=DEFAULT_REUTERS_RSS_URL,
        ap_world_rss_url=DEFAULT_AP_WORLD_RSS_URL,
        ft_rss_url=DEFAULT_FT_RSS_URL,
        x_cookies_file=Path("data/x-cookies.json"),
    )


class SourceRegistryTests(unittest.TestCase):
    def test_build_sources_supports_reuters_rss(self) -> None:
        sources = build_sources(make_config())

        self.assertEqual(len(sources), 5)
        self.assertTrue(any(isinstance(source, ReutersRSSSource) for source in sources))
        self.assertTrue(any(getattr(source, "source_id", "") == "rss:reuters" for source in sources))

    def test_build_sources_supports_ap_world_rss(self) -> None:
        sources = build_sources(make_config())

        self.assertEqual(len(sources), 5)
        self.assertTrue(any(isinstance(source, APWorldRSSSource) for source in sources))
        self.assertTrue(any(getattr(source, "source_id", "") == "rss:ap-world" for source in sources))

    def test_build_sources_supports_ft_rss(self) -> None:
        sources = build_sources(make_config())

        self.assertEqual(len(sources), 5)
        self.assertTrue(any(isinstance(source, FTRSSSource) for source in sources))
        self.assertTrue(any(getattr(source, "source_id", "") == "rss:ft" for source in sources))

    def test_build_sources_supports_x_kobeissi_letter(self) -> None:
        sources = build_sources(make_config())

        self.assertEqual(len(sources), 5)
        self.assertTrue(any(isinstance(source, XKobeissiLetterSource) for source in sources))
        self.assertTrue(any(getattr(source, "source_id", "") == "x:kobeissiletter" for source in sources))


if __name__ == "__main__":
    unittest.main()
