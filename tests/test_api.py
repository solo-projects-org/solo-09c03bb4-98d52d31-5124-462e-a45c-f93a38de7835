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
