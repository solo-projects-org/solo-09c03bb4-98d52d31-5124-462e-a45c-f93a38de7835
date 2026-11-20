import sqlite3
import threading

import pytest

from app import hll
from app.store import ConflictError, Storage


@pytest.fixture()
def storage(tmp_path):
    return Storage(str(tmp_path / "museum.db"))


def sketch(tickets, p=8):
    return hll.sketch_for_tickets(tickets, p)


def test_create_then_replace_then_ignore_stale(storage):
    a = sketch(["t1", "t2"], 8)
    first = storage.ingest("v1", "2026-03-05", 1, 8, a)
    assert first.status == "created"
    assert first.summary.p == 8

    b = sketch(["t1", "t2", "t3"], 8)
    second = storage.ingest("v1", "2026-03-05", 2, 8, b)
    assert second.status == "replaced"
    assert storage.get_daily("v1", "2026-03-05").revision == 2

    stale = storage.ingest("v1", "2026-03-05", 1, 8, a)
    assert stale.status == "ignored"
    assert stale.current_revision == 2
    assert storage.get_daily("v1", "2026-03-05").registers == b


def test_same_revision_same_content_is_idempotent(storage):
    a = sketch(["t1"], 8)
    storage.ingest("v1", "2026-03-05", 3, 8, a)
    again = storage.ingest("v1", "2026-03-05", 3, 8, a)
    assert again.status == "unchanged"


def test_content_hash_is_precision_sensitive(storage):
    # Same registers, different precision => different content, so a
    # same-revision submission with another p must conflict.
    a = sketch(["t1"], 8)
    storage.ingest("v1", "2026-03-05", 3, 8, a)
    with pytest.raises(ConflictError):
        storage.ingest("v1", "2026-03-05", 3, 9, [0] * 512)


def test_same_revision_different_content_conflicts(storage):
    storage.ingest("v1", "2026-03-05", 3, 8, sketch(["t1"], 8))
    with pytest.raises(ConflictError):
        storage.ingest("v1", "2026-03-05", 3, 8, sketch(["t2"], 8))
    stored = storage.get_daily("v1", "2026-03-05")
    assert stored.registers == sketch(["t1"], 8)


def test_month_summary_uses_lowest_precision_and_dedups_venues(storage):
    shared = ["shared-1", "shared-2"]
    a = sketch(shared + ["a-only"], 10)
    b = sketch(shared + ["b-only"], 7)
    storage.ingest("v1", "2026-03-01", 1, 10, a)
    storage.ingest("v2", "2026-03-01", 1, 7, b)

    summary = storage.get_monthly("2026-03")
    assert summary.p == 7
    assert summary.venue_count == 2
    assert summary.day_count == 1

    # Build the same union at p=7 from the ticket ids directly.
    expected = hll.sketch_for_tickets(shared + ["a-only", "b-only"], 7)
    assert summary.registers == expected

    # Duplicate ticket across venues counted once.
    single = hll.sketch_for_tickets(shared, 7)
    assert hll.estimate(summary.registers, 7) > hll.estimate(single, 7)
    assert abs(hll.estimate(summary.registers, 7) - 4) < 1.5


def test_correction_can_raise_month_precision(storage):
    storage.ingest("v1", "2026-04-02", 1, 5, sketch(["x"], 5))
    assert storage.get_monthly("2026-04").p == 5

    # Venue corrects its day with a higher-precision sketch; the month must
    # re-derive to the higher precision instead of staying at p=5.
    storage.ingest("v1", "2026-04-02", 2, 11, sketch(["x", "y"], 11))
    summary = storage.get_monthly("2026-04")
    assert summary.p == 11
    assert summary.registers == sketch(["x", "y"], 11)


def test_correction_can_shrink_registers(storage):
    big = sketch([f"ticket-{i}" for i in range(2000)], 12)
    small = sketch(["just-one"], 12)
    storage.ingest("v1", "2026-05-09", 1, 12, big)
    assert sum(storage.get_monthly("2026-05").registers) == sum(big)
    storage.ingest("v1", "2026-05-09", 2, 12, small)
    summary = storage.get_monthly("2026-05")
    assert summary.registers == small
    assert sum(summary.registers) < sum(big)


