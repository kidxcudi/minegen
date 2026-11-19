import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
from unittest import mock

from minegen import baseline, llm
from minegen.envfile import load_env_file, parse_env_text
from minegen.check import check_case, parse_output
from minegen.mutants import MUTANTS
from minegen.oracle import coverage_flags, expected_frequent, load_atoms, support
from minegen.runner import RunResult, render_driver, run_case, run_file
from minegen import cli
from minegen.sexpr import canon, find_forms, parse_one
from minegen.validator import validate, validate_all

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.environ.get("HYPERON_MINER_DIR", os.path.join(os.path.dirname(ROOT), "hyperon-miner"))
HAVE_REPO = os.path.isdir(os.path.join(REPO, "experiments"))
FIX = os.path.join(ROOT, "fixtures", "handwritten_proposals.json")


def read(path):
    with open(path) as f:
        return f.read()


def fixture_cases():
    cases, rej = validate_all(json.loads(read(FIX))["proposals"])
    assert not rej, rej
    return {c["name"]: c for c in cases}


def perfect_output(case):
    db = load_atoms("\n".join(case["atoms"]))
    exp, _ = expected_frequent(db, case["minsup"], case["conj_exp"])
    body = " ".join(f"(supportOf {k.replace('$v', '$_9')} {s})" for k, (_, s) in exp.items())
    return RunResult(0, f"==== Frequent miner started =====\n[{body}]\n", "")


class SexprTests(unittest.TestCase):
    def test_alpha_and_conjunction_order(self):
        self.assertEqual(canon(parse_one("(Inheritance $a $b)")), canon(parse_one("(Inheritance $q $r)")))
        self.assertNotEqual(canon(parse_one("(Inheritance $a $a)")), canon(parse_one("(Inheritance $a $b)")))
        a = parse_one("(, (Inheritance $x P) (Inheritance $x Q))")
        b = parse_one("(, (Inheritance $y Q) (Inheritance $y P))")
        self.assertEqual(canon(a), canon(b))

    def test_find_forms_in_noisy_output(self):
        out = '==== x ====\n[(supportOf (, (Inheritance $_1 a) (Inheritance $_1 b)) 5), (supportOf (Inheritance $_2 c) 10)]'
        self.assertEqual(len(find_forms(out, "supportOf")), 2)


class OracleTests(unittest.TestCase):
    def test_deck_worked_example(self):
        db = load_atoms("(Inheritance A B)(Inheritance A C)(Inheritance D D)")
        sup = {p: support(parse_one(p), db) for p in
               ["(Inheritance $X $Y)", "(Inheritance A $Y)", "(Inheritance D $Y)", "(Inheritance $Y $Y)", "(Inheritance $X B)"]}
        self.assertEqual(list(sup.values()), [3, 2, 1, 1, 1])

    def test_join_support_exceeds_clause_support(self):
        db = load_atoms("(Inheritance a1 B)(Inheritance a2 B)(Inheritance a3 B)(Inheritance B c1)(Inheritance B c2)(Inheritance B c3)")
        self.assertEqual(support(parse_one("(Inheritance $X $Y)"), db), 6)
        self.assertEqual(support(parse_one("(, (Inheritance $X $Y) (Inheritance $Y $Z))"), db), 9)

    @unittest.skipUnless(HAVE_REPO, "hyperon-miner checkout not found (set HYPERON_MINER_DIR)")
    def test_reproduces_repo_golden_output(self):
        exp = os.path.join(REPO, "experiments")
        db = load_atoms(read(os.path.join(exp, "data", "ugly_man_sodaDrinker.metta")))
        t = read(os.path.join(exp, "frequent-pattern-miner", "tests", "test-frequent-pattern-miner.metta"))
        i2, i1, i3 = t.index("!(let $result2"), t.index("!(let $result1"), t.index("!(let $result3")

        def golden(block):
            return {canon(parse_one(f)[1]): int(parse_one(f)[2]) for f in find_forms(block, "supportOf")}
        for conj, block in ((False, t[i1:i3]), (True, t[i2:i1])):
            exp, _ = expected_frequent(db, 5, conj)
            self.assertEqual({k: v[1] for k, v in exp.items()}, golden(block))   # full equality, both modes

    def test_conjunction_mode_returns_conjunctions_only(self):
        db = load_atoms("(Inheritance a1 B)(Inheritance a2 B)(Inheritance B c1)(Inheritance B c2)")
        singles, _ = expected_frequent(db, 2, False)
        conjs, _ = expected_frequent(db, 2, True)
        self.assertTrue(singles and all(not k.startswith("(,") for k in singles))
        self.assertTrue(conjs and all(k.startswith("(,") for k in conjs))

    def test_ground_patterns_are_expected_when_frequent(self):
        exp, _ = expected_frequent(load_atoms("(Root system init)"), 1, False)
        self.assertIn("(Root system init)", exp)
        exp, _ = expected_frequent(load_atoms("(Edge a b)(Edge a b)(Edge a b)"), 3, False)   # duplicates count
        self.assertEqual(exp["(Edge a b)"][1], 3)

    def test_joins_on_several_shared_variables(self):
        db = load_atoms("(Between a b c)(Between a b d)")
        exp, _ = expected_frequent(db, 2, True)
        self.assertEqual(exp["(, (Between $v0 $v1 $v2) (Between $v0 $v1 $v3))"][1], 4)


