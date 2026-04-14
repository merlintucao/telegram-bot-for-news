from __future__ import annotations

import html
import re

from .config import AppConfig
from .html_text import html_to_text
from .models import SourcePost
from .rss import RSSFeedSource


def _normalize_investing_story_text(title: str, description: str) -> str:
    title_clean = (title or "").strip()
    description_text = html_to_text(html.unescape(description or ""))
    description_clean = re.sub(r"\s*\(https?://[^\s)]+\)\s*", " ", description_text)
    description_clean = re.sub(r"\s+", " ", description_clean).strip()
    if description_clean:
        if description_clean.casefold() == title_clean.casefold():
            return title_clean
        if title_clean and description_clean.startswith(title_clean):
            description_clean = description_clean[len(title_clean) :].strip(" -:\n")
        description_clean = re.sub(
            r"\bInvesting(?:\.com)?\b$",
            "",
            description_clean,
            flags=re.IGNORECASE,
        ).strip(" -:\n")
        if description_clean:
            return description_clean
    return title_clean


class InvestingRSSSource(RSSFeedSource):
    def __init__(self, config: AppConfig, feed_url: str | None = None) -> None:
        super().__init__(config, feed_url or config.investing_rss_url)
        self.source_id = "rss:investing"
        self.source_name = "Investing"

    def _parse_rss_feed(self, root):  # type: ignore[override]
        metadata = super()._parse_rss_feed(root)
        metadata["source_name"] = "Investing"
        posts = [
            SourcePost(
                source_id=self.source_id,
                source_name="Investing",
                id=post.id,
                account_handle="Investing",
                created_at=post.created_at,
                url=post.url,
                body_text=_normalize_investing_story_text(
                    str(post.raw_payload.get("title") or ""),
                    str(post.raw_payload.get("description") or ""),
                ),
                is_reply=post.is_reply,
                is_reblog=post.is_reblog,
                media_attachments=post.media_attachments,
                raw_payload={
                    **post.raw_payload,
                    "feed_title": metadata.get("source_name", "Investing"),
                },
                categories=post.categories,
            )
            for post in metadata["posts"]
        ]
        metadata["posts"] = posts
        return metadata
