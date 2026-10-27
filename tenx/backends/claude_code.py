"""Agents run as headless Claude Code sessions on the user's own sign-in.

The command line and the way a session is started, stopped and read are the
runner's own (``agent_command`` and ``run_agent`` in ``tenx.engineer``);
this backend gives them the shape of ``AgentBackend`` and changes nothing.
"""

from __future__ import annotations

from pathlib import Path

from tenx.engineer import EngineerError, agent_command, run_agent
from tenx.ports import (
    RUN_AGENT,
    STRUCTURED_ANSWER,
    AgentRun,
    Mounts,
    PlatformError,
    Role,
    Sandbox,
    require,
)


class ClaudeCode:
    """Runs each agent with ``claude -p``, loading the add-on from ``plugin_dir``."""

    name = "Claude Code"

    def __init__(self, claude: str, plugin_dir: Path) -> None:
        self.claude = claude
        self.plugin_dir = Path(plugin_dir)

    def capabilities(self) -> frozenset[str]:
        return frozenset({RUN_AGENT, STRUCTURED_ANSWER})

    def command(self, role: Role) -> list[str]:
        return agent_command(self.claude, self.plugin_dir, role)

    def run(
        self,
        role: Role,
        workspace: Path,
        report_file: Path,
        timeout: float,
        sandbox: Sandbox | None = None,
        mounts: Mounts | None = None,
    ) -> AgentRun:
        require(self, RUN_AGENT)
        if role.json_schema is not None:
            require(self, STRUCTURED_ANSWER)
        try:
            return run_agent(workspace, self.command(role), report_file, timeout, sandbox, mounts)
        except EngineerError as error:
            raise PlatformError(str(error)) from error
