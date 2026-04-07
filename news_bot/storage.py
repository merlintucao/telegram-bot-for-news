from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import SourcePost


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class RunRecord:
    run_id: int
    started_at: str
    finished_at: str | None
    dry_run: bool
    status: str
    fetched_count: int
    sent_count: int
    filtered_count: int
    bootstrapped: bool
    sources_processed: int
    error_message: str | None


@dataclass(slots=True)
class SourceEventRecord:
    source_key: str
    source_name: str
    event_type: str
    status_id: str | None
    post_url: str | None
    detail: str | None
    created_at: str
    run_id: int | None


@dataclass(slots=True)
class SourceStatusRecord:
    source_key: str
    source_name: str
    checkpoint_id: str | None
    checkpoint_updated_at: str | None
    last_delivered: SourceEventRecord | None
    last_bootstrap: SourceEventRecord | None
    last_error: SourceEventRecord | None
    recent_filtered: tuple[SourceEventRecord, ...]
    consecutive_failures: int
    last_success_at: str | None
    last_alerted_at: str | None


@dataclass(slots=True)
class SourceHealthRecord:
    source_key: str
    consecutive_failures: int
    last_success_at: str | None
    last_error_at: str | None
    last_error_detail: str | None
    last_alerted_failure_count: int
    last_alerted_at: str | None


