"""The operate workflow's own steps: take in an existing project, and diagnose a failure.

``intake`` copies the project a person points the run at into the run's
folder, so every later step works on the copy and the person's folder is
read once and never written. The validator then checks the copy as it
checks an engineer's project (the ``gate`` step).

``diagnostics`` runs only when the validator fails the project. It makes
the diagnosis packet (``tenx.diagnosis``) from the run's files and what
the validator's build wrote about itself, gives a copy to the diagnostics
agent, which can read but not change files, and checks its answer in code
(``tenx.diagnostics``). The diagnosis is advice: it is kept as
``DIAGNOSIS.md`` and the run's findings, and the run's status stays the
validator's. A packet that cannot be made, an answer that fails its check,
a crash or a ceiling that cannot hold the call each leave the run without
a diagnosis, and say why.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import stat
from pathlib import Path

from tenx import artefacts, diagnosis
from tenx import diagnostics as answers
from tenx.domain import AgentFinding, ArtefactRef, Evidence, ValidationResult
from tenx.graph.build import (
    CHECKS_FILE,
    DIAGNOSTICS_STEP,
    GATE_STEP,
    NOT_CARRIED,
    Toolkit,
    _fresh,
    _request,
    _saved_report,
    call_agent,
)
from tenx.graph.state import GraphState, plain
from tenx.ports import BUILD_RECORD

__all__ = ["DIAGNOSIS_PACKET_DIR", "PROJECT_DIR", "copy_project", "diagnostics_node", "intake_node"]

PROJECT_DIR = "project"
DIAGNOSIS_PACKET_DIR = "diagnosis-packet"
# The profiles and the overlap report together, as the packet carries them.
SOURCES_DIR = "diagnosis-sources"


def copy_project(source: Path, destination: Path) -> list[str]:
    """Copy a project's files into ``destination``; return what was left out.

    A link is copied as a link and never followed: this copy runs outside the
    sandbox and could otherwise read what the sandbox hides. What dbt writes
    when it builds (``target``, ``logs``, packages, database files) is not
    copied, as the validator builds from nothing. Anything that is not a
    folder, a file or a link (a named pipe, a socket) is left out.
    """
    left_out: list[str] = []

    def skip(folder: str, names: list[str]) -> set[str]:
        skipped = set()
        for name in names:
            if any(fnmatch.fnmatch(name, pattern) for pattern in NOT_CARRIED):
                skipped.add(name)
                continue
            mode = os.lstat(os.path.join(folder, name)).st_mode
            if not (stat.S_ISDIR(mode) or stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                skipped.add(name)
                where = Path(folder, name).relative_to(source).as_posix()
                left_out.append(where)
        return skipped

    shutil.copytree(source, destination, symlinks=True, ignore=skip)
    return sorted(left_out)


def intake_node(toolkit: Toolkit):
    """Copy the project the run checks into the run's folder."""

    def intake(state: GraphState) -> dict:
        request = _request(state)
        folder = _fresh(Path(request.out) / PROJECT_DIR)
        left_out = copy_project(Path(request.project), folder)
        update = {"artefacts": {"project": ArtefactRef.of("project", folder).dump()}}
        if left_out:
            claim = "the project's copy leaves out what is not a file, folder or link: "
            finding = AgentFinding(
                agent="intake", claim=claim + ", ".join(left_out), kind="fact", confidence="high"
            )
            update["findings"] = [finding.dump()]
        return plain(update)

    return intake


def _no_diagnosis(update: dict, problem: str) -> dict:
    finding = AgentFinding(
        agent=DIAGNOSTICS_STEP, claim=f"no diagnosis: {problem}", kind="fact", confidence="high"
    )
    return plain({**update, "findings": [finding.dump()]})


def _findings(answer: dict, written: Path) -> list[dict]:
    found = [
        AgentFinding(
            agent=DIAGNOSTICS_STEP,
            claim=f"the diagnosis: {answer['summary']}",
            kind="inference",
            confidence="medium",
            evidence=(Evidence(kind="file", ref=str(written)),),
            open_questions=tuple(answer["missing"]),
        )
    ]
    for item in answer["causes"]:
        probable = item["likelihood"] == answers.PROBABLE
        found.append(
            AgentFinding(
                agent=DIAGNOSTICS_STEP,
                claim=f"{item['likelihood']} cause, {item['cause']}: {item['claim']}",
                kind="inference" if probable else "hypothesis",
                confidence="medium" if probable else "low",
                evidence=(
                    Evidence(
                        kind="file", ref=f"{item['file']}:{item['line']}", detail=item["evidence"]
                    ),
                ),
                assets=(item["model"],),
            )
        )
    return [finding.dump() for finding in found]


def diagnostics_node(toolkit: Toolkit):
    """The diagnostics agent reads the packet of a project the validator failed, and advises."""

    def diagnostics(state: GraphState) -> dict:
        request = _request(state)
        out = Path(request.out)
        found = state["artefacts"]
        if ValidationResult.load(state["validations"][-1]).passed:
            return _no_diagnosis({}, "the project passed every check")
        gate = Path(found["gate report"]["path"]).parent
        work = gate / artefacts.WORK_DIR
        record = {}
        if BUILD_RECORD in toolkit.validator.capabilities():
            record = toolkit.validator.build_record(work.resolve())
        report = _saved_report(gate / CHECKS_FILE)
        sources = _fresh(out / SOURCES_DIR)
        shutil.copytree(found["profiles"]["path"], sources)
        shutil.copytree(found["overlap"]["path"], sources, dirs_exist_ok=True)
        try:
            packet = diagnosis.build(
                _fresh(out / DIAGNOSIS_PACKET_DIR),
                goal=Path(request.goal),
                profiles=sources,
                project=Path(found["project"]["path"]).resolve(),
                report=report,
                raw=Path(request.raw).resolve(),
                build_folder=work.resolve(),
                build_files=record,
                secrets=(Path(request.declaration), out / GATE_STEP),
            )
        except diagnosis.PacketError as problem:
            return _no_diagnosis({}, str(problem))

        def prepare(work: Path) -> None:
            shutil.copytree(packet.folder, work, dirs_exist_ok=True)

        call = call_agent(toolkit, state, DIAGNOSTICS_STEP, {"packet": packet.folder}, prepare)
        if call.stopped:
            # The run has its verdict; a diagnosis it cannot afford leaves it as it is.
            return _no_diagnosis({"costs": call.update["costs"]}, call.update["reason"])
        files = {
            name: (packet.folder / name).read_text(encoding="utf-8", errors="replace")
            for name in packet.files
        }
        try:
            answer = answers.diagnosis_from_report(call.report, files, report)
        except answers.DiagnosisError as problem:
            return _no_diagnosis(call.update, str(problem))
        written = call.folder / answers.DIAGNOSIS_FILE
        artefacts.write_text(written, answers.render(answer))
        number = call.update["attempts"][DIAGNOSTICS_STEP]
        ref = ArtefactRef.of("diagnosis", written, attempt=number).dump()
        return plain(
            {**call.update, "artefacts": {"diagnosis": ref}, "findings": _findings(answer, written)}
        )

    return diagnostics
