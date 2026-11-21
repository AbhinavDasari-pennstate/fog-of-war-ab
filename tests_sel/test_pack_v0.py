import pack
from conftest import tiny_tests


def test_empty_is_no_workers():
    assert pack.pack({}) == []


def test_every_test_once_and_at_most_eight_workers():
    tests = {t["number"]: round(t["seconds"] * 1000) for t in tiny_tests(300)}
    workers = pack.pack(tests)
    flat = [n for w in workers for n in w]
    assert sorted(flat) == sorted(tests)
    assert 1 <= len(workers) <= 8


def test_picks_one_worker_for_a_single_short_test():
    assert pack.pack({5: 100}) == [[5]]


def test_splits_when_workers_pay_for_themselves():
    tests = {n: 10_000 for n in range(8)}
    workers = pack.pack(tests)
    cost = max(sum(tests[n] for n in w) for w in workers) + 2000 * len(workers)
    assert len(workers) == 8
    assert cost == 10_000 + 2000 * 8
