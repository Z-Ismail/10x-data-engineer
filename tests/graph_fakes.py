"""Stand-in ports for the build graph's tests, which record every call made to them."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from tenx.domain import EngineeringRequest
from tenx.graph.build import CHECKPOINT_FILE, Toolkit, build_graph, config
from tenx.graph.checkpoints import open_checkpoints
from tenx.policy import Budget, Policy
from tenx.ports import (
    OVERLAP,
    PROFILE,
    RUN_AGENT,
    SCAFFOLD,
    STRUCTURED_ANSWER,
    VALIDATE,
    AgentRun,
    CheckResult,
    Mounts,
    Role,
    ValidationReport,
)
from tenx.sandbox import Unisolated

ENGINEER = Role("engineer", "tenx:engineer", "Build what GOAL.md asks for.", ("Edit",), 60)
CROSS_SOURCE = Role(
    "cross-source", "tenx:cross-source", "Work out the joins.", (), 20, json_schema={}
)
# One table, as a profiler describes it.
PROFILE_JSON = {"table": "shop.orders", "columns": [{"name": "id"}, {"name": "email"}]}
ANSWER = {
    "joins": [
        {
            "left": "shop.orders.email",
            "right": "shop.orders.id",
            "prepare": "Lower-case the email.",
            "cardinality": "many-to-one",
            "unmatched": "Test orders.",
            "evidence": "overlap.txt.",
        }
    ],
    "cautions": ["Check for shared domains."],
}


def report(cost=0.1, turns=5, answer=None, basis="list") -> dict:
    """A report as ``claude -p --output-format json`` gives one."""
    made = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": turns,
        "duration_ms": 1000 * turns,
        "total_cost_usd": cost,
        "modelUsage": {
            "claude-sonnet-5-5": {
                "inputTokens": 10,
                "cacheCreationInputTokens": 100,
                "cacheReadInputTokens": 1000,
                "outputTokens": 50,
                "costBasis": basis,
            }
        },
        "permission_denials": [],
    }
    if answer is not None:
        made["structured_output"] = answer
    return made


class Fakes:
    """Ports that do their work in plain Python and note each call in ``calls``."""

    def __init__(self, missing: dict | None = None) -> None:
        self.calls: list[str] = []
        self.missing = missing or {}
        # What each agent reports, by role name; None writes no report.
        self.reports = {"cross-source": report(0.07, 9, ANSWER), "engineer": report(0.11, 18)}
        self.verdict = True
        self.seen: dict[str, list[str]] = {}

    def _capable(self, port: str, all_of: set[str]) -> frozenset[str]:
        return frozenset(all_of - set(self.missing.get(port, ())))

    # SourceProfiler
    def profiler_capabilities(self):
        return self._capable("profiler", {PROFILE, OVERLAP})

    def profile(self, raw: Path, out: Path) -> list[dict]:
        self.calls.append("profile")
        out.mkdir(parents=True)
        (out / "shop.orders.json").write_text(json.dumps(PROFILE_JSON))
        (out / "shop.orders.txt").write_text("table shop.orders\n")
        return [PROFILE_JSON]

    def overlap(self, raw: Path, out: Path) -> dict:
        self.calls.append("overlap")
        out.mkdir(parents=True)
        (out / "overlap.txt").write_text("no pairs\n")
        (out / "overlap.json").write_text("{}\n")
        return {}

    # TransformEngine
    def scaffold(self, project: Path, raw: Path) -> None:
        self.calls.append("scaffold")
        (project / "dbt_project.yml").write_text(f"raw_dir: {raw}\n")
        (project / "models").mkdir()

    # AgentBackend
    def run_agent(self, role, workspace, report_file, timeout, sandbox=None, mounts=None):
        self.calls.append(role.name)
        self.seen[role.name] = sorted(
            path.relative_to(workspace).as_posix() for path in workspace.rglob("*")
        )
        if role.name == "engineer":
            (workspace / "models" / "orders.sql").write_text("select 1\n")
        made = self.reports.get(role.name)
        if made is None:
            return AgentRun(1, None, message="crashed")
        report_file.write_text(json.dumps(made))
        return AgentRun(0, made)

    # Validator
    def validate(self, project, raw, declaration, workdir, sandbox=None, hidden=()):
        self.calls.append("gate")
        (workdir / "built.duckdb").write_text("tables\n")
        status = "pass" if self.verdict else "fail"
        results = (
            CheckResult("build", "project", "pass", "built"),
            CheckResult("rows", "orders", status, "6 rows"),
        )
        return ValidationReport(results, self.verdict, f"gate {status}\n")

    def toolkit(self, out: Path, policy: Policy | None = None, budget: Budget | None = None):
        fakes = self

        def port(name: str, capabilities, **methods):
            kind = type(name, (), {"name": name, "capabilities": lambda self: capabilities()})
            made = kind()
            for method, function in methods.items():
                setattr(made, method, function)
            return made

        plugin = out / "plugin"
        (plugin / "agents").mkdir(parents=True, exist_ok=True)
        for agent in ("engineer", "cross-source"):
            (plugin / "agents" / f"{agent}.md").write_text(f"You are the {agent}.\n")
        return Toolkit(
            profiler=port(
                "fake profiler",
                fakes.profiler_capabilities,
                profile=fakes.profile,
                overlap=fakes.overlap,
            ),
            engine=port(
                "fake engine",
                lambda: fakes._capable("engine", {SCAFFOLD}),
                scaffold=fakes.scaffold,
            ),
            validator=port(
                "fake gate",
                lambda: fakes._capable("validator", {VALIDATE}),
                validate=fakes.validate,
            ),
            backend=port(
                "fake backend",
                lambda: fakes._capable("backend", {RUN_AGENT, STRUCTURED_ANSWER}),
                run=fakes.run_agent,
            ),
            sandbox=Unisolated(),
            roles={"engineer": ENGINEER, "cross-source": CROSS_SOURCE},
            mounts=lambda workspace, raw: Mounts(workspace=workspace),
            plugin=plugin,
            hidden=(),
            budget=budget or Budget(policy),
            timeout=60,
        )


class GraphCase:
    """A temporary run folder with raw files, a goal and a declaration."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.raw = self.root / "raw"
        self.raw.mkdir()
        (self.raw / "orders.csv").write_text("id,email\n1,a@x.com\n")
        self.goal = self.root / "goal.md"
        self.goal.write_text("Build orders.\n")
        self.declaration = self.root / "checks.json"
        self.declaration.write_text("{}\n")
        self.out = self.root / "run"
        self.out.mkdir()

    def request(self, **changes) -> EngineeringRequest:
        fields = {
            "run_id": "run",
            "raw": str(self.raw),
            "goal": str(self.goal),
            "declaration": str(self.declaration),
            "out": str(self.out),
            "isolation": "unisolated",
        }
        return EngineeringRequest(**{**fields, **changes})

    def invoke(self, toolkit, request=None, nodes=None, resume=False):
        """Run (or resume) the graph to its end; return the final state."""
        with open_checkpoints(self.out / CHECKPOINT_FILE) as saver:
            app = build_graph(toolkit, nodes).compile(checkpointer=saver)
            start = None if resume else {"run_id": "run", "request": request.dump()}
            return app.invoke(start, config("run"), durability="sync")
