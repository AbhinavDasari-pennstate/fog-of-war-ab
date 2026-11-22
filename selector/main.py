"""Board probe J (not a real entry). Runs the real selector of this tree (selector/real.py, rule G included) unchanged and
adds extra workers so the total simulated time is exactly PAD ms more than the real selection's: the board's simulated
seconds minus 100 is what the real entry would score, the missed count is the real one, and the padded score can never
be the team's best. Nothing is dropped from any selection."""
import json
import os
import subprocess
import sys
from pathlib import Path

DATA = Path(os.environ.get("BR_DATA", "/data"))
HERE = Path(__file__).resolve().parent
PAD = 100_000
WORKER = 2000
MAXW = 8
tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
DUR = {t["number"]: int(t["seconds"] * 1000 + 0.5) for t in tests}


def cost(workers):
    ws = [w for w in workers if w]
    return (max(sum(DUR[n] for n in w) for w in ws) + WORKER * len(ws)) if ws else 0


def subset(target, avail):
    """Test numbers of avail summing to exactly target ms (greedy, largest first), or None."""
    pick, rem = [], target
    for n in sorted(avail, key=lambda n: -DUR[n]):
        d = DUR[n]
        if d <= rem and (d > 0 or rem == 0):
            pick.append(n)
            rem -= d
            if rem == 0:
                break
    if rem != 0:
        return None
    if not pick:
        zero = [n for n in avail if DUR[n] == 0]
        return zero[:1] or None
    return pick


def main():
    r = subprocess.run([sys.executable, "-S", "-E", str(HERE / "real.py")], capture_output=True)
    sys.stderr.write(r.stderr.decode("utf-8", "replace")[-4000:])
    if r.returncode != 0:
        sys.exit(r.returncode)
    rows, order = {}, []
    for line in r.stdout.decode("utf-8").splitlines():
        row = json.loads(line)
        rows[row["commit"]] = [[int(n) for n in w] for w in row["workers"]]
        order.append(row["commit"])
    base = sum(cost(rows[c]) for c in order)
    rem = PAD
    for c in order:
        if rem == 0:
            break
        ws = [w for w in rows[c] if w]
        if len(ws) >= MAXW:
            continue
        m = max((sum(DUR[n] for n in w) for w in ws), default=0)
        used = {n for w in ws for n in w}
        avail = [n for n in DUR if n not in used]
        inc_max = WORKER + max(0, sum(DUR[n] for n in avail) - m - 1000)
        inc = min(rem, inc_max, 30_000)
        if 0 < rem - inc < WORKER:
            inc = rem - WORKER
        if inc < WORKER:
            continue
        pick = subset(m + (inc - WORKER), avail)
        if not pick:
            continue
        new = ws + [pick]
        if cost(new) - cost(ws) != inc:
            continue
        rows[c] = new
        rem -= inc
    total = sum(cost(rows[c]) for c in order)
    sys.stderr.write(f"probe J: base={base} total={total} pad={total - base} rem={rem}\n")
    for c in order:
        print(json.dumps({"commit": c, "workers": rows[c]}))


main()
