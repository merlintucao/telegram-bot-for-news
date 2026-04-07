from __future__ import annotations

from .config import AppConfig
from .source_types import SourceAdapter
from .rss import RSSFeedSource
from .truthsocial import TruthSocialClient


def build_sources(config: AppConfig) -> list[SourceAdapter]:
    sources: list[SourceAdapter] = []
    seen: set[str] = set()
    rss_count = 0

    for source_name in config.enabled_sources:
        normalized = source_name.strip().lower()
        if normalized in seen:
            continue

        if normalized in {"truthsocial", "truthsocial_trump"}:
            sources.append(TruthSocialClient(config))
            seen.add(normalized)
            continue

        if normalized == "rss":
            if not config.rss_feed_urls:
                raise ValueError(
                    "ENABLED_SOURCES includes rss but RSS_FEED_URLS is empty."
                )
            for feed_url in config.rss_feed_urls:
                sources.append(RSSFeedSource(config, feed_url, ordinal=rss_count))
                rss_count += 1
            seen.add(normalized)
            continue

        raise ValueError(
            f"Unsupported source '{source_name}'. Supported values: truthsocial_trump, rss"
        )

    return sources
