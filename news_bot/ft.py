from __future__ import annotations

from .config import AppConfig
from .models import SourcePost
from .rss import RSSFeedSource


def _normalize_ft_story_text(title: str, description: str) -> str:
    description_clean = (description or "").strip()
    title_clean = (title or "").strip()
    if description_clean:
        return description_clean
    return title_clean


class FTRSSSource(RSSFeedSource):
    def __init__(self, config: AppConfig, feed_url: str | None = None) -> None:
        super().__init__(config, feed_url or config.ft_rss_url)
        self.source_id = "rss:ft"
        self.source_name = "FT"

    def _parse_rss_feed(self, root):  # type: ignore[override]
        metadata = super()._parse_rss_feed(root)
        metadata["source_name"] = "FT"
        posts = [
            SourcePost(
                source_id=self.source_id,
                source_name="FT",
                id=post.id,
                account_handle="FT",
                created_at=post.created_at,
                url=post.url,
                body_text=_normalize_ft_story_text(
                    str(post.raw_payload.get("title") or ""),
                    str(post.raw_payload.get("description") or ""),
                ),
                is_reply=post.is_reply,
                is_reblog=post.is_reblog,
                media_attachments=post.media_attachments,
                raw_payload={
                    **post.raw_payload,
                    "feed_title": metadata.get("source_name", "FT"),
                },
                categories=post.categories,
            )
            for post in metadata["posts"]
        ]
        metadata["posts"] = posts
        return metadata
