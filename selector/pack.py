"""Deterministic worker packing in integer milliseconds."""

import time

WORKER_COST_MS = 2000
MAX_WORKERS = 8
EXACT_NODE_LIMIT = 20_000
EXACT_CPU_SECONDS = 0.020


def _lpt(order: list[tuple[int, int]], k: int) -> tuple[list[list[int]], int]:
    workers: list[list[int]] = [[] for _ in range(k)]
    loads = [0] * k
    for number, ms in order:
        i = loads.index(min(loads))
        workers[i].append(number)
        loads[i] += ms
    return workers, max(loads)


def _improve(workers, durations, lower):
    """Reduce the busiest load by moving or swapping one test at a time."""
    loads = [sum(durations[n] for n in w) for w in workers]
    while max(loads) > lower:
        high = loads.index(max(loads))
        old = loads[high]
        # Only changes to a busiest worker can lower the makespan.
        if loads.count(old) > 1:
            break
        best = None
        # Limit swap candidates so large selections stay fast.
        for a in workers[high][:30]:
            av = durations[a]
            for j, worker in enumerate(workers):
                if j == high:
                    continue
                target = max(loads[x] for x in range(len(loads)) if x not in (high, j)) if len(loads) > 2 else 0
                candidate = max(target, old - av, loads[j] + av)
                if candidate < old and (best is None or candidate < best[0]):
                    best = (candidate, a, j, None)
                for b in worker[:30]:
                    bv = durations[b]
                    if bv >= av:
                        continue
                    candidate = max(target, old - av + bv, loads[j] + av - bv)
                    if candidate < old and (best is None or candidate < best[0]):
                        best = (candidate, a, j, b)
        if best is None:
            break
        _, a, j, b = best
        workers[high].remove(a)
        workers[j].append(a)
        loads[high] -= durations[a]
        loads[j] += durations[a]
        if b is not None:
            workers[j].remove(b)
            workers[high].append(b)
            loads[j] -= durations[b]
            loads[high] += durations[b]
    return workers, max(loads)


def _exact(order, workers, bound, lower, budget):
    """Search distinct load assignments until the lower bound or budget is met."""
    k = len(workers)
    loads = [0] * k
    current = [[] for _ in range(k)]
    best = [bound, [w[:] for w in workers]]

    def visit(i):
        if best[0] == lower or budget[0] <= 0 or time.process_time() >= budget[1]:
            return
        budget[0] -= 1
        if i == len(order):
            best[0] = max(loads)
            best[1] = [w[:] for w in current]
            return
        number, duration = order[i]
        seen = set()
        for j in sorted(range(k), key=loads.__getitem__):
            load = loads[j]
            if load in seen or load + duration >= best[0]:
                continue
            seen.add(load)
            loads[j] += duration
            current[j].append(number)
            visit(i + 1)
            current[j].pop()
            loads[j] = load
            if best[0] == lower or budget[0] <= 0 or time.process_time() >= budget[1]:
                return

    visit(0)
    return best[1], best[0]


def pack(tests: dict[int, int]) -> list[list[int]]:
    """Return the cheapest split, breaking cost ties toward fewer workers."""
    if not tests:
        return []
    order = sorted(tests.items(), key=lambda kv: (-kv[1], kv[0]))
    total = sum(tests.values())
    best_cost = float('inf')
    best_workers = []
    budget = [EXACT_NODE_LIMIT, time.process_time() + EXACT_CPU_SECONDS]
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        if WORKER_COST_MS * k >= best_cost:
            break
        lower = max(order[0][1], (total + k - 1) // k)
        if lower + WORKER_COST_MS * k >= best_cost:
            continue
        workers, makespan = _lpt(order, k)
        if makespan > lower:
            workers, makespan = _improve(workers, tests, lower)
            if len(order) <= 25:
                workers, makespan = _exact(order, workers, makespan, lower, budget)
        cost = makespan + WORKER_COST_MS * k
        if cost < best_cost:
            best_cost, best_workers = cost, workers
    return [w for w in best_workers if w]