def test_month_boundaries_exclude_neighbouring_days(storage):
    storage.ingest("v1", "2026-06-30", 1, 8, sketch(["a"], 8))
    storage.ingest("v1", "2026-07-01", 1, 8, sketch(["b"], 8))
    june = storage.get_monthly("2026-06")
    july = storage.get_monthly("2026-07")
    assert june.registers == sketch(["a"], 8)
    assert july.registers == sketch(["b"], 8)
    assert june.day_count == 1 and july.day_count == 1


def test_december_rolls_into_next_year(storage):
    storage.ingest("v1", "2026-12-31", 1, 8, sketch(["a"], 8))
    assert storage.get_monthly("2026-12") is not None
    assert storage.get_monthly("2027-01") is None


def test_summary_is_available_after_reopen(tmp_path):
    path = str(tmp_path / "museum.db")
    first = Storage(path)
    first.ingest("v1", "2026-08-08", 1, 8, sketch(["a", "b"], 8))
    expected = first.get_monthly("2026-08")

    reopened = Storage(path)
    summary = reopened.get_monthly("2026-08")
    assert summary.registers == expected.registers
    assert summary.p == expected.p
    assert hll.estimate(summary.registers, summary.p) == pytest.approx(
        hll.estimate(expected.registers, expected.p)
    )


def test_failed_write_leaves_no_partial_state(tmp_path, monkeypatch):
    path = str(tmp_path / "museum.db")
    storage = Storage(path)
    storage.ingest("v1", "2026-09-01", 1, 8, sketch(["a"], 8))
    before = storage.get_monthly("2026-09")

    def boom(self, conn, month):
        raise RuntimeError("simulated crash during summary rebuild")

    monkeypatch.setattr(Storage, "_recompute_month", boom)
    with pytest.raises(RuntimeError):
        storage.ingest("v1", "2026-09-02", 1, 8, sketch(["b"], 8))
    monkeypatch.undo()

    # The daily sketch must not have been committed either.
    assert storage.get_daily("v1", "2026-09-02") is None
    after = storage.get_monthly("2026-09")
    assert after.registers == before.registers
    assert after.updated_at == before.updated_at


def test_concurrent_backfills_never_lose_a_current_sketch(storage):
    """Two venues backfill different days at once; the month must include both.

    Writers take BEGIN IMMEDIATE before reading, so each rebuild sees every
    committed current sketch; the last committed summary is complete.
    """
    venues = [f"venue-{i}" for i in range(6)]
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        try:
            storage.ingest(
                venues[index],
                f"2026-10-{index + 1:02d}",
                1,
                8,
                sketch([f"{venues[index]}-ticket"], 8),
            )
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(len(venues))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    summary = storage.get_monthly("2026-10")
    assert summary.venue_count == len(venues)
    assert summary.day_count == len(venues)
    expected = hll.sketch_for_tickets([f"{venue}-ticket" for venue in venues], 8)
    assert summary.registers == expected


def test_concurrent_corrections_converge(storage):
    """Concurrent higher revisions race; whichever wins, the summary matches it."""
    storage.ingest("v1", "2026-11-03", 1, 8, sketch(["base"], 8))
    errors: list[BaseException] = []

    def worker(n: int) -> None:
        try:
            storage.ingest("v1", "2026-11-03", 2, 8, sketch([f"t{n}"], 8))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # All eight are revision 2 with different content: exactly one may win and
    # the other seven must be reported as conflicts, never silently merged.
    assert len(errors) == 7
    assert all(isinstance(exc, ConflictError) for exc in errors)
    current = storage.get_daily("v1", "2026-11-03")
    assert current.revision == 2
    assert storage.get_monthly("2026-11").registers == current.registers


def test_corrupt_register_blob_is_rejected_on_read(tmp_path):
    path = str(tmp_path / "museum.db")
    storage = Storage(path)
    storage.ingest("v1", "2026-12-01", 1, 4, sketch(["a"], 4))
    conn = sqlite3.connect(path)
    conn.execute(
        "UPDATE daily_sketch SET registers = ? WHERE venue_id = 'v1'", (b"\xff" * 16,)
    )
    conn.commit()
    conn.close()
    with pytest.raises(hll.SketchError):
        storage.get_daily("v1", "2026-12-01")


