"""python -m minegen {generate,oracle,coverage,check,mutate}"""
import argparse
import json
import os
import shutil
import sys
import time

from . import baseline, llm
from .envfile import load_env_file
from .check import check_case
from .mutants import MUTANTS, evaluate
from .oracle import coverage_flags, expected_frequent, load_atoms
from .runner import petta_flags, run_case
from .validator import CATEGORIES, validate_all

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def find_repo(arg=None):
    """hyperon-miner checkout: flag, then HYPERON_MINER_DIR, then a sibling folder named hyperon-miner."""
    explicit = os.path.expanduser(arg or os.environ.get("HYPERON_MINER_DIR") or "") or None
    cands = [explicit] if explicit else [os.path.join(os.getcwd(), "..", "hyperon-miner"),
                                         os.path.join(HERE, "..", "hyperon-miner")]
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "experiments")):
            return os.path.abspath(c)
    sys.exit("hyperon-miner checkout not found. Pass --repo, set HYPERON_MINER_DIR, "
             "or put it next to minegen as ../hyperon-miner")


def _resolve_launcher(c):
    """A launcher can be a file, a folder holding run.sh, or a bare command name such as 'petta'."""
    c = os.path.expanduser(c)
    if os.path.isdir(c) and os.path.isfile(os.path.join(c, "run.sh")):
        c = os.path.join(c, "run.sh")
    if os.path.isfile(c):
        return os.path.abspath(c)
    return shutil.which(c)


def find_petta(arg=None):
    """PeTTa launcher: flag, then PETTA_RUN, then `petta` or `run.sh` on PATH, then ~/PeTTa/run.sh.
    It is called the way you call it by hand: <launcher> file.metta"""
    for label, c in (("--petta-run", arg), ("PETTA_RUN", os.environ.get("PETTA_RUN"))):
        if c:
            found = _resolve_launcher(c)
            if found:
                return found
            sys.exit(f"{label} is {c!r}, which is not a file, a command on PATH, or a folder containing run.sh")
    for name in ("petta", "run.sh"):
        found = shutil.which(name)
        if found:
            return found
    default = os.path.expanduser("~/PeTTa/run.sh")
    if os.path.isfile(default):
        return default
    sys.exit("PeTTa launcher not found (also looked for ~/PeTTa/run.sh). Put `petta` or run.sh on PATH, or set PETTA_RUN, "
             "or pass --petta-run. "
             "Note: shell aliases and unexported shell variables are invisible to Python; use an exported variable, "
             "a script on PATH, or PETTA_RUN.")


def _load(path):
    if not os.path.isfile(path):
        fixed = path.rstrip(".,;:")
        if fixed != path and os.path.isfile(fixed):
            print(f"note: using {fixed!r} (ignoring the trailing punctuation in {path!r})", file=sys.stderr)
            path = fixed
        else:
            found = sorted(f for f in os.listdir(".") if f.endswith(".json") and "case" in f)
            sys.exit(f"cases file not found: {path!r}" + (f". JSON files here: {', '.join(found)}" if found else ""))
    with open(path) as f:
        return json.load(f)["cases"]


def cmd_generate(a):
    if a.backend == "random":  # the baseline: random datasets in the same schema, no LLM, same validator
        cases = baseline.random_cases(a.count, a.seed)
        meta = {"backend": "random", "model": "none", "request_sha256": None, "from_cache": False, "seed": a.seed}
        with open(a.out, "w") as f:
            json.dump({"meta": meta, "cases": cases, "rejected": []}, f, indent=1)
        print(f"backend=random seed={a.seed}")
        print(f"{len(cases)} random cases -> {a.out}")
        return
    cats = a.categories.split(",") if a.categories else list(CATEGORIES)
    try:
        be = llm.make_backend(a.backend, a.model, os.path.join(HERE, "fixtures", "handwritten_proposals.json"))
        props, meta = llm.propose(be, cats, a.per_category, cache_dir=a.cache, replay_only=(a.backend == "replay"))
    except (RuntimeError, ValueError) as e:
        sys.exit(f"generate failed: {e}")
    cases, rejected = validate_all(props)
    with open(a.out, "w") as f:
        json.dump({"meta": meta, "cases": cases, "rejected": rejected}, f, indent=1)
    print(f"backend={meta['backend']} model={meta['model']} from_cache={meta['from_cache']}")
    print(f"{len(props)} proposed, {len(cases)} accepted, {len(rejected)} rejected -> {a.out}")
    for r in rejected:
        print("  rejected:", r["proposal"].get("name") if isinstance(r["proposal"], dict) else r["proposal"], r["reasons"])


