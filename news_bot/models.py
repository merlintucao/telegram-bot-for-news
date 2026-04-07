from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class MediaAttachment:
    kind: str
    url: str
    preview_url: str | None = None
    description: str | None = None


@dataclass(slots=True)
class SourcePost:
    source_id: str
    source_name: str
    id: str
    account_handle: str
    created_at: str
    url: str
    body_text: str
    is_reply: bool
    is_reblog: bool
    media_attachments: tuple[MediaAttachment, ...]
    raw_payload: dict[str, Any]
    categories: tuple[str, ...] = ()

    @property
    def sort_key(self) -> tuple[str, str]:
        if self.id.isdigit():
            stable_id = f"{int(self.id):020d}"
        else:
            stable_id = self.id
        return (self.created_at or "", stable_id)

    @property
    def media_urls(self) -> tuple[str, ...]:
        return tuple(attachment.url for attachment in self.media_attachments if attachment.url)