# -- legacy (version-1) database migration ---------------------------------


def _legacy_db(tmp_path):
    """Create a database in the original pre-fix (user_version=0) layout."""
    path = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(
        """
        CREATE TABLE daily_sketch (
            venue_id      TEXT    NOT NULL,
            day           TEXT    NOT NULL,
            revision      INTEGER NOT NULL,
            p             INTEGER NOT NULL,
            registers     BLOB    NOT NULL,
            content_hash  TEXT    NOT NULL,
            updated_at    TEXT    NOT NULL,
            PRIMARY KEY (venue_id, day)
        );
        CREATE TABLE monthly_summary (
            month        TEXT    PRIMARY KEY,
            p            INTEGER NOT NULL,
            registers    BLOB    NOT NULL,
            day_count    INTEGER NOT NULL,
            venue_count  INTEGER NOT NULL,
            updated_at   TEXT    NOT NULL
        );
        """
    )
    conn.execute("PRAGMA user_version = 0")
    return conn, path


def _legacy_insert(conn, venue_id, day, revision, p, registers):
    from app.store import content_fingerprint, utc_now

    conn.execute(
        "INSERT INTO daily_sketch "
        "(venue_id, day, revision, p, registers, content_hash, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            venue_id,
            day,
            revision,
            p,
            bytes(registers),
            content_fingerprint(p, registers),
            utc_now(),
        ),
    )


def test_migration_strips_lf_and_merges_matching_top_revision(tmp_path):
    conn, path = _legacy_db(tmp_path)
    regs_low = sketch(["t1", "t2"], 4)
    regs_same_p_same_regs = list(regs_low)
    # The LF-suffixed copy arrives at a higher revision but identical precision
    # and registers once folded: the venue-day must merge into one current row.
    _legacy_insert(conn, "museum-a", "2026-03-04", 1, 4, regs_low)
    _legacy_insert(conn, "museum-a\n", "2026-03-04", 2, 4, regs_same_p_same_regs)
    # Stale revisions are dropped during the highest-revision compaction.
    _legacy_insert(conn, "museum-a\n", "2026-03-05", 7, 4, sketch(["x"], 4))
    _legacy_insert(conn, "museum-a", "2026-03-05", 9, 4, sketch(["y"], 4))
    conn.commit()
    conn.close()

    storage = Storage(path)
    assert sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0] == 2

    # No LF venue ids survive anywhere.
    check = sqlite3.connect(path)
    dirty = check.execute(
        "SELECT COUNT(*) FROM daily_sketch WHERE venue_id LIKE '%\n'"
    ).fetchone()[0]
    dirty += check.execute(
        "SELECT COUNT(*) FROM daily_conflict WHERE venue_id LIKE '%\n'"
    ).fetchone()[0]
    assert dirty == 0
    check.close()

    day4 = storage.get_daily("museum-a", "2026-03-04")
    assert day4 is not None
    assert day4.revision == 2
    assert day4.registers == regs_low
    assert storage.get_conflict_day("museum-a", "2026-03-04") is None

    day5 = storage.get_daily("museum-a", "2026-03-05")
    assert day5.revision == 9
    assert day5.registers == sketch(["y"], 4)

    # The month reports normally and counts a single venue over two days.
    summary = storage.get_monthly("2026-03")
    assert summary is not None
    assert summary.venue_count == 1
    assert summary.day_count == 2
    expected = hll.merge_sketches([(4, regs_low), (4, sketch(["y"], 4))])
    assert summary.registers == expected


