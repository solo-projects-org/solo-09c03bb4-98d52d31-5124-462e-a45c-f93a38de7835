# Museum Combined-Ticket Deduplication Service

A FastAPI + SQLite service that receives one HyperLogLog (HLL) daily sketch per
venue and answers "how many distinct combined tickets were used this month?".
A ticket scanned at several venues is counted once, because every venue uses the
same 64-bit hash and the monthly table is the merge of the current daily tables.

## Upgrading from the version-1 database

Venue codes pasted from a text list could carry a trailing line feed.  The old
`$`-anchored validation accepted `"museum-a\n"` as a different venue, so one
physical venue could own two rows for a day and the monthly summary merged a
daily sketch that should have been replaced.  Version 1.1 fixes this:

* Submissions with a trailing CR/LF (or any character outside
  `[A-Za-z0-9._-]`) in `venue_id` are rejected with HTTP 422; nothing is stored.
* Opening an old database runs a one-time, fully transactional migration
  (`PRAGMA user_version` 0 → 2):
  1. trailing CR/LF is stripped from every stored venue id;
  2. each venue-day keeps only its highest revision;
  3. highest revisions with identical precision and registers merge into one
     current row;
  4. highest revisions that differ are preserved verbatim in `daily_conflict`;
  5. affected monthly summaries are rebuilt, or withdrawn while conflicts remain.
* A crash anywhere in the migration rolls the whole database back; the next
  startup retries it.  Reopening an already-migrated database is a no-op.

### Conflicted venue-days

While a conflict exists for any venue-day in a month, that month produces no
report: `GET /api/v1/reports/monthly/{month}` answers HTTP 409 and lists every
conflicted venue-day with its preserved variants and the minimum revision that
will resolve it.  The same list is available at `GET /api/v1/conflicts`
(optionally `?month=YYYY-MM`).  Reading a conflicted venue-day directly also
returns 409 with the variants.

The venue resolves its day by submitting a **strictly higher** revision under
the clean venue id (`status: conflict_resolved`).  Stale revisions are ignored,
an already-kept same-revision variant is an idempotent `unchanged`, and new
same-revision content is still a 409.  Other venues' backfills keep following
the ordinary rules.  Monthly output resumes only after the *last* conflict in
the month clears, and is then recomputed from the merged current sketches —
never from a pre-conflict summary.

## Data contract

| Field | Meaning |
| --- | --- |
| `venue_id` | Venue code, `[A-Za-z0-9._-]{1,64}` |
| `day` | UTC date, `YYYY-MM-DD` |
| `revision` | Monotonic integer per `(venue_id, day)`; higher replaces the whole record |
| `p` | HLL precision, `4 <= p <= 12` |
| `registers` | Exactly `2**p` integers in `[0, 64-p+1]`; `0` = empty bucket |

The shared hash is documented live at `GET /api/v1/hash-spec`:

* hash = first 8 bytes of SHA-256 over the UTF-8 ticket id, big-endian (64 bits);
* bucket = the high `p` bits;
* `rho` = 1 + leading zero count of the remaining `64-p` bits, or `64-p+1` when
  those bits are all zero;
* a register keeps the maximum `rho` seen for its bucket.

Merging several sketches always targets the **lowest** precision in the set.
The fold is exact: the resulting table is byte-identical to hashing the union
of the ticket ids and rebuilding at that precision
(`app/hll.py:merge_sketches`, property-tested against rebuild in
`tests/test_hll.py`).

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/api/v1/daily-sketches` | Create / replace / ignore a daily sketch |
| `GET` | `/api/v1/daily-sketches/{venue_id}/{day}` | Read current sketch (409 + variants if conflicted) |
| `GET` | `/api/v1/conflicts` | List conflicted venue-days, optionally `?month=YYYY-MM` |
| `GET` | `/api/v1/reports/monthly/{month}` | Summary: estimate, precision, registers (409 while conflicts remain) |
| `GET` | `/api/v1/hash-spec` | Shared hash and rho rules, with a sample |
| `GET` | `/healthz` | Liveness + database path |

`POST` responses report `status`:

* `created` — first submission for that venue/day;
* `replaced` — higher revision replaced the whole record;
* `conflict_resolved` — higher revision cleared a migrated/divergent conflict;
* `ignored` — lower revision; the current record is untouched (HTTP 200);
* `unchanged` — same revision and same content (idempotent retry, HTTP 200);
* HTTP `409 revision_conflict` — same revision, different content;
* HTTP `422` — malformed sketch, including a `venue_id` with trailing CR/LF.

Every response also carries `month_blocked` (true while the month has an
unresolved conflict) and the month the submission belongs to.

Same-content comparison covers both `p` and the registers, so re-sending the
same registers at a different precision is a conflict, not an update.

## Storage and consistency

SQLite lives at `MUSEUM_DB_PATH` (default `/data/museum.db`), WAL mode,
`synchronous=FULL`, `busy_timeout=30s`.  The on-disk schema is versioned with
`PRAGMA user_version` (current: 2); an older file is migrated inside one
transaction on startup.

* Every write runs in `BEGIN IMMEDIATE`: the daily row, any conflict-table
  change, and the recomputed/withdrawn monthly summary commit together or not at
  all. A crash, exception, or `kill -9` before commit leaves the previous state
  readable. The same guarantee covers the startup migration.
* A venue-day is either one current `daily_sketch` row or one or more
  `daily_conflict` variants, never both. A month with any conflicted venue-day
  has no `monthly_summary` row until every conflict is resolved.
* Each write re-reads **all current** daily sketches of the month after taking
  the write lock and recomputes the month from scratch. Concurrent backfills
  therefore serialise and the last commit always includes every other current
  sketch. This also means a correction that raises the minimum precision or
  lowers register values is reflected — the summary is never an append-only
  max that could never shrink.
* The monthly report reads only the `monthly_summary` row; it never merges on
  demand, and refuses (409) instead of serving a stale summary while blocked.

## Run with Docker Compose

```bash
docker compose up --build -d
docker compose ps                 # wait for (healthy)
open http://localhost:8000/docs   # Swagger UI
```

Data persists in the named volume `museum-data`. The image runs as UID/GID
`10001` and ships `/data` owned by that UID, so Docker copies that ownership
into a fresh named volume and the server can write there. If you switch to a
bind mount, make it writable for UID `10001`. To reset:

```bash
docker compose down -v
```

Smoke-test from Swagger:

1. `POST /api/v1/daily-sketches` with the prefilled example (`p=4`). Execute.
2. `GET /api/v1/reports/monthly/2026-05` — expect `precision: 4`,
   `registers` with 16 entries.
3. Submit a second venue for the same month and query again; `venue_count`
   becomes 2 while a ticket present in both sketches is still counted once.

Or run the scripts:

```bash
python3 scripts/demo.py http://localhost:8000
python3 scripts/verify.py http://localhost:8000   # needs an empty month range
```

## Local development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
MUSEUM_DB_PATH=/tmp/museum.db uvicorn app.main:app --reload
python -m pytest -q
```

