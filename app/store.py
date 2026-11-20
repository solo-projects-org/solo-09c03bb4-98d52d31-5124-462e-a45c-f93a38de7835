"""SQLite persistence for daily venue sketches and monthly summaries.

Invariants
----------
* A daily sketch and the recomputed monthly summary are written in one
  ``BEGIN IMMEDIATE`` transaction.  Either both become visible or neither does.
* Every write transaction re-reads all current daily sketches of the affected
  month before merging.  SQLite serialises writers, so concurrent backfills
  cannot commit a summary that misses another writer's current sketch.
* A correction always recomputes the month from scratch, so raising the
  minimum precision or lowering register values is reflected instead of being
  frozen into an incrementally merged summary.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator, Sequence

from . import hll

DEFAULT_DB_PATH = os.environ.get("MUSEUM_DB_PATH", "/data/museum.db")
BUSY_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class DailySketch:
    venue_id: str
    day: str
    revision: int
    p: int
    registers: list[int]
    content_hash: str
    updated_at: str


@dataclass(frozen=True)
class MonthlySummary:
    month: str
    p: int
    registers: list[int]
    day_count: int
    venue_count: int
    updated_at: str


@dataclass(frozen=True)
class IngestOutcome:
    status: str  # created | replaced | unchanged | ignored
    venue_id: str
    day: str
    revision: int
    current_revision: int
    month: str
    summary: MonthlySummary | None


class ConflictError(Exception):
    """Same revision was already stored with different content."""

    def __init__(self, venue_id: str, day: str, revision: int, current_revision: int):
        super().__init__(
            f"{venue_id}/{day} revision {revision} conflicts with stored content"
        )
        self.venue_id = venue_id
        self.day = day
        self.revision = revision
        self.current_revision = current_revision


def month_of(day: str) -> str:
    return day[:7]


def next_month(month: str) -> str:
    year, mon = (int(part) for part in month.split("-"))
    if mon == 12:
        return f"{year + 1:04d}-01"
    return f"{year:04d}-{mon + 1:02d}"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def content_fingerprint(p: int, registers: Sequence[int]) -> str:
    """Canonical fingerprint of a sketch payload (precision + registers)."""
    payload = bytes([p]) + bytes(registers)
    return hashlib.sha256(payload).hexdigest()


class Storage:
    def __init__(self, path: str = DEFAULT_DB_PATH):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.initialize()

    # -- connections -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path, timeout=BUSY_TIMEOUT_MS / 1000.0, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA synchronous = FULL")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def initialize(self) -> None:
        # journal_mode is persisted in the database file, so it is set once
        # here instead of on every connection (which would need a write lock).
        conn = self._connect()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS daily_sketch (
                    venue_id      TEXT    NOT NULL,
                    day           TEXT    NOT NULL,
                    revision      INTEGER NOT NULL,
                    p             INTEGER NOT NULL,
                    registers     BLOB    NOT NULL,
                    content_hash  TEXT    NOT NULL,
                    updated_at    TEXT    NOT NULL,
                    PRIMARY KEY (venue_id, day)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_sketch_month
                    ON daily_sketch (day);
                CREATE TABLE IF NOT EXISTS monthly_summary (
                    month        TEXT    PRIMARY KEY,
                    p            INTEGER NOT NULL,
                    registers    BLOB    NOT NULL,
                    day_count    INTEGER NOT NULL,
                    venue_count  INTEGER NOT NULL,
                    updated_at   TEXT    NOT NULL
                );
                """
            )
        finally:
            conn.close()

    @contextmanager
    def _write_transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            # Take the write lock before reading, so the recompute below sees
            # every sketch that was current when this writer started.
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.rollback()
                raise
            else:
                conn.commit()
        finally:
            conn.close()

    # -- ingest ----------------------------------------------------------
    def ingest(
        self,
        venue_id: str,
        day: str,
        revision: int,
        p: int,
        registers: Sequence[int],
    ) -> IngestOutcome:
        hll.validate_precision(p)
        clean = hll.validate_registers(p, registers)
        fingerprint = content_fingerprint(p, clean)
        month = month_of(day)

        with self._write_transaction() as conn:
            existing = conn.execute(
                "SELECT revision, content_hash FROM daily_sketch "
                "WHERE venue_id = ? AND day = ?",
                (venue_id, day),
            ).fetchone()

            if existing is None:
                status = "created"
                conn.execute(
                    "INSERT INTO daily_sketch "
                    "(venue_id, day, revision, p, registers, content_hash, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (venue_id, day, revision, p, bytes(clean), fingerprint, utc_now()),
                )
            elif revision > existing["revision"]:
                status = "replaced"
                conn.execute(
                    "UPDATE daily_sketch SET revision = ?, p = ?, registers = ?, "
                    "content_hash = ?, updated_at = ? "
                    "WHERE venue_id = ? AND day = ?",
                    (revision, p, bytes(clean), fingerprint, utc_now(), venue_id, day),
                )
            elif revision == existing["revision"]:
                if existing["content_hash"] == fingerprint:
                    return IngestOutcome(
                        status="unchanged",
                        venue_id=venue_id,
                        day=day,
                        revision=revision,
                        current_revision=existing["revision"],
                        month=month,
                        summary=self._read_summary(conn, month),
                    )
                raise ConflictError(venue_id, day, revision, existing["revision"])
            else:
                return IngestOutcome(
                    status="ignored",
                    venue_id=venue_id,
                    day=day,
                    revision=revision,
                    current_revision=existing["revision"],
                    month=month,
                    summary=self._read_summary(conn, month),
                )

            summary = self._recompute_month(conn, month)
            return IngestOutcome(
                status=status,
                venue_id=venue_id,
                day=day,
                revision=revision,
                current_revision=revision,
                month=month,
                summary=summary,
            )

    def _recompute_month(self, conn: sqlite3.Connection, month: str) -> MonthlySummary:
        start, end = f"{month}-01", f"{next_month(month)}-01"
        rows = conn.execute(
            "SELECT venue_id, day, p, registers FROM daily_sketch "
            "WHERE day >= ? AND day < ? ORDER BY day, venue_id",
            (start, end),
        ).fetchall()
        if not rows:
            raise RuntimeError(f"month {month} has no current daily sketches")

        sketches: list[tuple[int, list[int]]] = []
        days: set[str] = set()
        venues: set[str] = set()
        for row in rows:
            p = int(row["p"])
            registers = list(row["registers"])
            # Re-validate what we read: a corrupted row must not silently
            # poison the monthly summary.
            hll.validate_registers(p, registers)
            sketches.append((p, registers))
            venues.add(row["venue_id"])
            days.add(row["day"])

        merged = hll.merge_sketches(sketches)
        target_p = min(p for p, _ in sketches)
        now = utc_now()
        conn.execute(
            "INSERT INTO monthly_summary "
            "(month, p, registers, day_count, venue_count, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(month) DO UPDATE SET "
            "p = excluded.p, registers = excluded.registers, "
            "day_count = excluded.day_count, venue_count = excluded.venue_count, "
            "updated_at = excluded.updated_at",
            (month, target_p, bytes(merged), len(days), len(venues), now),
        )
        return MonthlySummary(
            month=month,
            p=target_p,
            registers=merged,
            day_count=len(days),
            venue_count=len(venues),
            updated_at=now,
        )

    # -- reads -----------------------------------------------------------
    @staticmethod
    def _read_summary(conn: sqlite3.Connection, month: str) -> MonthlySummary | None:
        row = conn.execute(
            "SELECT month, p, registers, day_count, venue_count, updated_at "
            "FROM monthly_summary WHERE month = ?",
            (month,),
        ).fetchone()
        if row is None:
            return None
        p = int(row["p"])
        registers = list(row["registers"])
        hll.validate_registers(p, registers)
        return MonthlySummary(
            month=row["month"],
            p=p,
            registers=registers,
            day_count=int(row["day_count"]),
            venue_count=int(row["venue_count"]),
            updated_at=row["updated_at"],
        )

    def get_monthly(self, month: str) -> MonthlySummary | None:
        conn = self._connect()
        try:
            return self._read_summary(conn, month)
        finally:
            conn.close()

    def get_daily(self, venue_id: str, day: str) -> DailySketch | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT venue_id, day, revision, p, registers, content_hash, updated_at "
                "FROM daily_sketch WHERE venue_id = ? AND day = ?",
                (venue_id, day),
            ).fetchone()
            if row is None:
                return None
            p = int(row["p"])
            registers = list(row["registers"])
            hll.validate_registers(p, registers)
            return DailySketch(
                venue_id=row["venue_id"],
                day=row["day"],
                revision=int(row["revision"]),
                p=p,
                registers=registers,
                content_hash=row["content_hash"],
                updated_at=row["updated_at"],
            )
        finally:
            conn.close()