def test_migration_keeps_distinct_top_revisions_as_conflicts(tmp_path):
    conn, path = _legacy_db(tmp_path)
    variant_a = sketch(["ticket-a"], 4)
    variant_b = sketch(["ticket-b"], 5)
    _legacy_insert(conn, "museum-a", "2026-03-04", 3, 4, variant_a)
    _legacy_insert(conn, "museum-a\n", "2026-03-04", 3, 5, variant_b)
    # A stale lower revision under a third spelling (e.g. CRLF) is dropped,
    # not preserved, since only highest revisions are compacted.
    _legacy_insert(conn, "museum-a\r\n", "2026-03-04", 1, 4, sketch(["old"], 4))
    # A second venue-day in the same month stays clean.
    clean_regs = sketch(["clean"], 6)
    _legacy_insert(conn, "museum-b", "2026-03-09", 1, 6, clean_regs)
    # The old, polluted summary must not survive.
    conn.execute(
        "INSERT INTO monthly_summary "
        "(month, p, registers, day_count, venue_count, updated_at) "
        "VALUES ('2026-03', 6, ?, 1, 2, '2026-01-01T00:00:00+00:00')",
        (bytes(64),),
    )
    conn.commit()
    conn.close()

    storage = Storage(path)

    # The conflicted venue-day has no current row, two kept originals, and the
    # stale revision-1 variant is gone.
    assert storage.get_daily("museum-a", "2026-03-04") is None
    conflict = storage.get_conflict_day("museum-a", "2026-03-04")
    assert conflict is not None
    assert conflict.highest_revision == 3
    assert conflict.required_revision == 4
    assert {v.revision for v in conflict.variants} == {3}
    assert {v.p for v in conflict.variants} == {4, 5}
    kept_registers = {tuple(v.registers) for v in conflict.variants}
    assert kept_registers == {tuple(variant_a), tuple(variant_b)}

    # Month output is withdrawn while the conflict exists.
    assert storage.get_monthly("2026-03") is None
    assert [c.day for c in storage.get_conflicts("2026-03")] == ["2026-03-04"]
    # Other months are untouched.
    assert storage.get_conflicts("2026-02") == []


def test_migration_crlf_suffix_is_stripped(tmp_path):
    conn, path = _legacy_db(tmp_path)
    regs = sketch(["t1"], 4)
    _legacy_insert(conn, "museum-a\r\n", "2026-03-04", 1, 4, regs)
    conn.commit()
    conn.close()

    storage = Storage(path)
    stored = storage.get_daily("museum-a", "2026-03-04")
    assert stored is not None
    assert stored.revision == 1
    assert storage.get_monthly("2026-03") is not None


def test_migration_is_idempotent_across_reopens(tmp_path):
    conn, path = _legacy_db(tmp_path)
    _legacy_insert(conn, "museum-a", "2026-03-04", 1, 4, sketch(["a"], 4))
    _legacy_insert(conn, "museum-a\n", "2026-03-04", 2, 4, sketch(["a"], 4))
    conn.commit()
    conn.close()

    first = Storage(path)
    first_summary = first.get_monthly("2026-03")
    reopened = Storage(path)  # must not re-migrate or duplicate rows
    check = sqlite3.connect(path)
    assert check.execute("SELECT COUNT(*) FROM daily_sketch").fetchone()[0] == 1
    assert check.execute("SELECT COUNT(*) FROM daily_conflict").fetchone()[0] == 0
    assert check.execute("PRAGMA user_version").fetchone()[0] == 2
    check.close()
    assert reopened.get_monthly("2026-03").updated_at == first_summary.updated_at


