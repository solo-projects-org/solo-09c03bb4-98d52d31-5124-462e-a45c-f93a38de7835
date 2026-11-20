"""SQLite persistence for daily venue sketches and monthly summaries.

Schema versions
---------------
``PRAGMA user_version`` tracks the on-disk schema:

* ``1`` (implicit ``user_version = 0``): the original two-table layout.  Venue
  ids copied from text lists could carry a trailing LF and were stored as a
  distinct venue, so the same physical venue could own several rows for one
  day.  Monthly summaries could therefore merge a daily sketch that should
  have been replaced.
* ``2``: adds ``daily_conflict``.  Opening a version-1 database runs a single
  transactional migration: trailing CR/LF is stripped from venue ids, each
  venue-day keeps only its highest revision, equal-precision/equal-register
  top revisions merge into one row, and differing top revisions are preserved
  verbatim as conflict variants.  Affected monthly summaries are rebuilt or
  withdrawn while conflicts remain.

Invariants
----------
* A daily sketch, its conflict state, and the monthly summary are written in
  one ``BEGIN IMMEDIATE`` transaction.  Either the whole change becomes visible
  or none of it does; a crash or exception leaves the last successful state.
* A venue-day lives in exactly one state: one current ``daily_sketch`` row, or
  one or more ``daily_conflict`` variants and no ``daily_sketch`` row.
* A month carrying any conflicted venue-day has no ``monthly_summary`` row and
  its report answers HTTP 409 with the conflict list.  Reports resume only
  after every conflict in the month is resolved, and are then recomputed from
  the merged current sketches.
* Every non-blocked rebuild re-reads all current daily sketches of the month,
  so corrections that raise the minimum precision or lower register values are
  reflected instead of being frozen into an incrementally merged summary.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime

from . import hll

DEFAULT_DB_PATH = os.environ.get("MUSEUM_DB_PATH", "/data/museum.db")
BUSY_TIMEOUT_MS = 30_000
SCHEMA_VERSION = 2

# Migration only strips trailing CR/LF; the accepted venue-id alphabet has
# never included those characters, so stripping them never removes a real
# character.  The result is re-validated against the live pattern.
VENUE_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")


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
class ConflictVariant:
    """One preserved original submission for a conflicted venue-day."""

    revision: int
    p: int
    registers: list[int]
    content_hash: str
    updated_at: str


@dataclass(frozen=True)
class ConflictDay:
    venue_id: str
    day: str
    month: str
    highest_revision: int
    required_revision: int
    variants: list[ConflictVariant]


@dataclass(frozen=True)
class IngestOutcome:
    status: str  # created | replaced | unchanged | ignored | conflict_resolved
    venue_id: str
    day: str
    revision: int
    current_revision: int
    month: str
    month_blocked: bool
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
    return datetime.now(UTC).isoformat(timespec="microseconds")


def content_fingerprint(p: int, registers: Sequence[int]) -> str:
    """Canonical fingerprint of a sketch payload (precision + registers)."""
    payload = bytes([p]) + bytes(registers)
    return hashlib.sha256(payload).hexdigest()


def normalize_legacy_venue_id(venue_id: str) -> str:
    """Strip the trailing CR/LF that text-list copies could attach."""
    return venue_id.rstrip("\r\n")


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
            # Schema creation and the whole migration run in one transaction:
            # a crash anywhere rolls back to the original version-1 database,
            # and the next startup retries the migration from scratch.
            conn.execute("BEGIN IMMEDIATE")
            try:
                has_daily_table = conn.execute(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'daily_sketch'"
                ).fetchone()
                if has_daily_table is None:
                    self._create_schema(conn)
                else:
                    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                    if version < SCHEMA_VERSION:
                        self._migrate(conn, version)
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            conn.close()

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        # Individual CREATE statements (not executescript, which would commit
        # the surrounding migration transaction).
        conn.execute(
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
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_sketch_month ON daily_sketch (day)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monthly_summary (
                month        TEXT    PRIMARY KEY,
                p            INTEGER NOT NULL,
                registers    BLOB    NOT NULL,
                day_count    INTEGER NOT NULL,
                venue_count  INTEGER NOT NULL,
                updated_at   TEXT    NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS daily_conflict (
                venue_id      TEXT    NOT NULL,
                day           TEXT    NOT NULL,
                revision      INTEGER NOT NULL,
                p             INTEGER NOT NULL,
                registers     BLOB    NOT NULL,
                content_hash  TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL,
                PRIMARY KEY (venue_id, day, content_hash)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_daily_conflict_month "
            "ON daily_conflict (day)"
        )

    def _migrate(self, conn: sqlite3.Connection, version: int) -> None:
        self._create_schema(conn)
        if version < 2:
            self._migrate_v1_to_v2(conn)

    def _migrate_v1_to_v2(self, conn: sqlite3.Connection) -> None:
        """Merge LF-suffixed venue rows; preserve unsolvable venue-days as conflicts."""
        rows = conn.execute(
            "SELECT rowid AS rid, venue_id, day, revision, p, registers, "
            "content_hash, updated_at FROM daily_sketch"
        ).fetchall()

        # group key = (normalized venue id, day); every row belongs to exactly
        # one group, including rows whose id was already clean.
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            normalized = normalize_legacy_venue_id(row["venue_id"])
            if normalized != row["venue_id"] and not VENUE_ID_PATTERN.match(normalized):
                raise RuntimeError(
                    f"cannot migrate venue_id {row['venue_id']!r}: normalized id "
                    "does not match [A-Za-z0-9._-]{1,64}"
                )
            groups.setdefault((normalized, row["day"]), []).append(row)

        touched_months: set[str] = set()
        for (venue_id, day), group in groups.items():
            if len(group) == 1 and group[0]["venue_id"] == venue_id:
                continue  # untouched clean row
            touched_months.add(month_of(day))

            highest = max(int(row["revision"]) for row in group)
            top_rows = [row for row in group if int(row["revision"]) == highest]
            # Distinct payloads among the highest revisions.
            distinct: dict[str, sqlite3.Row] = {}
            for row in top_rows:
                distinct.setdefault(row["content_hash"], row)

            row_ids = [row["rid"] for row in group]
            conn.execute(
                "DELETE FROM daily_sketch WHERE rowid IN ({})".format(
                    ",".join("?" for _ in row_ids)
                ),
                row_ids,
            )

            if len(distinct) == 1:
                # Same precision and registers: the variants describe one
                # sketch, so the venue-day merges into a single current row.
                winner = next(iter(distinct.values()))
                p = int(winner["p"])
                registers = list(winner["registers"])
                hll.validate_registers(p, registers)
                conn.execute(
                    "INSERT INTO daily_sketch "
                    "(venue_id, day, revision, p, registers, content_hash, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        venue_id,
                        day,
                        highest,
                        p,
                        bytes(registers),
                        winner["content_hash"],
                        winner["updated_at"],
                    ),
                )
            else:
                # Unmergeable top revisions: keep every distinct original.
                for row in distinct.values():
                    p = int(row["p"])
                    registers = list(row["registers"])
                    hll.validate_registers(p, registers)
                    conn.execute(
                        "INSERT INTO daily_conflict "
                        "(venue_id, day, revision, p, registers, content_hash, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            venue_id,
                            day,
                            int(row["revision"]),
                            p,
                            bytes(registers),
                            row["content_hash"],
                            row["updated_at"],
                        ),
                    )

        # Every month that could have changed (including months only present in
        # the old summary) is rebuilt or withdrawn inside this same transaction.
        months = set(touched_months)
        months.update(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT substr(day, 1, 7) AS month FROM daily_sketch"
            ).fetchall()
        )
        months.update(
            row[0]
            for row in conn.execute("SELECT month FROM monthly_summary").fetchall()
        )
        months.update(
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT substr(day, 1, 7) AS month FROM daily_conflict"
            ).fetchall()
        )
        for month in sorted(months):
            self._refresh_month(conn, month)

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
            conflict_day = self._read_conflict_day(conn, venue_id, day)

            if conflict_day is not None:
                outcome = self._ingest_into_conflict(
                    conn, conflict_day, venue_id, day, revision, p, clean, fingerprint
                )
                if outcome is not None:
                    return outcome
                # Higher revision: the conflict is resolved below.
                status = "conflict_resolved"
            else:
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
                        (
                            venue_id,
                            day,
                            revision,
                            p,
                            bytes(clean),
                            fingerprint,
                            utc_now(),
                        ),
                    )
                elif revision > existing["revision"]:
                    status = "replaced"
                    conn.execute(
                        "UPDATE daily_sketch SET revision = ?, p = ?, registers = ?, "
                        "content_hash = ?, updated_at = ? "
                        "WHERE venue_id = ? AND day = ?",
                        (
                            revision,
                            p,
                            bytes(clean),
                            fingerprint,
                            utc_now(),
                            venue_id,
                            day,
                        ),
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
                            month_blocked=self._month_has_conflicts(conn, month),
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
                        month_blocked=self._month_has_conflicts(conn, month),
                        summary=self._read_summary(conn, month),
                    )

            summary = self._refresh_month(conn, month)
            return IngestOutcome(
                status=status,
                venue_id=venue_id,
                day=day,
                revision=revision,
                current_revision=revision,
                month=month,
                month_blocked=summary is None
                and self._month_has_conflicts(conn, month),
                summary=summary,
            )

    def _ingest_into_conflict(
        self,
        conn: sqlite3.Connection,
        conflict_day: ConflictDay,
        venue_id: str,
        day: str,
        revision: int,
        p: int,
        clean: list[int],
        fingerprint: str,
    ) -> IngestOutcome | None:
        """Apply ingest rules against a conflicted venue-day.

        Returns a terminal outcome for stale/idempotent/conflicting writes,
        or ``None`` when the incoming revision resolves the conflict.
        """
        month = month_of(day)
        highest = conflict_day.highest_revision
        if revision < highest:
            return IngestOutcome(
                status="ignored",
                venue_id=venue_id,
                day=day,
                revision=revision,
                current_revision=highest,
                month=month,
                month_blocked=True,
                summary=self._read_summary(conn, month),
            )
        if revision == highest:
            already_kept = conn.execute(
                "SELECT 1 FROM daily_conflict "
                "WHERE venue_id = ? AND day = ? AND content_hash = ?",
                (venue_id, day, fingerprint),
            ).fetchone()
            if already_kept is not None:
                return IngestOutcome(
                    status="unchanged",
                    venue_id=venue_id,
                    day=day,
                    revision=revision,
                    current_revision=highest,
                    month=month,
                    month_blocked=True,
                    summary=self._read_summary(conn, month),
                )
            raise ConflictError(venue_id, day, revision, highest)

        # A strictly higher revision from the correct venue id supersedes every
        # preserved variant and clears the venue-day conflict.
        conn.execute(
            "DELETE FROM daily_conflict WHERE venue_id = ? AND day = ?",
            (venue_id, day),
        )
        conn.execute(
            "INSERT INTO daily_sketch "
            "(venue_id, day, revision, p, registers, content_hash, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (venue_id, day, revision, p, bytes(clean), fingerprint, utc_now()),
        )
        return None

    # -- monthly recompute ----------------------------------------------
    def _month_has_conflicts(self, conn: sqlite3.Connection, month: str) -> bool:
        start, end = f"{month}-01", f"{next_month(month)}-01"
        row = conn.execute(
            "SELECT COUNT(*) FROM daily_conflict WHERE day >= ? AND day < ?",
            (start, end),
        ).fetchone()
        return int(row[0]) > 0

    def _refresh_month(
        self, conn: sqlite3.Connection, month: str
    ) -> MonthlySummary | None:
        """Rebuild the month, or withdraw its summary while conflicts remain."""
        start, end = f"{month}-01", f"{next_month(month)}-01"
        if self._month_has_conflicts(conn, month):
            conn.execute("DELETE FROM monthly_summary WHERE month = ?", (month,))
            return None
        daily_count = conn.execute(
            "SELECT COUNT(*) FROM daily_sketch WHERE day >= ? AND day < ?",
            (start, end),
        ).fetchone()[0]
        if daily_count == 0:
            conn.execute("DELETE FROM monthly_summary WHERE month = ?", (month,))
            return None
        return self._recompute_month(conn, month)

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

    # -- conflict reads --------------------------------------------------
    @staticmethod
    def _read_conflict_day(
        conn: sqlite3.Connection, venue_id: str, day: str
    ) -> ConflictDay | None:
        rows = conn.execute(
            "SELECT revision, p, registers, content_hash, updated_at "
            "FROM daily_conflict WHERE venue_id = ? AND day = ? "
            "ORDER BY revision, updated_at, content_hash",
            (venue_id, day),
        ).fetchall()
        if not rows:
            return None
        variants: list[ConflictVariant] = []
        highest = -1
        for row in rows:
            p = int(row["p"])
            registers = list(row["registers"])
            hll.validate_registers(p, registers)
            revision = int(row["revision"])
            highest = max(highest, revision)
            variants.append(
                ConflictVariant(
                    revision=revision,
                    p=p,
                    registers=registers,
                    content_hash=row["content_hash"],
                    updated_at=row["updated_at"],
                )
            )
        return ConflictDay(
            venue_id=venue_id,
            day=day,
            month=month_of(day),
            highest_revision=highest,
            required_revision=highest + 1,
            variants=variants,
        )

    def get_conflict_day(self, venue_id: str, day: str) -> ConflictDay | None:
        conn = self._connect()
        try:
            return self._read_conflict_day(conn, venue_id, day)
        finally:
            conn.close()

    def get_conflicts(self, month: str | None = None) -> list[ConflictDay]:
        conn = self._connect()
        try:
            if month is None:
                rows = conn.execute(
                    "SELECT DISTINCT venue_id, day FROM daily_conflict "
                    "ORDER BY day, venue_id"
                ).fetchall()
            else:
                start, end = f"{month}-01", f"{next_month(month)}-01"
                rows = conn.execute(
                    "SELECT DISTINCT venue_id, day FROM daily_conflict "
                    "WHERE day >= ? AND day < ? ORDER BY day, venue_id",
                    (start, end),
                ).fetchall()
            days = [
                self._read_conflict_day(conn, row["venue_id"], row["day"])
                for row in rows
            ]
            return [day for day in days if day is not None]
        finally:
            conn.close()

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