# The miner's real result line for a one-atom database, copied from a run on 19 Nov 2026. It has 6 results,
# 4 unique conjunctions, and 2 of them printed twice (note the fresh variable ids).
REAL_SINGLE_ATOM_OUTPUT = (
    "((supportOf (, (Singleton element $_110728) (Singleton $_110746 $_110728)) 1) "
    "(supportOf (, (Singleton $_110800 $_110806) (Singleton $_110800 x)) 1) "
    "(supportOf (, (Singleton element $_110884) (Singleton $_110902 $_110884)) 1) "
    "(supportOf (, (Singleton $_110956 x) (Singleton $_110956 $_110986)) 1) "
    "(supportOf (, (Singleton $_111034 $_111040) (Singleton $_111034 $_111064)) 1) "
    "(supportOf (, (Singleton $_111112 $_111118) (Singleton $_111136 $_111118)) 1)) \n")


class RealMinerOutputTests(unittest.TestCase):
    def test_real_single_atom_output_has_exactly_two_duplicates_and_nothing_else_wrong(self):
        case = {"id": "x", "name": "single_atom", "edge_category": "single_atom", "atoms": ["(Singleton element x)"],
                "minsup": 1, "depth": 0, "conj_exp": True, "uncertain": False}
        r = check_case(case, RunResult(0, "\"==== Formating  started =====\"\ntrue\n" + REAL_SINGLE_ATOM_OUTPUT, ""))
        self.assertEqual((r["reported"], r["expected"]), (4, 4))                # no missing, no extra patterns
        self.assertEqual(sorted(d["kind"] for d in r["duplicates"]),
                         ["identical up to variable names", "the same clauses in a different order"])
        self.assertTrue(all(len(d["copies"]) == 2 for d in r["duplicates"]))
        self.assertEqual(r["verdict"], "FAIL")

    def test_miner_substitution_can_repeat_a_variable_in_a_clause(self):
        db = load_atoms("(Ref n n)(Ref n a)(Ref n b)")
        exp, _ = expected_frequent(db, 2, True)
        self.assertIn(canon(parse_one("(, (Ref $x $x) (Ref $x $y))")), exp)    # seen in real miner output


class ValidatorTests(unittest.TestCase):
    base = {"name": "case_one", "edge_category": "self_link", "rationale": "r", "atoms": ["(Inheritance A B)"],
            "minsup": 1, "depth": 0, "conj_exp": False}

    def test_fixtures_accepted(self):
        self.assertEqual(len(fixture_cases()), 11)

    def test_rejections(self):
        def bad(**kw):
            case, why = validate({**self.base, **kw})
            self.assertIsNone(case)
            return why
        self.assertTrue(any("expected results" in w for w in bad(expected="x")))
        bad(atoms=["(Inheritance $X B)"])
        bad(atoms=["(Inheritance A B"])
        bad(atoms=["Inheritance"])
        bad(atoms=["(Inheritance A B)"] * 2)
        bad(minsup=0)
        bad(depth=1)
        bad(edge_category="nope")
        bad(atoms=[f"(Inheritance a{i} B)" for i in range(31)])

    def test_numbers_allowed_but_uncertain_and_messages_are_accurate(self):
        # atoms taken from a real Gemini reply that the first validator wrongly rejected
        for atoms in (["(Score A 100)", "(Score B 100)", "(Score C 100)"],
                      ["(Eval (Func x) (Arg 1))", "(Eval (Func x) (Arg 2))", "(Eval (Func y) (Arg 1))"],
                      ["(Type X 1)", "(Type Y 2)"]):
            case, why = validate({**self.base, "atoms": atoms, "minsup": 2})
            self.assertEqual(why, [], atoms)
            self.assertTrue(case["uncertain"])
        _, why = validate({**self.base, "atoms": ["(Inheritance $X B)"]})
        self.assertIn("variable", why[0])
        _, why = validate({**self.base, "atoms": ["(Inheritance A b!)"]})
        self.assertIn("invalid symbol", why[0])
        _, why = validate({**self.base, "atoms": ["(Inheritance A 1.5)"]})
        self.assertIn("invalid symbol", why[0])
        _, why = validate({**self.base, "atoms": ["(Inheritance (X) B)"]})
        self.assertIn("head and at least one argument", why[0])
        # duplicates outside the duplicate category are still rejected
        _, why = validate({**self.base, "atoms": ["(Inheritance A A)", "(Inheritance A A)"]})
        self.assertIn("duplicate atoms", why[0])

    def test_dedup_against_seen(self):
        c, _ = validate(self.base)
        from minegen.validator import case_key
        self.assertEqual(validate(self.base, frozenset({case_key(c)}))[1], ["duplicate of an existing case"])