def test_recovering_migrated_conflict_resumes_month_output(tmp_path):
    conn, path = _legacy_db(tmp_path)
    variant_a = sketch(["ticket-a"], 4)
    variant_b = sketch(["ticket-b"], 5)
    _legacy_insert(conn, "museum-a", "2026-03-04", 3, 4, variant_a)
    _legacy_insert(conn, "museum-a\n", "2026-03-04", 3, 5, variant_b)
    clean_regs = sketch(["clean"], 6)
    _legacy_insert(conn, "museum-b", "2026-03-09", 1, 6, clean_regs)
    conn.commit()
    conn.close()

    storage = Storage(path)
    assert storage.get_monthly("2026-03") is None

    # Same revision as the conflict cannot resolve it: an already-kept variant
    # is an idempotent no-op, new content at that revision is a conflict.
    unchanged = storage.ingest("museum-a", "2026-03-04", 3, 4, variant_a)
    assert unchanged.status == "unchanged"
    assert unchanged.month_blocked is True
    with pytest.raises(ConflictError):
        storage.ingest("museum-a", "2026-03-04", 3, 4, sketch(["brand-new"], 4))
    assert storage.get_monthly("2026-03") is None

    # A higher revision under the clean venue id clears the conflict.
    resolved = sketch(["ticket-a", "ticket-c"], 4)
    outcome = storage.ingest("museum-a", "2026-03-04", 4, 4, resolved)
    assert outcome.status == "conflict_resolved"
    assert outcome.month_blocked is False
    assert storage.get_conflict_day("museum-a", "2026-03-04") is None
    assert storage.get_conflicts() == []

    summary = storage.get_monthly("2026-03")
    assert summary is not None
    assert summary.venue_count == 2
    assert summary.day_count == 2
    # Recomputed from the merged current sketches, not any stale summary.
    expected = hll.merge_sketches([(4, resolved), (6, clean_regs)])
    assert summary.registers == expected
    assert summary.p == min(4, 6)

    # The recovered state survives a restart.
    reopened = Storage(path)
    after_restart = reopened.get_monthly("2026-03")
    assert after_restart.registers == summary.registers
    assert reopened.get_conflicts() == []


def test_two_conflicts_must_both_resolve_before_month_resumes(tmp_path):
    conn, path = _legacy_db(tmp_path)
    _legacy_insert(conn, "v1", "2026-03-01", 2, 4, sketch(["a1"], 4))
    _legacy_insert(conn, "v1\n", "2026-03-01", 2, 5, sketch(["a2"], 5))
    _legacy_insert(conn, "v2", "2026-03-02", 2, 4, sketch(["b1"], 4))
    _legacy_insert(conn, "v2\n", "2026-03-02", 2, 5, sketch(["b2"], 5))
    conn.commit()
    conn.close()

    storage = Storage(path)
    assert len(storage.get_conflicts("2026-03")) == 2
    assert storage.get_monthly("2026-03") is None

    storage.ingest("v1", "2026-03-01", 3, 4, sketch(["a-final"], 4))
    # One conflict left: the month must still produce nothing.
    assert storage.get_monthly("2026-03") is None
    assert len(storage.get_conflicts("2026-03")) == 1

    storage.ingest("v2", "2026-03-02", 3, 4, sketch(["b-final"], 4))
    summary = storage.get_monthly("2026-03")
    assert summary is not None
    assert summary.venue_count == 2
    expected = hll.merge_sketches(
        [(4, sketch(["a-final"], 4)), (4, sketch(["b-final"], 4))]
    )
    assert summary.registers == expected


def test_migration_without_lf_rows_is_a_noop(tmp_path):
    conn, path = _legacy_db(tmp_path)
    regs = sketch(["a", "b"], 4)
    _legacy_insert(conn, "museum-a", "2026-03-04", 1, 4, regs)
    conn.commit()
    conn.close()

    storage = Storage(path)
    stored = storage.get_daily("museum-a", "2026-03-04")
    assert stored.registers == regs
    assert stored.revision == 1
    assert storage.get_conflicts() == []
    assert storage.get_monthly("2026-03").registers == regs


def test_ingest_writes_conflict_state_and_summary_atomically(tmp_path, monkeypatch):
    conn, path = _legacy_db(tmp_path)
    _legacy_insert(conn, "v1", "2026-03-01", 2, 4, sketch(["a1"], 4))
    _legacy_insert(conn, "v1\n", "2026-03-01", 2, 5, sketch(["a2"], 5))
    conn.commit()
    conn.close()

    storage = Storage(path)
    assert storage.get_monthly("2026-03") is None

    def boom(self, conn, month):
        raise RuntimeError("simulated crash during summary rebuild")

    monkeypatch.setattr(Storage, "_refresh_month", boom)
    with pytest.raises(RuntimeError):
        storage.ingest("v1", "2026-03-01", 3, 4, sketch(["a3"], 4))
    monkeypatch.undo()

    # Rolled back as a unit: conflict remains, no current row, no summary.
    reopened = Storage(path)
    assert reopened.get_daily("v1", "2026-03-01") is None
    assert reopened.get_conflict_day("v1", "2026-03-01") is not None
    assert reopened.get_monthly("2026-03") is None
