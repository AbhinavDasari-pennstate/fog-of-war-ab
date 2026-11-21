"""Score selector outputs on labelled sets: python tools/score_sets.py <set> <output.jsonl> [<other.jsonl>]
Prints simulated seconds, misses, and the difference against a second output (e.g. v1)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval as E  # noqa: E402


def main(argv):
    ds = E.load_set(argv[0])
    res = E.score(Path(argv[1]).read_bytes(), ds)
    if not res["ok"]:
        print("REJECTED", res)
        return 2
    print(f"{argv[0]}: {len(ds.answers)} commits simulated {res['simulated_ms'] / 1000:.3f} s misses {len(res['misses'])} {res['misses']}")
    if len(argv) > 2:
        r2 = E.score(Path(argv[2]).read_bytes(), ds)
        print(f"  other: simulated {r2['simulated_ms'] / 1000:.3f} s misses {len(r2['misses'])}; saving {(r2['simulated_ms'] - res['simulated_ms']) / 1000:.3f} s")
    return 1 if res["misses"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
