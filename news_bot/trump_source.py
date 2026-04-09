from __future__ import annotations

import re
from typing import Iterable

from .config import AppConfig
from .models import SourcePost
from .rss import RSSFeedSource
from .source_types import SourceAdapter, SourceError, SourceProbeResult
from .truthsocial import TruthSocialClient

TRUTHSOCIAL_POST_URL_RE = re.compile(
    r"https?://truthsocial\.com/@[^/\s]+(?:/posts)?/(\d+)",
    flags=re.IGNORECASE,
)


def _find_truthsocial_post_url(*candidates: str) -> tuple[str | None, str | None]:
    for candidate in candidates:
        for match in TRUTHSOCIAL_POST_URL_RE.finditer(candidate or ""):
            return (match.group(0), match.group(1))
    return (None, None)


class TrumpFallbackFeedSource(RSSFeedSource):
    def __init__(self, config: AppConfig, feed_url: str, ordinal: int = 0) -> None:
        super().__init__(config, feed_url, ordinal=ordinal)
        self.feed_source_id = self.source_id
        self.feed_source_name = self.source_name
        self.source_id = f"truthsocial:{config.truthsocial_handle}"
        self.source_name = "Truth Social"

    def probe(self) -> SourceProbeResult:
        posts = self.fetch_posts(limit=1)
        latest_id = posts[0].id if posts else "no matching Truth Social links"
        return SourceProbeResult(
            source_id=self.source_id,
            source_name=self.source_name,
            detail_lines=(
                f"fallback feed url: {self.feed_url}",
                f"fallback feed title: {self.feed_source_name}",
                f"latest mirrored Truth Social post id: {latest_id}",
            ),
        )

    def fetch_posts(
        self,
        since_id: str | None = None,
        limit: int | None = None,
    ) -> list[SourcePost]:
        feed_posts = super().fetch_posts(limit=None)
        normalized = list(self._normalize_posts(feed_posts))

        if since_id:
            collected: list[SourcePost] = []
            for post in normalized:
                if post.id == since_id:
                    break
                collected.append(post)
            normalized = collected

        if limit is not None:
            normalized = normalized[:limit]

        return normalized

    def _normalize_posts(self, feed_posts: Iterable[SourcePost]) -> Iterable[SourcePost]:
        for post in feed_posts:
            raw_original_url = str(post.raw_payload.get("originalUrl") or "").strip()
            raw_original_id = str(post.raw_payload.get("originalId") or "").strip()
            truth_url, truth_id = _find_truthsocial_post_url(
                raw_original_url,
                post.url,
                post.body_text,
                str(post.raw_payload.get("link") or ""),
                str(post.raw_payload.get("description") or ""),
            )
            if raw_original_id and not truth_id:
                truth_id = raw_original_id
            if raw_original_url and not truth_url:
                truth_url = raw_original_url
            if not truth_id or not truth_url:
                continue
            yield SourcePost(
                source_id=self.source_id,
                source_name=self.source_name,
                id=truth_id,
                account_handle=self.config.truthsocial_handle,
                created_at=post.created_at,
                url=truth_url,
                body_text=post.body_text,
                is_reply=False,
                is_reblog=False,
                media_attachments=post.media_attachments,
                raw_payload={
                    **post.raw_payload,
                    "fallback_feed_url": self.feed_url,
                    "fallback_source_id": self.feed_source_id,
                    "fallback_source_name": self.feed_source_name,
                    "truthsocial_url": truth_url,
                    "truthsocial_id": truth_id,
                },
                categories=post.categories,
            )


class ResilientTrumpSource:
    def __init__(
        self,
        config: AppConfig,
        primary: TruthSocialClient | None = None,
        fallbacks: tuple[SourceAdapter, ...] = (),
    ) -> None:
        self.config = config
        self.primary = primary or TruthSocialClient(config)
        self.fallbacks = fallbacks
        self.source_id = self.primary.source_id
        self.source_name = self.primary.source_name

    def fetch_posts(
        self,
        since_id: str | None = None,
        limit: int | None = None,
    ) -> list[SourcePost]:
        errors: list[str] = []

        try:
            return self.primary.fetch_posts(since_id=since_id, limit=limit)
        except SourceError as exc:
            errors.append(f"primary API: {exc}")

        for fallback in self.fallbacks:
            try:
                return fallback.fetch_posts(since_id=since_id, limit=limit)
            except SourceError as exc:
                errors.append(f"{fallback.source_name}: {exc}")

        raise SourceError("; ".join(errors) or "No Truth Social source succeeded.")

    def probe(self) -> SourceProbeResult:
        detail_lines: list[str] = []
        try:
            primary_result = self.primary.probe()
            detail_lines.append("active backend: primary API")
            detail_lines.extend(primary_result.detail_lines)
            return SourceProbeResult(
                source_id=self.source_id,
                source_name=self.source_name,
                detail_lines=tuple(detail_lines),
            )
        except SourceError as exc:
            detail_lines.append(f"primary API failed: {exc}")

        for fallback in self.fallbacks:
            try:
                fallback_result = fallback.probe()
                detail_lines.append(f"active backend: fallback feed ({fallback.feed_source_name if isinstance(fallback, TrumpFallbackFeedSource) else fallback.source_name})")
                detail_lines.extend(fallback_result.detail_lines)
                return SourceProbeResult(
                    source_id=self.source_id,
                    source_name=self.source_name,
                    detail_lines=tuple(detail_lines),
                )
            except SourceError as exc:
                detail_lines.append(f"fallback failed ({fallback.source_name}): {exc}")

        raise SourceError("; ".join(detail_lines) or "No Truth Social source succeeded.")
