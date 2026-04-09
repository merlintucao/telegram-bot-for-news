from __future__ import annotations

import unittest
from pathlib import Path

from news_bot.config import AppConfig, DEFAULT_FT_RSS_URL
from news_bot.ft import FTRSSSource


def make_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("ft_rss",),
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
        ft_rss_url=DEFAULT_FT_RSS_URL,
    )


class FTRSSSourceTests(unittest.TestCase):
    def test_parse_ft_feed_uses_description_as_story_text(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title><![CDATA[International homepage]]></title>
    <item>
      <title><![CDATA[Netanyahu approves direct talks with Lebanon amid strained ceasefire]]></title>
      <description><![CDATA[Stocks rise on hopes for truce after Israeli strikes on country have threatened to derail planned peace talks]]></description>
      <link>https://www.ft.com/content/5fa84873-0c45-462d-9f8b-3adbc9f0a164</link>
      <guid isPermaLink="false">5fa84873-0c45-462d-9f8b-3adbc9f0a164</guid>
      <pubDate>Thu, 09 Apr 2026 16:00:25 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
        source = FTRSSSource(make_config())

        metadata = source._parse_feed(xml_text)

        self.assertEqual(metadata["source_name"], "FT")
        self.assertEqual(len(metadata["posts"]), 1)
        post = metadata["posts"][0]
        self.assertEqual(post.source_id, "rss:ft")
        self.assertEqual(post.source_name, "FT")
        self.assertEqual(post.account_handle, "FT")
        self.assertEqual(post.url, "https://www.ft.com/content/5fa84873-0c45-462d-9f8b-3adbc9f0a164")
        self.assertEqual(
            post.body_text,
            "Stocks rise on hopes for truce after Israeli strikes on country have threatened to derail planned peace talks",
        )

    def test_parse_ft_feed_falls_back_to_title_when_description_missing(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title><![CDATA[International homepage]]></title>
    <item>
      <title><![CDATA[Will Trump stick with his Iran truce?]]></title>
      <description><![CDATA[]]></description>
      <link>https://www.ft.com/content/e6883be4-a756-4c0b-b9f9-ac28554ad42f</link>
      <guid isPermaLink="false">e6883be4-a756-4c0b-b9f9-ac28554ad42f</guid>
      <pubDate>Wed, 08 Apr 2026 23:55:11 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
        source = FTRSSSource(make_config())

        metadata = source._parse_feed(xml_text)
        post = metadata["posts"][0]

        self.assertEqual(post.body_text, "Will Trump stick with his Iran truce?")


if __name__ == "__main__":
    unittest.main()
