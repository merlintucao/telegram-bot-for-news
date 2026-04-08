from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import urlparse
from typing import Callable

from .config import AppConfig
from .filtering import PostFilter, build_post_filter
from .image_summary import ImageSummarizer, build_image_summarizer, ImageSummaryError
from .models import MediaAttachment, SourcePost
from .routing import SourceRouter, build_router
from .sources import SourceAdapter, build_sources
from .storage import SourceHealthRecord, StateStore
from .telegram import TelegramError, TelegramSender
from .translate import GoogleTranslateTranslator, TranslationError, Translator

LOGGER = logging.getLogger(__name__)

HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
URL_PATTERN = re.compile(r"https?://\S+")
VIETNAM_TZ = timezone(timedelta(hours=7))


def trim_message(text: str, limit: int = 4096) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _post_caption_text(post: SourcePost) -> str:
    return post.body_text.strip()


def _format_header(post: SourcePost) -> str:
    if post.source_id == "truthsocial:realDonaldTrump":
        return "🚨 BREAKING from Donald Trump"

    if post.source_id.startswith("rss:"):
        kind = "story"
    else:
        kind = "post"

    if post.is_reblog:
        kind = "retruth"
    elif post.is_reply:
        kind = "reply"

    header = f"{post.source_name} {kind}"
    publisher = post.account_handle.strip()
    if publisher and publisher.casefold() != post.source_name.casefold():
        prefix = "@" if HANDLE_PATTERN.match(publisher) else ""
        header = f"{header} from {prefix}{publisher}"
    return header


def _format_posted_at(created_at: str) -> str:
    value = created_at.strip()
    if not value:
        return created_at
    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return created_at
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    localized = parsed.astimezone(VIETNAM_TZ)
    return localized.strftime("%H:%M %d/%m/%Y")


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _truncate_sentence(text: str, limit: int) -> str:
    cleaned = _normalize_spaces(text)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 3].rstrip(" ,;:-") + "..."


def _drop_terminal_punctuation(text: str) -> str:
    return text.rstrip(" .!?:;")


def _lowercase_first_char(text: str) -> str:
    if not text:
        return text
    return text[0].lower() + text[1:]


def _rewrite_trump_summary_vi(sentences: list[str], limit: int) -> str:
    if not sentences:
        return ""

    lead = _drop_terminal_punctuation(sentences[0])
    if lead:
        summary_parts = [f"Ông Donald Trump cho rằng {lead}."]
    else:
        summary_parts = []

    for sentence in sentences[1:]:
        sentence = sentence.strip()
        if not sentence:
            continue
        projected = " ".join(summary_parts + [sentence]).strip()
        if len(projected) > limit and len(summary_parts) >= 2:
            break
        if len(projected) > limit:
            summary_parts.append(_truncate_sentence(sentence, max(20, limit - len(" ".join(summary_parts)))))
            break
        summary_parts.append(sentence)

    return _truncate_sentence(" ".join(summary_parts), limit)


def _summarize_caption(text: str, limit: int = 260, source_id: str = "") -> str:
    cleaned = _normalize_spaces(URL_PATTERN.sub("", text))
    if not cleaned:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    compact_sentences: list[str] = []
    current_length = 0
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        separator = 1 if compact_sentences else 0
        projected = current_length + separator + len(sentence)
        if projected > limit and len(compact_sentences) >= 2:
            break
        if projected > limit:
            compact_sentences.append(_truncate_sentence(sentence, limit))
            break
        compact_sentences.append(sentence)
        current_length = projected
    if not compact_sentences:
        return _truncate_sentence(cleaned, limit)
    if source_id == "truthsocial:realDonaldTrump":
        return _rewrite_trump_summary_vi(compact_sentences, limit)
    return " ".join(compact_sentences)


def _sentence_without_urls(text: str) -> str:
    return _normalize_spaces(URL_PATTERN.sub("", text))


