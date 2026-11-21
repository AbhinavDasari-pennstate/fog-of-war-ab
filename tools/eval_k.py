"""Score a selector on labelled sets as simulated + K x native CPU.

    python tools/eval_k.py --selector <main.py> --sets sample synth-holdout ... [--env K=V ...] [--save <prefix>]
CPU is user+sys of the selector process and all its children (RUSAGE_CHILDREN). K = 2.5 and 3 are printed.
"""
import argparse
import os
import resource
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval as E  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selector", required=True)
    ap.add_argument("--sets", nargs="+", required=True)
    ap.add_argument("--env", nargs="*", default=[])
    ap.add_argument("--python", default=sys.executable)
    ap.add_argument("--save", default="")
    a = ap.parse_args()
    extra = dict(kv.split("=", 1) for kv in a.env)
    for name in a.sets:
        ds = E.load_set(name)
        env = dict(os.environ, BR_DATA=str(ds.data_dir), BR_SRC=str(E.DEFAULT_SRC), PYTHON_CPU_COUNT="2", **extra)
        b = resource.getrusage(resource.RUSAGE_CHILDREN)
        r = subprocess.run([a.python, a.selector], env=env, capture_output=True)
        c = resource.getrusage(resource.RUSAGE_CHILDREN)
        cpu = (c.ru_utime - b.ru_utime) + (c.ru_stime - b.ru_stime)
        if a.save:
            Path(a.save + f".{name}.jsonl").write_bytes(r.stdout)
            Path(a.save + f".{name}.err").write_bytes(r.stderr)
        res = E.score(r.stdout, ds)
        if not res["ok"]:
            print(name, "REJECTED", res)
            continue
        sim = res["simulated_ms"] / 1000
        print(f"{name:16s} sim {sim:9.1f} cpu {cpu:6.1f} misses {len(res['misses'])} "
              f"K2.5 {sim + 2.5 * cpu:9.1f} K3 {sim + 3 * cpu:9.1f}", flush=True)


main()
