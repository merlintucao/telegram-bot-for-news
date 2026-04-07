from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Callable

from .config import AppConfig
from .filtering import PostFilter, build_post_filter
from .models import SourcePost
from .routing import SourceRouter, build_router
from .sources import SourceAdapter, build_sources
from .storage import SourceHealthRecord, StateStore
from .telegram import TelegramError, TelegramSender
from .translate import GoogleTranslateTranslator, TranslationError, Translator

LOGGER = logging.getLogger(__name__)

HANDLE_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
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
    return localized.strftime("%d/%m/%Y %H:%M")


def format_post_message(post: SourcePost, translated_text: str | None = None) -> str:
    lines = [_format_header(post)]

    if post.created_at:
        lines.extend(["", f"Posted: {_format_posted_at(post.created_at)}"])

    if post.url:
        lines.append(f"Link: {post.url}")

    snippet = translated_text or _post_caption_text(post)
    if snippet:
        lines.extend(["", snippet])

    if post.media_urls:
        lines.extend(["", "Media:"])
        lines.extend(post.media_urls[:3])

    return trim_message("\n".join(lines))


def format_post_caption(post: SourcePost, translated_text: str | None = None) -> str:
    lines = [_format_header(post)]

    info_lines: list[str] = []
    if post.created_at:
        info_lines.append(f"Posted: {_format_posted_at(post.created_at)}")
    if post.url:
        info_lines.append(f"Link: {post.url}")
    if info_lines:
        lines.extend(["", *info_lines])

    snippet = translated_text or _post_caption_text(post)
    if snippet:
        lines.extend(["", snippet])
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
    ) -> None:
        self.config = config
        self.store = store
        self.sources = sources
        self.router = router
        self.post_filter = post_filter
        self.sender = sender
        self.sleep_fn = sleep_fn
        self.translator = translator

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
            message = format_post_message(post, translated_text=translated_text)
            media_caption = format_post_caption(post, translated_text=translated_text)
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
        try:
            translated_text = self.translator.translate(original_caption)
        except TranslationError as exc:
            LOGGER.warning("Translation failed for %s: %s", post.id, exc)
            return None
        normalized = translated_text.strip()
        if not normalized:
            return None
        return normalized


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
