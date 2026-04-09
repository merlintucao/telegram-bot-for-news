from __future__ import annotations

import html
import logging
import re
import urllib.error
import urllib.request
from urllib.parse import urlparse
from xml.etree import ElementTree as ET

from .config import AppConfig
from .html_text import html_to_text
from .models import SourcePost
from .rss import RSSFeedSource, _child_text, _children, _local_name
from .source_types import SourceError

_REUTERS_SUFFIX_RE = re.compile(r"\s*-\s*Reuters\s*$", flags=re.IGNORECASE)
_META_DESCRIPTION_RE = re.compile(
    r"""<meta[^>]+(?:name|property)=["'](?:description|og:description|twitter:description)["'][^>]+content=["']([^"']+)["']""",
    flags=re.IGNORECASE,
)
_JSON_LD_DESCRIPTION_RE = re.compile(
    r'''"description"\s*:\s*"((?:[^"\\]|\\.)*)"''',
    flags=re.IGNORECASE,
)
LOGGER = logging.getLogger(__name__)


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


def _normalize_reuters_article_summary(title: str, summary_text: str) -> str:
    title_clean = _strip_reuters_suffix(title)
    summary_clean = html.unescape(summary_text or "").strip()
    if not summary_clean:
        return title_clean
    summary_clean = _strip_reuters_suffix(summary_clean)
    if summary_clean.casefold() == title_clean.casefold():
        return title_clean
    if summary_clean.startswith(title_clean):
        summary_clean = summary_clean[len(title_clean) :].strip(" -:\n")
    if not summary_clean:
        return title_clean
    return f"{title_clean}\n\n{summary_clean}"


def _extract_reuters_summary_from_html(html_text: str) -> str:
    for pattern in (_META_DESCRIPTION_RE, _JSON_LD_DESCRIPTION_RE):
        match = pattern.search(html_text)
        if match:
            return html.unescape(match.group(1)).strip()
    return ""


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
        posts = super().fetch_posts(since_id=since_id, limit=limit)
        return [self._enrich_post(post) for post in posts]

    def _parse_rss_feed(self, root: ET.Element) -> dict[str, object]:
        channel = next((child for child in root if _local_name(child.tag) == "channel"), None)
        if channel is None:
            raise SourceError(f"RSS feed {self.feed_url} is missing a channel element.")

        posts = []
        for item in _children(channel, "item"):
            source_name = _child_text(item, ("source",)).strip()
            title = _child_text(item, ("title",))
            description = _child_text(item, ("description", "encoded", "content"))
            source_hint = " ".join(part for part in (title, description, source_name) if part)
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

    def _enrich_post(self, post: SourcePost) -> SourcePost:
        title = str(post.raw_payload.get("title") or "")
        if not post.url or not title:
            return post

        try:
            article_url, summary = self._fetch_article_summary(post.url)
        except SourceError as exc:
            LOGGER.warning("Reuters article summary fetch failed for %s: %s", post.id, exc)
            return post

        if not summary:
            return post

        final_url = article_url or post.url
        return SourcePost(
            source_id=post.source_id,
            source_name=post.source_name,
            id=post.id,
            account_handle=post.account_handle,
            created_at=post.created_at,
            url=final_url,
            body_text=_normalize_reuters_article_summary(title, summary),
            is_reply=post.is_reply,
            is_reblog=post.is_reblog,
            media_attachments=post.media_attachments,
            raw_payload={
                **post.raw_payload,
                "article_url": final_url,
                "article_summary": summary,
            },
            categories=post.categories,
        )

    def _fetch_article_summary(self, url: str) -> tuple[str, str]:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": self.config.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                final_url = response.geturl() or url
                html_text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise SourceError(f"Reuters article HTTP {exc.code} for {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"Reuters article request failed for {url}: {exc.reason}") from exc

        host = (urlparse(final_url).netloc or "").lower()
        if "reuters.com" not in host:
            return (final_url, "")
        return (final_url, _extract_reuters_summary_from_html(html_text))
