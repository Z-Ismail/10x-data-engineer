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

from tenx.domain import AgentFinding, ArtefactRef
from tenx.graph.build import (
    NOT_CARRIED,
    Toolkit,
    _fresh,
    _request,
)
from tenx.graph.state import GraphState, plain

__all__ = ["PROJECT_DIR", "copy_project", "intake_node"]

PROJECT_DIR = "project"


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
