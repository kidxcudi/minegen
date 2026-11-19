"""Run one generated case through PeTTa from a scratch folder, importing the miner from the repo by absolute path."""
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

# (space, module path under experiments/). The data import is added separately, from the scratch folder.
MODULES = [
    ("&self", "utils/common-utils"), ("&self", "utils/index_to_var"), ("&db", None),
    ("&self", "utils/sort"), ("&self", "utils/variable-combinations"),
    ("&self", "utils/surp-utils/blk-abstractness"), ("&self", "utils/surp-utils/partition-metta"),
    ("&self", "utils/remove_useless_clauses"),
    ("&self", "frequent-pattern-miner/valuation"), ("&self", "frequent-pattern-miner/shallow-abstractions"),
    ("&self", "frequent-pattern-miner/specialization"), ("&self", "frequent-pattern-miner/candidate"),
    ("&self", "frequent-pattern-miner/conjunction-expansion"),
    ("&self", "frequent-pattern-miner/frequent-pattern-miner-generic"),
]
CALL = "!(collapse (frequency-pattern-miner &db $x &cndp_{id} &conjsp_{id} {minsup} {depth} {conj}))\n"


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def _imp(path):
    """import! argument for an absolute path, quoted only when it holds characters the parser would split on."""
    path = path.replace("\\", "/")
    return f'"{path}"' if re.search(r'[\s()"]', path) else path


def render_driver(case, exp_dir, data_path):
    """Driver that imports the miner from the repo by ABSOLUTE path (PeTTa resolves absolute import! paths),
    so it can live in a scratch folder and the repo is never written to. data_path has no .metta extension."""
    lines = []
    for space, mod in MODULES:
        target = data_path if mod is None else os.path.join(exp_dir, mod)
        lines.append(f"!(import! {space} {_imp(target)})")
    return "\n".join(lines) + "\n" + CALL.format(
        id=case["id"], minsup=case["minsup"], depth=case["depth"], conj="True" if case["conj_exp"] else "False")


def _cwd_for(petta_run, path):
    """Mirror how PeTTa is normally used: `cd ~/PeTTa && sh run.sh <absolute file>`. For a run.sh launcher that is
    the launcher's own folder; for any other launcher, the folder of the file being run."""
    if os.path.basename(petta_run) == "run.sh":
        return os.path.dirname(os.path.realpath(petta_run))
    return os.path.dirname(path)


def petta_flags():
    """Extra launcher arguments placed after the file. Default `silent`, which makes PeTTa skip printing the source
    of every loaded function so stdout holds only results. Set PETTA_FLAGS= (empty) to disable."""
    return os.environ.get("PETTA_FLAGS", "silent").split()


def _exec(petta_run, path, timeout):
    """Run the launcher directly when it is executable, else through bash. Falls back to bash when the
    launcher has no shebang line (the OS refuses to execute those directly)."""
    flags = petta_flags()
    cmds = [[petta_run, path, *flags], ["bash", petta_run, path, *flags]] if os.access(petta_run, os.X_OK) \
        else [["bash", petta_run, path, *flags]]
    for i, cmd in enumerate(cmds):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=_cwd_for(petta_run, path))
        except OSError:
            if i == len(cmds) - 1:
                raise


def run_file(petta_run, path, timeout=300):
    path = os.path.abspath(path)  # always an absolute file path, as PeTTa's launcher expects
    try:
        p = _exec(petta_run, path, timeout)
        return RunResult(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
        return RunResult(-1, out, "", True)


def run_case(repo, petta_run, case, timeout=300):
    """Write the data file and driver into a fresh scratch folder, run PeTTa on the driver, delete the folder.
    Nothing is written into `repo`."""
    exp = os.path.join(os.path.abspath(repo), "experiments")
    scratch = tempfile.mkdtemp(prefix="minegen_run_")
    try:
        data = os.path.join(scratch, f"{case['id']}_data")
        with open(data + ".metta", "w") as f:
            f.write("\n".join(case["atoms"]) + "\n")
        drv = os.path.join(scratch, f"{case['id']}.metta")
        with open(drv, "w") as f:
            f.write(render_driver(case, exp, data))
        return run_file(petta_run, drv, timeout)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)