class CheckTests(unittest.TestCase):
    def test_correct_miner_passes_every_certain_case(self):
        for c in fixture_cases().values():
            if not c["uncertain"]:
                r = check_case(c, perfect_output(c))
                self.assertEqual(r["verdict"], "PASS", (c["name"], r))

    def test_boundary_bug_is_caught(self):
        c = fixture_cases()["exact_minsup_boundary"]
        db = load_atoms("\n".join(c["atoms"]))
        exp, _ = expected_frequent(db, c["minsup"], False)
        buggy = " ".join(f"(supportOf {k} {s})" for k, (_, s) in exp.items() if s > c["minsup"])  # '>' instead of '>='
        r = check_case(c, RunResult(0, f"[{buggy}]", ""))
        self.assertEqual(r["verdict"], "FAIL")
        self.assertTrue(any("missing frequent pattern" in f for f in r["failures"]))

    def test_wrong_count_below_minsup_duplicates_crash_timeout(self):
        c = fixture_cases()["exact_minsup_boundary"]
        good = perfect_output(c).stdout
        self.assertEqual(check_case(c, RunResult(0, good.replace(" 3)", " 4)"), ""))["verdict"], "FAIL")
        self.assertEqual(check_case(c, RunResult(0, good + "(supportOf (Inheritance $z Q) 2)", ""))["verdict"], "FAIL")
        self.assertEqual(check_case(c, RunResult(0, good.replace("]", " (supportOf (Inheritance $a $b) 5)]"), ""))["verdict"], "FAIL")
        self.assertEqual(check_case(c, RunResult(1, good, ""))["verdict"], "FAIL")
        self.assertEqual(check_case(c, RunResult(-1, "", "", True))["verdict"], "FAIL")

    def test_duplicate_reports_say_what_kind_of_duplicate(self):
        c = fixture_cases()["join_gain_three_by_three"]
        a = "(supportOf (, (Inheritance $_1 $_2) (Inheritance $_1 $_3)) 9)"
        b = "(supportOf (, (Inheritance $_4 $_6) (Inheritance $_4 $_5)) 9)"
        r = check_case(c, RunResult(0, f"[{a} {b}]", ""))
        self.assertTrue(any("2x" in f and "identical up to variable names" in f for f in r["failures"]), r["failures"])
        d = "(supportOf (, (Inheritance A $_1) (Inheritance $_2 $_1)) 3)"
        e = "(supportOf (, (Inheritance $_3 $_4) (Inheritance A $_4)) 3)"
        r = check_case(c, RunResult(0, f"[{d} {e}]", ""))
        self.assertTrue(any("different order" in f for f in r["failures"]), r["failures"])

    def test_duplicate_details_keep_the_raw_text_as_printed(self):
        c = fixture_cases()["join_gain_three_by_three"]
        a = "(supportOf (, (Inheritance $_17 $_18) (Inheritance $_17 $_19)) 9)"
        b = "(supportOf (, (Inheritance $_40  $_42) (Inheritance $_40 $_41)) 9)"
        r = check_case(c, RunResult(0, f"[{a} {b}]", ""))
        self.assertEqual(len(r["duplicates"]), 1)
        d = r["duplicates"][0]
        self.assertEqual(d["support"], 9)
        self.assertEqual(d["copies"], [a, b.replace("  ", " ")])                  # original variable names kept

    def test_empty_output_is_valid_when_nothing_is_frequent(self):
        c = fixture_cases()["nothing_reaches_minsup"]
        self.assertEqual(check_case(c, RunResult(0, "[]", ""))["verdict"], "PASS")

    def test_unverifiable_completeness_is_review_not_pass(self):
        c = fixture_cases()["nested_atoms"]
        self.assertEqual(check_case(c, RunResult(0, "[]", ""))["verdict"], "REVIEW")

    def test_uncertain_categories_downgrade_to_review(self):
        c = fixture_cases()["duplicate_atoms"]
        self.assertEqual(check_case(c, RunResult(0, "[]", ""))["verdict"], "REVIEW")

    def test_source_snippets_in_verbose_output_are_not_results(self):
        noisy = ("--> metta function -->\n(= (formatter $x) (supportOf $var $sup))\n"
                 "(= (g $y) (supportOf $pattern $cnt))\n"
                 "((supportOf (Inheritance $_1 P) 3) (supportOf (Inheritance $_2 $_3) 5))\n")
        found, errors = parse_output(noisy)
        self.assertEqual((len(found), errors), (2, []))

    def test_parse_output_reports_garbled_forms(self):
        _, errs = parse_output("(supportOf (Inheritance $a $b) notanumber)")
        self.assertTrue(errs)


