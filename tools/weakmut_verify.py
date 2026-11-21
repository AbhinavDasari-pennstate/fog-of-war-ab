#!/usr/bin/env python3
"""Run each dropped (commit, test) pair for real: apply the patch to a scratch sqlglot copy, run the test, revert.

    python tools/weakmut_verify.py --pairs pairs.json --data <dataset dir> --src <scratch copy> --py <python>
Prints one line per pair: passed (the drop was right) or FAILED (a miss the rule would have caused).
"""
import argparse
import json
import os
import subprocess
from pathlib import Path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--src", required=True)
    ap.add_argument("--py", required=True)
    a = ap.parse_args()
    tests = {t["number"]: t["id"] for t in json.loads((Path(a.data) / "tests.json").read_text())["tests"]}
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH=a.src, PYTHONDONTWRITEBYTECODE="1", PYTHON_CPU_COUNT="2")
    bad = 0
    for rec in json.loads(Path(a.pairs).read_text()):
        patch = str(Path(a.data).resolve() / "commits" / (rec["commit"] + ".patch"))
        subprocess.run(["patch", "-p1", "-s", "-i", patch], cwd=a.src, check=True)
        try:
            for n in rec["tests"]:
                r = subprocess.run([a.py, "-m", "pytest", tests[n], "-p", "no:cacheprovider", "-o", "addopts=", "-q", "-x"],
                                   cwd=a.src, env=env, capture_output=True, text=True)
                ok = r.returncode == 0
                bad += not ok
                last = [l for l in r.stdout.splitlines() if l.strip()][-1:]
                print(rec["commit"], n, tests[n].split("::")[-1], "passed" if ok else "FAILED", last, flush=True)
        finally:
            subprocess.run(["patch", "-R", "-p1", "-s", "-i", patch], cwd=a.src, check=True)
    print("failed pairs:", bad)


if __name__ == "__main__":
    main()
