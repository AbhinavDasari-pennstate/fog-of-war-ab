"""CPU diet, repeated work: file reads, patch application, the tests index, durations and the replay thread pool."""

import os
from pathlib import Path

import pytest

import diffparse
import main as M
import rules
from conftest import ROOT, tiny_tests

PATCH = (
    "diff --git a/m.py b/m.py\nindex 1..2 100644\n--- a/m.py\n+++ b/m.py\n"
    "@@ -1,3 +1,3 @@\n a = 1\n-b = 2\n+b = 3\n c = 4\n"
)
OLD = "a = 1\nb = 2\nc = 4\n"


def test_read_text_remembers_a_file_until_it_changes(tmp_path):
    f = tmp_path / "m.py"
    f.write_text("x = 1\n")
    a = diffparse.read_text(f)
    assert a == "x = 1\n" and diffparse.read_text(str(f)) is a
    f.write_text("x = 22\n")  # a different size
    b = diffparse.read_text(f)
    assert b == "x = 22\n"
    f.write_text("x = 33\n")  # the same size, a later modification time
    st = os.stat(f)
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
    assert diffparse.read_text(f) == "x = 33\n"


def test_read_text_fails_like_read_bytes_does(tmp_path):
    with pytest.raises(FileNotFoundError):
        diffparse.read_text(tmp_path / "missing.py")
    bad = tmp_path / "bad.py"
    bad.write_bytes(b"\xff\xfe")
    with pytest.raises(UnicodeDecodeError):
        diffparse.read_text(bad)


def test_apply_gives_the_same_text_object_for_the_same_text_and_change():
    change = diffparse.parse(PATCH)[0]
    text = OLD
    first = diffparse.apply(text, change)
    assert first == "a = 1\nb = 3\nc = 4\n" and diffparse.apply(text, change) is first
    again = diffparse.apply(OLD.encode().decode(), change)  # an equal text that is another object is applied afresh, same answer
    assert again == first


def test_apply_still_raises_for_a_patch_that_does_not_fit():
    change = diffparse.parse(PATCH)[0]
    for _ in range(2):
        with pytest.raises(diffparse.PatchError):
            diffparse.apply("zzz\nb = 2\nc = 4\n", change)


def test_ctx_index_is_built_once_per_tests_list_and_rebuilt_for_a_new_one():
    tests = tiny_tests(12)
    a = rules.Ctx(None, tests, "/nowhere")
    b = rules.Ctx(None, tests, "/nowhere")
    assert a.by_file is b.by_file and a.all == (1 << 12) - 1 and a.by_file == {"tests/t.py": (1 << 12) - 1}
    grown = tests + [{"number": 12, "id": "tests/u.py::U::t", "file": "tests/u.py", "seconds": 0.1}]
    c = rules.Ctx(None, grown, "/nowhere")
    assert c.all == (1 << 13) - 1 and c.by_file == {"tests/t.py": (1 << 12) - 1, "tests/u.py": 1 << 12}
    d = rules.Ctx(None, [], "/nowhere")
    assert d.all == 0 and d.by_file == {}


def test_durations_are_computed_once_per_tests_list_and_match_the_formula():
    tests = tiny_tests(30)
    want = {t["number"]: round(t["seconds"] * 1000 + 1e-9) for t in tests}
    got = M._dur(tests)
    assert got == want and M._dur(tests) is got
    assert M._dur(tiny_tests(5)) == {n: want[n] for n in range(5)}
    assert M._pack({3, 7, 9}, tests) == M.pack.pack({n: want[n] for n in (3, 7, 9)})


def test_lazy_pool_starts_no_thread_until_a_job_is_submitted(monkeypatch):
    import concurrent.futures as cf

    made = []
    real = cf.ThreadPoolExecutor

    class Counting(real):
        def __init__(self, *a, **k):
            made.append(1)
            super().__init__(*a, **k)

    monkeypatch.setattr(cf, "ThreadPoolExecutor", Counting)
    with M._LazyPool():
        pass
    assert made == []
    with M._LazyPool() as pool:
        fut = pool.submit(lambda x: x + 1, 41)
        assert fut.result() == 42
    assert made == [1]


def test_the_image_compiles_the_standard_library_it_runs_on():
    text = (ROOT / "Dockerfile").read_text()
    assert "compileall" in text and "/usr/local/lib/python3.13" in text
    assert text.index("/usr/local/lib/python3.13") < text.index("ENV PYTHONDONTWRITEBYTECODE=1")


def test_memory_valve_collects_once_when_the_process_is_large_and_then_waits(monkeypatch):
    import gc

    calls = []
    monkeypatch.setattr(gc, "collect", lambda *a: calls.append(a) or 0)
    monkeypatch.setattr(M, "_RSS_LIMIT", [500 << 20])
    monkeypatch.setattr(M, "_rss_bytes", lambda: 400 << 20)
    M._collect_if_large()
    assert calls == []  # small: nothing to do
    monkeypatch.setattr(M, "_rss_bytes", lambda: 900 << 20)
    M._collect_if_large()
    assert len(calls) == 1 and M._RSS_LIMIT[0] == 1200 << 20  # waits for 300 MB more
    M._collect_if_large()
    assert len(calls) == 1


def test_rss_bytes_reads_this_process_on_linux():
    if not Path("/proc/self/statm").exists():
        pytest.skip("no /proc here")
    assert M._rss_bytes() > 1 << 20
