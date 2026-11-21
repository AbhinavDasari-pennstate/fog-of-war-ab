#!/usr/bin/env python3
"""List (commit, test) pairs that the weak-mutation rule removed from the selection on a dataset.

    python tools/weakmut_pairs.py --data <dir with commits/ and tests.json> --maps <dir named maps with weakmut.bin> --src <pinned sqlglot> --out pairs.json
Runs the selector rules twice (with and without weakmut.bin) and reports the difference per commit.
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "selector"))
sys.path.insert(0, str(ROOT))
import diffparse  # noqa: E402
import rules  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--maps", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    sys.path.insert(0, str(Path(a.maps).parent))
    from maps.load import load

    tests = json.loads((Path(a.data) / "tests.json").read_text())["tests"]
    maps = load(a.maps)
    full = maps.weakmut  # a dict (per-test format) or a weakmut.Profile
    out = []
    for p in sorted((Path(a.data) / "commits").glob("*.patch")):
        changes = diffparse.parse(p.read_text(encoding="utf-8"))
        maps._weakmut = full
        on, _ = rules.select(changes, maps, tests, src_root=a.src)
        maps._weakmut = {}
        off, _ = rules.select(changes, maps, tests, src_root=a.src)
        gone = sorted(set(off) - set(on))
        if gone:
            out.append({"commit": p.stem, "tests": gone})
    Path(a.out).write_text(json.dumps(out))
    print(a.data, "commits with a drop:", len(out), "pairs:", sum(len(o["tests"]) for o in out))


if __name__ == "__main__":
    main()
