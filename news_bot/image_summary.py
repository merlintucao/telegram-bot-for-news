from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Protocol


LOGGER = logging.getLogger(__name__)


class ImageSummaryError(RuntimeError):
    pass


class ImageSummarizer(Protocol):
    def summarize_images(self, image_urls: list[str]) -> str:
        ...


class OpenAIImageSummarizer:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        timeout_seconds: int,
    ) -> None:
        self.api_key = api_key.strip()
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def summarize_images(self, image_urls: list[str]) -> str:
        clean_urls = [url.strip() for url in image_urls if url and url.strip()]
        if not clean_urls:
            raise ImageSummaryError("No image URLs provided.")

        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Summarize what these news-related images show in Vietnamese. "
                                "Be truthful, concise, and do not speculate. "
                                "Use one short paragraph with 1-2 sentences. "
                                "Mention visible text only if it is clearly legible."
                            ),
                        },
                        *[
                            {
                                "type": "input_image",
                                "image_url": image_url,
                                "detail": "low",
                            }
                            for image_url in clean_urls[:3]
                        ],
                    ],
                }
            ],
        }
        request = urllib.request.Request(
            f"{self.base_url}/responses",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ImageSummaryError(f"OpenAI HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ImageSummaryError(f"OpenAI request failed: {exc.reason}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ImageSummaryError(f"Invalid OpenAI JSON response: {raw[:200]!r}") from exc

        text = _extract_output_text(payload)
        if not text:
            raise ImageSummaryError(f"OpenAI response did not contain summary text: {payload!r}")
        return text


def build_image_summarizer(
    *,
    enabled: bool,
    provider: str,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: int,
) -> ImageSummarizer | None:
    if not enabled:
        return None
    normalized = provider.strip().lower() or "openai"
    if normalized != "openai":
        LOGGER.warning("Unsupported image summary provider %s; disabling image summaries.", provider)
        return None
    if not api_key.strip():
        LOGGER.warning("IMAGE_SUMMARY_ENABLED is on but OPENAI_API_KEY is missing; disabling image summaries.")
        return None
    return OpenAIImageSummarizer(
        api_key=api_key,
        model=model or "gpt-4.1-mini",
        base_url=base_url or "https://api.openai.com/v1",
        timeout_seconds=timeout_seconds,
    )


def _extract_output_text(payload: dict[str, object]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    fragments: list[str] = []
    for item in payload.get("output", []):
        if not isinstance(item, dict):
            continue
        for content in item.get("content", []):
            if not isinstance(content, dict):
                continue
            if content.get("type") in {"output_text", "text"}:
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    fragments.append(text.strip())
    return "\n".join(fragment for fragment in fragments if fragment).strip()
