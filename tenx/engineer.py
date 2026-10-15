"""Have the engineer agent build a dbt project from raw files, then gate it.

The agent works in a project folder prepared here. The folder holds a dbt
project with no models yet, a profile of every raw table and the goal, and
nothing else: the gate's declaration stays outside it, so the checks cannot
be read or edited by the agent.

    project/
        GOAL.md             what to build, in plain English
        source_profiles/    one profile per raw table, as text and JSON
        dbt_project.yml     reads the raw files from the variable raw_dir
        profiles.yml        builds into dev.duckdb inside the folder
        models/             empty; the agent writes the models here
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from tenx.profile import ProfileError, profile_folder

PROJECT_NAME = "tenx_project"
GOAL_FILE = "GOAL.md"
SOURCE_PROFILES_DIR = "source_profiles"
DEV_DATABASE = "dev.duckdb"


class EngineerError(Exception):
    """The run could not start or its inputs are unusable."""


def dbt_project_yml(raw: Path) -> str:
    # raw_dir is given a default so that the agent's own `dbt build` reads the
    # raw files; the gate passes its own value on the command line.
    return (
        f"name: {PROJECT_NAME}\n"
        'version: "1.0.0"\n'
        "config-version: 2\n"
        f"profile: {PROJECT_NAME}\n"
        "\n"
        'model-paths: ["models"]\n'
        "\n"
        "vars:\n"
        f"  raw_dir: {json.dumps(str(raw))}\n"
    )


def profiles_yml() -> str:
    return (
        f"{PROJECT_NAME}:\n"
        "  target: dev\n"
        "  outputs:\n"
        "    dev:\n"
        "      type: duckdb\n"
        f"      path: {DEV_DATABASE}\n"
        "      threads: 1\n"
    )


def prepare_project(project: Path, raw: Path, goal: Path) -> list[dict]:
    """Write the folder the agent works in. Returns the source profiles.

    The folder must not exist yet or be empty, so that nothing from an
    earlier attempt is left for the agent to find.
    """
    raw = Path(raw).resolve()
    goal = Path(goal)
    if not raw.is_dir():
        raise EngineerError(f"{raw} is not a folder of raw files")
    if not goal.is_file():
        raise EngineerError(f"{goal} does not exist")
    if not goal.read_text(encoding="utf-8").strip():
        raise EngineerError(f"{goal} is empty")
    project = Path(project)
    if project.exists() and (not project.is_dir() or any(project.iterdir())):
        raise EngineerError(f"{project} must be empty, so that the agent starts from nothing")
    project.mkdir(parents=True, exist_ok=True)
    try:
        profiles = profile_folder(raw, project / SOURCE_PROFILES_DIR)
    except ProfileError as error:
        raise EngineerError(str(error)) from error
    shutil.copyfile(goal, project / GOAL_FILE)
    (project / "dbt_project.yml").write_text(dbt_project_yml(raw), encoding="utf-8")
    (project / "profiles.yml").write_text(profiles_yml(), encoding="utf-8")
    (project / "models").mkdir()
    return profiles