class StateStore:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> sqlite3.Connection:
        connection = self._connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._session() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_checkpoint (
                    source_key TEXT PRIMARY KEY,
                    last_status_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS delivered_posts (
                    source_key TEXT NOT NULL,
                    status_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT NOT NULL,
                    raw_json TEXT NOT NULL,
                    PRIMARY KEY (source_key, status_id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    dry_run INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    fetched_count INTEGER NOT NULL DEFAULT 0,
                    sent_count INTEGER NOT NULL DEFAULT 0,
                    filtered_count INTEGER NOT NULL DEFAULT 0,
                    bootstrapped INTEGER NOT NULL DEFAULT 0,
                    sources_processed INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER,
                    source_key TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status_id TEXT,
                    post_url TEXT,
                    detail TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_source_events_lookup
                ON source_events (source_key, event_type, created_at DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bot_runs_started_at
                ON bot_runs (started_at DESC, id DESC)
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS source_health (
                    source_key TEXT PRIMARY KEY,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0,
                    last_success_at TEXT,
                    last_error_at TEXT,
                    last_error_detail TEXT,
                    last_alerted_failure_count INTEGER NOT NULL DEFAULT 0,
                    last_alerted_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def get_last_status_id(self, source_key: str) -> str | None:
        with self._session() as connection:
            row = connection.execute(
                "SELECT last_status_id FROM source_checkpoint WHERE source_key = ?",
                (source_key,),
            ).fetchone()
        return row["last_status_id"] if row else None

    def update_checkpoint(self, source_key: str, status_id: str) -> None:
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO source_checkpoint (source_key, last_status_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_key)
                DO UPDATE SET
                    last_status_id = excluded.last_status_id,
                    updated_at = excluded.updated_at
                """,
                (source_key, status_id, utc_now_iso()),
            )

    def was_delivered(self, source_key: str, status_id: str) -> bool:
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM delivered_posts
                WHERE source_key = ? AND status_id = ?
                """,
                (source_key, status_id),
            ).fetchone()
        return row is not None

    def start_run(self, dry_run: bool) -> int:
        with self._session() as connection:
            cursor = connection.execute(
                """
                INSERT INTO bot_runs (
                    started_at,
                    dry_run,
                    status
                )
                VALUES (?, ?, ?)
                """,
                (utc_now_iso(), 1 if dry_run else 0, "running"),
            )
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        fetched_count: int,
        sent_count: int,
        filtered_count: int,
        bootstrapped: bool,
        sources_processed: int,
        error_message: str | None = None,
    ) -> None:
        with self._session() as connection:
            connection.execute(
                """
                UPDATE bot_runs
                SET
                    finished_at = ?,
                    status = ?,
                    fetched_count = ?,
                    sent_count = ?,
                    filtered_count = ?,
                    bootstrapped = ?,
                    sources_processed = ?,
                    error_message = ?
                WHERE id = ?
                """,
                (
                    utc_now_iso(),
                    status,
                    fetched_count,
                    sent_count,
                    filtered_count,
                    1 if bootstrapped else 0,
                    sources_processed,
                    error_message,
                    run_id,
                ),
            )

    def log_source_event(
        self,
        *,
        run_id: int | None,
        source_key: str,
        source_name: str,
        event_type: str,
        status_id: str | None = None,
        post_url: str | None = None,
        detail: str | None = None,
    ) -> None:
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO source_events (
                    run_id,
                    source_key,
                    source_name,
                    event_type,
                    status_id,
                    post_url,
                    detail,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    source_key,
                    source_name,
                    event_type,
                    status_id,
                    post_url,
                    detail,
                    utc_now_iso(),
                ),
            )

    def record_delivery(self, source_key: str, post: SourcePost) -> None:
        delivered_at = utc_now_iso()
        with self._session() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO delivered_posts (
                    source_key,
                    status_id,
                    created_at,
                    delivered_at,
                    raw_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    post.id,
                    post.created_at,
                    delivered_at,
                    json.dumps(post.raw_payload, ensure_ascii=True),
                ),
            )
            connection.execute(
                """
                INSERT INTO source_checkpoint (source_key, last_status_id, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(source_key)
                DO UPDATE SET
                    last_status_id = excluded.last_status_id,
                    updated_at = excluded.updated_at
                """,
                (source_key, post.id, delivered_at),
            )

    def record_source_success(self, source_key: str) -> None:
        timestamp = utc_now_iso()
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO source_health (
                    source_key,
                    consecutive_failures,
                    last_success_at,
                    last_alerted_failure_count,
                    last_alerted_at,
                    updated_at
                )
                VALUES (?, 0, ?, 0, NULL, ?)
                ON CONFLICT(source_key)
                DO UPDATE SET
                    consecutive_failures = 0,
                    last_success_at = excluded.last_success_at,
                    last_alerted_failure_count = 0,
                    last_alerted_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (source_key, timestamp, timestamp),
            )

    def get_source_health(self, source_key: str) -> SourceHealthRecord | None:
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT
                    source_key,
                    consecutive_failures,
                    last_success_at,
                    last_error_at,
                    last_error_detail,
                    last_alerted_failure_count,
                    last_alerted_at
                FROM source_health
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()
        return self._row_to_source_health(row)

    def record_source_failure(self, source_key: str, detail: str | None = None) -> SourceHealthRecord:
        timestamp = utc_now_iso()
        with self._session() as connection:
            row = connection.execute(
                """
                SELECT
                    source_key,
                    consecutive_failures,
                    last_success_at,
                    last_error_at,
                    last_error_detail,
                    last_alerted_failure_count,
                    last_alerted_at
                FROM source_health
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()

            next_count = (int(row["consecutive_failures"]) if row else 0) + 1
            connection.execute(
                """
                INSERT INTO source_health (
                    source_key,
                    consecutive_failures,
                    last_success_at,
                    last_error_at,
                    last_error_detail,
                    last_alerted_failure_count,
                    last_alerted_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(source_key)
                DO UPDATE SET
                    consecutive_failures = excluded.consecutive_failures,
                    last_error_at = excluded.last_error_at,
                    last_error_detail = excluded.last_error_detail,
                    updated_at = excluded.updated_at
                """,
                (
                    source_key,
                    next_count,
                    row["last_success_at"] if row else None,
                    timestamp,
                    detail,
                    int(row["last_alerted_failure_count"]) if row else 0,
                    row["last_alerted_at"] if row else None,
                    timestamp,
                ),
            )
            updated_row = connection.execute(
                """
                SELECT
                    source_key,
                    consecutive_failures,
                    last_success_at,
                    last_error_at,
                    last_error_detail,
                    last_alerted_failure_count,
                    last_alerted_at
                FROM source_health
                WHERE source_key = ?
                """,
                (source_key,),
            ).fetchone()

        return self._row_to_source_health(updated_row)

    def mark_source_alert_sent(self, source_key: str, failure_count: int) -> None:
        timestamp = utc_now_iso()
        with self._session() as connection:
            connection.execute(
                """
                INSERT INTO source_health (
                    source_key,
                    consecutive_failures,
                    last_alerted_failure_count,
                    last_alerted_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(source_key)
                DO UPDATE SET
                    last_alerted_failure_count = excluded.last_alerted_failure_count,
                    last_alerted_at = excluded.last_alerted_at,
                    updated_at = excluded.updated_at
                """,
                (source_key, failure_count, failure_count, timestamp, timestamp),
            )

    def get_recent_runs(self, limit: int = 5) -> tuple[RunRecord, ...]:
        with self._session() as connection:
            rows = connection.execute(
                """
                SELECT
                    id,
                    started_at,
                    finished_at,
                    dry_run,
                    status,
                    fetched_count,
                    sent_count,
                    filtered_count,
                    bootstrapped,
                    sources_processed,
                    error_message
                FROM bot_runs
                ORDER BY started_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

        return tuple(self._row_to_run_record(row) for row in rows)

    def get_source_statuses(self, filtered_limit: int = 3) -> tuple[SourceStatusRecord, ...]:
        with self._session() as connection:
            source_rows = connection.execute(
                """
                SELECT source_key
                FROM source_checkpoint
                UNION
                SELECT source_key
                FROM delivered_posts
                UNION
                SELECT source_key
                FROM source_events
                UNION
                SELECT source_key
                FROM source_health
                ORDER BY source_key
                """
            ).fetchall()

            statuses: list[SourceStatusRecord] = []
            for source_row in source_rows:
                source_key = source_row["source_key"]
                name_row = connection.execute(
                    """
                    SELECT source_name
                    FROM source_events
                    WHERE source_key = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (source_key,),
                ).fetchone()
                checkpoint_row = connection.execute(
                    """
                    SELECT last_status_id, updated_at
                    FROM source_checkpoint
                    WHERE source_key = ?
                    """,
                    (source_key,),
                ).fetchone()
                delivered_row = connection.execute(
                    """
                    SELECT run_id, source_key, source_name, event_type, status_id, post_url, detail, created_at
                    FROM source_events
                    WHERE source_key = ? AND event_type = 'delivered'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (source_key,),
                ).fetchone()
                bootstrap_row = connection.execute(
                    """
                    SELECT run_id, source_key, source_name, event_type, status_id, post_url, detail, created_at
                    FROM source_events
                    WHERE source_key = ? AND event_type = 'bootstrap'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (source_key,),
                ).fetchone()
                error_row = connection.execute(
                    """
                    SELECT run_id, source_key, source_name, event_type, status_id, post_url, detail, created_at
                    FROM source_events
                    WHERE source_key = ? AND event_type = 'error'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (source_key,),
                ).fetchone()
                filtered_rows = connection.execute(
                    """
                    SELECT run_id, source_key, source_name, event_type, status_id, post_url, detail, created_at
                    FROM source_events
                    WHERE source_key = ? AND event_type = 'filtered'
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (source_key, filtered_limit),
                ).fetchall()
                health_row = connection.execute(
                    """
                    SELECT
                        source_key,
                        consecutive_failures,
                        last_success_at,
                        last_error_at,
                        last_error_detail,
                        last_alerted_failure_count,
                        last_alerted_at
                    FROM source_health
                    WHERE source_key = ?
                    """,
                    (source_key,),
                ).fetchone()
                health = self._row_to_source_health(health_row)

                statuses.append(
                    SourceStatusRecord(
                        source_key=source_key,
                        source_name=name_row["source_name"] if name_row else source_key,
                        checkpoint_id=checkpoint_row["last_status_id"] if checkpoint_row else None,
                        checkpoint_updated_at=checkpoint_row["updated_at"] if checkpoint_row else None,
                        last_delivered=self._row_to_source_event(delivered_row),
                        last_bootstrap=self._row_to_source_event(bootstrap_row),
                        last_error=self._row_to_source_event(error_row),
                        recent_filtered=tuple(
                            self._row_to_source_event(row) for row in filtered_rows if row is not None
                        ),
                        consecutive_failures=health.consecutive_failures if health else 0,
                        last_success_at=health.last_success_at if health else None,
                        last_alerted_at=health.last_alerted_at if health else None,
                    )
                )

        return tuple(statuses)

    @staticmethod
    def _row_to_run_record(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=int(row["id"]),
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            dry_run=bool(row["dry_run"]),
            status=row["status"],
            fetched_count=int(row["fetched_count"]),
            sent_count=int(row["sent_count"]),
            filtered_count=int(row["filtered_count"]),
            bootstrapped=bool(row["bootstrapped"]),
            sources_processed=int(row["sources_processed"]),
            error_message=row["error_message"],
        )

    @staticmethod
    def _row_to_source_event(row: sqlite3.Row | None) -> SourceEventRecord | None:
        if row is None:
            return None
        return SourceEventRecord(
            source_key=row["source_key"],
            source_name=row["source_name"],
            event_type=row["event_type"],
            status_id=row["status_id"],
            post_url=row["post_url"],
            detail=row["detail"],
            created_at=row["created_at"],
            run_id=row["run_id"],
        )

    @staticmethod
    def _row_to_source_health(row: sqlite3.Row | None) -> SourceHealthRecord | None:
        if row is None:
            return None
        return SourceHealthRecord(
            source_key=row["source_key"],
            consecutive_failures=int(row["consecutive_failures"]),
            last_success_at=row["last_success_at"],
            last_error_at=row["last_error_at"],
            last_error_detail=row["last_error_detail"],
            last_alerted_failure_count=int(row["last_alerted_failure_count"]),
            last_alerted_at=row["last_alerted_at"],
        )
