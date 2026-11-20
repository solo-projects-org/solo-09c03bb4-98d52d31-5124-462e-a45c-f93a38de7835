"""HyperLogLog primitives shared by the ingest and reporting paths.

Wire format
-----------
A sketch is a precision ``p`` (4 <= p <= 12) plus exactly ``2**p`` registers.
Every venue hashes ticket identifiers with the same 64-bit function
(``hash_ticket``): the first 8 bytes of SHA-256, interpreted big-endian.

For a 64-bit hash ``h`` at precision ``p``:

* the high ``p`` bits select the bucket;
* ``rho`` is one plus the number of leading zero bits in the remaining
  ``64 - p`` bits, and is ``64 - p + 1`` when those bits are all zero;
* a register stores the maximum ``rho`` seen for its bucket (0 = empty).

Merging
-------
``merge_sketches`` folds every sketch into the lowest precision present.
The fold is exact: its result is bit-for-bit identical to hashing the union of
the underlying ticket identifiers and building a fresh table at that lowest
precision.  It therefore also supports corrections that lower registers or
raise the precision.

A source bucket at precision ``p_s`` maps to exactly one target bucket
``index >> shift`` at ``p_t = p_s - shift``.  Let ``offset`` be the low
``shift`` bits of the source bucket and ``v`` its register value.  Since the
target remainder is the source remainder padded on the left by ``offset``:

* ``offset == 0``: the target register becomes ``v + shift``;
* ``offset != 0``: the leading-zero run stops inside ``offset``, independently
  of ``v``, so the target register is ``shift - bit_length(offset) + 1``.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable, Sequence

MASK64 = (1 << 64) - 1
TWO_64 = 1 << 64
MIN_PRECISION = 4
MAX_PRECISION = 12


class SketchError(ValueError):
    """Raised when a sketch is invalid or cannot be interpreted."""


def validate_precision(p: int) -> int:
    if not isinstance(p, int) or isinstance(p, bool):
        raise SketchError("precision must be an integer")
    if not MIN_PRECISION <= p <= MAX_PRECISION:
        raise SketchError(
            f"precision must be between {MIN_PRECISION} and {MAX_PRECISION}"
        )
    return p


def max_rho(p: int) -> int:
    return (64 - p) + 1


def validate_registers(p: int, registers: Sequence[int]) -> list[int]:
    """Validate a register table and return it as a plain list."""
    validate_precision(p)
    expected = 1 << p
    if len(registers) != expected:
        raise SketchError(
            f"precision {p} requires exactly {expected} registers, got {len(registers)}"
        )
    limit = max_rho(p)
    out: list[int] = []
    for index, value in enumerate(registers):
        if not isinstance(value, int) or isinstance(value, bool):
            raise SketchError(f"register {index} is not an integer")
        if value < 0 or value > limit:
            raise SketchError(
                f"register {index} must be between 0 and {limit} for precision {p}"
            )
        out.append(value)
    return out


def hash_ticket(ticket: str) -> int:
    """Hash a ticket id with the single 64-bit hash every venue shares."""
    if not isinstance(ticket, str):
        raise TypeError("ticket must be a string")
    digest = hashlib.sha256(ticket.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass(frozen=True)
class BucketRho:
    bucket: int
    rho: int


def bucket_and_rho(hashed: int, p: int) -> BucketRho:
    """Split a 64-bit hash into its bucket index and rho value."""
    validate_precision(p)
    if not isinstance(hashed, int) or isinstance(hashed, bool):
        raise SketchError("hash must be an integer")
    if hashed < 0 or hashed > MASK64:
        raise SketchError("hash must fit in 64 bits")
    remaining_bits = 64 - p
    bucket = hashed >> remaining_bits
    remainder = hashed & ((1 << remaining_bits) - 1)
    if remainder == 0:
        rho = remaining_bits + 1
    else:
        rho = remaining_bits - remainder.bit_length() + 1
    return BucketRho(bucket=bucket, rho=rho)


def build_sketch(ticket_hashes: Iterable[int], p: int) -> list[int]:
    """Build a register table from already-hashed ticket identifiers."""
    validate_precision(p)
    registers = [0] * (1 << p)
    remaining_bits = 64 - p
    for hashed in ticket_hashes:
        if not isinstance(hashed, int) or isinstance(hashed, bool):
            raise SketchError("hashes must be integers")
        if hashed < 0 or hashed > MASK64:
            raise SketchError("hash must fit in 64 bits")
        bucket = hashed >> remaining_bits
        remainder = hashed & ((1 << remaining_bits) - 1)
        if remainder == 0:
            rho = remaining_bits + 1
        else:
            rho = remaining_bits - remainder.bit_length() + 1
        if rho > registers[bucket]:
            registers[bucket] = rho
    return registers


def sketch_for_tickets(tickets: Iterable[str], p: int) -> list[int]:
    """Convenience builder used by tests and the demo script."""
    return build_sketch((hash_ticket(t) for t in tickets), p)


def merge_sketches(sketches: Sequence[tuple[int, Sequence[int]]]) -> list[int]:
    """Merge sketches into the lowest precision present, exactly.

    Each source bucket maps to one target bucket.  A source register value
    ``v`` becomes ``v + shift`` when the low ``shift`` bits of the source
    bucket are zero, and ``v + shift - bit_length(offset)`` otherwise.  This is
    exact and associative, so the result equals rebuilding the table from the
    union of ticket hashes at the lowest precision.
    """
    if not sketches:
        raise SketchError("at least one sketch is required to merge")
    target_p = min(validate_precision(p) for p, _ in sketches)
    merged = [0] * (1 << target_p)

    for p, registers in sketches:
        validate_precision(p)
        validate_registers(p, registers)
        if p == target_p:
            merged = [a if a >= b else b for a, b in zip(merged, registers)]
            continue

        shift = p - target_p
        mask = (1 << shift) - 1
        for index, value in enumerate(registers):
            if value == 0:
                continue
            offset = index & mask
            if offset == 0:
                contribution = value + shift
            else:
                # The target remainder starts with the non-zero ``offset``, so
                # its leading-zero run is fixed and the source ``rho`` no
                # longer matters.
                contribution = shift - offset.bit_length() + 1
            target = index >> shift
            if contribution > merged[target]:
                merged[target] = contribution

    limit = max_rho(target_p)
    for index, value in enumerate(merged):
        if value > limit:  # defensive; the fold cannot exceed this
            raise SketchError(
                f"merged register {index} exceeds {limit} at precision {target_p}"
            )
    return merged


def estimate(registers: Sequence[int], p: int) -> float:
    """HyperLogLog cardinality estimate.

    Uses linear counting for the small range, and the 64-bit large-range
    correction so a saturated table cannot report more than ``2**64``.
    """
    validate_precision(p)
    validate_registers(p, registers)
    m = 1 << p
    inverse_sum = math.fsum(2.0 ** (-value) for value in registers)
    raw = _alpha(m) * m * m / inverse_sum
    zeros = registers.count(0)
    if raw <= 2.5 * m and zeros:
        return m * math.log(m / zeros)
    if raw > TWO_64 / 30:
        # For 64-bit hashes the estimate saturates below 2**64.
        ratio = raw / TWO_64
        if ratio >= 1.0:
            return float(TWO_64)
        return -TWO_64 * math.log(1.0 - ratio)
    return raw


def _alpha(m: int) -> float:
    if m == 16:
        return 0.673
    if m == 32:
        return 0.697
    if m == 64:
        return 0.709
    return 0.7213 / (1.0 + 1.079 / m)
