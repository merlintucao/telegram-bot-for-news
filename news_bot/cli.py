from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import sys
import time
from dataclasses import asdict

from .config import AppConfig
from .cookies import load_cookie_jar
from .filtering import build_post_filter
from .routing import build_router
from .service import NewsBotService
from .storage import StateStore
from .source_types import SourceError
from .sources import build_sources
from .telegram import TelegramError, TelegramSender


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Poll Truth Social and forward new posts to Telegram."
    )
    parser.add_argument(
        "command",
        choices=("once", "run", "doctor", "status", "notify"),
        help="Run one polling cycle, keep polling on an interval, inspect setup health, show saved bot status, or send a Telegram test message.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print messages instead of sending them to Telegram.",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="Path to the env file to load before reading environment variables.",
    )
    parser.add_argument(
        "--skip-network",
        action="store_true",
        help="Skip Truth Social network probes during the doctor command.",
    )
    parser.add_argument(
        "--status-limit",
        type=int,
        default=3,
        help="Number of recent runs and recent filtered events to show in the status command.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON output for the status command.",
    )
    parser.add_argument(
        "--notify-target",
        choices=("main", "alert", "both", "routed", "all"),
        default="both",
        help="Choose which Telegram destination receives the notify test message.",
    )
    parser.add_argument(
        "--notify-message",
        default="",
        help="Custom text for the notify test message.",
    )
    parser.add_argument(
        "--notify-source",
        default="",
        help="Optional source id or wildcard pattern used when notify-target includes routed or all.",
    )
    return parser


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def run_doctor(config: AppConfig, skip_network: bool) -> int:
    ok = True

    print("Doctor report")
    print(f"- Telegram bot token: {'set' if config.telegram_bot_token else 'missing'}")
    print(f"- Telegram chat id: {'set' if config.telegram_chat_id else 'missing'}")
    print(
        "- Telegram alert chat id: "
        + ("set" if config.telegram_alert_chat_id else "disabled")
    )
    print(f"- Source route rules: {len(config.source_chat_routes)}")
    print(f"- Source keyword filters: {len(config.source_keyword_filters)}")
    print(f"- Source category filters: {len(config.source_category_filters)}")
    print(f"- Source failure alert threshold: {config.source_failure_alert_threshold}")
    print(f"- Source retry attempts: {config.source_retry_attempts}")
    print(f"- Source retry backoff seconds: {config.source_retry_backoff_seconds}")
    print(
        "- Translation target language: "
        + (config.translation_target_language or "disabled")
    )
    print(
        "- Continue on source error: "
        + ("enabled" if config.continue_on_source_error else "disabled")
    )
    print(f"- Enabled sources: {', '.join(config.enabled_sources)}")
    normalized_sources = {name.lower() for name in config.enabled_sources}
    if "rss" in normalized_sources:
        print(f"- RSS feed urls configured: {len(config.rss_feed_urls)}")
    if normalized_sources.intersection({"truthsocial", "truthsocial_trump"}):
        print(f"- Truth Social access mode: {config.truthsocial_auth_mode}")
        print(
            "- Truth Social account id: "
            + (config.truthsocial_account_id or "not set; will use account lookup")
        )
        print(
            "- Truth Social cookie auto-reload: "
            + (
                "enabled"
                if config.truthsocial_auth_mode != "public" and config.truthsocial_reload_cookies
                else "disabled"
            )
        )
    print(f"- State database path: {config.state_db_path}")

    if not config.telegram_bot_token:
        ok = False

    if normalized_sources.intersection({"truthsocial", "truthsocial_trump"}):
        cookies_required = config.truthsocial_auth_mode == "cookies"
        if config.truthsocial_cookies_file is None:
            if cookies_required:
                print("- Truth Social cookies: missing (required in cookies mode)")
                ok = False
            else:
                print("- Truth Social cookies: not configured (optional in public/auto mode)")
        else:
            cookie_path = config.truthsocial_cookies_file
            if not cookie_path.exists():
                if cookies_required:
                    print(f"- Truth Social cookies: missing file at {cookie_path} (required in cookies mode)")
                    ok = False
                else:
                    print(
                        f"- Truth Social cookies: missing file at {cookie_path} "
                        "(optional unless you want cookie fallback)"
                    )
            else:
                try:
                    jar = load_cookie_jar(cookie_path)
                    cookies = list(jar)
                    domains = sorted({cookie.domain for cookie in cookies if getattr(cookie, "domain", "")})
                    print(
                        f"- Truth Social cookies: {len(cookies)} cookies loaded from {cookie_path}"
                    )
                    if domains:
                        preview = ", ".join(domains[:5])
                        suffix = " ..." if len(domains) > 5 else ""
                        print(f"  domains: {preview}{suffix}")
                    if not cookies and cookies_required:
                        ok = False
                except Exception as exc:
                    print(f"- Truth Social cookies: failed to load ({exc})")
                    if cookies_required:
                        ok = False

    try:
        router = build_router(
            default_chat_id=config.telegram_chat_id,
            raw_rules=config.source_chat_routes,
        )
        post_filter = build_post_filter(
            raw_keyword_rules=config.source_keyword_filters,
            raw_category_rules=config.source_category_filters,
        )
        sources = build_sources(config)
        print(f"- Source registry: ok ({len(sources)} source(s))")
        for source in sources:
            destinations = ", ".join(router.destinations_for_source(source.source_id)) or "<none>"
            filter_notes: list[str] = []
            keyword_terms = post_filter._terms_for_source(source.source_id, post_filter.keyword_rules)
            category_terms = post_filter._terms_for_source(source.source_id, post_filter.category_rules)
            if keyword_terms is not None:
                filter_notes.append("keywords=" + "|".join(keyword_terms))
            if category_terms is not None:
                filter_notes.append("categories=" + "|".join(category_terms))
            suffix = f" [{', '.join(filter_notes)}]" if filter_notes else ""
            print(f"  {source.source_id} -> {destinations}{suffix}")
            if not router.destinations_for_source(source.source_id):
                ok = False
    except ValueError as exc:
        print(f"- Source registry: failed ({exc})")
        return 1

    if skip_network:
        print("- Source probes: skipped (--skip-network)")
        return 0 if ok else 1

    for source in sources:
        print(f"- Source probe: {source.source_id}")
        try:
            result = source.probe()
            print(f"  status: ok ({result.source_name})")
            for detail in result.detail_lines:
                print(f"  {detail}")
        except SourceError as exc:
            print("  status: failed")
            print(f"  {exc}")
            ok = False

    return 0 if ok else 1


