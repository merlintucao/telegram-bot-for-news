from __future__ import annotations

import html
import logging
import re
import urllib.error
import urllib.request

from .config import AppConfig
from .models import SourcePost
from .rss import RSSFeedSource
from .source_types import SourceError

_META_DESCRIPTION_RE = re.compile(
    r"""<meta[^>]+(?:name|property)=["'](?:description|og:description|twitter:description)["'][^>]+content=["']([^"']+)["']""",
    flags=re.IGNORECASE,
)
LOGGER = logging.getLogger(__name__)


def _extract_ap_summary_from_html(html_text: str) -> str:
    match = _META_DESCRIPTION_RE.search(html_text)
    if not match:
        return ""
    return html.unescape(match.group(1)).strip()


def _normalize_ap_story_text(title: str, summary_text: str) -> str:
    title_clean = (title or "").strip()
    summary_clean = (summary_text or "").strip()
    if not summary_clean:
        return title_clean
    if summary_clean.casefold() == title_clean.casefold():
        return title_clean
    if summary_clean.startswith(title_clean):
        summary_clean = summary_clean[len(title_clean) :].strip(" -:\n")
    if not summary_clean:
        return title_clean
    return summary_clean


class APWorldRSSSource(RSSFeedSource):
    def __init__(self, config: AppConfig, feed_url: str | None = None) -> None:
        super().__init__(config, feed_url or config.ap_world_rss_url)
        self.source_id = "rss:ap-world"
        self.source_name = "AP News"

    def _parse_rss_feed(self, root):  # type: ignore[override]
        metadata = super()._parse_rss_feed(root)
        metadata["source_name"] = "AP News"
        posts = [
            SourcePost(
                source_id=self.source_id,
                source_name="AP News",
                id=post.id,
                account_handle="AP News",
                created_at=post.created_at,
                url=post.url,
                body_text=post.body_text,
                is_reply=post.is_reply,
                is_reblog=post.is_reblog,
                media_attachments=post.media_attachments,
                raw_payload={
                    **post.raw_payload,
                    "feed_title": metadata.get("source_name", "AP News"),
                },
                categories=post.categories,
            )
            for post in metadata["posts"]
        ]
        metadata["posts"] = posts
        return metadata

    def fetch_posts(
        self,
        since_id: str | None = None,
        limit: int | None = None,
    ) -> list[SourcePost]:
        posts = super().fetch_posts(since_id=since_id, limit=limit)
        return [self._enrich_post(post) for post in posts]

    def _enrich_post(self, post: SourcePost) -> SourcePost:
        title = str(post.raw_payload.get("title") or "")
        if not post.url or not title:
            return post
        try:
            summary = self._fetch_article_summary(post.url)
        except SourceError as exc:
            LOGGER.warning("AP article summary fetch failed for %s: %s", post.id, exc)
            return post
        if not summary:
            return post
        return SourcePost(
            source_id=post.source_id,
            source_name=post.source_name,
            id=post.id,
            account_handle=post.account_handle,
            created_at=post.created_at,
            url=post.url,
            body_text=_normalize_ap_story_text(title, summary),
            is_reply=post.is_reply,
            is_reblog=post.is_reblog,
            media_attachments=post.media_attachments,
            raw_payload={
                **post.raw_payload,
                "article_summary": summary,
            },
            categories=post.categories,
        )

    def _fetch_article_summary(self, url: str) -> str:
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "User-Agent": self.config.user_agent,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                html_text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise SourceError(f"AP article HTTP {exc.code} for {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"AP article request failed for {url}: {exc.reason}") from exc
        return _extract_ap_summary_from_html(html_text)
