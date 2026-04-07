from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from news_bot.config import AppConfig
from news_bot.truthsocial import TruthSocialClient


def make_config(cookie_file: Path) -> AppConfig:
    return AppConfig(
        telegram_bot_token="token",
        telegram_chat_id="@chat",
        source_chat_routes=(),
        source_keyword_filters=(),
        source_category_filters=(),
        enabled_sources=("truthsocial_trump",),
        rss_feed_urls=(),
        truthsocial_handle="realDonaldTrump",
        truthsocial_account_id="123",
        truthsocial_base_url="https://truthsocial.com",
        truthsocial_cookies_file=cookie_file,
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


def write_cookie_file(path: Path, value: str) -> None:
    payload = [
        {
            "name": "session",
            "value": value,
            "domain": ".truthsocial.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
        }
    ]
    path.write_text(json.dumps(payload), encoding="utf-8")


class TruthSocialClientTests(unittest.TestCase):
    def test_cookie_jar_reloads_when_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cookie_path = Path(tmpdir) / "truthsocial_cookies.json"
            write_cookie_file(cookie_path, "one")
            client = TruthSocialClient(make_config(cookie_path))

            self.assertEqual(next(iter(client.cookie_jar)).value, "one")

            write_cookie_file(cookie_path, "updated-two")
            client._reload_cookies_if_needed()

            self.assertEqual(next(iter(client.cookie_jar)).value, "updated-two")


if __name__ == "__main__":
    unittest.main()
