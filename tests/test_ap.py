from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from news_bot.ap import APWorldRSSSource, _extract_ap_summary_from_html
from news_bot.config import AppConfig, DEFAULT_AP_WORLD_RSS_URL
from news_bot.models import SourcePost


def make_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("ap_world_rss",),
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
        ap_world_rss_url=DEFAULT_AP_WORLD_RSS_URL,
    )


class APWorldRSSSourceTests(unittest.TestCase):
    def test_extract_ap_summary_from_html_prefers_meta_description(self) -> None:
        html_text = """
<html><head>
  <meta property="og:description" content="Israeli Prime Minister Benjamin Netanyahu says he has authorized direct negotiations with Lebanon as soon as possible.">
</head></html>
"""
        self.assertEqual(
            _extract_ap_summary_from_html(html_text),
            "Israeli Prime Minister Benjamin Netanyahu says he has authorized direct negotiations with Lebanon as soon as possible.",
        )

    def test_parse_ap_world_feed_uses_ap_identity(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>World News: Top &amp; Breaking World News Today | AP News</title>
    <item>
      <title>Netanyahu authorizes direct talks with Lebanon ‘as soon as possible’</title>
      <description></description>
      <link>https://apnews.com/article/test-story</link>
      <guid isPermaLink="false">https://apnews.com/article/test-story</guid>
      <pubDate>Thu, 09 Apr 2026 16:05:14 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
        source = APWorldRSSSource(make_config())

        metadata = source._parse_feed(xml_text)

        self.assertEqual(metadata["source_name"], "AP News")
        self.assertEqual(len(metadata["posts"]), 1)
        post = metadata["posts"][0]
        self.assertEqual(post.source_id, "rss:ap-world")
        self.assertEqual(post.source_name, "AP News")
        self.assertEqual(post.account_handle, "AP News")
        self.assertEqual(post.url, "https://apnews.com/article/test-story")
        self.assertEqual(post.body_text, "Netanyahu authorizes direct talks with Lebanon ‘as soon as possible’")

    def test_fetch_posts_enriches_ap_items_with_article_summary(self) -> None:
        source = APWorldRSSSource(make_config())
        post = SourcePost(
            source_id="rss:ap-world",
            source_name="AP News",
            id="ap-1",
            account_handle="AP News",
            created_at="2026-04-09T16:05:14Z",
            url="https://apnews.com/article/test-story",
            body_text="Netanyahu authorizes direct talks with Lebanon ‘as soon as possible’",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={
                "title": "Netanyahu authorizes direct talks with Lebanon ‘as soon as possible’",
                "link": "https://apnews.com/article/test-story",
                "description": "",
            },
        )

        with patch("news_bot.ap.RSSFeedSource.fetch_posts", return_value=[post]):
            with patch.object(
                APWorldRSSSource,
                "_fetch_article_summary",
                return_value=(
                    "Israeli Prime Minister Benjamin Netanyahu says he has authorized direct negotiations with Lebanon as soon as possible."
                ),
            ):
                posts = source.fetch_posts()

        self.assertEqual(len(posts), 1)
        enriched = posts[0]
        self.assertEqual(
            enriched.body_text,
            "Israeli Prime Minister Benjamin Netanyahu says he has authorized direct negotiations with Lebanon as soon as possible.",
        )


if __name__ == "__main__":
    unittest.main()