def _summarize_links(post: SourcePost, limit: int = 120) -> list[str]:
    lines: list[str] = []
    card = post.raw_payload.get("card")
    if isinstance(card, dict):
        title = _normalize_spaces(str(card.get("title") or ""))
        description = _normalize_spaces(str(card.get("description") or ""))
        if title:
            detail = title
            if description and description.casefold() != title.casefold():
                detail = f"{title} - {_truncate_sentence(description, max(20, limit - len(title) - 3))}"
            lines.append(f"Link summary: {_truncate_sentence(detail, limit)}")

    body_urls = []
    seen_urls: set[str] = set()
    for match in URL_PATTERN.finditer(post.body_text):
        url = match.group(0).rstrip(").,!?")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        body_urls.append(url)

    if not body_urls:
        return lines

    sentence_candidates = re.split(r"(?<=[.!?])\s+", post.body_text)
    for url in body_urls[:3]:
        described = False
        for sentence in sentence_candidates:
            if url not in sentence:
                continue
            sentence_summary = _sentence_without_urls(sentence)
            if sentence_summary:
                lines.append(f"Link summary: {_truncate_sentence(sentence_summary, limit)}")
                described = True
                break
        if described:
            continue
        domain = urlparse(url).netloc.replace("www.", "")
        if domain:
            lines.append(f"Link summary: Link to {domain}")

    deduped: list[str] = []
    seen_lines: set[str] = set()
    for line in lines:
        if line in seen_lines:
            continue
        seen_lines.add(line)
        deduped.append(line)
    return deduped[:3]


def _summarize_media(post: SourcePost, limit: int = 120) -> list[str]:
    return _summarize_media_attachments(post.media_attachments, limit=limit)


def _summarize_media_attachments(
    attachments: tuple[MediaAttachment, ...],
    limit: int = 120,
) -> list[str]:
    if not attachments:
        return []
    described = [
        (
            attachment.kind.strip().lower() or "media",
            attachment.description.strip(),
        )
        for attachment in attachments
        if attachment.description and attachment.description.strip()
    ]
    if described:
        lines: list[str] = []
        for kind, description in described[:3]:
            label = _media_label(kind)
            lines.append(f"{label}: {_truncate_sentence(description, limit)}")
        return lines

    counts: dict[str, int] = {}
    for attachment in attachments:
        label = _media_count_label(attachment.kind)
        counts[label] = counts.get(label, 0) + 1

    parts = []
    for label, count in counts.items():
        suffix = "" if count == 1 else "s"
        parts.append(f"{count} {label}{suffix}")
    if len(parts) == 1:
        return [f"The post includes {parts[0]}."]
    if len(parts) == 2:
        return [f"The post includes {parts[0]} and {parts[1]}."]
    return [f"The post includes {', '.join(parts[:-1])}, and {parts[-1]}."]


def _media_count_label(kind: str) -> str:
    normalized = kind.strip().lower() or "media"
    if normalized in {"photo", "image"}:
        return "image"
    if normalized in {"video", "gif", "gifv"}:
        return "video"
    return normalized


def _media_label(kind: str) -> str:
    normalized = _media_count_label(kind)
    if normalized == "image":
        return "Image summary"
    if normalized == "video":
        return "Video summary"
    return "Media summary"


def _is_image_attachment(attachment: MediaAttachment) -> bool:
    return _media_count_label(attachment.kind) == "image"


def _build_auxiliary_summary_lines(post: SourcePost) -> list[str]:
    lines: list[str] = []
    lines.extend(_summarize_links(post))
    lines.extend(_summarize_media(post))
    return lines


def _build_summary_lines(
    post: SourcePost,
    translated_text: str | None,
    translated_auxiliary_lines: list[str] | None = None,
) -> list[str]:
    summary_lines: list[str] = []
    caption_summary = _summarize_caption(
        translated_text or _post_caption_text(post),
        limit=700,
        source_id=post.source_id,
    )
    if caption_summary:
        summary_lines.append(caption_summary)
    summary_lines.extend(
        _build_auxiliary_summary_lines(post)
        if translated_auxiliary_lines is None
        else translated_auxiliary_lines
    )
    return summary_lines


