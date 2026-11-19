"""Deterministic gate between the LLM and the test suite.

The LLM may only propose INPUTS (a dataset and miner parameters). Anything that smuggles in
expected results, variables, oversized data, or an unparseable atom is rejected here.
"""
import hashlib
import re

from .sexpr import ParseError, canon, is_var, parse_one, to_str

CATEGORIES = {
    "boundary_minsup": "a pattern whose support is exactly minsup, and another with exactly minsup-1",
    "self_link": "atoms whose arguments repeat, e.g. (Inheritance D D), so repeated-variable patterns matter",
    "join_gain": "a shared variable where the joined conjunction has MORE support than each clause (e.g. 3x3=9)",
    "nothing_frequent": "no pattern reaches minsup",
    "single_atom": "a database with one atom",
    "single_value_column": "every atom shares the same constant in one position",
    "ternary_links": "links with three arguments, e.g. (Evaluation likes ann bob)",
    "mixed_link_types": "several link types (Inheritance, Similarity) over the same symbols",
    "nested_atoms": "atoms that contain sub-expressions, e.g. (Inheritance (Concept cat) pet)",
    "duplicate_atoms": "the same atom listed more than once",
}
UNCERTAIN_CATEGORIES = {"nested_atoms", "duplicate_atoms"}  # semantics not pinned down by the repo
REQUIRED = {"name", "edge_category", "rationale", "atoms", "minsup", "depth", "conj_exp"}
SYMBOL = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]{0,30}$")
NUMBER = re.compile(r"^(0|[1-9][0-9]{0,5})$")  # non-negative integers are allowed leaves, but flagged uncertain
LIMITS = {"max_atoms": 30, "max_arity": 4, "max_nesting": 2, "max_minsup": 8}


def _depth(e):
    return 0 if isinstance(e, str) else 1 + max((_depth(x) for x in e), default=0)


def _leaf_problem(e):
    """Return a human-readable reason this expression is not an acceptable ground atom, else None."""
    if isinstance(e, str):
        if is_var(e):
            return f"contains the variable {e!r} (data must be ground)"
        if not (SYMBOL.match(e) or NUMBER.match(e)):
            return f"invalid symbol {e!r} (use letters, digits, _ or -, starting with a letter, or a whole number)"
        return None
    if len(e) < 2:
        return "every expression needs a head and at least one argument"
    for x in e:
        why = _leaf_problem(x)
        if why:
            return why
    return None


def _has_number(e):
    return bool(NUMBER.match(e)) if isinstance(e, str) else any(_has_number(x) for x in e)


def case_key(case):
    atoms = sorted(canon(parse_one(a)) for a in case["atoms"])
    return hashlib.sha1(repr((atoms, case["minsup"], case["depth"], case["conj_exp"])).encode()).hexdigest()


def validate(prop, seen_keys=frozenset()):
    """Return (case_or_None, reasons). reasons is empty when the proposal is accepted."""
    why = []
    if not isinstance(prop, dict):
        return None, ["proposal is not an object"]
    extra = set(prop) - REQUIRED
    if extra:
        why.append(f"unexpected keys {sorted(extra)} (proposals carry inputs only, never expected results)")
    missing = REQUIRED - set(prop)
    if missing:
        return None, why + [f"missing keys {sorted(missing)}"]
    if not (isinstance(prop["name"], str) and re.match(r"^[a-z0-9_]{3,40}$", prop["name"])):
        why.append("name must match [a-z0-9_]{3,40}")
    if prop["edge_category"] not in CATEGORIES:
        why.append(f"unknown edge_category {prop['edge_category']!r}")
    if not isinstance(prop["rationale"], str) or not prop["rationale"].strip():
        why.append("rationale must be a non-empty string")
    ms, dp, ce = prop["minsup"], prop["depth"], prop["conj_exp"]
    if not (isinstance(ms, int) and not isinstance(ms, bool) and 1 <= ms <= LIMITS["max_minsup"]):
        why.append(f"minsup must be an int in 1..{LIMITS['max_minsup']}")
    if dp != 0 or isinstance(dp, bool):
        why.append("depth must be 0 in this version")
    if not isinstance(ce, bool):
        why.append("conj_exp must be a boolean")
    atoms_in = prop["atoms"]
    atoms, nested, numeric = [], False, False
    if not isinstance(atoms_in, list) or not (1 <= len(atoms_in) <= LIMITS["max_atoms"]):
        why.append(f"atoms must be a list of 1..{LIMITS['max_atoms']} strings")
        atoms_in = []
    for s in atoms_in:
        try:
            e = parse_one(s) if isinstance(s, str) else None
        except ParseError as ex:
            why.append(f"atom {s!r}: {ex}")
            continue
        if not isinstance(e, tuple):
            why.append(f"atom {s!r}: must be an expression in parentheses")
            continue
        problem = _leaf_problem(e)
        if problem:
            why.append(f"atom {s!r}: {problem}")
            continue
        numeric = numeric or _has_number(e)
        if len(e) > LIMITS["max_arity"]:
            why.append(f"atom {s!r}: arity above {LIMITS['max_arity']}")
        if _depth(e) - 1 > LIMITS["max_nesting"]:
            why.append(f"atom {s!r}: nesting deeper than {LIMITS['max_nesting']}")
        nested = nested or any(isinstance(x, tuple) for x in e)
        atoms.append(to_str(e))
    uncertain = prop["edge_category"] in UNCERTAIN_CATEGORIES or nested or numeric  # numbers are grounded atoms in MeTTa
    if len(set(atoms)) < len(atoms):
        if prop["edge_category"] == "duplicate_atoms":
            uncertain = True
        else:
            why.append("duplicate atoms (only allowed for category duplicate_atoms)")
    if why:
        return None, why
    case = {"name": prop["name"], "edge_category": prop["edge_category"], "rationale": prop["rationale"].strip(),
            "atoms": atoms, "minsup": ms, "depth": 0, "conj_exp": ce, "uncertain": uncertain}
    key = case_key(case)
    if key in seen_keys:
        return None, ["duplicate of an existing case"]
    case["id"] = "gen_" + key[:8]
    return case, []


def validate_all(proposals, seen_keys=frozenset()):
    seen, accepted, rejected = set(seen_keys), [], []
    for p in proposals:
        case, why = validate(p, frozenset(seen))
        if case:
            seen.add(case_key(case))
            accepted.append(case)
        else:
            rejected.append({"proposal": p, "reasons": why})
    return accepted, rejected