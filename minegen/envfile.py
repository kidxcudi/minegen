"""Minimal .env loader (stdlib only). Real environment variables always win over the file."""
import os


def parse_env_text(text):
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        if key:
            out[key] = val
    return out


def load_env_file(path=".env", environ=None):
    """Load KEY=VALUE pairs into the environment without overriding existing variables.
    Returns the list of keys that were set. A missing file is not an error."""
    environ = os.environ if environ is None else environ
    if not path or not os.path.isfile(path):
        return []
    with open(path) as f:
        pairs = parse_env_text(f.read())
    set_keys = []
    for k, v in pairs.items():
        if k not in environ and v != "":
            environ[k] = v
            set_keys.append(k)
    return set_keys
