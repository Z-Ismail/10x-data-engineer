"""Count what a contract keeps of the raw files, in a process kept apart from the run.

A contract's rules for leaving rows out are SQL that an agent may have
written, and DuckDB's own advice is to treat SQL like code and not to run
it from an untrusted source without a sandbox. So the counts are made by a
separate Python process, in the run's sandbox when it has one: it sees the
raw files read-only and nothing of the run, the repository or the
declaration. Inside it DuckDB may read only the raw folder, cannot change
its settings, and runs each count only if it is exactly one SELECT. A count
that takes longer than the time limit stops the process and everything it
started.

The counts are made the way the gate makes them (``tenx.gate.expected_rows``):
a row matched by two rules is charged to the first, and exact copies are
counted among the rows the rules keep.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from pathlib import Path

from tenx.contract import ContractError, to_declaration
from tenx.gate import GateError, claimed_by_filters, parse_declaration, source_files
from tenx.ports import KeptRows
from tenx.profile import relation
from tenx.sandbox import Mounts, Sandbox, SandboxError, Unisolated, python_folders

TIMEOUT_SECONDS = 300.0

# Run as ``python -I -c``: reads the raw folder and the counts to make as
# JSON on stdin, prints one JSON object. Kept to the standard library and
# DuckDB, which are all the sandbox shows it.
SCRIPT = r"""
import json, sys
import duckdb
job = json.load(sys.stdin)
con = duckdb.connect()
con.execute("SET allowed_directories = ?", [[job["raw"]]])
con.execute("SET enable_external_access = false")
con.execute("SET lock_configuration = true")
counts = {}
for label, query in job["queries"]:
    try:
        statements = con.extract_statements(query)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise ValueError("a rule must be part of one SELECT, not hold statements of its own")
        counts[label] = con.execute(query).fetchone()[0]
    except Exception as error:
        print(json.dumps({"failed": label, "error": str(error)}))
        sys.exit(1)
print(json.dumps({"counts": counts}))
"""


def _queries(contract: dict, raw: Path) -> tuple[list, list[tuple]]:
    """The counts each model needs, and how to read them back."""
    try:
        files = source_files(Path(raw))
        models = parse_declaration(to_declaration(contract), "the contract")
    except GateError as error:
        raise ContractError(str(error)) from error
    queries, plan = [], []
    for model in models:
        if model.source not in files:
            raise ContractError(f"model {model.name}: no raw file for {model.source}")
        source = relation(files[model.source])
        claimed, kept_condition = claimed_by_filters(model)
        kept = f"SELECT * FROM {source} WHERE {kept_condition}"
        labels = {
            f"rule {name}": f"SELECT count(*) FROM {source} WHERE {where}"
            for name, where in claimed
        }
        labels["source"] = f"SELECT count(*) FROM {source}"
        labels["kept"] = f"SELECT count(*) FROM ({kept})"
        labels["distinct"] = f"SELECT count(*) FROM (SELECT DISTINCT * FROM ({kept}))"
        queries += [[f"{model.name}\t{label}", query] for label, query in labels.items()]
        plan.append((model, [name for name, _ in claimed]))
    return queries, plan


def _run(command: list[str], job: str, cwd: Path, timeout: float) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(job, timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()
        raise ContractError(
            f"counting the contract's rows did not finish within {timeout:g} seconds"
        ) from None
    return process.returncode, output


def count_kept(
    contract: dict,
    raw: Path,
    workdir: Path,
    sandbox: Sandbox | None = None,
    hidden: tuple[Path, ...] = (),
    timeout: float = TIMEOUT_SECONDS,
) -> dict[str, KeptRows]:
    """For each model, its source rows, what each rule removes and what is kept."""
    raw = Path(raw).resolve()
    queries, plan = _queries(contract, raw)
    workdir = Path(workdir).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    seen = Mounts(
        workspace=workdir,
        read_only=(raw, *python_folders()),
        hidden=tuple(path for path in hidden if path.exists()),
    )
    try:
        # The interpreter as given, made absolute and tidied but not resolved:
        # a virtual environment's python is a link, and only its own path
        # finds the environment's packages. The process starts in workdir,
        # and the folders on a path such as ../venv/bin/python may be hidden.
        python = os.path.normpath(os.path.abspath(sys.executable))
        command = (sandbox or Unisolated()).command([python, "-I", "-c", SCRIPT], seen)
    except SandboxError as error:
        raise ContractError(f"the rules cannot be counted apart from the run: {error}") from error
    job = json.dumps({"raw": f"{raw}/", "queries": queries})
    code, output = _run(command, job, workdir, timeout)
    try:
        answer = json.loads(output.strip().splitlines()[-1])
    except (ValueError, IndexError):
        answer = None
    if not isinstance(answer, dict):
        raise ContractError(f"counting the contract's rows failed: {output.strip()[-500:]}")
    if code != 0 or "counts" not in answer:
        name = str(answer.get("failed", "")).partition("\t")[0]
        sources = {model.name: model.source for model, _ in plan}
        raise ContractError(
            f"model {name}: could not count the rows of {sources.get(name)}: {answer.get('error')}"
        )
    counts = answer["counts"]
    kept = {}
    for model, rules in plan:

        def got(label: str, name: str = model.name) -> int:
            return counts[f"{name}\t{label}"]

        excluded = {name: got(f"rule {name}") for name in rules}
        expected = got("kept")
        for exclusion in model.exclusions:
            if exclusion.duplicate_rows:
                excluded[exclusion.name] = got("kept") - got("distinct")
                expected = got("distinct")
        kept[model.name] = KeptRows(
            source_rows=got("source"),
            excluded=tuple((item.name, excluded[item.name]) for item in model.exclusions),
            kept=expected,
        )
    return kept
