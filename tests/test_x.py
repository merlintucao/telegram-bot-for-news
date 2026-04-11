from __future__ import annotations

import asyncio
import sqlite3
import unittest
from pathlib import Path

from news_bot.config import AppConfig, DEFAULT_AP_WORLD_RSS_URL, DEFAULT_FT_RSS_URL, DEFAULT_REUTERS_RSS_URL
from news_bot.x import (
    XKobeissiLetterSource,
    _dedupe_and_sort_posts,
    _extract_x_timeline_items_from_graphql,
    _filter_newer_posts,
    _normalize_x_created_at,
    _normalize_x_item,
    _normalize_x_tweet_result,
)


def make_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("x_kobeissi_letter",),
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
        x_kobeissi_url="https://x.com/KobeissiLetter",
        x_backend="twscrape",
        x_auth_mode="cookies",
        x_cookies_file=Path("x_cookies.json"),
        x_profile_dir=None,
        x_poll_limit=20,
        x_headless=True,
        x_twscrape_db_path=Path("data/test_x_accounts.db"),
        x_twscrape_account_username="x_session",
    )


def make_item(
    *,
    post_id: str,
    text: str = "Post body",
    quote_text: str = "",
    is_reply: bool = False,
    is_reblog: bool = False,
    is_ad: bool = False,
) -> dict[str, object]:
    return {
        "url": f"https://x.com/KobeissiLetter/status/{post_id}",
        "handle": "KobeissiLetter",
        "text": text,
        "quote_text": quote_text,
        "created_at": "2026-04-11T12:00:00Z",
        "media": [],
        "is_reply": is_reply,
        "is_reblog": is_reblog,
        "is_ad": is_ad,
    }