## Verification

`python -m pytest -q` → 58 tests pass. Beyond the original 40 (HLL properties,
transactional storage, HTTP behaviour), the suite now covers:

* 422 rejection of venue ids with trailing LF / CR / CRLF / surrounding spaces;
* legacy-database migration: LF stripping, highest-revision compaction,
  equal-payload merge, distinct-payload conflicts, CRLF handling, idempotent
  reopen, and untouched clean months;
* blocked-month 409 payloads on the daily read, monthly report, and
  `/api/v1/conflicts`; stale/equal/higher revision behaviour against a
  conflict; multi-conflict months; recovery by higher revision and restart
  persistence;
* atomic rollback when a write (or the migration) fails mid-transaction.

A live-server evidence run (`scripts/verify.py` against an empty month range)
still produces:

| Check | Result |
| --- | --- |
| Month at lowest precision equals fresh rebuild of the ticket union | exact match (p=7, ~471 vs 500 truth) |
| Correction raises month precision 5 → 12 | pass |
| Correction shrinks registers | pass |
| 48 concurrent venue backfills across 12 days | all HTTP 200 |
| Concurrent summary has all 48 venues / 12 days, equal to rebuild | pass |

An additional end-to-end run against a hand-built legacy database confirmed the
full incident lifecycle over real HTTP: 422 on LF ids, one mergeable venue-day
collapsed to a single row, one divergent venue-day preserved with both
originals, the month blocked (409) with its old polluted summary withdrawn,
normal backfills from other venues still accepted, resolution via revision 4,
and the report resuming from a fresh merge — surviving a process restart. A
simulated crash inside the migration left the version-1 database byte-intact and
retryable.

`ruff check` and `ruff format --check` are clean for the changed modules.

## Known limitations

* Only one process may own the SQLite file; run a single replica (the default
  Compose setup). Horizontal scaling would need a shared database.
* Image build, `docker compose up`, and the Swagger UI inside a container were
  **not executed in this environment** — it has no Docker daemon and cannot run
  containers. What was verified instead: the exact `requirements.txt` installs
  into a clean Python 3.11 environment; the app boots under `uvicorn` from that
  environment and serves `/docs` (HTTP 200) and every endpoint over real HTTP;
  `docker-compose.yml` parses as valid Compose YAML with its service, named
  volume, and healthcheck intact; and the healthcheck command returns 0 against
  a live server. The unchanged Compose file mounts the named volume at
  `/data`, so an upgraded image reopens and migrates the existing database on
  startup.
* Each ingest recomputes the whole month inside the write transaction. At 8
  venues × 31 days the rebuild is ~0.04 s; the cost grows linearly with the
  number of stored venue-days.
* Registers are stored as one blob of `2**p` bytes; a `p=12` row is 4 KiB.
* The estimator is the classic HLL estimator: ~1.04/√m relative error
  (measured std. dev. ≈ 1.0% at p=12, 6.5% at p=8 for 400 tickets).
* No authentication — put it behind a trusted network or a reverse proxy.
* `GET /api/v1/daily-sketches/{venue_id}/{day}` is a convenience read; venues
  are assumed to retain their own raw ticket ids.

## Layout

```
app/hll.py       HLL hashing, validation, exact merge, estimator
app/store.py     SQLite schema v2, one-time v1 migration, conflict table,
                 transactional ingest, monthly recompute / withdrawal
app/main.py      FastAPI models/routes (LF rejection, 409 conflict payloads)
tests/           unit, migration, storage-consistency, and HTTP tests
scripts/         stdlib-only demo and acceptance-check clients
Dockerfile / docker-compose.yml
```