def run_status(config: AppConfig, limit: int, as_json: bool = False) -> int:
    store = StateStore(config.state_db_path)
    recent_runs = store.get_recent_runs(limit=limit)
    source_statuses = store.get_source_statuses(filtered_limit=limit)

    if as_json:
        print(
            json.dumps(
                {
                    "runs": [asdict(run) for run in recent_runs],
                    "sources": [asdict(status) for status in source_statuses],
                },
                ensure_ascii=True,
                indent=2,
            )
        )
        return 0

    print("Status report")
    if not recent_runs:
        print("- Runs: none recorded yet")
    else:
        print("- Recent runs:")
        for run in recent_runs:
            dry_run_suffix = " dry-run" if run.dry_run else ""
            error_suffix = f" error={run.error_message}" if run.error_message else ""
            print(
                "  "
                f"{run.started_at} status={run.status}{dry_run_suffix} "
                f"fetched={run.fetched_count} sent={run.sent_count} "
                f"filtered={run.filtered_count} sources={run.sources_processed}{error_suffix}"
            )

    if not source_statuses:
        print("- Sources: none recorded yet")
        return 0

    print("- Sources:")
    for status in source_statuses:
        print(f"  {status.source_key} ({status.source_name})")
        if status.checkpoint_id:
            print(
                "    "
                f"checkpoint={status.checkpoint_id} updated_at={status.checkpoint_updated_at}"
            )
        else:
            print("    checkpoint=<none>")

        if status.last_delivered:
            delivered = status.last_delivered
            delivered_line = (
                f"last_delivered={delivered.status_id} at={delivered.created_at}"
            )
            if delivered.post_url:
                delivered_line += f" url={delivered.post_url}"
            print(f"    {delivered_line}")
        elif status.last_bootstrap:
            bootstrap = status.last_bootstrap
            bootstrap_line = (
                f"last_bootstrap={bootstrap.status_id} at={bootstrap.created_at}"
            )
            if bootstrap.post_url:
                bootstrap_line += f" url={bootstrap.post_url}"
            print(f"    {bootstrap_line}")
        else:
            print("    last_delivered=<none>")

        if status.last_error:
            error = status.last_error
            error_line = f"last_error at={error.created_at}"
            if error.detail:
                error_line += f" detail={error.detail}"
            print(f"    {error_line}")
        else:
            print("    last_error=<none>")

        if status.consecutive_failures:
            health_line = f"health=degraded consecutive_failures={status.consecutive_failures}"
            if status.last_success_at:
                health_line += f" last_success_at={status.last_success_at}"
            if status.last_alerted_at:
                health_line += f" last_alerted_at={status.last_alerted_at}"
            print(f"    {health_line}")
        else:
            health_line = "health=ok"
            if status.last_success_at:
                health_line += f" last_success_at={status.last_success_at}"
            print(f"    {health_line}")

        if status.recent_filtered:
            print("    recent_filtered:")
            for event in status.recent_filtered:
                detail = f" reason={event.detail}" if event.detail else ""
                print(
                    "      "
                    f"{event.status_id} at={event.created_at}{detail}"
                )
        else:
            print("    recent_filtered=<none>")

    return 0