class RunnerTests(unittest.TestCase):
    def test_driver_uses_absolute_paths_into_the_repo_and_scratch(self):
        c = fixture_cases()["join_gain_three_by_three"]
        d = render_driver(c, "/repo/experiments", "/scratch/data")
        self.assertIn("!(import! &self /repo/experiments/frequent-pattern-miner/frequent-pattern-miner-generic)", d)
        self.assertIn("!(import! &self /repo/experiments/utils/common-utils)", d)
        self.assertIn("!(import! &db /scratch/data)", d)
        self.assertNotIn("../", d)
        self.assertIn(f"&conjsp_{c['id']} 4 0 True))", d)
        self.assertLess(d.index("&db /scratch/data"), d.index("frequent-pattern-miner/valuation"))  # data before miner modules

    def test_paths_with_spaces_are_quoted(self):
        d = render_driver(fixture_cases()["single_atom_db"], "/my repo/experiments", "/tmp/x/data")
        self.assertIn('!(import! &self "/my repo/experiments/utils/common-utils")', d)
        self.assertIn("!(import! &db /tmp/x/data)", d)

    def test_run_case_never_touches_the_repo_and_cleans_its_scratch(self):
        c = fixture_cases()["exact_minsup_boundary"]
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fake_repo(os.path.join(tmp, "repo"))
            before = sorted(os.path.join(d, f) for d, _, fs in os.walk(repo) for f in fs)
            fake = _fake_petta(os.path.join(tmp, "launch"), body='dirname "$1"; ls "$(dirname "$1")"; echo \'[(supportOf (Inheritance $_1 P) 3)]\'')
            r = run_case(repo, fake, c, timeout=20)
            self.assertEqual(r.returncode, 0)
            lines = r.stdout.split("\n")
            scratch = lines[0]
            self.assertTrue(any(l.endswith("_data.metta") for l in lines))       # data file existed during the run
            self.assertIn("supportOf", r.stdout)
            self.assertFalse(os.path.exists(scratch))                            # scratch removed
            self.assertEqual(before, sorted(os.path.join(d, f) for d, _, fs in os.walk(repo) for f in fs))


class LlmTests(unittest.TestCase):
    class Stub:
        name, model = "stub", "stub-1"
        calls = 0

        def complete(self, system, user):
            self.calls += 1
            return "Here you go:\n```json\n" + json.dumps(json.loads(read(FIX))["proposals"][:2]) + "\n```"

    def test_parse_fenced_reply_and_cache_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            be = self.Stub()
            p1, m1 = llm.propose(be, ["self_link"], 1, cache_dir=tmp)
            p2, m2 = llm.propose(be, ["self_link"], 1, cache_dir=tmp)
            self.assertEqual(p1, p2)
            self.assertEqual(be.calls, 1)
            self.assertTrue(m2["from_cache"])
            p3, _ = llm.propose(None, ["self_link"], 1, cache_dir=tmp, replay_only=True)
            self.assertEqual(p1, p3)

    def test_prompt_never_asks_for_expected_results(self):
        system, user = llm.build_prompt(["self_link"], 1)
        self.assertIn("Never compute or state expected results", system)
        self.assertNotIn('"expected', user)

    def test_replay_without_cache_fails_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                llm.propose(None, ["self_link"], 1, cache_dir=tmp, replay_only=True)


