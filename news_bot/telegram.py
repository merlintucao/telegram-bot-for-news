from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .models import MediaAttachment, SourcePost

LOGGER = logging.getLogger(__name__)


class TelegramError(RuntimeError):
    pass


class TelegramSender:
    def __init__(self, bot_token: str, chat_id: str, timeout_seconds: int = 20) -> None:
        if not bot_token:
            raise TelegramError("TELEGRAM_BOT_TOKEN is required.")

        self.bot_token = bot_token
        self.chat_id = chat_id
        self.timeout_seconds = timeout_seconds

    def _resolve_chat_id(self, chat_id: str | None) -> str:
        resolved = chat_id or self.chat_id
        if not resolved:
            raise TelegramError("A Telegram chat id is required.")
        return resolved

    def _call_api(self, method: str, payload: dict[str, object]) -> dict[str, object]:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        body = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"},
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise TelegramError(f"Telegram HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise TelegramError(f"Telegram request failed: {exc.reason}") from exc

        parsed = json.loads(raw)
        if not parsed.get("ok"):
            raise TelegramError(f"Telegram API error: {parsed}")
        return parsed

    def send_message(self, text: str, chat_id: str | None = None) -> None:
        self._call_api(
            "sendMessage",
            {
                "chat_id": self._resolve_chat_id(chat_id),
                "text": text,
                "disable_web_page_preview": False,
            },
        )

    def send_post(
        self,
        post: SourcePost,
        text: str,
        chat_id: str | None = None,
        media_caption: str | None = None,
    ) -> None:
        resolved_chat_id = self._resolve_chat_id(chat_id)
        if post.media_attachments:
            try:
                self._send_attachments(
                    post.media_attachments,
                    chat_id=resolved_chat_id,
                    caption=media_caption or "",
                )
            except TelegramError as exc:
                LOGGER.warning(
                    "Telegram media delivery failed for post %s; falling back to text only: %s",
                    post.id,
                    exc,
                )

        self.send_message(text, chat_id=resolved_chat_id)

    def _send_attachments(
        self,
        attachments: tuple[MediaAttachment, ...],
        chat_id: str,
        caption: str,
    ) -> None:
        if len(attachments) == 1:
            self._send_attachment(attachments[0], chat_id=chat_id, caption=caption)
            return

        album_ready = all(
            self._telegram_media_type(attachment) in {"photo", "video"}
            for attachment in attachments
        )
        if album_ready:
            for chunk_start in range(0, len(attachments), 10):
                chunk = attachments[chunk_start : chunk_start + 10]
                media = [
                    {
                        "type": self._telegram_media_type(attachment),
                        "media": attachment.url,
                        **(
                            {"caption": _trim_caption(caption)}
                            if chunk_start == 0 and index == 0 and caption
                            else {}
                        ),
                    }
                    for index, attachment in enumerate(chunk)
                ]
                self._call_api(
                    "sendMediaGroup",
                    {
                        "chat_id": chat_id,
                        "media": media,
                    },
                )
            return

        for attachment in attachments:
            self._send_attachment(attachment, chat_id=chat_id, caption=caption)
            caption = ""

    def _send_attachment(
        self,
        attachment: MediaAttachment,
        chat_id: str,
        caption: str = "",
    ) -> None:
        media_type = self._telegram_media_type(attachment)
        if media_type == "photo":
            method = "sendPhoto"
            payload = {"chat_id": chat_id, "photo": attachment.url}
        elif media_type == "video":
            method = "sendVideo"
            payload = {"chat_id": chat_id, "video": attachment.url}
        else:
            method = "sendDocument"
            payload = {"chat_id": chat_id, "document": attachment.url}

        if caption:
            payload["caption"] = _trim_caption(caption)
        self._call_api(method, payload)

    @staticmethod
    def _telegram_media_type(attachment: MediaAttachment) -> str:
        kind = attachment.kind.strip().lower()
        if kind in {"image", "photo"}:
            return "photo"
        if kind in {"video", "gifv", "gif"}:
            return "video"
        return "document"


def _trim_caption(text: str, limit: int = 1024) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
