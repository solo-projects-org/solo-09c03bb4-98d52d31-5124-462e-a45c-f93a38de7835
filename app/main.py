"""FastAPI application: venue daily-sketch ingestion and monthly reports."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import date
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import hll
from .store import (
    ConflictDay,
    ConflictError,
    ConflictVariant,
    MonthlySummary,
    Storage,
)

# \Z, not $: Python's $ also matches before a trailing newline, which let a
# venue id copied from a text list with an LF ("museum-a\n") pass as a second
# venue.  \Z only matches the very end of the string.
DAY_PATTERN = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
MONTH_PATTERN = re.compile(r"\A\d{4}-\d{2}\Z")
VENUE_PATTERN = re.compile(r"\A[A-Za-z0-9._-]{1,64}\Z")
MAX_REVISION = 2**31 - 1


def get_storage(request: Request) -> Storage:
    return request.app.state.storage


StorageDep = Annotated[Storage, Depends(get_storage)]


class SketchSubmission(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "venue_id": "museum-a",
                "day": "2026-05-03",
                "revision": 1,
                "p": 4,
                "registers": [0, 3, 0, 5, 1, 0, 0, 2, 0, 4, 0, 0, 1, 0, 0, 0],
            }
        },
    )

    venue_id: Annotated[str, Field(min_length=1, max_length=64)]
    day: Annotated[str, Field(description="UTC date, YYYY-MM-DD")]
    revision: Annotated[int, Field(strict=True, ge=0, le=MAX_REVISION)]
    p: Annotated[int, Field(strict=True, ge=4, le=12)]
    registers: Annotated[
        list[Annotated[int, Field(strict=True)]],
        Field(description="exactly 2**p registers"),
    ]

    @field_validator("venue_id")
    @classmethod
    def _venue(cls, value: str) -> str:
        if not VENUE_PATTERN.match(value):
            if value.endswith(("\n", "\r")):
                raise ValueError(
                    "venue_id must not carry trailing CR/LF; resubmit with the "
                    "clean venue code (allowed: [A-Za-z0-9._-]{1,64})"
                )
            raise ValueError("venue_id must match [A-Za-z0-9._-]{1,64}")
        return value

    @field_validator("day")
    @classmethod
    def _day(cls, value: str) -> str:
        if not DAY_PATTERN.match(value):
            raise ValueError("day must be a UTC date formatted YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("day is not a valid calendar date") from exc
        return value


class SketchResponse(BaseModel):
    venue_id: str
    day: str
    revision: int
    p: int
    registers: list[int]
    updated_at: str


class IngestResponse(BaseModel):
    status: str
    venue_id: str
    day: str
    revision: int
    current_revision: int
    month: str
    month_blocked: bool = False


class MonthlyReport(BaseModel):
    month: str
    estimate: float
    precision: int
    registers: list[int]
    day_count: int
    venue_count: int
    updated_at: str


class ConflictVariantResponse(BaseModel):
    revision: int
    p: int
    registers: list[int]
    content_hash: str
    updated_at: str


class ConflictDayResponse(BaseModel):
    venue_id: str
    day: str
    month: str
    highest_revision: int
    required_revision: int
    variants: list[ConflictVariantResponse]


class ConflictListResponse(BaseModel):
    month: str | None
    blocked: bool
    conflict_count: int
    conflicts: list[ConflictDayResponse]


def _conflict_day(conflict: ConflictDay) -> ConflictDayResponse:
    return ConflictDayResponse(
        venue_id=conflict.venue_id,
        day=conflict.day,
        month=conflict.month,
        highest_revision=conflict.highest_revision,
        required_revision=conflict.required_revision,
        variants=[_conflict_variant(v) for v in conflict.variants],
    )


def _conflict_variant(variant: ConflictVariant) -> ConflictVariantResponse:
    return ConflictVariantResponse(
        revision=variant.revision,
        p=variant.p,
        registers=variant.registers,
        content_hash=variant.content_hash,
        updated_at=variant.updated_at,
    )


def _conflict_payload(month: str, conflicts: list[ConflictDay]) -> dict:
    return {
        "error": "month_blocked_by_conflicts",
        "message": (
            f"month {month} has {len(conflicts)} conflicted venue-day(s); "
            "reports resume after each is resolved with a higher revision "
            "under the clean venue id"
        ),
        "month": month,
        "conflict_count": len(conflicts),
        "conflicts": [_conflict_day(conflict).model_dump() for conflict in conflicts],
    }


class HealthResponse(BaseModel):
    status: str
    database: str


class HashSpecResponse(BaseModel):
    algorithm: str
    digest_bits: int
    bits_used: str
    bucket_rule: str
    rho_rule: str
    empty_register: int
    sample_ticket: str
    sample_hash: int
    sample_bucket_p8: int
    sample_rho_p8: int


HASH_SPEC = {
    "algorithm": "SHA-256, first 8 bytes, big-endian unsigned integer",
    "digest_bits": 64,
    "bits_used": "all 64 bits",
    "bucket_rule": "high p bits of the 64-bit hash",
    "rho_rule": (
        "1 + leading zero count of the remaining 64-p bits; "
        "64-p+1 when those bits are all zero"
    ),
    "empty_register": 0,
    "sample_ticket": "TICKET-00001",
}


def _report(summary: MonthlySummary) -> MonthlyReport:
    return MonthlyReport(
        month=summary.month,
        estimate=hll.estimate(summary.registers, summary.p),
        precision=summary.p,
        registers=summary.registers,
        day_count=summary.day_count,
        venue_count=summary.venue_count,
        updated_at=summary.updated_at,
    )


def create_app(db_path: str | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.storage = Storage(db_path) if db_path is not None else Storage()
        yield

    app = FastAPI(
        title="Museum Combined-Ticket Dedup Service",
        version="1.1.0",
        description=(
            "Venues submit daily HyperLogLog sketches of combined-ticket ids. "
            "Identical tickets scanned at several venues are counted once. "
            "Monthly reports are served from a persisted summary that is "
            "rebuilt transactionally from the current daily sketches. A month "
            "with unresolved venue-day conflicts (from the legacy LF-suffixed "
            "venue-id migration or same-revision divergence) produces no "
            "report until every conflict is resolved by a higher revision."
        ),
        lifespan=lifespan,
    )

    @app.get("/healthz", response_model=HealthResponse, tags=["ops"])
    def healthz(storage: StorageDep) -> HealthResponse:
        return HealthResponse(status="ok", database=storage.path)

    @app.get(
        "/api/v1/hash-spec",
        response_model=HashSpecResponse,
        tags=["ops"],
        summary="The single 64-bit hash and bucket/rho rules venues must mirror",
    )
    def hash_spec() -> HashSpecResponse:
        hashed = hll.hash_ticket(HASH_SPEC["sample_ticket"])
        split = hll.bucket_and_rho(hashed, 8)
        return HashSpecResponse(
            **HASH_SPEC,
            sample_hash=hashed,
            sample_bucket_p8=split.bucket,
            sample_rho_p8=split.rho,
        )

    @app.post(
        "/api/v1/daily-sketches",
        response_model=IngestResponse,
        status_code=200,
        tags=["sketches"],
        summary="Submit (or correct) one venue's daily sketch",
        responses={
            409: {"description": "Same revision already stored with other content"},
            422: {"description": "Malformed sketch"},
        },
    )
    def submit(submission: SketchSubmission, storage: StorageDep) -> IngestResponse:
        try:
            clean = hll.validate_registers(submission.p, submission.registers)
        except hll.SketchError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_sketch", "message": str(exc)},
            ) from exc
        try:
            outcome = storage.ingest(
                venue_id=submission.venue_id,
                day=submission.day,
                revision=submission.revision,
                p=submission.p,
                registers=clean,
            )
        except ConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "revision_conflict",
                    "message": str(exc),
                    "venue_id": exc.venue_id,
                    "day": exc.day,
                    "revision": exc.revision,
                    "current_revision": exc.current_revision,
                },
            ) from exc
        return IngestResponse(
            status=outcome.status,
            venue_id=outcome.venue_id,
            day=outcome.day,
            revision=outcome.revision,
            current_revision=outcome.current_revision,
            month=outcome.month,
            month_blocked=outcome.month_blocked,
        )

    @app.get(
        "/api/v1/conflicts",
        response_model=ConflictListResponse,
        tags=["reports"],
        summary="List conflicted venue-days (optionally for one month)",
    )
    def list_conflicts(
        storage: StorageDep, month: str | None = None
    ) -> ConflictListResponse:
        if month is not None:
            _validate_month(month)
        conflicts = storage.get_conflicts(month)
        return ConflictListResponse(
            month=month,
            blocked=bool(conflicts),
            conflict_count=len(conflicts),
            conflicts=[_conflict_day(conflict) for conflict in conflicts],
        )

    @app.get(
        "/api/v1/daily-sketches/{venue_id}/{day}",
        response_model=SketchResponse,
        tags=["sketches"],
        summary="Read the current sketch for one venue/day",
        responses={
            409: {"description": "Venue-day has unresolved content conflicts"},
        },
    )
    def read_daily(venue_id: str, day: str, storage: StorageDep) -> SketchResponse:
        _validate_path_day(day)
        conflict = storage.get_conflict_day(venue_id, day)
        if conflict is not None:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "venue_day_conflict",
                    "message": (
                        f"{venue_id}/{day} has {len(conflict.variants)} conflicting "
                        f"variants; submit revision {conflict.required_revision} or "
                        "higher with the clean venue id to resolve"
                    ),
                    "venue_id": venue_id,
                    "day": day,
                    "highest_revision": conflict.highest_revision,
                    "required_revision": conflict.required_revision,
                    "conflict": _conflict_day(conflict).model_dump(),
                },
            )
        stored = storage.get_daily(venue_id, day)
        if stored is None:
            raise HTTPException(status_code=404, detail="no sketch for that venue/day")
        return SketchResponse(
            venue_id=stored.venue_id,
            day=stored.day,
            revision=stored.revision,
            p=stored.p,
            registers=stored.registers,
            updated_at=stored.updated_at,
        )

    @app.get(
        "/api/v1/reports/monthly/{month}",
        response_model=MonthlyReport,
        tags=["reports"],
        summary="Monthly deduplicated ticket estimate from the stored summary",
        responses={
            409: {"description": "Month has unresolved venue-day conflicts"},
        },
    )
    def monthly(month: str, storage: StorageDep) -> MonthlyReport:
        _validate_month(month)
        conflicts = storage.get_conflicts(month)
        if conflicts:
            raise HTTPException(
                status_code=409,
                detail=_conflict_payload(month, conflicts),
            )
        summary = storage.get_monthly(month)
        if summary is None:
            raise HTTPException(status_code=404, detail=f"no summary for {month}")
        return _report(summary)

    return app


def _validate_path_day(day: str) -> None:
    if not DAY_PATTERN.match(day):
        raise HTTPException(status_code=422, detail="day must be YYYY-MM-DD")
    try:
        date.fromisoformat(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="day is not a valid date") from exc


def _validate_month(month: str) -> None:
    if not MONTH_PATTERN.match(month):
        raise HTTPException(status_code=422, detail="month must be YYYY-MM")
    _, mon = month.split("-")
    if not 1 <= int(mon) <= 12:
        raise HTTPException(status_code=422, detail="month is not a valid month")


app = create_app()