def format_post_message(
    post: SourcePost,
    translated_text: str | None = None,
    translated_auxiliary_lines: list[str] | None = None,
) -> str:
    lines = [_format_header(post)]

    if post.created_at:
        lines.extend(["", f"Posted: {_format_posted_at(post.created_at)}"])
    summary_lines = _build_summary_lines(
        post,
        translated_text,
        translated_auxiliary_lines=translated_auxiliary_lines,
    )
    if summary_lines:
        lines.extend(["", *summary_lines])

    return trim_message("\n".join(lines))


def format_post_caption(
    post: SourcePost,
    translated_text: str | None = None,
    translated_auxiliary_lines: list[str] | None = None,
) -> str:
    lines = [_format_header(post)]

    info_lines: list[str] = []
    if post.created_at:
        info_lines.append(f"Posted: {_format_posted_at(post.created_at)}")
    if info_lines:
        lines.extend(["", *info_lines])
    summary_lines = _build_summary_lines(
        post,
        translated_text,
        translated_auxiliary_lines=translated_auxiliary_lines,
    )
    if summary_lines:
        lines.extend(["", *summary_lines])
    return trim_message("\n".join(lines), limit=1024)


def describe_exception(exc: Exception) -> str:
    detail = str(exc).strip()
    if detail:
        return f"{exc.__class__.__name__}: {detail}"
    return exc.__class__.__name__


def format_failure_alert_message(
    source: SourceAdapter,
    consecutive_failures: int,
    error_detail: str,
) -> str:
    lines = [
        "Source failure alert",
        "",
        f"Source: {source.source_name}",
        f"Source ID: {source.source_id}",
        f"Consecutive failures: {consecutive_failures}",
        f"Latest error: {error_detail}",
    ]
    return trim_message("\n".join(lines))


def format_recovery_alert_message(
    source: SourceAdapter,
    consecutive_failures: int,
    last_error_detail: str | None,
) -> str:
    lines = [
        "Source recovered",
        "",
        f"Source: {source.source_name}",
        f"Source ID: {source.source_id}",
        f"Recovered after failures: {consecutive_failures}",
    ]
    if last_error_detail:
        lines.append(f"Previous error: {last_error_detail}")
    return trim_message("\n".join(lines))


@dataclass(slots=True)
class RunSummary:
    fetched_count: int
    sent_count: int
    filtered_count: int = 0
    bootstrapped: bool = False
    sources_processed: int = 0
    failed_sources: int = 0


@dataclass(slots=True)
class AuxiliarySummaryEntry:
    text: str
    already_vietnamese: bool = False
    placeholder: str | None = None


