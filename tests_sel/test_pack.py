import json
import random
import time
from pathlib import Path

import pack
import pytest


def cost(workers, tests):
    return max((sum(tests[n] for n in w) for w in workers), default=0) + 2000 * len(workers)


def oracle(tests):
    values = sorted(tests.values(), reverse=True)
    best = sum(values) + 2000
    loads = []

    def visit(i):
        nonlocal best
        if i == len(values):
            best = min(best, max(loads) + 2000 * len(loads))
            return
        seen = set()
        for j in range(len(loads)):
            if loads[j] in seen:
                continue
            seen.add(loads[j])
            loads[j] += values[i]
            visit(i + 1)
            loads[j] -= values[i]
        if len(loads) < 8:
            loads.append(values[i])
            visit(i + 1)
            loads.pop()

    visit(0)
    return best


@pytest.mark.parametrize('seed', range(8))
def test_small_sets_match_all_assignments(seed):
    rng = random.Random(seed)
    for n in range(1, 10):
        tests = {i: rng.randint(1, 16000) for i in range(n)}
        if seed % 2 and n > 1:
            tests[0] *= 8
        workers = pack.pack(tests)
        assert sorted(n for w in workers for n in w) == sorted(tests)
        assert cost(workers, tests) == oracle(tests)


def test_empty():
    assert pack.pack({}) == []


def test_full_sample_cost_and_speed():
    path = Path('/Users/vidyutsriram/hackathons/builders-buildathon-2026/kits/rwx/x/sample/tests.json')
    data = json.loads(path.read_text())['tests']
    tests = {t['number']: round(t['seconds'] * 1000) for t in data}
    assert len(tests) == 1620
    start = time.perf_counter()
    workers = pack.pack(tests)
    elapsed = time.perf_counter() - start
    assert sorted(n for w in workers for n in w) == sorted(tests)
    assert cost(workers, tests) <= 36817
    assert elapsed < 0.020


def test_lpt_counterexample():
    tests = {0: 8095, 1: 4318, 2: 5823, 3: 14628, 4: 5407, 5: 1151, 6: 1542, 7: 3455}
    assert cost(pack.pack(tests), tests) == oracle(tests)