class GeminiTests(unittest.TestCase):
    class Resp:
        def __init__(self, payload):
            self.b = json.dumps(payload).encode()
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False
        def read(self, *n):
            return self.b

    def test_request_shape_and_thought_parts_skipped(self):
        reply = {"candidates": [{"content": {"parts": [{"text": "hidden", "thought": True}, {"text": "[1,", }, {"text": "2]"}]}}]}
        with mock.patch("minegen.llm.urllib.request.urlopen", return_value=self.Resp(reply)) as uo:
            be = llm.GeminiBackend(model="gemini-test", api_key="k")
            self.assertEqual(be.complete("SYS", "USER"), "[1,2]")
        req = uo.call_args[0][0]
        self.assertTrue(req.full_url.endswith("/v1beta/models/gemini-test:generateContent"))
        self.assertEqual(req.get_header("X-goog-api-key"), "k")
        body = json.loads(req.data)
        self.assertEqual(body["systemInstruction"]["parts"][0]["text"], "SYS")
        self.assertEqual(body["contents"][0]["parts"][0]["text"], "USER")
        self.assertEqual(body["generationConfig"]["responseMimeType"], "application/json")

    def test_errors_are_readable(self):
        err = urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO(b'{"error":"model not found"}'))
        with mock.patch("minegen.llm.urllib.request.urlopen", side_effect=err):
            with self.assertRaisesRegex(RuntimeError, "404.*gemini-x.*model not found"):
                llm.GeminiBackend(model="gemini-x", api_key="k").complete("s", "u")
        with mock.patch("minegen.llm.urllib.request.urlopen", return_value=self.Resp({"candidates": []})):
            with self.assertRaisesRegex(RuntimeError, "no candidates"):
                llm.GeminiBackend(api_key="k").complete("s", "u")

    def test_missing_key_and_bad_model_name(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "GEMINI_API_KEY"):
                llm.GeminiBackend()
        with self.assertRaisesRegex(RuntimeError, "invalid model"):
            llm.GeminiBackend(model="../x", api_key="k")

    def test_gemini_reply_flows_through_propose_and_validator(self):
        props = json.loads(read(FIX))["proposals"][:3]
        reply = {"candidates": [{"content": {"parts": [{"text": json.dumps(props)}]}}]}
        with mock.patch("minegen.llm.urllib.request.urlopen", return_value=self.Resp(reply)):
            got, meta = llm.propose(llm.GeminiBackend(model="gemini-test", api_key="k"), ["self_link"], 1)
        cases, rej = validate_all(got)
        self.assertEqual((len(cases), len(rej), meta["backend"]), (3, 0, "gemini"))


class EnvTests(unittest.TestCase):
    def test_parse_env_text(self):
        got = parse_env_text('# c\nA=1\nexport B="two words"\nC=x # trailing\nD=\nE=\'q\'\nnoequals\n')
        self.assertEqual(got, {"A": "1", "B": "two words", "C": "x", "D": "", "E": "q"})

    def test_env_file_never_overrides_real_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, ".env")
            with open(path, "w") as f:
                f.write("X1=file\nX2=file\nX3=\n")
            environ = {"X1": "real"}
            self.assertEqual(load_env_file(path, environ), ["X2"])
            self.assertEqual(environ, {"X1": "real", "X2": "file"})
            self.assertEqual(load_env_file(os.path.join(tmp, "missing"), environ), [])

    def test_model_and_key_precedence(self):
        env = {"MINEGEN_MODEL": "m-env", "MINEGEN_API_KEY": "generic", "GEMINI_API_KEY": "specific"}
        with mock.patch.dict(os.environ, env, clear=True):
            be = llm.GeminiBackend()
            self.assertEqual((be.model, be.key), ("m-env", "generic"))
            self.assertEqual(llm.GeminiBackend(model="m-arg", api_key="k-arg").model, "m-arg")
            self.assertEqual(llm.GeminiBackend(model="m-arg", api_key="k-arg").key, "k-arg")
        with mock.patch.dict(os.environ, {"GOOGLE_API_KEY": "g"}, clear=True):
            be = llm.GeminiBackend()
            self.assertEqual((be.model, be.key), ("gemini-2.5-flash", "g"))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "a"}, clear=True):
            be = llm.AnthropicBackend()
            self.assertEqual((be.model, be.key), ("claude-sonnet-5-5", "a"))
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "MINEGEN_API_KEY"):
                llm.AnthropicBackend()

    def test_key_never_reaches_cache_or_cases(self):
        reply = {"candidates": [{"content": {"parts": [{"text": json.dumps(json.loads(read(FIX))["proposals"][:1])}]}}]}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("minegen.llm.urllib.request.urlopen", return_value=GeminiTests.Resp(reply)):
            be = llm.GeminiBackend(model="gemini-test", api_key="SECRET-KEY-123")
            _, meta = llm.propose(be, ["self_link"], 1, cache_dir=tmp)
            blob = "".join(read(os.path.join(tmp, n)) for n in os.listdir(tmp)) + json.dumps(meta)
            self.assertNotIn("SECRET-KEY-123", blob)

    def test_fixture_backend_bypasses_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            llm.propose(llm.FixtureBackend(FIX), ["self_link"], 1, cache_dir=tmp)
            self.assertEqual(os.listdir(tmp), [])

    def test_make_backend(self):
        self.assertIsNone(llm.make_backend("replay"))
        self.assertEqual(llm.make_backend("fixture", fixture_path=FIX).name, "fixture")
        with self.assertRaises(ValueError):
            llm.make_backend("nope")


