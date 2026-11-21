import subprocess
import sys

from conftest import ROOT

WRAP = ROOT / "tools" / "cpu_wrap.py"


def test_wrapper_passes_stdout_through_and_reports_cpu_on_stderr():
    code = "import sys; print('hello'); print('log', file=sys.stderr); sum(range(3_000_000))"
    r = subprocess.run([sys.executable, str(WRAP), sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0
    assert r.stdout == "hello\n"
    last = r.stderr.strip().splitlines()[-1]
    assert last.startswith("CPU_SECONDS ")
    assert float(last.split()[1]) > 0.0


def test_wrapper_propagates_exit_code():
    r = subprocess.run([sys.executable, str(WRAP), sys.executable, "-c", "raise SystemExit(3)"], capture_output=True, text=True)
    assert r.returncode == 3
