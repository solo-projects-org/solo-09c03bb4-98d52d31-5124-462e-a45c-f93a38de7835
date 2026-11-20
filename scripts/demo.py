#!/usr/bin/env python3
"""Try the running service: submit two venues' sketches and read the month.

Usage (after ``docker compose up``)::

    python scripts/demo.py [http://127.0.0.1:8000]

The script only needs the standard library (urllib), so it can run from the
host without installing anything.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
PRECISION = 8
MONTH = "2026-05"


def hash_ticket(ticket: str) -> int:
    return int.from_bytes(hashlib.sha256(ticket.encode()).digest()[:8], "big")


def rho(remainder: int, bits: int) -> int:
    return bits + 1 if remainder == 0 else bits - remainder.bit_length() + 1


def sketch(tickets: list[str], p: int) -> list[int]:
    registers = [0] * (1 << p)
    bits = 64 - p
    for ticket in tickets:
        hashed = hash_ticket(ticket)
        bucket = hashed >> bits
        value = rho(hashed & ((1 << bits) - 1), bits)
        if value > registers[bucket]:
            registers[bucket] = value
    return registers


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def main() -> int:
    # 200 tickets are shared by both venues, plus 100 unique ones each.
    shared = [f"TICKET-{i:05d}" for i in range(200)]
    venue_a = shared + [f"A-{i:05d}" for i in range(100)]
    venue_b = shared + [f"B-{i:05d}" for i in range(100)]

    for venue, tickets, day in (
        ("venue-a", venue_a, f"{MONTH}-03"),
        ("venue-b", venue_b, f"{MONTH}-04"),
    ):
        status, body = call(
            "POST",
            "/api/v1/daily-sketches",
            {
                "venue_id": venue,
                "day": day,
                "revision": 1,
                "p": PRECISION,
                "registers": sketch(tickets, PRECISION),
            },
        )
        print(f"POST {venue} {day}: {status} {body.get('status')}")

    status, report = call("GET", f"/api/v1/reports/monthly/{MONTH}")
    print(f"GET /api/v1/reports/monthly/{MONTH}: {status}")
    if status == 200:
        print(
            "  estimate={:.1f} precision={} days={} venues={}".format(
                report["estimate"],
                report["precision"],
                report["day_count"],
                report["venue_count"],
            )
        )
        print(f"  expected unique tickets=400, registers={len(report['registers'])}")
    else:
        print(f"  {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
