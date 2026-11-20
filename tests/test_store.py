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