def _fake_repo(root):
    for sub in ("experiments/data", "experiments/frequent-pattern-miner/tests", "experiments/utils"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    return root


def _fake_petta(path, shebang=True, body='echo "[]"'):
    with open(path, "w") as f:
        f.write(("#!/bin/sh\n" if shebang else "") + body + "\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
    return path


class RandomBackendTests(unittest.TestCase):
    def test_random_backend_writes_valid_deterministic_cases(self):
        with tempfile.TemporaryDirectory() as tmp:
            outs = []
            for name in ("a.json", "b.json"):
                path = os.path.join(tmp, name)
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.main(["generate", "--backend", "random", "--count", "12", "--seed", "5",
                              "--env-file", os.path.join(tmp, "n"), "--out", path])
                outs.append(json.loads(read(path)))
            self.assertEqual(len(outs[0]["cases"]), 12)
            self.assertEqual([c["id"] for c in outs[0]["cases"]], [c["id"] for c in outs[1]["cases"]])
            self.assertEqual(outs[0]["meta"]["backend"], "random")
            self.assertTrue(all(c["edge_category"] == "random" for c in outs[0]["cases"]))


class OracleCliTests(unittest.TestCase):
    def test_oracle_lists_cases_and_fails_helpfully(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = os.path.join(tmp, "cases.json")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["generate", "--backend", "fixture", "--env-file", os.path.join(tmp, "none"), "--out", cases])
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["oracle", "--env-file", os.path.join(tmp, "none"), "--cases", cases])
            self.assertIn("exact_minsup_boundary", buf.getvalue())
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cli.main(["oracle", "--env-file", os.path.join(tmp, "none"), "--cases", cases, "exact_minsup"])  # prefix
            self.assertIn("supportOf", buf.getvalue())
            self.assertIn("atoms (5): (Inheritance a1 P)", buf.getvalue())
            buf = io.StringIO()
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(buf):
                cli.main(["oracle", "--env-file", os.path.join(tmp, "none"), "--cases", cases, "no_such_case"])
            self.assertEqual(ctx.exception.code, 2)
            self.assertIn("Available", buf.getvalue())


class CheckFlagsTests(unittest.TestCase):
    def _run(self, tmp, extra, body='echo "RAW-MARKER [ ]"'):
        repo = _fake_repo(os.path.join(tmp, "repo"))
        run_sh = _fake_petta(os.path.join(tmp, "run.sh"), body=body)
        cases = os.path.join(tmp, "cases.json")
        none = os.path.join(tmp, "none")
        with mock.patch.dict(os.environ, {"HYPERON_MINER_DIR": repo, "PETTA_RUN": run_sh}):
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["generate", "--env-file", none, "--out", cases])
            buf, err = io.StringIO(), io.StringIO()
            with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                cli.main(["check", "--env-file", none, "--cases", cases, "--report", os.path.join(tmp, "r.json")] + extra)
        return ctx.exception.code, buf.getvalue(), err.getvalue(), cases

    def test_only_filters_and_show_output_prints_raw_stdout(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                code, out, _, _ = self._run(tmp, ["--only", "exact_minsup,single_atom", "--show-output"])
            finally:
                os.chdir(cwd)
            self.assertEqual(out.count("RAW-MARKER"), 2)
            self.assertTrue(os.path.exists(os.path.join(tmp, "check_raw", "exact_minsup_boundary.out")))
            self.assertIn("summary:", out)
            self.assertNotIn("deck_worked_example", out)

    def test_duplicates_report_is_written_for_a_reviewer(self):
        dup = ("echo '[(supportOf (, (Inheritance $_1 $_2) (Inheritance $_1 $_3)) 5) "
               "(supportOf (, (Inheritance $_7 $_8) (Inheritance $_7 $_9)) 5)]'")  # single quotes: keep $_ literal
        with tempfile.TemporaryDirectory() as tmp:
            report_md = os.path.join(tmp, "dups.md")
            code, out, err, _ = self._run(tmp, ["--only", "exact_minsup", "--dup-report", report_md], body=dup)
            text = open(report_md).read()
            self.assertIn("exact_minsup_boundary", text)
            self.assertIn("(Inheritance a1 P)", text)                              # the dataset is included
            self.assertIn("$_7", text)                                             # raw variable names are kept
            self.assertIn("duplicated values, support 5", out)
            self.assertIn("duplicates evidence written", err)

    def test_no_duplicates_means_no_evidence_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            report_md = os.path.join(tmp, "dups.md")
            self._run(tmp, ["--only", "single_atom", "--dup-report", report_md])
            self.assertFalse(os.path.exists(report_md))

    def test_no_match_for_only_is_a_clean_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(SystemExit, "matches no case"):
                with mock.patch.dict(os.environ, {"HYPERON_MINER_DIR": _fake_repo(os.path.join(tmp, "r")),
                                                  "PETTA_RUN": _fake_petta(os.path.join(tmp, "run.sh"))}):
                    cases = os.path.join(tmp, "cases.json")
                    with contextlib.redirect_stdout(io.StringIO()):
                        cli.main(["generate", "--env-file", os.path.join(tmp, "n"), "--out", cases])
                    cli.main(["check", "--env-file", os.path.join(tmp, "n"), "--cases", cases, "--only", "zzz"])

    def test_trailing_punctuation_in_cases_path_is_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cases.json")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.main(["generate", "--env-file", os.path.join(tmp, "n"), "--out", path])
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(len(cli._load(path + ".")), 11)
            self.assertIn("trailing punctuation", err.getvalue())
            with self.assertRaisesRegex(SystemExit, "cases file not found"):
                cli._load(os.path.join(tmp, "missing.json"))


class DiscoveryTests(unittest.TestCase):
    def test_repo_resolution_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            good = _fake_repo(os.path.join(tmp, "r1"))
            other = _fake_repo(os.path.join(tmp, "r2"))
            with mock.patch.dict(os.environ, {"HYPERON_MINER_DIR": other}):
                self.assertEqual(cli.find_repo(good), os.path.abspath(good))   # flag beats env
                self.assertEqual(cli.find_repo(), os.path.abspath(other))
            with mock.patch.dict(os.environ, {}, clear=True):
                os.makedirs(os.path.join(tmp, "work"))
                _fake_repo(os.path.join(tmp, "hyperon-miner"))
                cwd = os.getcwd()
                try:
                    os.chdir(os.path.join(tmp, "work"))   # sibling ../hyperon-miner
                    self.assertEqual(os.path.realpath(cli.find_repo()), os.path.realpath(os.path.join(tmp, "hyperon-miner")))
                finally:
                    os.chdir(cwd)
            with mock.patch.dict(os.environ, {"HYPERON_MINER_DIR": os.path.join(tmp, "nope")}):
                with self.assertRaises(SystemExit):
                    cli.find_repo()

    def test_petta_resolution_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = _fake_petta(os.path.join(tmp, "a.sh"))
            os.makedirs(os.path.join(tmp, "PeTTa"))
            b = _fake_petta(os.path.join(tmp, "PeTTa", "run.sh"))
            with mock.patch.dict(os.environ, {"PETTA_RUN": a}, clear=True):
                self.assertEqual(cli.find_petta(b), os.path.abspath(b))               # flag beats env
                self.assertEqual(cli.find_petta(), os.path.abspath(a))
            with mock.patch.dict(os.environ, {"PETTA_RUN": os.path.join(tmp, "PeTTa")}, clear=True):
                self.assertEqual(cli.find_petta(), os.path.abspath(b))                # folder with run.sh
            with mock.patch.dict(os.environ, {"PATH": os.path.join(tmp, "PeTTa")}, clear=True):
                self.assertEqual(cli.find_petta(), b)                                 # run.sh on PATH
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            petta = _fake_petta(os.path.join(bindir, "petta"))
            _fake_petta(os.path.join(bindir, "run.sh"))
            with mock.patch.dict(os.environ, {"PATH": bindir}, clear=True):
                self.assertEqual(cli.find_petta(), petta)                             # `petta` beats run.sh on PATH
            with mock.patch.dict(os.environ, {"PATH": bindir, "PETTA_RUN": "petta"}, clear=True):
                self.assertEqual(cli.find_petta(), petta)                             # bare command name
            empty_home = os.path.join(tmp, "emptyhome")
            os.makedirs(empty_home)
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent", "HOME": empty_home}, clear=True):
                with self.assertRaisesRegex(SystemExit, "aliases"):
                    cli.find_petta()
            home = os.path.join(tmp, "home")
            os.makedirs(os.path.join(home, "PeTTa"))
            default = _fake_petta(os.path.join(home, "PeTTa", "run.sh"))
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent", "HOME": home}, clear=True):
                self.assertEqual(cli.find_petta(), default)                           # ~/PeTTa/run.sh fallback
            with mock.patch.dict(os.environ, {"PATH": "/nonexistent", "HOME": home, "PETTA_RUN": "~/PeTTa/run.sh"}, clear=True):
                self.assertEqual(cli.find_petta(), default)                           # ~ is expanded
            with mock.patch.dict(os.environ, {"PETTA_RUN": os.path.join(tmp, "missing")}, clear=True):
                with self.assertRaisesRegex(SystemExit, "PETTA_RUN"):
                    cli.find_petta()

    def test_silent_flag_is_passed_after_the_file_and_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = os.path.join(tmp, "x.metta")
            open(drv, "w").close()
            echo = _fake_petta(os.path.join(tmp, "launch"), body='echo "ARGS:$#:$2"')
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PETTA_FLAGS", None)
                self.assertEqual(run_file(echo, drv, 20).stdout.strip(), "ARGS:2:silent")
            with mock.patch.dict(os.environ, {"PETTA_FLAGS": ""}):
                self.assertEqual(run_file(echo, drv, 20).stdout.strip(), "ARGS:1:")
            with mock.patch.dict(os.environ, {"PETTA_FLAGS": "--silent -x"}):
                self.assertEqual(run_file(echo, drv, 20).stdout.strip(), "ARGS:3:--silent")

    def test_launcher_with_and_without_shebang(self):
        with tempfile.TemporaryDirectory() as tmp:
            drv = os.path.join(tmp, "x.metta")
            open(drv, "w").close()
            for shebang in (True, False):
                r = run_file(_fake_petta(os.path.join(tmp, f"l{shebang}.sh"), shebang=shebang, body='echo "[ok]"'), drv, 20)
                self.assertEqual((r.returncode, r.stdout.strip()), (0, "[ok]"))

    def test_run_sh_launcher_runs_from_its_own_folder_like_the_users_petta_function(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "PeTTa"))
            os.makedirs(os.path.join(tmp, "elsewhere"))
            drv = os.path.join(tmp, "elsewhere", "x.metta")
            open(drv, "w").close()
            run_sh = _fake_petta(os.path.join(tmp, "PeTTa", "run.sh"), body='pwd; echo "$1"')
            r = run_file(run_sh, drv, 20)
            lines = r.stdout.split()
            self.assertEqual(os.path.realpath(lines[0]), os.path.realpath(os.path.join(tmp, "PeTTa")))
            self.assertEqual(lines[1], os.path.abspath(drv))                          # absolute file path
            other = _fake_petta(os.path.join(tmp, "launch"), body="pwd")
            self.assertEqual(os.path.realpath(run_file(other, drv, 20).stdout.strip()),
                             os.path.realpath(os.path.join(tmp, "elsewhere")))         # other launchers: file's folder

    def test_check_never_writes_into_the_checkout(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = _fake_repo(os.path.join(tmp, "repo"))
            run_sh = _fake_petta(os.path.join(tmp, "run.sh"))
            cases, report = os.path.join(tmp, "cases.json"), os.path.join(tmp, "r.json")
            with mock.patch.dict(os.environ, {"HYPERON_MINER_DIR": repo, "PETTA_RUN": run_sh}):
                with contextlib.redirect_stdout(io.StringIO()):
                    cli.main(["generate", "--env-file", os.path.join(tmp, "none"), "--out", cases])
                before = sorted(os.path.join(d, f) for d, _, fs in os.walk(repo) for f in fs)
                with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
                    cli.main(["check", "--env-file", os.path.join(tmp, "none"), "--cases", cases, "--report", report])
                after = sorted(os.path.join(d, f) for d, _, fs in os.walk(repo) for f in fs)
            self.assertEqual(ctx.exception.code, 1)   # an empty miner must fail
            self.assertEqual(before, after)
            self.assertTrue(os.path.exists(report))


class BaselineAndMutantTests(unittest.TestCase):
    def test_random_baseline_is_valid_and_deterministic(self):
        a, b = baseline.random_cases(8, seed=3), baseline.random_cases(8, seed=3)
        self.assertEqual(len(a), 8)
        self.assertEqual([c["id"] for c in a], [c["id"] for c in b])

    def test_coverage_flags(self):
        f = coverage_flags(load_atoms("(Inheritance A B)(Inheritance A C)(Inheritance D D)"), 2)
        self.assertTrue(f["self_link"] and f["boundary_at_minsup"])

    @unittest.skipUnless(HAVE_REPO, "hyperon-miner checkout not found")
    def test_every_mutation_target_exists_exactly_once(self):
        for m in MUTANTS:
            self.assertEqual(read(os.path.join(REPO, m["file"])).count(m["old"]), 1, m["id"])


if __name__ == "__main__":
    unittest.main()