def build_notify_message(
    kind: str,
    message: str = "",
    source_ids: tuple[str, ...] = (),
) -> str:
    if message.strip():
        return message.strip()
    if kind == "alert":
        return "Telegram alert chat test from news_bot."
    if kind == "routed":
        if not source_ids:
            return "Telegram routed chat test from news_bot."
        label = "Source" if len(source_ids) == 1 else "Sources"
        return "Telegram routed chat test from news_bot.\n" + f"{label}: {', '.join(source_ids)}"
    return "Telegram main chat test from news_bot."


def run_notify(
    config: AppConfig,
    *,
    target: str = "both",
    message: str = "",
    source_pattern: str = "",
    sender: TelegramSender | None = None,
) -> int:
    targets: list[tuple[str, str, tuple[str, ...]]] = []
    if target in {"main", "both", "all"} and config.telegram_chat_id:
        targets.append(("main", config.telegram_chat_id, ()))
    if target in {"alert", "both", "all"} and config.telegram_alert_chat_id:
        targets.append(("alert", config.telegram_alert_chat_id, ()))

    if target in {"routed", "all"}:
        try:
            router = build_router(
                default_chat_id=config.telegram_chat_id,
                raw_rules=config.source_chat_routes,
            )
            sources = build_sources(config)
        except ValueError as exc:
            print(f"Notify failed: {exc}")
            return 1

        route_map: dict[str, list[str]] = {}
        matched_sources = 0
        for source in sources:
            if source_pattern and not fnmatch.fnmatchcase(source.source_id, source_pattern):
                continue
            matched_sources += 1
            for chat_id in router.destinations_for_source(source.source_id):
                route_map.setdefault(chat_id, []).append(source.source_id)

        if source_pattern and matched_sources == 0:
            print(f"No sources matched notify pattern '{source_pattern}'.")
            return 1

        for chat_id, source_ids in route_map.items():
            targets.append(("routed", chat_id, tuple(source_ids)))

    if not targets:
        print("No Telegram destinations configured for notify.")
        return 1

    try:
        resolved_sender = sender or TelegramSender(
            bot_token=config.telegram_bot_token,
            chat_id=config.telegram_chat_id,
            timeout_seconds=config.request_timeout_seconds,
        )
        for kind, chat_id, source_ids in targets:
            resolved_sender.send_message(
                build_notify_message(kind, message, source_ids=source_ids),
                chat_id=chat_id,
            )
            if kind == "routed" and source_ids:
                print(f"Sent {kind} test message to {chat_id} for {', '.join(source_ids)}")
            else:
                print(f"Sent {kind} test message to {chat_id}")
    except TelegramError as exc:
        print(f"Notify failed: {exc}")
        return 1

    return 0


def run_command(
    config: AppConfig,
    command: str,
    dry_run: bool,
    skip_network: bool,
    status_limit: int,
    json_output: bool,
    notify_target: str,
    notify_message: str,
    notify_source: str,
) -> int:
    if command == "doctor":
        return run_doctor(config, skip_network=skip_network)
    if command == "status":
        return run_status(config, limit=status_limit, as_json=json_output)
    if command == "notify":
        return run_notify(
            config,
            target=notify_target,
            message=notify_message,
            source_pattern=notify_source,
        )

    service = NewsBotService.from_config(config, dry_run=dry_run)
    continuous = command == "run"

    if not continuous:
        summary = service.run_once(dry_run=dry_run)
        log_fn = logging.warning if summary.failed_sources else logging.info
        log_fn(
            "Cycle complete: fetched=%s sent=%s filtered=%s bootstrapped=%s failed_sources=%s",
            summary.fetched_count,
            summary.sent_count,
            summary.filtered_count,
            summary.bootstrapped,
            summary.failed_sources,
        )
        return 1 if summary.failed_sources else 0

    while True:
        try:
            summary = service.run_once(dry_run=dry_run)
            log_fn = logging.warning if summary.failed_sources else logging.info
            log_fn(
                "Cycle complete: fetched=%s sent=%s filtered=%s bootstrapped=%s failed_sources=%s",
                summary.fetched_count,
                summary.sent_count,
                summary.filtered_count,
                summary.bootstrapped,
                summary.failed_sources,
            )
        except SourceError:
            logging.exception("Source polling failed")
        except Exception:
            logging.exception("Unexpected bot failure")

        time.sleep(config.poll_interval_seconds)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    config = AppConfig.from_env(args.env_file)
    configure_logging(config.log_level)

    try:
        return run_command(
            config=config,
            command=args.command,
            dry_run=args.dry_run,
            skip_network=args.skip_network,
            status_limit=args.status_limit,
            json_output=args.json,
            notify_target=args.notify_target,
            notify_message=args.notify_message,
            notify_source=args.notify_source,
        )
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except Exception as exc:
        logging.exception("Bot startup failed")
        print(f"Error: {exc}", file=sys.stderr)
        return 1
