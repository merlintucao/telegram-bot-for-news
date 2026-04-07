from __future__ import annotations

import hashlib
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from .config import AppConfig
from .html_text import html_to_text
from .models import MediaAttachment, SourcePost
from .source_types import SourceError, SourceProbeResult

LOGGER = logging.getLogger(__name__)


def _local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[1]
    return tag


def _child_text(element: ET.Element, names: tuple[str, ...]) -> str:
    for child in element:
        if _local_name(child.tag) in names:
            return "".join(child.itertext()).strip()
    return ""


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _normalize_datetime(raw_value: str) -> str:
    raw = raw_value.strip()
    if not raw:
        return ""

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return raw

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    basis = f"{parsed.netloc}{parsed.path}".lower()
    slug = re.sub(r"[^a-z0-9]+", "-", basis).strip("-")
    if slug:
        return slug[:50]
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]


def _display_name_from_url(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or "RSS Feed"
    return host.removeprefix("www.") or "RSS Feed"


def _attachment_kind_from_type(mime_type: str) -> str:
    lowered = mime_type.lower()
    if lowered.startswith("image/"):
        return "image"
    if lowered.startswith("video/"):
        return "video"
    if lowered.startswith("audio/"):
        return "audio"
    return "document"


class RSSFeedSource:
    def __init__(self, config: AppConfig, feed_url: str, ordinal: int = 0) -> None:
        self.config = config
        self.feed_url = feed_url
        base_slug = _slug_from_url(feed_url)
        suffix = f"-{ordinal}" if ordinal else ""
        self.source_id = f"rss:{base_slug}{suffix}"
        self.source_name = _display_name_from_url(feed_url)

    def probe(self) -> SourceProbeResult:
        metadata = self._fetch_feed_metadata()
        latest_id = metadata["posts"][0].id if metadata["posts"] else "no entries returned"
        return SourceProbeResult(
            source_id=self.source_id,
            source_name=metadata["source_name"],
            detail_lines=(
                f"feed url: {self.feed_url}",
                f"feed title: {metadata['source_name']}",
                f"latest entry id: {latest_id}",
            ),
        )

    def fetch_posts(
        self,
        since_id: str | None = None,
        limit: int | None = None,
    ) -> list[SourcePost]:
        metadata = self._fetch_feed_metadata()
        posts = metadata["posts"]

        if since_id:
            collected: list[SourcePost] = []
            for post in posts:
                if post.id == since_id:
                    break
                collected.append(post)
            posts = collected

        if limit is not None:
            posts = posts[:limit]

        return posts

    def _fetch_feed_metadata(self) -> dict[str, Any]:
        request = urllib.request.Request(
            self.feed_url,
            headers={
                "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.8",
                "User-Agent": self.config.user_agent,
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.config.request_timeout_seconds) as response:
                xml_text = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise SourceError(f"RSS HTTP {exc.code} for {self.feed_url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise SourceError(f"RSS request failed for {self.feed_url}: {exc.reason}") from exc

        return self._parse_feed(xml_text)

    def _parse_feed(self, xml_text: str) -> dict[str, Any]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            raise SourceError(f"RSS feed parse failed for {self.feed_url}: {exc}") from exc

        root_name = _local_name(root.tag)
        if root_name == "rss":
            return self._parse_rss_feed(root)
        if root_name == "feed":
            return self._parse_atom_feed(root)

        raise SourceError(
            f"Unsupported feed format for {self.feed_url}: root tag '{root_name}'"
        )

    def _parse_rss_feed(self, root: ET.Element) -> dict[str, Any]:
        channel = next((child for child in root if _local_name(child.tag) == "channel"), None)
        if channel is None:
            raise SourceError(f"RSS feed {self.feed_url} is missing a channel element.")

        feed_title = _child_text(channel, ("title",)) or self.source_name
        self.source_name = feed_title
        posts = [self._parse_rss_item(item, feed_title) for item in _children(channel, "item")]
        return {"source_name": feed_title, "posts": posts}

    def _parse_atom_feed(self, root: ET.Element) -> dict[str, Any]:
        feed_title = _child_text(root, ("title",)) or self.source_name
        self.source_name = feed_title
        posts = [self._parse_atom_entry(entry, feed_title) for entry in _children(root, "entry")]
        return {"source_name": feed_title, "posts": posts}

    def _parse_rss_item(self, item: ET.Element, feed_title: str) -> SourcePost:
        title = _child_text(item, ("title",))
        link = _child_text(item, ("link",))
        guid = _child_text(item, ("guid",))
        description = _child_text(item, ("description", "encoded", "content"))
        published = _normalize_datetime(_child_text(item, ("pubDate", "published", "updated")))
        categories = tuple(
            text
            for text in (
                "".join(child.itertext()).strip()
                for child in item
                if _local_name(child.tag) == "category"
            )
            if text
        )
        entry_id = guid or link or title
        if not entry_id:
            raise SourceError(f"RSS item in {self.feed_url} is missing guid/link/title.")

        body_parts = [part for part in (title, html_to_text(description) if description else "") if part]
        body_text = body_parts[0] if len(body_parts) == 1 else "\n\n".join(body_parts)

        attachments = []
        for child in item:
            child_name = _local_name(child.tag)
            if child_name == "enclosure":
                enclosure_url = (child.attrib.get("url") or "").strip()
                if enclosure_url:
                    attachments.append(
                        MediaAttachment(
                            kind=_attachment_kind_from_type(child.attrib.get("type", "")),
                            url=enclosure_url,
                        )
                    )
            if child_name == "content":
                media_url = (child.attrib.get("url") or "").strip()
                if media_url:
                    attachments.append(
                        MediaAttachment(
                            kind=_attachment_kind_from_type(child.attrib.get("type", "")),
                            url=media_url,
                        )
                    )

        return SourcePost(
            source_id=self.source_id,
            source_name=feed_title,
            id=entry_id,
            account_handle=feed_title,
            created_at=published,
            url=link,
            body_text=body_text,
            is_reply=False,
            is_reblog=False,
            media_attachments=tuple(attachments),
            raw_payload={
                "id": entry_id,
                "title": title,
                "link": link,
                "published": published,
                "feed_url": self.feed_url,
            },
            categories=categories,
        )

    def _parse_atom_entry(self, entry: ET.Element, feed_title: str) -> SourcePost:
        title = _child_text(entry, ("title",))
        entry_id = _child_text(entry, ("id",))
        updated = _normalize_datetime(_child_text(entry, ("updated", "published")))
        summary = _child_text(entry, ("summary", "content"))
        link = ""
        attachments = []
        categories = []

        for child in entry:
            child_name = _local_name(child.tag)
            if child_name == "category":
                category = (
                    child.attrib.get("term")
                    or child.attrib.get("label")
                    or "".join(child.itertext()).strip()
                )
                if category:
                    categories.append(category.strip())
                continue
            if child_name != "link":
                continue

            rel = (child.attrib.get("rel") or "alternate").strip().lower()
            href = (child.attrib.get("href") or "").strip()
            if not href:
                continue

            if rel == "enclosure":
                attachments.append(
                    MediaAttachment(
                        kind=_attachment_kind_from_type(child.attrib.get("type", "")),
                        url=href,
                    )
                )
                continue

            if not link and rel in {"alternate", ""}:
                link = href

        entry_id = entry_id or link or title
        if not entry_id:
            raise SourceError(f"Atom entry in {self.feed_url} is missing id/link/title.")

        summary_text = html_to_text(summary) if summary else ""
        body_parts = [part for part in (title, summary_text) if part]
        body_text = body_parts[0] if len(body_parts) == 1 else "\n\n".join(body_parts)

        return SourcePost(
            source_id=self.source_id,
            source_name=feed_title,
            id=entry_id,
            account_handle=feed_title,
            created_at=updated,
            url=link,
            body_text=body_text,
            is_reply=False,
            is_reblog=False,
            media_attachments=tuple(attachments),
            raw_payload={
                "id": entry_id,
                "title": title,
                "link": link,
                "published": updated,
                "feed_url": self.feed_url,
            },
            categories=tuple(categories),
        )
