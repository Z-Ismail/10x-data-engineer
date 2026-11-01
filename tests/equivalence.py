"""Run the old runner and the graph on the same inputs with a stand-in claude, and compare.

    python tests/equivalence.py [--seeds 1 7 42 2026] [--sizes 200 1000]
                                [--scenarios pass fail] [--unisolated]

The stand-in answers the same way every time, so any difference between the
two runs comes from the runners. Both arms run in the same output folder,
one after the other (the folder is renamed in between), so that paths
written into the artefacts are the same too. The old runner runs in this
process, so that its gate report, which it only prints, can be kept; the
graph runs through its command line.

Into its report the stand-in writes its arguments, a hash of every file in
its folder before it changes anything, the environment variables the runner
sets for it, and which of a few paths it can see. Comparing the reports
then compares the command, the starting folder and the sandbox's view.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from demo.generate import generate
from tenx import engineer
from tenx.equivalence import Comparison, compare_runs
from tenx.gate import render
from tenx.sandbox import Unisolated, default_sandbox

FIXTURES = ROOT / "tests" / "fixtures"
REFERENCE_MODELS = FIXTURES / "demo_project" / "models"
DECLARATION = FIXTURES / "demo_gate.json"
GOAL = ROOT / "demo" / "goal.md"
TEST_MODE_FILTER = "where livemode = 'true'"

STAND_IN = """
import hashlib, json, os, shutil, sys, time
from pathlib import Path

