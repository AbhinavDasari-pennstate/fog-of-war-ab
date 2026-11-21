#!/usr/bin/env python3
"""Build maps/lazyimp.bin: module file -> mask of tests that import it, cold (statically or lazily).

    python3 tools/build_lazyimp.py --src <pinned sqlglot checkout> --tests <tests.json> --out maps [--jobs 3]

One pytest process per test file with tools.lazyimp (fork per test). For each test the cold import set
is (modules loaded by collecting its file) union (modules the test itself imported). The result only
ever adds to maps/imports.bin: selector rules take the union. Needs the project's venv (duckdb etc).
"""

from __future__ import annotations

import argparse
import json
import marshal
import os
import subprocess
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def run_file(py: str, src: str, tf: str, out: str) -> tuple[str, int]:
    env = dict(
        os.environ,
        LAZY_ROOT=src,
        LAZY_OUT=out,
        PYTHONPATH=f"{src}{os.pathsep}{HERE}",
        PYTHONDONTWRITEBYTECODE="1",
        SKIP_INTEGRATION="0",
        PYTHON_CPU_COUNT="2",
    )
    r = subprocess.run(
        [py, "-m", "pytest", tf, "-p", "no:cacheprovider", "-o", "addopts=", "-p", "tools.lazyimp", "-q"],
        cwd=src, env=env, capture_output=True, text=True, timeout=1800,
    )
    return tf, r.returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--tests", required=True)
    ap.add_argument("--out", default=str(HERE / "maps"))
    ap.add_argument("--python", default=str(HERE / ".venv" / "bin" / "python"))
    ap.add_argument("--jobs", type=int, default=3)
    ap.add_argument("--work", required=True, help="scratch dir for the per-file json")
    a = ap.parse_args()
    src = os.path.abspath(a.src)
    tests = json.loads(Path(a.tests).read_text("utf-8"))["tests"]
    files = sorted({t["file"] for t in tests})
    os.makedirs(a.work, exist_ok=True)
    with ThreadPoolExecutor(a.jobs) as ex:
        for tf, rc in ex.map(lambda f: run_file(a.python, src, f, a.work), files):
            print(f"{tf}: exit {rc}", file=sys.stderr)
    by_id = {t["id"]: t["number"] for t in tests}
    mask: dict[str, int] = {}
    seen: set[int] = set()
    for p in sorted(Path(a.work).glob("*.json")):
        d = json.loads(p.read_text())
        coll = d["collect"]
        for nodeid, mods in d["tests"].items():
            n = by_id.get(nodeid)
            if n is None:
                continue
            seen.add(n)
            if "<child died>" in mods:
                print(f"child died: {nodeid}", file=sys.stderr)
                continue
            for m in set(coll) | set(mods):
                mask[m] = mask.get(m, 0) | (1 << n)
    missing = sorted(set(by_id.values()) - seen)
    print(f"tests seen {len(seen)} of {len(by_id)}; modules {len(mask)}; missing {missing[:10]}", file=sys.stderr)
    if missing:
        return 3
    blob = zlib.compress(marshal.dumps(dict(sorted(mask.items()))), 9)
    Path(a.out, "lazyimp.bin").write_bytes(blob)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
