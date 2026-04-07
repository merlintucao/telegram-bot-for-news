from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from news_bot.config import AppConfig


class ConfigTests(unittest.TestCase):
    def test_from_env_defaults_to_trump_public_account_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            env_path = Path(tmpdir) / ".env"
            env_path.write_text(
                "\n".join(
                    (
                        "TELEGRAM_BOT_TOKEN=token",
                        "TELEGRAM_CHAT_ID=@chat",
                        "TRUTHSOCIAL_HANDLE=realDonaldTrump",
                    )
                ),
                encoding="utf-8",
            )

            with patch.dict("os.environ", {}, clear=True):
                config = AppConfig.from_env(env_file=env_path)

        self.assertEqual(config.truthsocial_account_id, "107780257626128497")
        self.assertEqual(config.truthsocial_auth_mode, "auto")


if __name__ == "__main__":
    unittest.main()