here = Path.cwd()
agent = sys.argv[sys.argv.index("--agent") + 1]
seen = {
    str(path.relative_to(here)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(here.rglob("*"))
    if path.is_file()
}
record = {
    "argv": sys.argv[1:],
    "seen": seen,
    "visible": {path: os.path.exists(path) for path in json.loads(os.environ["STAND_IN_PROBE"])},
    "environment": {
        name: os.environ.get(name) for name in ("CLAUDE_CODE_TOOL_MEMORY_LIMIT", "DBT_PROFILES_DIR")
    },
}
turns, cost = (9, 0.1204) if agent == "tenx:cross-source" else (7, 0.0812)
report = {
    "type": "result", "subtype": "success", "is_error": False, "num_turns": turns,
    "duration_ms": 41300, "duration_api_ms": 30000, "total_cost_usd": cost,
    "result": json.dumps(record, sort_keys=True),
    "modelUsage": {"claude-sonnet-5-5": {
        "inputTokens": 20, "cacheCreationInputTokens": 12000,
        "cacheReadInputTokens": 50000, "outputTokens": 2500, "costBasis": "list"}},
    "permission_denials": [],
}
if agent == "tenx:cross-source":
    report["structured_output"] = json.loads(os.environ["STAND_IN_ANSWER"])
else:
    if os.environ.get("STAND_IN_HANG"):
        (here / "HANGING").write_text("waiting to be killed")
        time.sleep(30)
    if os.environ["STAND_IN_ENGINEER"] == "crash":
        print("Error: not signed in", file=sys.stderr)
        sys.exit(1)
    shutil.copytree(os.environ["STAND_IN_MODELS"], here / "models", dirs_exist_ok=True)
print(json.dumps(report))
"""

GOOD_ANSWER = {
    "joins": [
        {
            "left": "billing.customers.email",
            "right": "crm.accounts.billing_email",
            "prepare": "Match on the domain after lower(trim()).",
            "cardinality": "many-to-one",
            "unmatched": "Prospects in the CRM have no billing customer.",
            "evidence": "overlap.txt: 79% of billing emails found after normalising.",
        }
    ],
    "cautions": ["Some CRM accounts hold another mailbox at the company's domain."],
}
# Names a column no profile has, so the answer fails its check.
BAD_ANSWER = {
    **GOOD_ANSWER,
    "joins": [{**GOOD_ANSWER["joins"][0], "left": "billing.customers.nowhere"}],
}


@dataclass(frozen=True)
class Scenario:
    """How the stand-in behaves in one comparison."""

    name: str
    models: str = "reference"  # "reference" or "broken" (no invoices test-mode filter)
    answer: dict = None
    engineer: str = "report"  # "report" or "crash"
    cross_source: bool = True
    killed: bool = False  # the graph is killed in the engineer's call, then resumed


SCENARIOS = {
    scenario.name: scenario
    for scenario in (
        Scenario("pass", answer=GOOD_ANSWER),
        Scenario("fail", models="broken", answer=GOOD_ANSWER),
        Scenario("no notes", answer=BAD_ANSWER),
        Scenario("no report", answer=GOOD_ANSWER, engineer="crash"),
        Scenario("engineer alone", cross_source=False),
        Scenario("resumed", answer=GOOD_ANSWER, killed=True),
    )
}


def stand_in(folder: Path) -> Path:
    """Write the stand-in claude, with the models it copies beside it.

    The sandbox shows an agent the folder its claude is in, read-only, so
    the models are put there rather than left in the hidden repository.
    """
    folder.mkdir(parents=True)
    claude = folder / "claude"
    claude.write_text(f"#!{sys.executable}\n{STAND_IN}", encoding="utf-8")
    claude.chmod(0o755)
    shutil.copytree(REFERENCE_MODELS, folder / "reference")
    shutil.copytree(REFERENCE_MODELS, folder / "broken")
    invoices = folder / "broken" / "staging" / "stg_billing__invoices.sql"
    sql = invoices.read_text(encoding="utf-8")
    if TEST_MODE_FILTER not in sql:
        raise RuntimeError(f"{invoices} no longer has the test-mode filter to remove")
    invoices.write_text(sql.replace(TEST_MODE_FILTER, ""), encoding="utf-8")
    return claude


def probes(raw: Path, out: Path) -> list[str]:
    """Paths whose visibility the stand-in records: what the sandbox shows and hides."""
    return [
        str(Path(raw).resolve()),
        str(DECLARATION),
        str(ROOT / "README.md"),
        str(out.resolve() / engineer.PLUGIN_COPY_DIR / "agents" / "engineer.md"),
    ]


def environment(scenario: Scenario, claude: Path, raw: Path, out: Path) -> dict:
    return {
        "STAND_IN_ANSWER": json.dumps(scenario.answer or {}),
        "STAND_IN_ENGINEER": scenario.engineer,
        "STAND_IN_MODELS": str(claude.parent / scenario.models),
        "STAND_IN_PROBE": json.dumps(probes(raw, out)),
        "PATH": f"{claude.parent}{os.pathsep}{os.environ['PATH']}",
    }


def run_old(scenario, claude, raw, out, sandbox) -> str:
    """The old runner, in this process. Returns its gate report as it prints it."""
    saved = dict(os.environ)
    os.environ.update(environment(scenario, claude, raw, out))
    try:
        outcome = engineer.run(
            raw,
            GOAL,
            DECLARATION,
            out,
            claude=str(claude),
            cross_source=scenario.cross_source,
            sandbox=sandbox,
        )
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return render(outcome.results)


def run_graph(scenario, claude, raw, out, unisolated) -> list[int]:
    """The graph, through its command line. Returns the exit status of each call."""
    command = [sys.executable, "-m", "tenx.run"]
    start = [
        *command,
        "run",
        "--raw",
        str(raw),
        "--goal",
        str(GOAL),
        "--checks",
        str(DECLARATION),
        "--out",
        str(out),
    ]
    start += [] if scenario.cross_source else ["--no-cross-source"]
    start += ["--unisolated"] if unisolated else []
    settings = {**os.environ, **environment(scenario, claude, raw, out)}
    quiet = {"cwd": ROOT, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if not scenario.killed:
        return [subprocess.run(start, env=settings, timeout=600, check=False, **quiet).returncode]
    first = subprocess.Popen(start, env={**settings, "STAND_IN_HANG": "1"}, **quiet)
    _kill_when_hanging(first, out / "engineer" / "attempt-1" / "work" / "HANGING")
    resumed = [*command, "resume", "--out", str(out)]
    again = subprocess.run(resumed, env=settings, timeout=600, check=False, **quiet)
    return [first.returncode, again.returncode]


def _kill_when_hanging(process: subprocess.Popen, marker: Path) -> None:
    for _ in range(1200):
        if marker.exists() or process.poll() is not None:
            break
        try:
            process.wait(timeout=0.25)
        except subprocess.TimeoutExpired:
            pass
    if not marker.exists():
        process.kill()
        raise RuntimeError("the graph never reached the engineer's call")
    process.kill()
    process.wait()


@dataclass(frozen=True)
class Pair:
    old: Path
    graph: Path
    comparison: Comparison
    graph_exits: list[int]


def run_pair(raw: Path, root: Path, scenario: Scenario, unisolated: bool = False) -> Pair:
    """Run both arms in ``root/run`` in turn, keep them as ``root/old`` and ``root/graph``."""
    root.mkdir(parents=True, exist_ok=True)
    claude = stand_in(root / "bin")
    out = root / "run"
    sandbox = Unisolated() if unisolated else default_sandbox()
    report = run_old(scenario, claude, raw, out, sandbox)
    out.rename(root / "old")
    exits = run_graph(scenario, claude, raw, out, unisolated)
    out.rename(root / "graph")
    comparison = compare_runs(root / "old", root / "graph", report)
    return Pair(root / "old", root / "graph", comparison, exits)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 7, 42, 2026])
    parser.add_argument("--sizes", type=int, nargs="+", default=[200, 1000])
    parser.add_argument(
        "--scenarios", nargs="+", default=["pass", "fail"], choices=sorted(SCENARIOS)
    )
    parser.add_argument("--unisolated", action="store_true", help="run without the sandbox")
    args = parser.parse_args(argv)
    different = 0
    with tempfile.TemporaryDirectory() as tmp:
        for seed in args.seeds:
            for size in args.sizes:
                data = Path(tmp) / f"data-{seed}-{size}"
                generate(seed, size, data / "raw", data / "answers")
                for name in args.scenarios:
                    root = Path(tmp) / f"{seed}-{size}-{name.replace(' ', '-')}"
                    pair = run_pair(data / "raw", root, SCENARIOS[name], args.unisolated)
                    usage = json.loads((pair.graph / "usage.json").read_text(encoding="utf-8"))
                    verdict = pair.comparison.render()
                    where = f"seed {seed}, {size} companies, {name} (gate {usage['gate']})"
                    print(f"{where}: {verdict}", end="")
                    different += not pair.comparison.identical
                    shutil.rmtree(root)
    print(f"{different} of {len(args.seeds) * len(args.sizes) * len(args.scenarios)} differ")
    return 1 if different else 0


if __name__ == "__main__":
    sys.exit(main())
