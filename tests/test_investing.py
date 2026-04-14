from __future__ import annotations

import unittest
from pathlib import Path

from news_bot.config import AppConfig, DEFAULT_INVESTING_RSS_URL
from news_bot.investing import InvestingRSSSource


def make_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("investing_rss",),
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
        investing_rss_url=DEFAULT_INVESTING_RSS_URL,
    )


class InvestingRSSSourceTests(unittest.TestCase):
    def test_parse_investing_feed_uses_description_as_story_text(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Investing.com News</title>
    <item>
      <title>US stocks rise as traders weigh earnings</title>
      <description><![CDATA[US stocks rose on Thursday as traders weighed fresh earnings and economic data.]]></description>
      <link>https://www.investing.com/news/stock-market-news/test-story</link>
      <guid isPermaLink="false">test-story</guid>
      <pubDate>Thu, 09 Apr 2026 16:05:14 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
        source = InvestingRSSSource(make_config())

        metadata = source._parse_feed(xml_text)

        self.assertEqual(metadata["source_name"], "Investing")
        self.assertEqual(len(metadata["posts"]), 1)
        post = metadata["posts"][0]
        self.assertEqual(post.source_id, "rss:investing")
        self.assertEqual(post.source_name, "Investing")
        self.assertEqual(post.account_handle, "Investing")
        self.assertEqual(post.url, "https://www.investing.com/news/stock-market-news/test-story")
        self.assertEqual(
            post.body_text,
            "US stocks rose on Thursday as traders weighed fresh earnings and economic data.",
        )

    def test_parse_investing_feed_cleans_html_description(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Investing.com News</title>
    <item>
      <title>Oil prices climb after OPEC meeting</title>
      <description><![CDATA[<a href=\"https://www.investing.com/news/commodities-news/test-story-3\">Oil prices climb after OPEC meeting</a>&nbsp;&nbsp;<font color=\"#6f6f6f\">Investing.com</font>]]></description>
      <link>https://www.investing.com/news/commodities-news/test-story-3</link>
      <guid isPermaLink="false">test-story-3</guid>
      <pubDate>Thu, 09 Apr 2026 16:05:14 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
        source = InvestingRSSSource(make_config())

        metadata = source._parse_feed(xml_text)
        post = metadata["posts"][0]

        self.assertEqual(post.body_text, "Oil prices climb after OPEC meeting")

    def test_parse_investing_feed_falls_back_to_title_when_description_missing(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Investing.com News</title>
    <item>
      <title>Oil prices climb after OPEC meeting</title>
      <description><![CDATA[]]></description>
      <link>https://www.investing.com/news/commodities-news/test-story-2</link>
      <guid isPermaLink="false">test-story-2</guid>
      <pubDate>Thu, 09 Apr 2026 16:05:14 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
        source = InvestingRSSSource(make_config())

        metadata = source._parse_feed(xml_text)
        post = metadata["posts"][0]

        self.assertEqual(post.body_text, "Oil prices climb after OPEC meeting")


if __name__ == "__main__":
    unittest.main()
