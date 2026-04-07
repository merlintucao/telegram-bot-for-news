from __future__ import annotations

import re
from html.parser import HTMLParser


class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {"p", "div", "section", "article", "blockquote", "li", "ul", "ol"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.anchor_href: str | None = None
        self.anchor_text: list[str] = []

    def _trailing_newlines(self) -> int:
        trailing = 0
        for part in reversed(self.parts):
            for character in reversed(part):
                if character == "\n":
                    trailing += 1
                    continue
                return trailing
        return trailing

    def _ensure_break(self, count: int) -> None:
        if not self.parts:
            return
        missing = count - self._trailing_newlines()
        if missing > 0:
            self.parts.append("\n" * missing)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self._ensure_break(1)
        if tag == "a":
            attrs_map = dict(attrs)
            self.anchor_href = attrs_map.get("href")
            self.anchor_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS:
            self._ensure_break(2)
        if tag == "a":
            text = "".join(self.anchor_text).strip()
            href = self.anchor_href
            if href and text and href not in text:
                self.parts.append(f" ({href})")
            self.anchor_href = None
            self.anchor_text = []

    def handle_data(self, data: str) -> None:
        if not data:
            return
        self.parts.append(data)
        if self.anchor_href is not None:
            self.anchor_text.append(data)

    def get_text(self) -> str:
        text = "".join(self.parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return re.sub(r"[ \t]+\n", "\n", text).strip()


def html_to_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    parser.close()
    return parser.get_text()
