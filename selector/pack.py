"""Worker packing: longest-first onto the lightest worker, best worker count 1..8.

Cost of a commit is the busiest worker's load plus 2000 ms per worker, in whole ms.
T6 replaces the LPT step with an optimal search; the interface stays the same.
"""

WORKER_COST_MS = 2000
MAX_WORKERS = 8


def _lpt(order: list[tuple[int, int]], k: int) -> tuple[list[list[int]], int]:
    workers: list[list[int]] = [[] for _ in range(k)]
    loads = [0] * k
    for number, ms in order:
        i = loads.index(min(loads))
        workers[i].append(number)
        loads[i] += ms
    return workers, max(loads)


def pack(tests: dict[int, int]) -> list[list[int]]:
    """tests maps test number to duration in ms. Returns the cheapest split."""
    if not tests:
        return []
    order = sorted(tests.items(), key=lambda kv: (-kv[1], kv[0]))
    best: tuple[int, list[list[int]]] | None = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        if best is not None and WORKER_COST_MS * k >= best[0]:
            break
        workers, makespan = _lpt(order, k)
        cost = makespan + WORKER_COST_MS * k
        if best is None or cost < best[0]:
            best = (cost, workers)
    assert best is not None
    return [w for w in best[1] if w]