def _list_cases(cases):
    for c in cases:
        flag = "  (uncertain)" if c.get("uncertain") else ""
        print(f"  {c['name']:34} {c['id']}  {c['edge_category']:20} minsup={c['minsup']} conj={c['conj_exp']}{flag}")


def cmd_oracle(a):
    cases = _load(a.cases)
    if not a.id:
        print(f"{len(cases)} cases in {a.cases}:")
        _list_cases(cases)
        return
    hits = [c for c in cases if a.id in (c["id"], c["name"])] or \
           [c for c in cases if c["name"].startswith(a.id) or c["id"].startswith(a.id)]
    if len(hits) != 1:
        print(f"{'no case' if not hits else 'several cases'} matches {a.id!r} in {a.cases}. Available:")
        _list_cases(cases)
        sys.exit(2)
    case = hits[0]
    db = load_atoms("\n".join(case["atoms"]))
    exp, scope = expected_frequent(db, case["minsup"], case["conj_exp"], case["depth"])
    print(f"{case['name']}  minsup={case['minsup']} conj_exp={case['conj_exp']} completeness={scope['completeness']}")
    print(f"atoms ({len(case['atoms'])}): " + " ".join(case["atoms"]))
    for k, (_, sup) in sorted(exp.items(), key=lambda kv: (-kv[1][1], kv[0])):
        print(f"  (supportOf {k} {sup})")


def _cov(cases):
    keys = ["boundary_at_minsup", "boundary_below_minsup", "self_link", "join_gain", "nothing_frequent", "single_atom", "nested"]
    tot = {k: 0 for k in keys}
    for c in cases:
        fl = coverage_flags(load_atoms("\n".join(c["atoms"])), c["minsup"])
        for k in keys:
            tot[k] += bool(fl[k])
    return tot


def cmd_coverage(a):
    suites = {"generated": _load(a.cases), "random": baseline.random_cases(a.n or len(_load(a.cases)), a.seed)}
    print(f"{'edge condition':26}" + "".join(f"{k:>12}" for k in suites))
    cov = {k: _cov(v) for k, v in suites.items()}
    for key in cov["generated"]:
        print(f"{key:26}" + "".join(f"{cov[k][key]:>12}" for k in suites))
    print(f"{'cases':26}" + "".join(f"{len(v):>12}" for v in suites.values()))


def _say(msg):
    print(msg, file=sys.stderr, flush=True)  # progress goes to stderr so stdout stays the report


def _write_duplicates_report(path, cases, results, cases_file):
    """Evidence file for a reviewer: for every case with duplicate output, the dataset, the settings and the
    duplicated patterns exactly as the miner printed them. Written only when duplicates exist."""
    by_id = {c["id"]: c for c in cases}
    bad = [r for r in results if r["duplicates"]]
    if not bad:
        return
    lines = ["# Duplicate conjunctions reported by the miner", "",
             "The miner's formatter is meant to return only unique conjunctions. Each block below shows a case, its",
             "input, and the patterns that were reported more than once, copied exactly as PeTTa printed them",
             f"(original variable names, original clause order). Source cases: `{cases_file}`.", ""]
    for r in bad:
        c = by_id[r["id"]]
        lines += [f"## {c['name']} ({c['edge_category']})", "",
                  f"- minsup: {c['minsup']}, conj_exp: {c['conj_exp']}, depth: {c['depth']}",
                  f"- data ({len(c['atoms'])} atoms): `{' '.join(c['atoms'])}`", ""]
        for d in r["duplicates"]:
            lines += [f"Reported {len(d['copies'])} times, support {d['support']}, kind: {d['kind']}.", "", "```"]
            lines += d["copies"]
            lines += ["```", ""]
    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")
    print(f"duplicates evidence written to {path}", file=sys.stderr)


