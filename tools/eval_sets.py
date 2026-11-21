#!/usr/bin/env python3
"""Score one selector on labelled sets (tools/eval.py functions, any dataset directory).

    python tools/eval_sets.py --selector selector/main.py [--maps DIR] SET [SET ...]
SET is "sample" or a directory name under rwx-data (needs tests.json, commits/, answers.json).
Prints simulated seconds, misses, floor, and per-commit selected counts as JSON to --json.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval as E  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selector", required=True)
    ap.add_argument("--maps")
    ap.add_argument("--json")
    ap.add_argument("sets", nargs="+")
    a = ap.parse_args()
    if a.maps:
        os.environ["BR_MAPS"] = a.maps
    out = {}
    for name in a.sets:
        ds = E.load_set(name)
        stdout, stderr, cpu = E.run_native(ds, Path(a.selector))
        res = E.score(stdout, ds)
        if not res["ok"]:
            print(name, "REJECTED", res)
            continue
        floor = sum(E.floor_ms(ds).values())
        print(f"{name}: {len(ds.answers)} commits  simulated {res['simulated_ms']/1000:.3f} s  misses {len(res['misses'])}  floor {floor/1000:.3f} s  cpu {cpu:.1f} s")
        for c, lost in res["misses"]:
            print("  MISS", c, lost)
        drops = [l.strip() for l in stderr.splitlines() if "weakmut" in l and "dropped" in l]
        print(f"  weakmut drops on {len(drops)} commits")
        out[name] = {"simulated_ms": res["simulated_ms"], "misses": res["misses"], "costs": res["costs"],
                     "selected": res["selected"], "drops": drops}
    if a.json:
        Path(a.json).write_text(json.dumps(out))


if __name__ == "__main__":
    main()
