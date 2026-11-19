"""LLM adapter. The model proposes inputs; it never computes or asserts expected results.

Backends: Gemini HTTP API, Anthropic HTTP API, cache replay (offline, deterministic),
and a fixture backend that serves hand-written proposals so the pipeline runs with no model.

Settings, highest priority first: explicit argument, then environment, then default.
  model:   --model, MINEGEN_MODEL, built-in default
  api key: MINEGEN_API_KEY, then GEMINI_API_KEY or GOOGLE_API_KEY (Gemini) / ANTHROPIC_API_KEY (Anthropic)
Keys are never written to the cache, to cases.json, or to any log.
"""
import hashlib
import json
import os
import re
import urllib.error
import urllib.request

from .validator import CATEGORIES

def env_first(*names):
    """First non-empty environment variable among names, else None."""
    for n in names:
        v = os.environ.get(n)
        if v:
            return v
    return None


SYSTEM = ("You write edge-case datasets for regression-testing a frequent-pattern miner that runs over "
          "a MeTTa Atomspace. You propose INPUTS only. Never compute or state expected results; a separate "
          "program computes them. Reply with a JSON array and nothing else.")

SPEC = """Background:
- Data is a list of ground atoms such as (Inheritance Allen human). No variables in data.
- A pattern is an atom with variables, e.g. (Inheritance $X human). Its support is the number of
  ways it matches the data. For a conjunction (, c1 c2) it is the number of joint bindings, so it can
  exceed the support of each clause (3 links A->B and 3 links B->C give a join of 9).
- The miner starts from the most abstract pattern, specializes it, and keeps patterns with
  support >= minsup (pruning is safe because a specialization never matches more than its parent).
- conj_exp=true also joins frequent patterns that share a variable. depth 0 means at most 2 clauses.
Constraints: 1 to 30 atoms; symbols match [A-Za-z][A-Za-z0-9_-]* (whole numbers such as 100 are also allowed);
arity at most 4 including the head;
minsup 1..8; depth must be 0.
Each proposal is an object with exactly these keys:
  name (snake_case), edge_category, rationale (one sentence on what bug it could expose),
  atoms (array of strings, one atom each), minsup (int), depth (0), conj_exp (bool).
Example:
[{"name":"deck_worked_example","edge_category":"self_link","rationale":"D->D binds both variables to one value",
  "atoms":["(Inheritance A B)","(Inheritance A C)","(Inheritance D D)"],"minsup":2,"depth":0,"conj_exp":false}]
"""


def build_prompt(categories, per_category):
    cats = "\n".join(f"- {c}: {CATEGORIES[c]}" for c in categories)
    user = (f"{SPEC}\nPropose {per_category} distinct test cases for each category below. "
            f"Vary dataset size, minsup and conj_exp.\nCategories:\n{cats}\n")
    return SYSTEM, user


def extract_json(text):
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j < i:
        raise ValueError("no JSON array in model reply")
    return json.loads(text[i: j + 1])


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, model=None, api_key=None, max_tokens=4000):
        self.model = model or env_first("MINEGEN_MODEL") or "claude-sonnet-5-5"
        self.key = api_key or env_first("MINEGEN_API_KEY", "ANTHROPIC_API_KEY")
        self.max_tokens = max_tokens
        if not self.key:
            raise RuntimeError("no API key: set MINEGEN_API_KEY or ANTHROPIC_API_KEY")

    def complete(self, system, user):
        body = json.dumps({"model": self.model, "max_tokens": self.max_tokens, "system": system,
                           "messages": [{"role": "user", "content": user}]}).encode()
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", data=body, headers={
            "content-type": "application/json", "x-api-key": self.key, "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=120) as r:
            data = json.load(r)
        return "".join(b.get("text", "") for b in data["content"] if b.get("type") == "text")


