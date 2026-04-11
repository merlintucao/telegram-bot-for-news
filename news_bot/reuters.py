from __future__ import annotations

import html
import re
from xml.etree import ElementTree as ET

from .config import AppConfig
from .html_text import html_to_text
from .models import SourcePost
from .rss import RSSFeedSource, _child_text, _children, _local_name
from .source_types import SourceError

_REUTERS_SUFFIX_RE = re.compile(r"\s*-\s*Reuters\s*$", flags=re.IGNORECASE)


def _strip_reuters_suffix(text: str) -> str:
    return _REUTERS_SUFFIX_RE.sub("", text or "").strip()


def _normalize_reuters_snippet(title: str, description_html: str) -> str:
    title_clean = _strip_reuters_suffix(title)
    description_text = html_to_text(html.unescape(description_html or ""))
    description_text = re.sub(r"\s*\(https?://[^\s)]+\)\s*", " ", description_text)
    description_clean = _strip_reuters_suffix(description_text)
    if description_clean.casefold() == title_clean.casefold():
        return title_clean
    if description_clean.startswith(title_clean):
        description_clean = description_clean[len(title_clean) :].strip(" -:\n")
    description_clean = re.sub(r"\bReuters\b$", "", description_clean, flags=re.IGNORECASE).strip(" -:\n")
    if not description_clean:
        return title_clean
    return f"{title_clean}\n\n{description_clean}"


class ReutersRSSSource(RSSFeedSource):
    def __init__(self, config: AppConfig, feed_url: str | None = None) -> None:
        super().__init__(config, feed_url or config.reuters_rss_url)
        self.source_id = "rss:reuters"
        self.source_name = "Reuters"

    def fetch_posts(
        self,
        since_id: str | None = None,
        limit: int | None = None,
    ) -> list[SourcePost]:
        return super().fetch_posts(since_id=since_id, limit=limit)

    def _parse_rss_feed(self, root: ET.Element) -> dict[str, object]:
        channel = next((child for child in root if _local_name(child.tag) == "channel"), None)
        if channel is None:
            raise SourceError(f"RSS feed {self.feed_url} is missing a channel element.")

        posts = []
        for item in _children(channel, "item"):
            source_name = _child_text(item, ("source",)).strip()
            source_url = ""
            source_element = next((child for child in item if _local_name(child.tag) == "source"), None)
            if source_element is not None:
                source_url = str(source_element.get("url") or "").strip()
            title = _child_text(item, ("title",))
            description = _child_text(item, ("description", "encoded", "content"))
            source_hint = " ".join(
                part for part in (title, description, source_name, source_url) if part
            )
            if "reuters" not in source_hint.lower():
                continue
            posts.append(self._parse_rss_item(item, "Reuters"))
        return {"source_name": "Reuters", "posts": posts}

    def _parse_rss_item(self, item: ET.Element, feed_title: str) -> SourcePost:
        post = super()._parse_rss_item(item, feed_title)
        title = str(post.raw_payload.get("title") or "")
        description = str(post.raw_payload.get("description") or "")
        source_name = str(post.raw_payload.get("source") or "Reuters").strip() or "Reuters"
        return SourcePost(
            source_id=self.source_id,
            source_name="Reuters",
            id=post.id,
            account_handle=source_name,
            created_at=post.created_at,
            url=post.url,
            body_text=_normalize_reuters_snippet(title, description),
            is_reply=False,
            is_reblog=False,
            media_attachments=post.media_attachments,
            raw_payload={
                **post.raw_payload,
                "source": source_name,
                "feed_title": feed_title,
            },
            categories=post.categories,
        )
