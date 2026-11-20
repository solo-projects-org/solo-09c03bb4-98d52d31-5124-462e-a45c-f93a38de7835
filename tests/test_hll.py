import random

import pytest

from app import hll


def test_hash_is_stable_and_64_bit():
    assert hll.hash_ticket("T-1") == hll.hash_ticket("T-1")
    assert hll.hash_ticket("T-1") != hll.hash_ticket("T-2")
    assert 0 <= hll.hash_ticket("anything") < 2**64


def test_bucket_and_rho_matches_build():
    for p in (4, 7, 12):
        for ticket in [f"T-{i}" for i in range(200)]:
            hashed = hll.hash_ticket(ticket)
            split = hll.bucket_and_rho(hashed, p)
            built = hll.sketch_for_tickets([ticket], p)
            assert built[split.bucket] == split.rho


def test_rho_saturates_for_all_zero_remainder():
    p = 4
    split = hll.bucket_and_rho(0, p)
    assert split.bucket == 0
    assert split.rho == 64 - p + 1


def test_register_validation():
    with pytest.raises(hll.SketchError):
        hll.validate_precision(3)
    with pytest.raises(hll.SketchError):
        hll.validate_precision(13)
    with pytest.raises(hll.SketchError):
        hll.validate_registers(4, [0] * 15)
    with pytest.raises(hll.SketchError):
        hll.validate_registers(4, [0] * 16 + [61])
    with pytest.raises(hll.SketchError):
        hll.validate_registers(4, [0] * 15 + [62])
    hll.validate_registers(4, [0] * 15 + [61])
    with pytest.raises(hll.SketchError):
        hll.validate_registers(4, [0] * 15 + [True])


def test_fold_down_matches_rebuild():
    rng = random.Random(11)
    for _ in range(400):
        source_p = rng.randint(4, 12)
        target_p = rng.randint(4, source_p)
        hashes = [rng.getrandbits(64) for _ in range(rng.randint(0, 300))]
        source = hll.build_sketch(hashes, source_p)
        merged = hll.merge_sketches(
            [(source_p, source), (target_p, [0] * (1 << target_p))]
        )
        assert merged == hll.build_sketch(hashes, target_p)


def test_union_merge_matches_rebuild_at_lowest_precision():
    rng = random.Random(12)
    for _ in range(200):
        sketches = []
        all_hashes = []
        for _ in range(rng.randint(1, 4)):
            p = rng.randint(4, 12)
            hashes = [rng.getrandbits(64) for _ in range(rng.randint(0, 120))]
            all_hashes.extend(hashes)
            sketches.append((p, hll.build_sketch(hashes, p)))
        target_p = min(p for p, _ in sketches)
        assert hll.merge_sketches(sketches) == hll.build_sketch(all_hashes, target_p)


def test_merge_is_associative_and_order_independent():
    rng = random.Random(13)
    parts = []
    for _ in range(5):
        p = rng.randint(4, 10)
        hashes = [rng.getrandbits(64) for _ in range(60)]
        parts.append((p, hll.build_sketch(hashes, p)))
    target_p = min(p for p, _ in parts)
    direct = hll.merge_sketches(parts)
    left = parts[0]
    for part in parts[1:]:
        acc_p = min(left[0], part[0])
        acc = hll.merge_sketches([left, part])
        left = (acc_p, acc)
    assert left == (target_p, direct)
    assert hll.merge_sketches(list(reversed(parts))) == direct


def test_correction_can_lower_registers():
    # A superseding daily sketch may legitimately have smaller registers; the
    # month must be rebuilt rather than max-merged with the stale table.
    old = hll.sketch_for_tickets([f"old-{i}" for i in range(300)], 8)
    new = hll.sketch_for_tickets(["only-one"], 8)
    assert hll.merge_sketches([(8, new)]) != hll.merge_sketches([(8, old), (8, new)])
    assert sum(new) < sum(old)


def test_estimate_never_exceeds_hash_space():
    # An all-maximum table is the extreme case for the raw estimator.
    p = 4
    registers = [hll.max_rho(p)] * (1 << p)
    result = hll.estimate(registers, p)
    assert 0 < result <= 2**64


def test_estimate_tracks_cardinality():
    tickets = [f"ticket-{i}" for i in range(5000)]
    sketch = hll.sketch_for_tickets(tickets, 12)
    result = hll.estimate(sketch, 12)
    assert 4500 <= result <= 5500


def test_estimate_linear_counting_for_small_sets():
    tickets = [f"ticket-{i}" for i in range(30)]
    sketch = hll.sketch_for_tickets(tickets, 12)
    result = hll.estimate(sketch, 12)
    assert 27 <= result <= 34


def test_estimate_after_merge_stays_close():
    rng = random.Random(14)
    tickets = [f"ticket-{rng.randrange(10**6)}" for _ in range(8000)]
    p = 11
    half = len(tickets) // 2
    left = hll.sketch_for_tickets(tickets[:half], p)
    right = hll.sketch_for_tickets(tickets[half:], p)
    merged = hll.merge_sketches([(p, left), (p, right)])
    assert merged == hll.sketch_for_tickets(tickets, p)
    assert abs(hll.estimate(merged, p) - len(set(tickets))) < 0.05 * len(set(tickets))
