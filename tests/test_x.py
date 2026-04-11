from __future__ import annotations

import unittest

from news_bot.x import (
    _dedupe_and_sort_posts,
    _extract_x_timeline_items_from_graphql,
    _filter_newer_posts,
    _normalize_x_created_at,
    _normalize_x_item,
    _normalize_x_tweet_result,
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


if __name__ == "__main__":
    unittest.main()
