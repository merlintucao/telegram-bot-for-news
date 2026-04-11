from __future__ import annotations

import unittest
from pathlib import Path

from news_bot.config import AppConfig
from news_bot.models import SourcePost
from news_bot.source_types import SourceError
from news_bot.trump_source import ResilientTrumpSource, TrumpFallbackFeedSource


def make_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("truthsocial_trump",),
        rss_feed_urls=(),
        truthsocial_fallback_feed_urls=("https://example.com/trump-feed.xml",),
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
    )


class FailingPrimary:
    source_id = "truthsocial:realDonaldTrump"
    source_name = "Truth Social"

    def fetch_posts(self, since_id: str | None = None, limit: int | None = None) -> list[SourcePost]:
        raise SourceError("primary blocked")

    def probe(self):
        raise SourceError("primary blocked")


class StaticTrumpFallbackFeed(TrumpFallbackFeedSource):
    def __init__(self, config: AppConfig, posts: list[SourcePost]) -> None:
        super().__init__(config, "https://example.com/trump-feed.xml")
        self._posts = posts

    def _fetch_feed_metadata(self):  # type: ignore[override]
        return {"source_name": "Trump Mirror", "posts": self._posts}


class ResilientTrumpSourceTests(unittest.TestCase):
    def test_fallback_feed_normalizes_truthsocial_link(self) -> None:
        config = make_config()
        fallback_post = SourcePost(
            source_id="rss:example",
            source_name="Trump Mirror",
            id="mirror-1",
            account_handle="Trump Mirror",
            created_at="2026-04-09T08:00:00Z",
            url="https://example.com/trump-post-1",
            body_text=(
                "Mirror text https://truthsocial.com/@realDonaldTrump/posts/116372694697146221"
            ),
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"link": "https://example.com/trump-post-1"},
        )
        fallback = StaticTrumpFallbackFeed(config, [fallback_post])

        posts = fallback.fetch_posts(limit=1)

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].source_id, "truthsocial:realDonaldTrump")
        self.assertEqual(posts[0].id, "116372694697146221")
        self.assertEqual(
            posts[0].url,
            "https://truthsocial.com/@realDonaldTrump/posts/116372694697146221",
        )

    def test_resilient_source_uses_fallback_when_primary_fails(self) -> None:
        config = make_config()
        fallback_post = SourcePost(
            source_id="rss:example",
            source_name="Trump Mirror",
            id="mirror-1",
            account_handle="Trump Mirror",
            created_at="2026-04-09T08:00:00Z",
            url="https://example.com/trump-post-1",
            body_text=(
                "Mirror text https://truthsocial.com/@realDonaldTrump/posts/116372694697146221"
            ),
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={"link": "https://example.com/trump-post-1"},
        )
        fallback = StaticTrumpFallbackFeed(config, [fallback_post])
        source = ResilientTrumpSource(
            config,
            primary=FailingPrimary(),  # type: ignore[arg-type]
            fallbacks=(fallback,),
        )

        posts = source.fetch_posts(limit=1)

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0].id, "116372694697146221")
        self.assertEqual(posts[0].source_id, "truthsocial:realDonaldTrump")

    def test_resilient_source_excludes_retruths_from_primary(self) -> None:
        config = make_config()

        class StaticPrimary:
            source_id = "truthsocial:realDonaldTrump"
            source_name = "Truth Social"

            def fetch_posts(self, since_id: str | None = None, limit: int | None = None) -> list[SourcePost]:
                return [
                    SourcePost(
                        source_id="truthsocial:realDonaldTrump",
                        source_name="Truth Social",
                        id="201",
                        account_handle="realDonaldTrump",
                        created_at="2026-04-09T08:00:00Z",
                        url="https://truthsocial.com/@realDonaldTrump/posts/201",
                        body_text="Retruthed text",
                        is_reply=False,
                        is_reblog=True,
                        media_attachments=(),
                        raw_payload={"id": "201"},
                    ),
                    SourcePost(
                        source_id="truthsocial:realDonaldTrump",
                        source_name="Truth Social",
                        id="202",
                        account_handle="realDonaldTrump",
                        created_at="2026-04-09T08:01:00Z",
                        url="https://truthsocial.com/@realDonaldTrump/posts/202",
                        body_text="Original trump post",
                        is_reply=False,
                        is_reblog=False,
                        media_attachments=(),
                        raw_payload={"id": "202"},
                    ),
                ]

            def probe(self):
                raise SourceError("unused")

        source = ResilientTrumpSource(
            config,
            primary=StaticPrimary(),  # type: ignore[arg-type]
            fallbacks=(),
        )

        posts = source.fetch_posts(limit=10)

        self.assertEqual([post.id for post in posts], ["202"])

    def test_fallback_prefers_original_fields_from_feed_payload(self) -> None:
        config = make_config()
        fallback_post = SourcePost(
            source_id="rss:example",
            source_name="Trump Mirror",
            id="mirror-1",
            account_handle="Trump Mirror",
            created_at="2026-04-09T08:00:00Z",
            url="https://example.com/trump-post-1",
            body_text="Mirror text without truthsocial url",
            is_reply=False,
            is_reblog=False,
            media_attachments=(),
            raw_payload={
                "link": "https://example.com/trump-post-1",
                "originalUrl": "https://truthsocial.com/@realDonaldTrump/116372694697146221",
                "originalId": "116372694697146221",
            },
        )
        fallback = StaticTrumpFallbackFeed(config, [fallback_post])

        posts = fallback.fetch_posts(limit=1)

        self.assertEqual(posts[0].id, "116372694697146221")
        self.assertEqual(
            posts[0].url,
            "https://truthsocial.com/@realDonaldTrump/116372694697146221",
        )


if __name__ == "__main__":
    unittest.main()