class NewsBotService:
    def __init__(
        self,
        config: AppConfig,
        store: StateStore,
        sources: list[SourceAdapter],
        router: SourceRouter,
        post_filter: PostFilter,
        sender: TelegramSender | _NoopSender,
        sleep_fn: Callable[[float], None] = time.sleep,
        translator: Translator | None = None,
        image_summarizer: ImageSummarizer | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.sources = sources
        self.router = router
        self.post_filter = post_filter
        self.sender = sender
        self.sleep_fn = sleep_fn
        self.translator = translator
        self.image_summarizer = image_summarizer

    @classmethod
    def from_config(cls, config: AppConfig, dry_run: bool = False) -> "NewsBotService":
        sender: TelegramSender | _NoopSender
        if dry_run:
            sender = _NoopSender()
        else:
            sender = TelegramSender(
                bot_token=config.telegram_bot_token,
                chat_id=config.telegram_chat_id,
                timeout_seconds=config.request_timeout_seconds,
            )

        return cls(
            config=config,
            store=StateStore(config.state_db_path),
            sources=build_sources(config),
            router=build_router(
                default_chat_id=config.telegram_chat_id,
                raw_rules=config.source_chat_routes,
            ),
            post_filter=build_post_filter(
                raw_keyword_rules=config.source_keyword_filters,
                raw_category_rules=config.source_category_filters,
            ),
            sender=sender,
            translator=_build_translator(config),
            image_summarizer=build_image_summarizer(
                enabled=config.image_summary_enabled,
                provider=config.image_summary_provider,
                api_key=config.openai_api_key,
                model=config.image_summary_model,
                base_url=config.openai_base_url,
                timeout_seconds=config.request_timeout_seconds,
            ),
        )

    def run_once(self, dry_run: bool = False) -> RunSummary:
        run_id = self.store.start_run(dry_run=dry_run)
        fetched_count = 0
        sent_count = 0
        filtered_count = 0
        bootstrapped = False
        sources_processed = 0
        failed_sources = 0
        failure_messages: list[str] = []

        try:
            for source in self.sources:
                try:
                    result = self._run_source_with_retries(source, dry_run=dry_run, run_id=run_id)
                except Exception as exc:
                    error_detail = describe_exception(exc)
                    self.store.log_source_event(
                        run_id=run_id,
                        source_key=source.source_id,
                        source_name=source.source_name,
                        event_type="error",
                        detail=error_detail,
                    )
                    if not dry_run:
                        health = self.store.record_source_failure(source.source_id, detail=error_detail)
                        self._maybe_send_source_failure_alert(
                            source=source,
                            run_id=run_id,
                            health=health,
                            error_detail=error_detail,
                        )
                    failed_sources += 1
                    failure_messages.append(f"{source.source_id}: {error_detail}")
                    if not self.config.continue_on_source_error:
                        raise
                    LOGGER.warning(
                        "Source %s failed after retries; continuing with remaining sources: %s",
                        source.source_id,
                        error_detail,
                    )
                    continue
                if not dry_run:
                    previous_health = self.store.get_source_health(source.source_id)
                    self.store.record_source_success(source.source_id)
                    self._maybe_send_source_recovery_alert(
                        source=source,
                        run_id=run_id,
                        previous_health=previous_health,
                    )
                fetched_count += result.fetched_count
                sent_count += result.sent_count
                filtered_count += result.filtered_count
                bootstrapped = bootstrapped or result.bootstrapped
                sources_processed += 1
        except Exception as exc:
            self.store.finish_run(
                run_id,
                status="error",
                fetched_count=fetched_count,
                sent_count=sent_count,
                filtered_count=filtered_count,
                bootstrapped=bootstrapped,
                sources_processed=sources_processed,
                error_message=trim_message("; ".join(failure_messages) or str(exc)),
            )
            raise

        summary = RunSummary(
            fetched_count=fetched_count,
            sent_count=sent_count,
            filtered_count=filtered_count,
            bootstrapped=bootstrapped,
            sources_processed=sources_processed,
            failed_sources=failed_sources,
        )
        run_status = "ok"
        error_message = None
        if failure_messages:
            run_status = "degraded" if sources_processed > 0 else "error"
            error_message = trim_message("; ".join(failure_messages), limit=2000)
        self.store.finish_run(
            run_id,
            status=run_status,
            fetched_count=summary.fetched_count,
            sent_count=summary.sent_count,
            filtered_count=summary.filtered_count,
            bootstrapped=summary.bootstrapped,
            sources_processed=summary.sources_processed,
            error_message=error_message,
        )
        return summary

    def _run_source_with_retries(
        self,
        source: SourceAdapter,
        *,
        dry_run: bool,
        run_id: int | None,
    ) -> RunSummary:
        attempts = max(1, self.config.source_retry_attempts)
        backoff_seconds = max(0, self.config.source_retry_backoff_seconds)
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                return self._run_source_once(source, dry_run=dry_run, run_id=run_id)
            except Exception as exc:
                last_error = exc
                if attempt >= attempts:
                    break

                delay = backoff_seconds * (2 ** (attempt - 1))
                LOGGER.warning(
                    "Source %s attempt %s/%s failed: %s",
                    source.source_id,
                    attempt,
                    attempts,
                    describe_exception(exc),
                )
                if delay > 0 and not dry_run:
                    LOGGER.info(
                        "Retrying source %s in %s second(s).",
                        source.source_id,
                        delay,
                    )
                    self.sleep_fn(float(delay))

        assert last_error is not None
        raise last_error

    def _maybe_send_source_failure_alert(
        self,
        *,
        source: SourceAdapter,
        run_id: int,
        health: SourceHealthRecord,
        error_detail: str,
    ) -> None:
        alert_chat_id = self.config.telegram_alert_chat_id.strip()
        threshold = self.config.source_failure_alert_threshold

        if not alert_chat_id or threshold <= 0:
            return
        if health.consecutive_failures < threshold:
            return
        if health.last_alerted_failure_count >= threshold:
            return

        try:
            self.sender.send_message(
                format_failure_alert_message(
                    source=source,
                    consecutive_failures=health.consecutive_failures,
                    error_detail=error_detail,
                ),
                chat_id=alert_chat_id,
            )
        except TelegramError as exc:
            LOGGER.warning(
                "Failed to send source failure alert for %s to %s: %s",
                source.source_id,
                alert_chat_id,
                exc,
            )
            return

        self.store.mark_source_alert_sent(source.source_id, health.consecutive_failures)
        self.store.log_source_event(
            run_id=run_id,
            source_key=source.source_id,
            source_name=source.source_name,
            event_type="alert",
            detail=f"sent failure alert to {alert_chat_id} at streak {health.consecutive_failures}",
        )

    def _maybe_send_source_recovery_alert(
        self,
        *,
        source: SourceAdapter,
        run_id: int,
        previous_health: SourceHealthRecord | None,
    ) -> None:
        if previous_health is None:
            return
        if previous_health.last_alerted_failure_count <= 0:
            return

        alert_chat_id = self.config.telegram_alert_chat_id.strip()
        if not alert_chat_id:
            return

        try:
            self.sender.send_message(
                format_recovery_alert_message(
                    source=source,
                    consecutive_failures=previous_health.consecutive_failures,
                    last_error_detail=previous_health.last_error_detail,
                ),
                chat_id=alert_chat_id,
            )
        except TelegramError as exc:
            LOGGER.warning(
                "Failed to send source recovery alert for %s to %s: %s",
                source.source_id,
                alert_chat_id,
                exc,
            )
            return

        self.store.log_source_event(
            run_id=run_id,
            source_key=source.source_id,
            source_name=source.source_name,
            event_type="recovered",
            detail=(
                f"sent recovery alert to {alert_chat_id} "
                f"after streak {previous_health.consecutive_failures}"
            ),
        )

    def _run_source_once(self, source: SourceAdapter, dry_run: bool, run_id: int | None) -> RunSummary:
        source_key = source.source_id
        last_status_id = self.store.get_last_status_id(source_key)

        if dry_run and last_status_id is None:
            posts = source.fetch_posts(limit=self.config.initial_history_limit)
            posts.sort(key=lambda post: post.sort_key)
            filtered_count = 0

            for post in posts:
                decision = self.post_filter.evaluate(post)
                if not decision.should_deliver:
                    filtered_count += 1
                    LOGGER.info(
                        "Dry run skipped %s from %s due to %s",
                        post.id,
                        source_key,
                        decision.reason or "filter",
                    )
                    continue
                translated_text = self._translate_post(post)
                LOGGER.info(
                    "Dry run message for %s:\n%s",
                    post.id,
                    format_post_message(post, translated_text=translated_text),
                )

            return RunSummary(
                fetched_count=len(posts),
                sent_count=0,
                filtered_count=filtered_count,
                bootstrapped=False,
            )

        if last_status_id is None and self.config.bootstrap_latest_only:
            latest = source.fetch_posts(limit=1)
            if not latest:
                LOGGER.info("No posts found during bootstrap for %s.", source_key)
                return RunSummary(fetched_count=0, sent_count=0, bootstrapped=True)

            self.store.update_checkpoint(source_key, latest[0].id)
            if not dry_run:
                self.store.log_source_event(
                    run_id=run_id,
                    source_key=source_key,
                    source_name=source.source_name,
                    event_type="bootstrap",
                    status_id=latest[0].id,
                    post_url=latest[0].url or None,
                    detail="initial checkpoint",
                )
            LOGGER.info(
                "Bootstrapped source %s at status %s without sending backlog.",
                source_key,
                latest[0].id,
            )
            return RunSummary(fetched_count=1, sent_count=0, bootstrapped=True)

        fetch_limit = self.config.initial_history_limit if last_status_id is None else self.config.fetch_limit
        posts = source.fetch_posts(since_id=last_status_id, limit=fetch_limit)
        posts.sort(key=lambda post: post.sort_key)

        sent_count = 0
        filtered_count = 0
        for post in posts:
            if self.store.was_delivered(source_key, post.id):
                continue

            decision = self.post_filter.evaluate(post)
            if not decision.should_deliver:
                filtered_count += 1
                if not dry_run:
                    self.store.log_source_event(
                        run_id=run_id,
                        source_key=source_key,
                        source_name=post.source_name,
                        event_type="filtered",
                        status_id=post.id,
                        post_url=post.url or None,
                        detail=decision.reason or "filter",
                    )
                LOGGER.info(
                    "Skipping %s from %s due to %s",
                    post.id,
                    source_key,
                    decision.reason or "filter",
                )
                continue

            translated_text = self._translate_post(post)
            translated_auxiliary_lines = self._translate_auxiliary_summary_lines(post)
            message = format_post_message(
                post,
                translated_text=translated_text,
                translated_auxiliary_lines=translated_auxiliary_lines,
            )
            media_caption = format_post_caption(
                post,
                translated_text=translated_text,
                translated_auxiliary_lines=translated_auxiliary_lines,
            )
            if dry_run:
                LOGGER.info("Dry run message for %s:\n%s", post.id, message)
                continue

            destinations = self.router.destinations_for_source(source_key)
            if not destinations:
                LOGGER.warning("No Telegram destinations configured for source %s", source_key)
                continue

            for chat_id in destinations:
                self.sender.send_post(
                    post,
                    message,
                    chat_id=chat_id,
                    media_caption=media_caption,
                )

            self.store.record_delivery(source_key, post)
            self.store.log_source_event(
                run_id=run_id,
                source_key=source_key,
                source_name=post.source_name,
                event_type="delivered",
                status_id=post.id,
                post_url=post.url or None,
                detail=f"delivered to {len(destinations)} chat(s)",
            )
            sent_count += 1

        if posts and not dry_run:
            self.store.update_checkpoint(source_key, posts[-1].id)

        return RunSummary(
            fetched_count=len(posts),
            sent_count=sent_count,
            filtered_count=filtered_count,
        )

    def _translate_post(self, post: SourcePost) -> str | None:
        if self.translator is None:
            return None
        original_caption = _post_caption_text(post)
        if not original_caption:
            return None
        translated_text = self._translate_text_with_retries(
            original_caption,
            context=f"post {post.id}",
        )
        if translated_text is None:
            return self.config.translation_failure_placeholder.strip() or None
        return translated_text

    def _translate_auxiliary_summary_lines(self, post: SourcePost) -> list[str]:
        entries = self._build_auxiliary_summary_entries(post)
        if not entries:
            return []
        if self.translator is None:
            return [entry.text.strip() for entry in entries if entry.text.strip()]

        translated_lines: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            if entry.already_vietnamese:
                translated = entry.text
            else:
                translated = self._translate_text_with_retries(
                    entry.text,
                    context=f"summary line for post {post.id}",
                )
            if translated is None:
                translated = entry.placeholder or self._auxiliary_placeholder_for_line(entry.text)
            normalized = translated.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            translated_lines.append(normalized)
        return translated_lines

    def _translate_text_with_retries(self, text: str, *, context: str) -> str | None:
        if self.translator is None:
            return None

        attempts = max(1, self.config.translation_retry_attempts)
        backoff_seconds = max(0, self.config.translation_retry_backoff_seconds)
        last_error: TranslationError | None = None

        for attempt in range(1, attempts + 1):
            try:
                translated_text = self.translator.translate(text)
            except TranslationError as exc:
                last_error = exc
                if attempt < attempts:
                    LOGGER.warning(
                        "Translation attempt %s/%s failed for %s: %s",
                        attempt,
                        attempts,
                        context,
                        exc,
                    )
                    if backoff_seconds > 0:
                        self.sleep_fn(float(backoff_seconds * (2 ** (attempt - 1))))
                    continue
                break

            normalized = translated_text.strip()
            if normalized:
                return normalized

        if last_error is not None:
            LOGGER.warning("Translation failed for %s after retries: %s", context, last_error)
        return None

    def _build_auxiliary_summary_entries(self, post: SourcePost) -> list[AuxiliarySummaryEntry]:
        entries: list[AuxiliarySummaryEntry] = []
        entries.extend(
            AuxiliarySummaryEntry(
                text=line,
                placeholder="Bai dang co kem lien ket lien quan.",
            )
            for line in _summarize_links(post)
        )

        image_summary = self._summarize_post_images(post)
        if image_summary:
            entries.append(
                AuxiliarySummaryEntry(
                    text=image_summary,
                    already_vietnamese=True,
                )
            )
        else:
            image_attachments = tuple(
                attachment for attachment in post.media_attachments if _is_image_attachment(attachment)
            )
            entries.extend(
                AuxiliarySummaryEntry(
                    text=line,
                    placeholder="Bai dang co kem hinh anh lien quan.",
                )
                for line in _summarize_media_attachments(image_attachments)
            )

        non_image_attachments = tuple(
            attachment for attachment in post.media_attachments if not _is_image_attachment(attachment)
        )
        entries.extend(
            AuxiliarySummaryEntry(
                text=line,
                placeholder="Bai dang co kem video hoac tep media.",
            )
            for line in _summarize_media_attachments(non_image_attachments)
        )
        return entries

    def _summarize_post_images(self, post: SourcePost) -> str | None:
        if self.image_summarizer is None:
            return None

        image_urls = [
            (attachment.preview_url or attachment.url).strip()
            for attachment in post.media_attachments
            if _is_image_attachment(attachment) and (attachment.preview_url or attachment.url).strip()
        ]
        if not image_urls:
            return None

        try:
            summary = self.image_summarizer.summarize_images(image_urls[:3])
        except ImageSummaryError as exc:
            LOGGER.warning("Image summary failed for post %s: %s", post.id, exc)
            return None

        normalized = summary.strip()
        if not normalized:
            return None
        if normalized.lower().startswith("hinh anh"):
            return normalized
        return f"Hinh anh cho thay: {normalized}"

    def _auxiliary_placeholder_for_line(self, line: str) -> str:
        lowered = line.lower()
        if "link" in lowered:
            return "Bai dang co kem lien ket lien quan."
        if "image" in lowered or "video" in lowered or "media" in lowered:
            return "Bai dang co kem hinh anh hoac video."
        return "Thong tin bo sung tam thoi chua san sang."


class _NoopSender:
    def send_post(
        self,
        post: SourcePost,
        text: str,
        chat_id: str | None = None,
        media_caption: str | None = None,
    ) -> None:
        LOGGER.debug("Skipping Telegram send in dry-run mode for %s: %s", chat_id or "-", text)

    def send_message(self, text: str, chat_id: str | None = None) -> None:
        LOGGER.debug("Skipping Telegram message in dry-run mode for %s: %s", chat_id or "-", text)


def _build_translator(config: AppConfig) -> Translator | None:
    target_language = config.translation_target_language.strip()
    if not target_language:
        return None
    return GoogleTranslateTranslator(
        target_language=target_language,
        endpoint=config.translation_endpoint,
        timeout_seconds=config.request_timeout_seconds,
    )
