from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from typing import Protocol


LOGGER = logging.getLogger(__name__)

_MAX_TRANSLATE_CHARS = 1400


class Translator(Protocol):
    def translate(self, text: str) -> str:
        ...


class TranslationError(RuntimeError):
    pass


class GoogleTranslateTranslator:
    def __init__(
        self,
        *,
        target_language: str,
        endpoint: str,
        timeout_seconds: int,
    ) -> None:
        self.target_language = target_language.strip().lower()
        self.endpoint = endpoint.rstrip("?")
        self.timeout_seconds = timeout_seconds
        self._cache: dict[str, str] = {}

    def translate(self, text: str) -> str:
        normalized = text.strip()
        if not normalized or not self.target_language:
            return text
        cached = self._cache.get(normalized)
        if cached is not None:
            return cached

        translated_parts = [
            self._translate_chunk(chunk)
            for chunk in _split_text(normalized, max_chars=_MAX_TRANSLATE_CHARS)
        ]
        translated = "\n\n".join(part for part in translated_parts if part.strip()).strip()
        if not translated:
            translated = text
        self._cache[normalized] = translated
        return translated

    def _translate_chunk(self, chunk: str) -> str:
        params = urllib.parse.urlencode(
            {
                "client": "gtx",
                "sl": "auto",
                "tl": self.target_language,
                "dt": "t",
                "q": chunk,
            }
        )
        url = f"{self.endpoint}?{params}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/135.0 Safari/537.36"
                ),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # pragma: no cover - network path
            raise TranslationError(f"Translation request failed: {exc}") from exc

        try:
            sentences = payload[0]
            translated = "".join(
                str(sentence[0])
                for sentence in sentences
                if isinstance(sentence, list) and sentence and sentence[0] is not None
            ).strip()
        except Exception as exc:
            raise TranslationError(f"Unexpected translation payload: {payload!r}") from exc

        if not translated:
            raise TranslationError("Translation payload did not contain translated text.")
        return translated


def _split_text(text: str, max_chars: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
    if len(paragraphs) <= 1:
        return _split_dense_text(text, max_chars=max_chars)

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for paragraph in paragraphs:
        paragraph_length = len(paragraph)
        separator = 2 if current else 0
        if paragraph_length > max_chars:
            if current:
                chunks.append("\n\n".join(current))
                current = []
                current_length = 0
            chunks.extend(_split_dense_text(paragraph, max_chars=max_chars))
            continue
        if current_length + separator + paragraph_length > max_chars:
            chunks.append("\n\n".join(current))
            current = [paragraph]
            current_length = paragraph_length
            continue
        current.append(paragraph)
        current_length += separator + paragraph_length

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def _split_dense_text(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    current = words[0]
    for word in words[1:]:
        if len(current) + 1 + len(word) > max_chars:
            chunks.append(current)
            current = word
            continue
        current = f"{current} {word}"
    chunks.append(current)
    return chunks