class XSourceHelpersTests(unittest.TestCase):
    def test_normalize_x_created_at_converts_twitter_timestamp(self) -> None:
        self.assertEqual(
            _normalize_x_created_at("Sat Apr 11 03:25:59 +0000 2026"),
            "2026-04-11T03:25:59+00:00",
        )

    def test_normalize_x_item_includes_original_post(self) -> None:
        post = _normalize_x_item(
            make_item(post_id="100"),
            source_id="x:kobeissiletter",
            source_name="X | Kobeissi Letter",
            default_handle="KobeissiLetter",
        )

        self.assertIsNotNone(post)
        assert post is not None
        self.assertEqual(post.id, "100")
        self.assertEqual(post.source_id, "x:kobeissiletter")
        self.assertEqual(post.body_text, "Post body")

    def test_normalize_x_item_includes_quote_post_context(self) -> None:
        post = _normalize_x_item(
            make_item(post_id="101", text="Main thought", quote_text="Quoted context body"),
            source_id="x:kobeissiletter",
            source_name="X | Kobeissi Letter",
            default_handle="KobeissiLetter",
        )

        self.assertIsNotNone(post)
        assert post is not None
        self.assertIn("Main thought", post.body_text)
        self.assertIn("Quoted context: Quoted context body", post.body_text)
        self.assertTrue(post.raw_payload["is_quote"])

    def test_normalize_x_item_excludes_reply(self) -> None:
        post = _normalize_x_item(
            make_item(post_id="102", is_reply=True),
            source_id="x:kobeissiletter",
            source_name="X | Kobeissi Letter",
            default_handle="KobeissiLetter",
        )
        self.assertIsNone(post)

    def test_normalize_x_item_excludes_reblog(self) -> None:
        post = _normalize_x_item(
            make_item(post_id="103", is_reblog=True),
            source_id="x:kobeissiletter",
            source_name="X | Kobeissi Letter",
            default_handle="KobeissiLetter",
        )
        self.assertIsNone(post)

    def test_dedupe_and_sort_posts_keeps_latest_unique_posts(self) -> None:
        first = _normalize_x_item(
            make_item(post_id="100"),
            source_id="x:kobeissiletter",
            source_name="X | Kobeissi Letter",
            default_handle="KobeissiLetter",
        )
        duplicate = _normalize_x_item(
            make_item(post_id="100", text="Duplicate copy"),
            source_id="x:kobeissiletter",
            source_name="X | Kobeissi Letter",
            default_handle="KobeissiLetter",
        )
        latest = _normalize_x_item(
            make_item(post_id="101"),
            source_id="x:kobeissiletter",
            source_name="X | Kobeissi Letter",
            default_handle="KobeissiLetter",
        )

        assert first is not None and duplicate is not None and latest is not None
        posts = _dedupe_and_sort_posts([first, duplicate, latest])

        self.assertEqual([post.id for post in posts], ["101", "100"])

    def test_filter_newer_posts_uses_numeric_ids(self) -> None:
        first = _normalize_x_item(
            make_item(post_id="100"),
            source_id="x:kobeissiletter",
            source_name="X | Kobeissi Letter",
            default_handle="KobeissiLetter",
        )
        latest = _normalize_x_item(
            make_item(post_id="101"),
            source_id="x:kobeissiletter",
            source_name="X | Kobeissi Letter",
            default_handle="KobeissiLetter",
        )

        assert first is not None and latest is not None
        posts = _filter_newer_posts([latest, first], "100")

        self.assertEqual([post.id for post in posts], ["101"])

    def test_normalize_x_tweet_result_supports_current_graphql_shape(self) -> None:
        tweet = {
            "__typename": "Tweet",
            "rest_id": "2042806338443419839",
            "legacy": {
                "created_at": "Sat Apr 11 03:25:59 +0000 2026",
                "full_text": "Fallback text",
                "entities": {
                    "media": [
                        {
                            "type": "photo",
                            "media_url_https": "https://pbs.twimg.com/media/example.jpg",
                            "expanded_url": "https://x.com/KobeissiLetter/status/2042806338443419839/photo/1",
                        }
                    ]
                },
            },
            "note_tweet": {
                "note_tweet_results": {
                    "result": {
                        "text": "Gold is reshaping the global financial system."
                    }
                }
            },
            "core": {
                "user_results": {
                    "result": {
                        "core": {
                            "screen_name": "KobeissiLetter",
                        }
                    }
                }
            },
            "quoted_status_result": {
                "result": {
                    "__typename": "Tweet",
                    "rest_id": "111",
                    "legacy": {"full_text": "Quoted context"},
                    "core": {
                        "user_results": {
                            "result": {
                                "core": {"screen_name": "OtherAccount"}
                            }
                        }
                    },
                }
            },
        }

        item = _normalize_x_tweet_result(tweet, default_handle="KobeissiLetter")

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item["url"], "https://x.com/KobeissiLetter/status/2042806338443419839")
        self.assertEqual(item["text"], "Gold is reshaping the global financial system.")
        self.assertEqual(item["quote_text"], "Quoted context")
        self.assertEqual(item["quote_url"], "https://x.com/OtherAccount/status/111")
        self.assertEqual(item["media"][0]["kind"], "image")

    def test_extract_x_timeline_items_from_graphql_reads_entries(self) -> None:
        payload = {
            "data": {
                "user": {
                    "result": {
                        "timeline": {
                            "timeline": {
                                "instructions": [
                                    {
                                        "type": "TimelinePinEntry",
                                        "entry": {
                                            "entryId": "tweet-200",
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": {
                                                            "__typename": "Tweet",
                                                            "rest_id": "200",
                                                            "legacy": {
                                                                "created_at": "Sat Apr 11 03:25:59 +0000 2026",
                                                                "full_text": "Pinned post",
                                                            },
                                                            "core": {
                                                                "user_results": {
                                                                    "result": {
                                                                        "core": {
                                                                            "screen_name": "KobeissiLetter"
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                        }
                                                    }
                                                }
                                            },
                                        },
                                    },
                                    {
                                        "type": "TimelineAddEntries",
                                        "entries": [
                                            {
                                                "entryId": "tweet-201",
                                                "content": {
                                                    "itemContent": {
                                                        "tweet_results": {
                                                            "result": {
                                                                "__typename": "Tweet",
                                                                "rest_id": "201",
                                                                "legacy": {
                                                                    "created_at": "Sat Apr 11 04:25:59 +0000 2026",
                                                                    "full_text": "Latest post",
                                                                },
                                                                "core": {
                                                                    "user_results": {
                                                                        "result": {
                                                                            "core": {
                                                                                "screen_name": "KobeissiLetter"
                                                                            }
                                                                        }
                                                                    }
                                                                },
                                                            }
                                                        }
                                                    }
                                                },
                                            }
                                        ],
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        }

        items = _extract_x_timeline_items_from_graphql(payload, default_handle="KobeissiLetter")

        self.assertEqual([item["url"] for item in items], [
            "https://x.com/KobeissiLetter/status/200",
            "https://x.com/KobeissiLetter/status/201",
        ])

    def test_prepare_twscrape_db_file_keeps_valid_sqlite_db_with_accounts_table(self) -> None:
        config = make_config()
        config.x_twscrape_db_path = Path("data/test_x_accounts_keep.db")
        db_path = config.x_twscrape_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE accounts (username TEXT PRIMARY KEY)")
            connection.commit()

        source = XKobeissiLetterSource(config)
        prepared = source._prepare_twscrape_db_file()

        self.assertEqual(prepared, db_path)
        self.assertTrue(db_path.exists())
        db_path.unlink()

    def test_prepare_twscrape_db_file_removes_zero_byte_db(self) -> None:
        config = make_config()
        config.x_twscrape_db_path = Path("data/test_x_accounts_zero.db")
        db_path = config.x_twscrape_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_bytes(b"")

        source = XKobeissiLetterSource(config)
        prepared = source._prepare_twscrape_db_file()

        self.assertEqual(prepared, db_path)
        self.assertFalse(db_path.exists())

    def test_prepare_twscrape_db_file_removes_schema_less_sqlite_db(self) -> None:
        config = make_config()
        config.x_twscrape_db_path = Path("data/test_x_accounts_schema_less.db")
        db_path = config.x_twscrape_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE something_else (id INTEGER PRIMARY KEY)")
            connection.commit()

        source = XKobeissiLetterSource(config)
        prepared = source._prepare_twscrape_db_file()

        self.assertEqual(prepared, db_path)
        self.assertFalse(db_path.exists())

    def test_prepare_twscrape_db_file_removes_invalid_sqlite_file(self) -> None:
        config = make_config()
        config.x_twscrape_db_path = Path("data/test_x_accounts_invalid.db")
        db_path = config.x_twscrape_db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.write_text("not-a-sqlite-db", encoding="utf-8")

        source = XKobeissiLetterSource(config)
        prepared = source._prepare_twscrape_db_file()

        self.assertEqual(prepared, db_path)
        self.assertFalse(db_path.exists())

    def test_ensure_twscrape_account_updates_existing_account(self) -> None:
        config = make_config()
        source = XKobeissiLetterSource(config)

        class FakeAccount:
            def __init__(self) -> None:
                self.user_agent = "old-agent"
                self.cookies = {"old": "cookie"}
                self.active = False
                self.locks = {"UserTweets": "stale-lock"}
                self.last_used = "yesterday"
                self.error_msg = "broken"

        class FakePool:
            def __init__(self) -> None:
                self.account = FakeAccount()
                self.add_calls = 0
                self.saved_account = None

            async def get_account(self, username: str):
                self.username = username
                return self.account

            async def add_account(self, **kwargs):
                self.add_calls += 1

            async def save(self, account):
                self.saved_account = account

        class FakeAPI:
            def __init__(self) -> None:
                self.pool = FakePool()

        api = FakeAPI()
        from news_bot import x as x_module

        original_loader = x_module.load_cookie_jar
        cookie1 = type("Cookie", (), {"name": "ct0", "value": "token"})()
        cookie2 = type("Cookie", (), {"name": "auth_token", "value": "auth"})()
        x_module.load_cookie_jar = lambda _path: [cookie1, cookie2]
        try:
            asyncio.run(
                source._ensure_twscrape_account(
                    api,
                    parse_cookies=lambda value: {"ct0": "token", "auth_token": "auth"},
                )
            )
        finally:
            x_module.load_cookie_jar = original_loader

        self.assertEqual(api.pool.add_calls, 0)
        self.assertIs(api.pool.saved_account, api.pool.account)
        self.assertEqual(api.pool.account.user_agent, "test-agent")
        self.assertEqual(api.pool.account.cookies["ct0"], "token")
        self.assertTrue(api.pool.account.active)
        self.assertEqual(api.pool.account.locks, {})
        self.assertIsNone(api.pool.account.last_used)
        self.assertIsNone(api.pool.account.error_msg)


if __name__ == "__main__":
    unittest.main()
