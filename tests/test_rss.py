from __future__ import annotations

import unittest
from pathlib import Path

from news_bot.config import AppConfig
from news_bot.rss import RSSFeedSource


def make_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("rss",),
        rss_feed_urls=("https://example.com/feed.xml",),
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
    )


class RSSFeedSourceTests(unittest.TestCase):
    def test_parse_rss_feed_returns_posts_and_title(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Example News</title>
    <item>
      <title>Story One</title>
      <link>https://example.com/story-one</link>
      <guid>story-1</guid>
      <pubDate>Tue, 07 Apr 2026 08:00:00 GMT</pubDate>
      <description><![CDATA[<p>Summary one</p>]]></description>
    </item>
    <item>
      <title>Story Two</title>
      <link>https://example.com/story-two</link>
      <guid>story-2</guid>
      <pubDate>Tue, 07 Apr 2026 09:00:00 GMT</pubDate>
      <description><![CDATA[<p>Summary two</p>]]></description>
      <enclosure url="https://example.com/image.jpg" type="image/jpeg" />
    </item>
  </channel>
</rss>
"""
        source = RSSFeedSource(make_config(), "https://example.com/feed.xml")

        metadata = source._parse_feed(xml_text)

        self.assertEqual(metadata["source_name"], "Example News")
        self.assertEqual(len(metadata["posts"]), 2)
        self.assertEqual(metadata["posts"][0].id, "story-1")
        self.assertIn("Story One", metadata["posts"][0].body_text)
        self.assertEqual(metadata["posts"][1].media_attachments[0].kind, "image")

    def test_parse_atom_feed_supports_since_id_filtering(self) -> None:
        xml_text = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Atom</title>
  <entry>
    <id>tag:example.com,2026:2</id>
    <title>Second</title>
    <updated>2026-04-07T09:00:00Z</updated>
    <link href="https://example.com/second" rel="alternate" />
    <summary>&lt;p&gt;Second summary&lt;/p&gt;</summary>
  </entry>
  <entry>
    <id>tag:example.com,2026:1</id>
    <title>First</title>
    <updated>2026-04-07T08:00:00Z</updated>
    <link href="https://example.com/first" rel="alternate" />
    <summary>&lt;p&gt;First summary&lt;/p&gt;</summary>
  </entry>
</feed>
"""
        class StaticRSSFeedSource(RSSFeedSource):
            def _fetch_feed_metadata(self):  # type: ignore[override]
                return self._parse_feed(xml_text)

        source = StaticRSSFeedSource(make_config(), "https://example.com/atom.xml")

        posts = source.fetch_posts(since_id="tag:example.com,2026:1")

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].id, "tag:example.com,2026:2")
        self.assertEqual(posts[0].source_name, "Example Atom")


if __name__ == "__main__":
    unittest.main()
