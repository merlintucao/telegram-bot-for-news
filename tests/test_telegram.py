from __future__ import annotations

import unittest

from news_bot.models import MediaAttachment, SourcePost
from news_bot.telegram import TelegramError, TelegramSender


def make_post(*attachments: MediaAttachment) -> SourcePost:
    return SourcePost(
        source_id="truthsocial:realDonaldTrump",
        source_name="Truth Social",
        id="101",
        account_handle="realDonaldTrump",
        created_at="2026-04-07T08:00:00Z",
        url="https://truthsocial.com/@realDonaldTrump/posts/101",
        body_text="hello world",
        is_reply=False,
        is_reblog=False,
        media_attachments=tuple(attachments),
        raw_payload={"id": "101"},
    )


class RecordingSender(TelegramSender):
    def __init__(self, fail_methods: set[str] | None = None) -> None:
        super().__init__(bot_token="token", chat_id="@chat", timeout_seconds=1)
        self.fail_methods = fail_methods or set()
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _call_api(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        self.calls.append((method, payload))
        if method in self.fail_methods:
            raise TelegramError(f"forced failure for {method}")
        return {"ok": True}


class TelegramSenderTests(unittest.TestCase):
    def test_send_post_uses_photo_and_text_for_single_image(self) -> None:
        sender = RecordingSender()
        post = make_post(MediaAttachment(kind="image", url="https://cdn.example.com/a.jpg"))

        sender.send_post(post, "body", chat_id="@photos", media_caption="caption text")

        self.assertEqual(sender.calls[0][0], "sendPhoto")
        self.assertEqual(sender.calls[1][0], "sendMessage")
        self.assertEqual(sender.calls[0][1]["chat_id"], "@photos")
        self.assertEqual(sender.calls[1][1]["chat_id"], "@photos")
        self.assertEqual(sender.calls[0][1]["caption"], "caption text")

    def test_send_post_uses_media_group_for_multiple_visual_attachments(self) -> None:
        sender = RecordingSender()
        post = make_post(
            MediaAttachment(kind="image", url="https://cdn.example.com/a.jpg"),
            MediaAttachment(kind="video", url="https://cdn.example.com/b.mp4"),
        )

        sender.send_post(post, "body", media_caption="caption text")

        self.assertEqual(sender.calls[0][0], "sendMediaGroup")
        self.assertEqual(sender.calls[1][0], "sendMessage")
        media = sender.calls[0][1]["media"]
        self.assertEqual(media[0]["caption"], "caption text")

    def test_send_post_falls_back_to_text_if_media_send_fails(self) -> None:
        sender = RecordingSender(fail_methods={"sendPhoto"})
        post = make_post(MediaAttachment(kind="image", url="https://cdn.example.com/a.jpg"))

        sender.send_post(post, "body", chat_id="@fallback", media_caption="caption text")

        self.assertEqual(sender.calls[0][0], "sendPhoto")
        self.assertEqual(sender.calls[1][0], "sendMessage")
        self.assertEqual(sender.calls[1][1]["chat_id"], "@fallback")


if __name__ == "__main__":
    unittest.main()
