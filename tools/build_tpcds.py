"""Build maps/tpcds.z, the per-query map for test_execute_tpcds (laptop only, one run at a time).

    python tools/build_tpcds.py --src <pinned sqlglot> --out maps/tpcds.z [--python .venv/bin/python]

Runs tools/tpcds_probe.py under several modes and PYTHONHASHSEEDs, then merges:
  cov     per sqlglot file, per line, the bitmask of queries that executed it (union over every run)
  base    lines executed outside any query (importing every module, setUpClass work)
  digests per query the set of observed parse / transpile / result / state digests (a pristine
          query whose digests are not the same in every mode and seed is marked unstable)
  cpu     median seconds a query takes alone
Refuses to write the map when any run raised an error or the modes disagree on a query that
the replay would have to trust (it records the disagreement as unstable instead and lists it).
"""

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import tempfile
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RUNS = [  # (mode, PYTHONHASHSEED, trace)
    ("alone", "0", True), ("alone", "1", True), ("alone", "2", False),
    ("fwd", "3", True), ("rev", "4", True), ("batch", "5", True),
    ("shuf1", "6", True), ("shuf2", "7", False), ("fwd", "random", False),
]


def run(python, src, mode, seed, trace, out):
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHON_CPU_COUNT="2")
    cmd = [python, os.path.join(ROOT, "tools", "tpcds_probe.py"), "--src", src, "--mode", mode, "--out", out]
    if trace:
        cmd.append("--trace")
    subprocess.run(cmd, env=env, check=True, stderr=subprocess.DEVNULL)
    return json.load(open(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--only", type=int, default=0, help="debug: run only the first N runs")
    a = ap.parse_args()
    src = os.path.abspath(a.src)
    runs = RUNS[: a.only] if a.only else RUNS
    STAGES = "PBOET"
    cov: dict[str, dict[int, list]] = {}
    base: dict[str, set] = {}
    seen: dict[int, dict[str, set]] = {}
    cpus: dict[int, list] = {}
    scpu: dict[int, dict[str, list]] = {}
    errors = []
    n = None
    tables: dict[int, list] = {}
    meta = None
    pyv = set()
    notok = set()
    gseen: dict[int, dict[str, set]] = {}
    with tempfile.TemporaryDirectory() as tmp:
        for k, (mode, seed, trace) in enumerate(runs):
            print(f"run {k + 1}/{len(runs)} {mode} seed {seed} trace {trace}", file=sys.stderr, flush=True)
            d = run(a.python, src, mode, seed, trace, os.path.join(tmp, f"r{k}.json"))
            n = d["n"]
            meta = d["meta"]
            impf = sorted(x[0] for x in d["import_failed"])
            pyv.add(d["py"].split("\n")[0])
            for f, lines in d["base"].items():
                base.setdefault(f, set()).update(lines)
            for key, r in d["res"].items():
                i = int(key)
                if "error" in r:
                    errors.append((mode, seed, i, r["error"]))
                    continue
                s = seen.setdefault(i, {st: set() for st in STAGES})
                for st in STAGES:
                    if st in r:
                        s[st].add(r[st])
                if not r["ok"]:
                    notok.add(i)
                tables[i] = r["tables"]
                if mode == "alone":
                    for st, v in {**r["G"], **r.get("Gx", {})}.items():
                        gseen.setdefault(i, {}).setdefault(st, set()).add(v)
                    cpus.setdefault(i, []).append(r["cpu"])
                    for st, v in r["stage_cpu"].items():
                        scpu.setdefault(i, {}).setdefault(st, []).append(v)
                for st, files in r.get("cov", {}).items():
                    si = STAGES.index(st)
                    for f, lines in files.items():
                        m = cov.setdefault(f, {})
                        for ln in lines:
                            m.setdefault(ln, [0] * 5)[si] |= 1 << i
    if errors:
        print("ERRORS", errors[:5], file=sys.stderr)
        sys.exit(1)
    unstable = [i for i, s in seen.items() if any(len(v) > 1 for v in s.values())]
    out = {
        "pin": subprocess.run(["git", "-C", src, "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
        "python": sorted(pyv),
        "n": n,
        "import_failed": impf,
        "meta_sha": [hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()[:16] for m in meta],
        "queries": {
            str(i): {
                **{st: sorted(seen[i][st]) for st in STAGES},
                "tables": tables[i], "cpu": round(min(cpus[i]), 4),
                "stage_cpu": {st: round(min(v), 4) for st, v in scpu.get(i, {}).items()},
                "G": {st: sorted(v) for st, v in gseen[i].items()},
                "unstable": i in unstable, "mutates": i in notok,
            }
            for i in sorted(seen)
        },
        "unstable": sorted(unstable),
        "mutating": sorted(notok),
        "cov": {f: {str(ln): [format(m, "x") for m in ms] for ln, ms in sorted(lines.items())}
                for f, lines in sorted(cov.items())},
        "base": {f: sorted(v) for f, v in sorted(base.items())},
        "runs": [list(r) for r in runs],
    }
    blob = zlib.compress(json.dumps(out, separators=(",", ":")).encode(), 9)
    with open(a.out, "wb") as f:
        f.write(blob)
    print(f"wrote {a.out}: {len(blob)} bytes; queries {len(seen)}; unstable {unstable}; "
          f"mutating {sorted(notok)}; files {len(cov)}; base lines {sum(len(v) for v in base.values())}", file=sys.stderr)


if __name__ == "__main__":
    main()
