#!/usr/bin/env python3
"""Run the pairs (commit, test) that one selector output drops and another keeps, for real.

    python tools/realdrop.py --src <pinned checkout> --python <venv python> --data <dataset dir> \
        --base <jsonl, the wider selection> --new <jsonl, the narrower one> --work <fresh empty dir> [--seeds 0,1,2] [--n 3] [commit ...]

One scratch copy of the tree under --work; each patch is applied with `patch`, the dropped tests of that commit
run under every hash seed (pytest -n N, SKIP_INTEGRATION=0), and the patch is reverted. Prints one line per
commit: PASS when every dropped test passed under every seed, else FAIL with the failing test ids.
A dropped test that fails is a miss the narrower selector would have caused.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET


def load(p):
    out = {}
    for line in open(p):
        r = json.loads(line)
        out[r["commit"]] = {t for w in r["workers"] for t in w}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--python", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--base", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--seeds", default="0,1,2")
    ap.add_argument("--n", default="3")
    ap.add_argument("commits", nargs="*")
    a = ap.parse_args()
    tests = {t["number"]: t["id"] for t in json.load(open(os.path.join(a.data, "tests.json")))["tests"]}
    base, new = load(a.base), load(a.new)
    ids = a.commits or sorted(c for c in base if base[c] - new.get(c, set()))
    os.makedirs(a.work, exist_ok=True)
    dst = os.path.join(a.work, "tree")
    shutil.copytree(a.src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
    bad = 0
    for cid in ids:
        dropped = sorted(base[cid] - new.get(cid, set()))
        if not dropped:
            continue
        patch = os.path.abspath(os.path.join(a.data, "commits", cid + ".patch"))
        r = subprocess.run(["patch", "-p1", "-s", "--no-backup-if-mismatch", "-i", patch], cwd=dst, capture_output=True, text=True)
        if r.returncode:
            print(cid, "NOAPPLY", r.stdout[-200:], flush=True)
            bad += 1
            continue
        fails = {}
        for seed in a.seeds.split(","):
            xml = os.path.join(a.work, f"{cid}-s{seed}.xml")
            t = subprocess.run(
                [a.python, "-m", "pytest", "-n", a.n, "-p", "no:cacheprovider", "-o", "addopts=", "-q", "--junitxml", xml,
                 *[tests[n] for n in dropped]],
                cwd=dst, capture_output=True, text=True,
                env=dict(os.environ, PYTHON_CPU_COUNT="2", PYTHONHASHSEED=seed, SKIP_INTEGRATION="0",
                         PYTHONDONTWRITEBYTECODE="1"))
            ran = 0
            if os.path.exists(xml):
                for tc in ET.parse(xml).getroot().iter("testcase"):
                    ran += 1
                    if tc.find("failure") is not None or tc.find("error") is not None:
                        fails.setdefault(seed, []).append(f"{tc.get('classname')}::{tc.get('name')}")
            if ran < len(dropped) - 0 and not fails.get(seed):
                fails.setdefault(seed, []).append(f"ran only {ran} of {len(dropped)}: {t.stdout[-200:]}")
        status = "PASS" if not fails else "FAIL"
        bad += status == "FAIL"
        print(cid, status, f"dropped {len(dropped)} seeds {a.seeds}", json.dumps(fails)[:600] if fails else "", flush=True)
        subprocess.run(["patch", "-R", "-p1", "-s", "--no-backup-if-mismatch", "-i", patch], cwd=dst, check=True)
    print("commits with a failing dropped pair or no apply:", bad)


if __name__ == "__main__":
    main()
