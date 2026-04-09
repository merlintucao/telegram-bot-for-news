from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from news_bot.config import AppConfig, DEFAULT_REUTERS_RSS_URL
from news_bot.models import SourcePost
from news_bot.reuters import ReutersRSSSource, _extract_reuters_summary_from_html


def make_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("reuters_rss",),
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
        reuters_rss_url=DEFAULT_REUTERS_RSS_URL,
    )


class ReutersRSSSourceTests(unittest.TestCase):
    def test_extract_reuters_summary_from_html_prefers_meta_description(self) -> None:
        html_text = """
<html><head>
  <meta property="og:description" content="Oil prices rose after new sanctions were announced.">
</head></html>
"""
        self.assertEqual(
            _extract_reuters_summary_from_html(html_text),
            "Oil prices rose after new sanctions were announced.",
        )

    def test_parse_google_news_reuters_item_uses_reuters_identity(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>"site:reuters.com" - Google News</title>
    <item>
      <title>US fourth-quarter GDP growth revised lower to a 0.5% rate - Reuters</title>
      <link>https://news.google.com/rss/articles/test-1</link>
      <guid>test-1</guid>
      <pubDate>Thu, 09 Apr 2026 13:27:31 GMT</pubDate>
      <description><![CDATA[<a href="https://news.google.com/rss/articles/test-1" target="_blank">US fourth-quarter GDP growth revised lower to a 0.5% rate</a>&nbsp;&nbsp;<font color="#6f6f6f">Reuters</font>]]></description>
      <source url="https://www.reuters.com">Reuters</source>
    </item>
    <item>
      <title>Unrelated story - AP News</title>
      <link>https://news.google.com/rss/articles/test-2</link>
      <guid>test-2</guid>
      <pubDate>Thu, 09 Apr 2026 13:27:31 GMT</pubDate>
      <description><![CDATA[<a href="https://news.google.com/rss/articles/test-2" target="_blank">Unrelated story</a>&nbsp;&nbsp;<font color="#6f6f6f">AP News</font>]]></description>
      <source url="https://apnews.com">AP News</source>
    </item>
  </channel>
</rss>
"""
        source = ReutersRSSSource(make_config())

        metadata = source._parse_feed(xml_text)

        self.assertEqual(metadata["source_name"], "Reuters")
        self.assertEqual(len(metadata["posts"]), 1)
        post = metadata["posts"][0]
        self.assertEqual(post.source_id, "rss:reuters")
        self.assertEqual(post.source_name, "Reuters")
        self.assertEqual(post.account_handle, "Reuters")
        self.assertEqual(post.url, "https://news.google.com/rss/articles/test-1")
        self.assertEqual(post.body_text, "US fourth-quarter GDP growth revised lower to a 0.5% rate")

    def test_parse_google_news_reuters_item_falls_back_to_title_when_description_duplicates(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>"site:reuters.com" - Google News</title>
    <item>
      <title>NATO's Rutte told allies Trump wants Hormuz commitments within days, diplomats say - Reuters</title>
      <link>https://news.google.com/rss/articles/test-3</link>
      <guid>test-3</guid>
      <pubDate>Thu, 09 Apr 2026 14:46:17 GMT</pubDate>
      <description><![CDATA[<a href="https://news.google.com/rss/articles/test-3" target="_blank">NATO's Rutte told allies Trump wants Hormuz commitments within days, diplomats say</a>&nbsp;&nbsp;<font color="#6f6f6f">Reuters</font>]]></description>
      <source url="https://www.reuters.com">Reuters</source>
    </item>
  </channel>
</rss>
"""
        source = ReutersRSSSource(make_config())

        metadata = source._parse_feed(xml_text)
        post = metadata["posts"][0]

        self.assertEqual(
            post.body_text,
            "NATO's Rutte told allies Trump wants Hormuz commitments within days, diplomats say",
        )

    def test_fetch_posts_enriches_reuters_items_with_article_summary(self) -> None:
        source = ReutersRSSSource(make_config())
        post = SourcePost(
            source_id="rss:reuters",
            source_name="Reuters",
            id="reuters-1",
            account_handle="Reuters",
            created_at="2026-04-09T14:46:17Z",
            url="https://news.google.com/rss/articles/test-3",
            body_text="NATO's Rutte told allies Trump wants Hormuz commitments within days, diplomats say",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={
                "title": "NATO's Rutte told allies Trump wants Hormuz commitments within days, diplomats say - Reuters",
                "link": "https://news.google.com/rss/articles/test-3",
                "description": "",
                "source": "Reuters",
            },
        )

        with patch("news_bot.reuters.RSSFeedSource.fetch_posts", return_value=[post]):
            with patch.object(
                ReutersRSSSource,
                "_fetch_article_summary",
                return_value=(
                    "https://www.reuters.com/world/test-story/",
                    "NATO Secretary General Mark Rutte urged allies to line up commitments on Hormuz within days, diplomats said.",
                ),
            ):
                posts = source.fetch_posts()

        self.assertEqual(len(posts), 1)
        enriched = posts[0]
        self.assertEqual(enriched.url, "https://www.reuters.com/world/test-story/")
        self.assertEqual(
            enriched.body_text,
            "NATO's Rutte told allies Trump wants Hormuz commitments within days, diplomats say\n\nNATO Secretary General Mark Rutte urged allies to line up commitments on Hormuz within days, diplomats said.",
        )


if __name__ == "__main__":
    unittest.main()
