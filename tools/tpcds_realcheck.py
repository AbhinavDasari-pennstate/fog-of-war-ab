"""Run the real test_execute_tpcds on patched copies of the pinned tree.

    python tools/tpcds_realcheck.py --src <pin> --python <venv python> --commits <dir with .patch files> id [id ...]

One scratch copy of the tree; each patch is applied with `patch -p1`, the real test runs, the patch is
reverted. Prints `<id> PASS|FAIL|NOAPPLY` per commit. Used to confirm that every pair the replay dropped
really passes.
"""

import os
import shutil
import subprocess
import sys
import tempfile


def main():
    a = sys.argv
    src = a[a.index("--src") + 1]
    py = a[a.index("--python") + 1]
    cdir = a[a.index("--commits") + 1]
    ids = [x for x in a[a.index("--commits") + 2:]]
    with tempfile.TemporaryDirectory() as tmp:
        dst = os.path.join(tmp, "tree")
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv"))
        for cid in ids:
            patch = os.path.abspath(os.path.join(cdir, "commits", cid + ".patch"))
            r = subprocess.run(["patch", "-p1", "-s", "--no-backup-if-mismatch", "-i", patch], cwd=dst, capture_output=True, text=True)
            if r.returncode:
                print(cid, "NOAPPLY", r.stdout[-200:], flush=True)
                continue
            res = []
            for seed in os.environ.get("REAL_SEEDS", "0,1,2").split(","):  # the real test under several hash seeds
                t = subprocess.run(
                    [py, "-m", "pytest", "tests/test_executor.py", "-k", "test_execute_tpcds", "-x", "-q",
                     "-p", "no:cacheprovider", "-o", "addopts=", "--no-header"],
                    cwd=dst, capture_output=True, text=True,
                    env=dict(os.environ, PYTHON_CPU_COUNT="2", PYTHONHASHSEED=seed),
                )
                res.append("P" if t.returncode == 0 else "F")
            print(cid, "PASS" if set(res) == {"P"} else "FAIL", "seeds " + os.environ.get("REAL_SEEDS", "0,1,2"), "".join(res), flush=True)
            subprocess.run(["patch", "-R", "-p1", "-s", "--no-backup-if-mismatch", "-i", patch], cwd=dst, check=True)


if __name__ == "__main__":
    main()