def cmd_check(a):
    repo, petta = find_repo(a.repo), find_petta(a.petta_run)
    cases = _load(a.cases)
    if a.only:
        wanted = [w for w in a.only.split(",") if w]
        cases = [c for c in cases if any(c["name"].startswith(w) or c["id"].startswith(w) for w in wanted)]
        if not cases:
            sys.exit(f"--only {a.only!r} matches no case. Run `python3 -m minegen oracle` to list them.")
    out = []
    _say(f"repo: {repo} (read only: nothing is copied into or written to it). launcher: {petta} "
         f"(extra flags: {petta_flags() or 'none'})")
    for i, c in enumerate(cases, 1):
        _say(f"[{i}/{len(cases)}] running {c['name']} (timeout {a.timeout}s) ...")
        t0 = time.monotonic()
        run = run_case(repo, petta, c, a.timeout)
        _say(f"[{i}/{len(cases)}] finished in {time.monotonic() - t0:.1f}s (exit code {run.returncode}{', TIMED OUT' if run.timed_out else ''})")
        r = check_case(c, run)
        out.append(r)
        print(f"{r['verdict']:7}{c['name']}")
        for m in r["failures"] + r["notes"]:
            print("        ", m)
        for d in r["duplicates"]:
            print(f"         duplicated values, support {d['support']}, as the miner printed them:")
            for copy in d["copies"]:
                print(f"            {copy}")
        if a.show_output:
            raw = (run.stdout or "") + (("\n---- stderr ----\n" + run.stderr) if run.stderr else "")
            os.makedirs("check_raw", exist_ok=True)
            raw_path = os.path.join("check_raw", f"{c['name']}.out")
            with open(raw_path, "w") as f:
                f.write(raw)
            print(f"    ---- raw output: {len(raw)} chars, full text saved to {raw_path}; last 3000 chars: ----")
            print(raw[-3000:])
            print("    ---- end ----")
    with open(a.report, "w") as f:
        json.dump(out, f, indent=1)
    _write_duplicates_report(a.dup_report, cases, out, a.cases)
    print(f"summary: " + ", ".join(f"{v}={sum(1 for r in out if r['verdict'] == v)}" for v in ("PASS", "REVIEW", "FAIL")))
    sys.exit(1 if any(r["verdict"] == "FAIL" for r in out) else 0)


def cmd_mutate(a):
    gen = _load(a.cases)
    suites = {"generated": gen, "random": baseline.random_cases(len(gen), a.seed)}
    rows = evaluate(find_repo(a.repo), find_petta(a.petta_run), suites, a.mutants.split(",") if a.mutants else None,
                    ci_tests=not a.skip_repo_tests, timeout=a.timeout)
    json.dump(rows, open(a.report, "w"), indent=1)
    cols = list(rows[0]["caught_by"])
    print(f"{'mutant':8}" + "".join(f"{c:>18}" for c in cols) + "  description")
    for r in rows:
        print(f"{r['id']:8}" + "".join(f"{str(r['caught_by'][c]):>18}" for c in cols) + f"  {r['desc']}")


def main(argv=None):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--env-file", default=".env", help="KEY=VALUE file loaded first; real env vars win (default .env)")
    pre, _ = common.parse_known_args(argv)
    load_env_file(pre.env_file)
    env = os.environ.get

    p = argparse.ArgumentParser(prog="minegen")
    sub = p.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate", parents=[common])
    g.add_argument("--backend", choices=["fixture", "gemini", "anthropic", "replay", "random"], default=env("MINEGEN_BACKEND", "fixture"))
    g.add_argument("--model", help="overrides MINEGEN_MODEL")
    g.add_argument("--categories"); g.add_argument("--per-category", type=int, default=2)
    g.add_argument("--count", type=int, default=20, help="number of cases for --backend random")
    g.add_argument("--seed", type=int, default=0, help="seed for --backend random")
    g.add_argument("--cache", default=env("MINEGEN_CACHE", ".minegen_cache")); g.add_argument("--out", default="cases.json"); g.set_defaults(f=cmd_generate)
    o = sub.add_parser("oracle", parents=[common]); o.add_argument("--cases", default="cases.json"); o.add_argument("id", nargs="?", help="case name or id (prefix ok); omit to list cases"); o.set_defaults(f=cmd_oracle)
    c = sub.add_parser("coverage", parents=[common]); c.add_argument("--cases", default="cases.json"); c.add_argument("--n", type=int); c.add_argument("--seed", type=int, default=0); c.set_defaults(f=cmd_coverage)
    for name, fn in (("check", cmd_check), ("mutate", cmd_mutate)):
        s = sub.add_parser(name, parents=[common])
        s.add_argument("--repo", help="hyperon-miner checkout (default: HYPERON_MINER_DIR, else ../hyperon-miner)")
        s.add_argument("--petta-run", help="PeTTa launcher (default: PETTA_RUN, else `petta` or run.sh on PATH, else ~/PeTTa/run.sh)")
        s.add_argument("--cases", default="cases.json"); s.add_argument("--timeout", type=int, default=300)
        s.add_argument("--report", default=f"{name}_report.json"); s.set_defaults(f=fn)
        if name == "check":
            s.add_argument("--only", help="comma-separated case names or ids (prefix ok); run just these")
            s.add_argument("--show-output", action="store_true", help="print PeTTa's raw output for each case")
            s.add_argument("--dup-report", default="duplicates_report.md", help="evidence file written when duplicates are found")
        if name == "mutate":
            s.add_argument("--mutants"); s.add_argument("--skip-repo-tests", action="store_true"); s.add_argument("--seed", type=int, default=0)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()