class GeminiBackend:
    """Google Gemini via the REST generateContent endpoint (no SDK)."""
    name = "gemini"

    def __init__(self, model=None, api_key=None, max_tokens=8192):
        self.model = model or env_first("MINEGEN_MODEL") or "gemini-2.5-flash"
        self.key = api_key or env_first("MINEGEN_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
        self.max_tokens = max_tokens  # thinking models count thinking tokens here, so keep it generous
        if not self.key:
            raise RuntimeError("no API key: set MINEGEN_API_KEY, GEMINI_API_KEY or GOOGLE_API_KEY")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", self.model):
            raise RuntimeError(f"invalid model name {self.model!r}")

    def complete(self, system, user):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = json.dumps({
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"temperature": 0.7, "maxOutputTokens": self.max_tokens,
                                 "responseMimeType": "application/json"},
        }).encode()
        req = urllib.request.Request(url, data=body, headers={
            "content-type": "application/json", "x-goog-api-key": self.key})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Gemini HTTP {e.code} for model {self.model!r}: {detail}") from None
        cands = data.get("candidates") or []
        if not cands:
            raise RuntimeError(f"Gemini returned no candidates: {data.get('promptFeedback')}")
        parts = cands[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts if not p.get("thought"))
        if not text:
            raise RuntimeError(f"Gemini returned no text (finishReason={cands[0].get('finishReason')})")
        return text


class FixtureBackend:
    """Serves hand-written proposals. These are NOT model output; they exist so the rest runs offline."""
    name = "fixture"
    model = "handwritten"
    cacheable = False  # the file is the source of truth, so a stale cache must never shadow it

    def __init__(self, path):
        self.path = path

    def complete(self, system, user):
        with open(self.path) as f:
            return json.dumps(json.load(f)["proposals"])


def _find_recorded(cache_dir, system, user):
    if not cache_dir or not os.path.isdir(cache_dir):
        return None
    best = None
    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if not name.endswith(".json"):
            continue
        with open(path) as f:
            rec = json.load(f)
        if rec.get("system") == system and rec.get("user") == user:
            if best is None or os.path.getmtime(path) > best[0]:
                best = (os.path.getmtime(path), {**rec, "sha": name[:-5]})
    return best[1] if best else None


def make_backend(name, model=None, fixture_path=None):
    """Build a backend by name. 'replay' returns None (propose() then reads the cache only)."""
    if name == "gemini":
        return GeminiBackend(model=model)
    if name == "anthropic":
        return AnthropicBackend(model=model)
    if name == "fixture":
        return FixtureBackend(fixture_path)
    if name == "replay":
        return None
    raise ValueError(f"unknown backend {name!r}")


def propose(backend, categories, per_category, cache_dir=None, replay_only=False):
    """Return (proposals, meta). With cache_dir, identical requests are served from disk so a
    recorded model run replays deterministically in CI."""
    system, user = build_prompt(categories, per_category)
    if replay_only:  # match a recorded run on the prompt alone; the model that produced it is in the file
        rec = _find_recorded(cache_dir, system, user)
        if rec is None:
            raise RuntimeError("no cached reply for this request (replay mode)")
        meta = {"backend": "replay", "model": rec["model"], "request_sha256": rec["sha"], "from_cache": True}
        return extract_json(rec["reply"]), meta
    model = getattr(backend, "model", "?")
    key = hashlib.sha256(json.dumps([model, system, user]).encode()).hexdigest()
    use_cache = bool(cache_dir) and getattr(backend, "cacheable", True)
    path = os.path.join(cache_dir, key + ".json") if use_cache else None
    cached = bool(path and os.path.exists(path))
    if cached:
        with open(path) as f:
            reply = json.load(f)["reply"]
    else:
        reply = backend.complete(system, user)
        if path:
            os.makedirs(cache_dir, exist_ok=True)
            with open(path, "w") as f:
                json.dump({"model": model, "system": system, "user": user, "reply": reply}, f, indent=1)
    meta = {"backend": getattr(backend, "name", "?"), "model": model, "request_sha256": key,
            "from_cache": cached}
    return extract_json(reply), meta