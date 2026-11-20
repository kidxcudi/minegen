"""Compare a miner run against the oracle. Verdicts: PASS, FAIL, REVIEW."""
from .oracle import expected_frequent, load_atoms, support
from .sexpr import ParseError, _rename, canon, find_forms, is_var, parse_one, to_str


def parse_output(text):
    """Pull every (supportOf <pattern> <n>) out of raw PeTTa stdout, whatever surrounds it."""
    found, errors = [], []
    for f in find_forms(text, "supportOf"):
        try:
            e = parse_one(f)
            if len(e) != 3:
                raise ParseError("bad arity")
            if is_var(e[2]):  # a source-code snippet such as (supportOf $pattern $cnt), not a result
                continue
            found.append((e[1], int(e[2]), " ".join(f.split())))
        except (ParseError, ValueError):
            errors.append(f"unparseable result: {f[:80]}")
    return found, errors


def check_case(case, run):
    """`run` needs .returncode, .stdout, .stderr, .timed_out (see runner.RunResult)."""
    fails, notes = [], []
    text = (run.stdout or "") + "\n" + (run.stderr or "")
    if run.timed_out:
        fails.append("timeout")
    elif run.returncode != 0:
        fails.append(f"miner exited with code {run.returncode}")
    if "\u274c" in text:  # the repo's own failure marker
        fails.append("run printed a failure marker")
    db = load_atoms("\n".join(case["atoms"]))
    reported, errors = parse_output(text)
    fails += errors
    seen, forms, raws = {}, {}, {}
    for pat, n, raw in reported:
        key = canon(pat)
        ordered = to_str(_rename(pat, {}))  # alpha-renamed but keeps the miner's clause order
        forms.setdefault(key, []).append(ordered)
        raws.setdefault(key, []).append(raw)  # exactly as the miner printed it, original variable names
        seen[key] = n
        true = support(pat, db)
        if n != true:
            fails.append(f"support mismatch for {key}: miner says {n}, oracle says {true}")
        if n < case["minsup"]:
            fails.append(f"below minsup: {key} has support {n}, minsup is {case['minsup']}")
    duplicates = []
    for key, orders in forms.items():
        if len(orders) > 1:
            kind = ("the same clauses in a different order" if len(set(orders)) > 1
                    else "identical up to variable names")
            fails.append(f"duplicate pattern reported {len(orders)}x ({kind}): {key}")
            duplicates.append({"pattern": key, "kind": kind, "support": seen[key], "copies": raws[key]})
    expected, scope = expected_frequent(db, case["minsup"], case["conj_exp"], case["depth"])
    if scope["completeness"]:
        for key, (_, s) in expected.items():
            if key not in seen:
                fails.append(f"missing frequent pattern: {key} (oracle support {s})")
        for key in seen:
            if key not in expected:
                notes.append(f"extra pattern outside the oracle's space: {key}")
    else:
        notes.append("completeness not checked (nested data)")
    if fails and case.get("uncertain"):
        verdict = "REVIEW"
        notes = fails + notes
        fails = []
    elif fails:
        verdict = "FAIL"
    elif not scope["completeness"] or any(n.startswith("extra pattern") for n in notes):
        verdict = "REVIEW"  # either the oracle cannot confirm completeness, or the miner went beyond its space
    else:
        verdict = "PASS"
    return {"id": case["id"], "name": case["name"], "verdict": verdict, "failures": fails, "notes": notes,
            "reported": len(seen), "expected": len(expected) if scope["completeness"] else None,
            "duplicates": duplicates}