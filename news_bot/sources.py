from __future__ import annotations

from .ap import APWorldRSSSource
from .config import AppConfig
from .ft import FTRSSSource
from .reuters import ReutersRSSSource
from .source_types import SourceAdapter
from .rss import RSSFeedSource
from .trump_source import ResilientTrumpSource, TrumpFallbackFeedSource
from .truthsocial import TruthSocialClient
from .x import XKobeissiLetterSource


def build_sources(config: AppConfig) -> list[SourceAdapter]:
    sources: list[SourceAdapter] = []
    seen: set[str] = set()
    rss_count = 0

    for source_name in config.enabled_sources:
        normalized = source_name.strip().lower()
        if normalized in seen:
            continue

        if normalized in {"truthsocial", "truthsocial_trump"}:
            fallback_feeds = tuple(
                TrumpFallbackFeedSource(config, feed_url, ordinal=index)
                for index, feed_url in enumerate(config.truthsocial_fallback_feed_urls)
            )
            if fallback_feeds:
                sources.append(
                    ResilientTrumpSource(
                        config,
                        primary=TruthSocialClient(config),
                        fallbacks=fallback_feeds,
                    )
                )
            else:
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

        if normalized == "reuters_rss":
            sources.append(ReutersRSSSource(config))
            seen.add(normalized)
            continue

        if normalized == "ap_world_rss":
            sources.append(APWorldRSSSource(config))
            seen.add(normalized)
            continue

        if normalized == "ft_rss":
            sources.append(FTRSSSource(config))
            seen.add(normalized)
            continue

        if normalized == "x_kobeissi_letter":
            sources.append(XKobeissiLetterSource(config))
            seen.add(normalized)
            continue

        raise ValueError(
            f"Unsupported source '{source_name}'. Supported values: truthsocial_trump, rss, reuters_rss, ap_world_rss, ft_rss, x_kobeissi_letter"
        )

    return sources
