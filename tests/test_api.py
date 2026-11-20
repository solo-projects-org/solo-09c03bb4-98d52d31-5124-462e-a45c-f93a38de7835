import threading

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "api.db"))
    with TestClient(app) as test_client:
        yield test_client


def payload(registers, *, venue="venue-a", day="2026-03-04", revision=1, p=4):
    return {
        "venue_id": venue,
        "day": day,
        "revision": revision,
        "p": p,
        "registers": registers,
    }


def sketch(tickets, p=4):
    from app import hll

    return hll.sketch_for_tickets(tickets, p)


def test_health(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_submit_and_read_monthly(client):
    body = payload(sketch(["t1", "t2"], 4))
    response = client.post("/api/v1/daily-sketches", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert response.json()["month"] == "2026-03"

    report = client.get("/api/v1/reports/monthly/2026-03")
    assert report.status_code == 200
    data = report.json()
    assert data["precision"] == 4
    assert data["day_count"] == 1
    assert data["venue_count"] == 1
    assert len(data["registers"]) == 16
    assert 1.5 <= data["estimate"] <= 2.5


def test_monthly_report_404_before_any_submission(client):
    assert client.get("/api/v1/reports/monthly/2025-01").status_code == 404


def test_negative_revision_rejected(client):
    response = client.post(
        "/api/v1/daily-sketches", json=payload([0] * 16, revision=-1)
    )
    assert response.status_code == 422


def test_trailing_lf_venue_id_is_rejected(client):
    # The original incident: copying a venue code from a text list keeps the
    # line ending, and the old regex ($ anchor) accepted it.
    body = payload(sketch(["a"], 4), venue="venue-a\n")
    response = client.post("/api/v1/daily-sketches", json=body)
    assert response.status_code == 422
    assert "CR/LF" in str(response.json()["detail"])
    # Nothing was stored, neither as a separate venue nor the clean one.
    assert client.get("/api/v1/daily-sketches/venue-a/2026-03-04").status_code == 404


def test_trailing_cr_and_crlf_venue_ids_are_rejected(client):
    for dirty in ("venue-a\r", "venue-a\r\n", " venue-a", "venue-a "):
        response = client.post(
            "/api/v1/daily-sketches", json=payload([0] * 16, venue=dirty)
        )
        assert response.status_code == 422, dirty


def test_clean_venue_after_rejected_lf_is_created_normally(client):
    dirty = payload(sketch(["a"], 4), venue="venue-a\n")
    assert client.post("/api/v1/daily-sketches", json=dirty).status_code == 422
    clean = payload(sketch(["a"], 4), venue="venue-a")
    response = client.post("/api/v1/daily-sketches", json=clean)
    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert response.json()["month_blocked"] is False


def test_wrong_register_count_rejected(client):
    response = client.post("/api/v1/daily-sketches", json=payload([0] * 17))
    assert response.status_code == 422


def test_out_of_range_register_rejected(client):
    bad = [0] * 15 + [62]
    response = client.post("/api/v1/daily-sketches", json=payload(bad))
    assert response.status_code == 422


def test_precision_out_of_range_rejected(client):
    response = client.post(
        "/api/v1/daily-sketches",
        json=payload([0] * 16, p=3),
    )
    assert response.status_code == 422


def test_unknown_field_rejected(client):
    body = payload([0] * 16)
    body["extra"] = True
    response = client.post("/api/v1/daily-sketches", json=body)
    assert response.status_code == 422


def test_bad_day_rejected(client):
    for day in ("2026-3-04", "2026-02-30", "04/03/2026", ""):
        response = client.post(
            "/api/v1/daily-sketches", json=payload([0] * 16, day=day)
        )
        assert response.status_code == 422, day


def test_conflict_returns_409_and_keeps_first(client):
    first = payload(sketch(["a"], 4))
    assert client.post("/api/v1/daily-sketches", json=first).status_code == 200
    conflicting = payload(sketch(["b"], 4))
    response = client.post("/api/v1/daily-sketches", json=conflicting)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "revision_conflict"

    stored = client.get("/api/v1/daily-sketches/venue-a/2026-03-04").json()
    assert stored["registers"] == sketch(["a"], 4)


def test_stale_revision_is_ignored_but_not_an_error(client):
    client.post(
        "/api/v1/daily-sketches", json=payload(sketch(["a", "b"], 4), revision=5)
    )
    response = client.post(
        "/api/v1/daily-sketches", json=payload(sketch(["a"], 4), revision=4)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "ignored"
    assert response.json()["current_revision"] == 5


def test_idempotent_resubmission(client):
    body = payload(sketch(["a"], 4), revision=2)
    assert client.post("/api/v1/daily-sketches", json=body).status_code == 200
    response = client.post("/api/v1/daily-sketches", json=body)
    assert response.status_code == 200
    assert response.json()["status"] == "unchanged"


def test_openapi_documents_endpoints(client):
    schema = client.get("/openapi.json").json()
    assert "/api/v1/daily-sketches" in schema["paths"]
    assert "/api/v1/reports/monthly/{month}" in schema["paths"]
    assert client.get("/docs").status_code == 200


def test_parallel_backfill_through_http(client):
    errors = []

    def worker(n: int) -> None:
        try:
            body = payload(
                sketch([f"t{n}"], 4),
                venue=f"venue-{n}",
                day=f"2026-04-{n + 1:02d}",
            )
            assert client.post("/api/v1/daily-sketches", json=body).status_code == 200
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    data = client.get("/api/v1/reports/monthly/2026-04").json()
    assert data["venue_count"] == 5
    assert data["day_count"] == 5


# -- conflict lifecycle (migration-produced or equivalent states) ----------


def _seed_conflict(storage, venue="venue-a", day="2026-03-04", rev=3):
    """Create a conflicted venue-day directly in storage, as the migration would."""
    import sqlite3

    from app.store import content_fingerprint, utc_now

    conn = sqlite3.connect(storage.path)
    try:
        for tag, p in (("a", 4), ("b", 5)):
            regs = sketch([f"{tag}-ticket"], p)
            conn.execute(
                "INSERT INTO daily_conflict "
                "(venue_id, day, revision, p, registers, content_hash, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    venue,
                    day,
                    rev,
                    p,
                    bytes(regs),
                    content_fingerprint(p, regs),
                    utc_now(),
                ),
            )
        conn.commit()
    finally:
        conn.close()


def test_blocked_month_returns_409_with_conflict_list(client, tmp_path):
    from app.store import Storage

    storage = Storage(str(tmp_path / "api.db"))
    # One clean venue-day keeps the month otherwise populated.
    storage.ingest("venue-ok", "2026-03-10", 1, 4, sketch(["ok-ticket"], 4))
    _seed_conflict(storage)

    response = client.get("/api/v1/reports/monthly/2026-03")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "month_blocked_by_conflicts"
    assert detail["conflict_count"] == 1
    conflict = detail["conflicts"][0]
    assert conflict["venue_id"] == "venue-a"
    assert conflict["day"] == "2026-03-04"
    assert conflict["highest_revision"] == 3
    assert conflict["required_revision"] == 4
    assert {variant["p"] for variant in conflict["variants"]} == {4, 5}


def test_conflicted_daily_read_returns_409_with_variants(client, tmp_path):
    from app.store import Storage

    storage = Storage(str(tmp_path / "api.db"))
    _seed_conflict(storage)
    response = client.get("/api/v1/daily-sketches/venue-a/2026-03-04")
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["error"] == "venue_day_conflict"
    assert detail["required_revision"] == 4
    assert len(detail["conflict"]["variants"]) == 2


def test_conflicts_endpoint_lists_all_and_per_month(client, tmp_path):
    from app.store import Storage

    storage = Storage(str(tmp_path / "api.db"))
    _seed_conflict(storage, venue="venue-a", day="2026-03-04")
    _seed_conflict(storage, venue="venue-b", day="2026-03-20", rev=2)
    _seed_conflict(storage, venue="venue-c", day="2026-04-02", rev=1)

    all_conflicts = client.get("/api/v1/conflicts")
    assert all_conflicts.status_code == 200
    body = all_conflicts.json()
    assert body["month"] is None
    assert body["conflict_count"] == 3

    march = client.get("/api/v1/conflicts?month=2026-03").json()
    assert march["conflict_count"] == 2
    assert {c["venue_id"] for c in march["conflicts"]} == {"venue-a", "venue-b"}

    empty = client.get("/api/v1/conflicts?month=2026-05").json()
    assert empty["conflict_count"] == 0
    assert empty["blocked"] is False

    assert client.get("/api/v1/conflicts?month=bad").status_code == 422


def test_stale_and_equal_revisions_against_conflict_do_not_change_state(
    client, tmp_path
):
    from app.store import Storage

    storage = Storage(str(tmp_path / "api.db"))
    storage.ingest("venue-ok", "2026-03-10", 1, 4, sketch(["ok"], 4))
    _seed_conflict(storage, rev=3)

    stale = client.post(
        "/api/v1/daily-sketches",
        json=payload(sketch(["new"], 4), revision=2),
    )
    assert stale.status_code == 200
    assert stale.json()["status"] == "ignored"
    assert stale.json()["current_revision"] == 3
    assert stale.json()["month_blocked"] is True

    same_rev_new_content = client.post(
        "/api/v1/daily-sketches",
        json=payload(sketch(["new"], 4), revision=3),
    )
    assert same_rev_new_content.status_code == 409
    assert same_rev_new_content.json()["detail"]["error"] == "revision_conflict"

    # The month is still blocked and the conflict still has two variants.
    assert client.get("/api/v1/reports/monthly/2026-03").status_code == 409
    listed = client.get("/api/v1/conflicts?month=2026-03").json()
    assert len(listed["conflicts"][0]["variants"]) == 2


def test_higher_revision_resolves_conflict_and_report_resumes(client, tmp_path):
    from app.store import Storage

    storage = Storage(str(tmp_path / "api.db"))
    storage.ingest("venue-ok", "2026-03-10", 1, 4, sketch(["ok-ticket"], 4))
    _seed_conflict(storage, rev=3)
    assert client.get("/api/v1/reports/monthly/2026-03").status_code == 409

    resolved_regs = sketch(["resolved-ticket", "ok-ticket"], 4)
    response = client.post(
        "/api/v1/daily-sketches",
        json=payload(resolved_regs, revision=4),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "conflict_resolved"
    assert body["month_blocked"] is False

    # Daily read is a normal sketch again and conflicts are gone.
    daily = client.get("/api/v1/daily-sketches/venue-a/2026-03-04")
    assert daily.status_code == 200
    assert daily.json()["revision"] == 4
    assert client.get("/api/v1/conflicts").json()["conflict_count"] == 0

    # Monthly output resumed, computed from the merged current sketches.
    report = client.get("/api/v1/reports/monthly/2026-03")
    assert report.status_code == 200
    from app import hll

    expected = hll.sketch_for_tickets(["resolved-ticket", "ok-ticket"], 4)
    assert report.json()["registers"] == expected
    assert report.json()["venue_count"] == 2
    assert report.json()["day_count"] == 2


def test_other_venue_backfills_stay_accepted_while_month_blocked(client, tmp_path):
    from app.store import Storage

    storage = Storage(str(tmp_path / "api.db"))
    _seed_conflict(storage, venue="venue-a", day="2026-03-04")

    # A different venue-day follows the ordinary ingest rules while blocked.
    response = client.post(
        "/api/v1/daily-sketches",
        json=payload(sketch(["b-ticket"], 4), venue="venue-b", day="2026-03-05"),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "created"
    assert response.json()["month_blocked"] is True

    correction = client.post(
        "/api/v1/daily-sketches",
        json=payload(
            sketch(["b-ticket", "b2"], 4),
            venue="venue-b",
            day="2026-03-05",
            revision=2,
        ),
    )
    assert correction.json()["status"] == "replaced"
    # Still blocked by venue-a; venue-b's current sketch is stored regardless.
    stored = client.get("/api/v1/daily-sketches/venue-b/2026-03-05").json()
    assert stored["revision"] == 2
    assert client.get("/api/v1/reports/monthly/2026-03").status_code == 409


def test_summary_returns_404_not_stale_after_migration_blocks_month(client, tmp_path):
    from app.store import Storage

    storage = Storage(str(tmp_path / "api.db"))
    storage.ingest("venue-a", "2026-03-04", 1, 4, sketch(["a"], 4))
    assert client.get("/api/v1/reports/monthly/2026-03").status_code == 200
    _seed_conflict(storage, venue="venue-z", day="2026-03-15", rev=1)

    # No stale pre-conflict summary may be served: the report is 409 and the
    # conflict list exposes exactly the seeded venue-day.  (A bare sqlite
    # insert does not run the service's summary refresh; that withdrawal is
    # covered at store level in the migration and ingest tests.)
    blocked = client.get("/api/v1/reports/monthly/2026-03")
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["conflicts"][0]["venue_id"] == "venue-z"
    assert client.get("/api/v1/conflicts").json()["conflict_count"] == 1
