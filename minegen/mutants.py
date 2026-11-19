"""Seeded bugs. Each is a single exact-string edit; apply() refuses to run if the target text moved."""
import os
import shutil
import tempfile
from concurrent.futures import ThreadPoolExecutor

from .check import check_case
from .runner import run_case, run_file

FM = "experiments/frequent-pattern-miner/"
MUTANTS = [
    {"id": "M1", "file": FM + "candidate.metta", "old": "(if (>= $result $minsup)", "new": "(if (> $result $minsup)",
     "desc": "single-clause minsup check off by one (>= to >)"},
    {"id": "M2", "file": "experiments/utils/common-utils.metta", "old": "(if (>= $sup $ms) True False)",
     "new": "(if (> $sup $ms) True False)", "desc": "sup-eval boundary off by one (>= to >)"},
    {"id": "M3", "file": FM + "conjunction-expansion.metta", "old": "(checker $db $npat $ms)",
     "new": "(checker $db $npat 0)", "desc": "conjunction expansion ignores minsup"},
    {"id": "M4", "file": "experiments/utils/common-utils.metta", "old": "(let $result (collapse (match $db $ptrn $ptrn))",
     "new": "(let $result (unique-atom (collapse (match $db $ptrn $ptrn)))",
     "desc": "counter dedups matches (only visible with duplicate atoms; may be equivalent)"},
    {"id": "M5", "file": FM + "candidate.metta", "old": "(if (>= $result $minsup)", "new": "(if (<= $result $minsup)",
     "desc": "control: minsup check inverted (every suite should catch this)"},
]
CI_TESTS = ["test-isurp-old.metta", "test-common-utils.metta", "test-isurp.metta", "test-est-tv.metta",
            "test-surp.metta", "test-emp-tv.metta", "test-jsd.metta", "test-pattern-miner.metta"]
EXTRA_TESTS = ["test-frequent-pattern-miner.metta"]  # exists in the repo but is not whitelisted in CI


def apply(repo, m):
    path = os.path.join(repo, m["file"])
    text = open(path).read()
    if text.count(m["old"]) != 1:
        raise RuntimeError(f"{m['id']}: target text found {text.count(m['old'])} times in {m['file']}")
    open(path, "w").write(text.replace(m["old"], m["new"]))


def _find_tests(repo, names):
    out = {}
    for root, _, files in os.walk(os.path.join(repo, "experiments")):
        for n in files:
            if n in names:
                out[n] = os.path.join(root, n)
    return out


def _suite(repo, petta_run, tests, timeout, workers):
    def one(item):
        name, path = item
        r = run_file(petta_run, path, timeout)
        bad = r.timed_out or r.returncode != 0 or "\u274c" in (r.stdout or "") + (r.stderr or "")
        return name, bad
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, tests.items()))


def _gen(repo, petta_run, cases, timeout, workers):
    with ThreadPoolExecutor(max_workers=workers) as ex:
        res = list(ex.map(lambda c: check_case(c, run_case(repo, petta_run, c, timeout)), cases))
    return {r["id"]: r["verdict"] for r in res}


def evaluate(repo, petta_run, suites, mutant_ids=None, ci_tests=True, timeout=300, workers=2):
    """suites: {label: [cases]}. Returns rows with which suites caught each mutant.
    A mutant is 'caught' by a suite when at least one of its tests/cases fails (REVIEW does not count)."""
    tests = _find_tests(repo, set(CI_TESTS + EXTRA_TESTS)) if ci_tests else {}
    rows = []
    work = tempfile.mkdtemp(prefix="minegen_")
    try:
        base = os.path.join(work, "base")  # one read of the (possibly slow) source drive; mutants copy from here
        shutil.copytree(repo, base, ignore=shutil.ignore_patterns(".git"))
        for m in [{"id": "clean", "desc": "unmodified miner"}] + [x for x in MUTANTS if not mutant_ids or x["id"] in mutant_ids]:
            copy = os.path.join(work, m["id"])
            shutil.copytree(base, copy)
            if m["id"] != "clean":
                apply(copy, m)
            row = {"id": m["id"], "desc": m["desc"], "caught_by": {}, "detail": {}}
            if tests:
                r = _suite(copy, petta_run, _find_tests(copy, set(tests)), timeout, workers)
                row["detail"]["repo_tests"] = r
                row["caught_by"]["ci_whitelist"] = any(v for k, v in r.items() if k in CI_TESTS)
                row["caught_by"]["repo_freq_test"] = any(v for k, v in r.items() if k in EXTRA_TESTS)
            for label, cases in suites.items():
                v = _gen(copy, petta_run, cases, timeout, workers)
                row["detail"][label] = v
                row["caught_by"][label] = sum(1 for x in v.values() if x == "FAIL")
            rows.append(row)
            shutil.rmtree(copy, ignore_errors=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return rows