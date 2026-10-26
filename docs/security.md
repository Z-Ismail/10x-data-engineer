# Security

The engineer agent may run only `dbt`, but dbt can read files through DuckDB
and can run Python models, so tool permissions alone do not stop an agent
reading files outside its project. So every agent runs inside an
operating-system sandbox, which applies to everything the agent starts.

On Linux the sandbox is [bubblewrap](https://github.com/containers/bubblewrap).
Inside it the computer's files are read-only; the home folder, the temporary
folders, the repository and the run folder are replaced by empty folders that
are discarded when the agent stops; then only what the agent's work needs is
put back: its own folder (the one place it can write), the add-on's agent
files, the programs it runs (Claude Code, Python and dbt), Claude Code's own
settings and sign-in, and, for the engineer, the raw files, read-only. The
gate's expectations, the reference project the gate is tested against, the
gate's work and the other agents' reports are never visible. The agent's
processes have their own process ids and stop when the run stops. The tests
check this by having a stand-in agent try to read each of these by `cat`, by
Python, by `dbt show` with `read_text()` and by a dbt Python model, and run in
CI.

Claude Code can put the shell commands it runs into a memory cgroup, with
`CLAUDE_CODE_TOOL_MEMORY_LIMIT`. Inside the sandbox `/sys` is read-only, so
when the runner is started from Claude Code's own shell, whose cgroup an
agent would reuse, no command could start. The runner sets
`CLAUDE_CODE_TOOL_MEMORY_LIMIT=none` for every sandboxed agent, so its
commands are not held to that memory cap.

The gate builds the agent's project twice with dbt, and building runs the
project's code, including any dbt Python model the agent wrote. So each build
runs in the same sandbox: it sees the project and the raw files read-only and
can write only its own work folder. The gate's declaration and the rows it
expects are hidden from both builds, and the first build's database from the
second, so a model can neither copy the answers nor change what it is
compared with. The gate's own checks, which read the declaration and the
built databases, run outside the sandbox after each build. The tests check
this with a project whose Python model tries, during both builds, to read
the declaration, the expected rows, the first build's database and a key in
the home folder, and to change the declaration, the gate's databases, the
project, the raw files and the home folder; every attempt fails in the
sandbox and succeeds without it. `python -m tenx.gate` also builds in the
sandbox, and refuses where none can be set up unless given `--unisolated`.

If bubblewrap is missing or cannot set up a sandbox, the runner refuses to
start the agents and says how to install or allow it. On Ubuntu 24.04 and
later, AppArmor allows bubblewrap to set up a sandbox only with a profile
that permits it; [the CI workflow](../.github/workflows/ci.yml) shows one. On macOS and Windows
there is no tested sandbox yet, so the runner refuses there too.
`--unisolated` runs the agents without a sandbox; the run's report and
`usage.json` then say so.

What the sandbox does not cover yet:

- The agents and the gate's builds keep network access, which Claude Code
  needs, so an agent could send out the raw data it can read.
- The agents and the gate's builds see your environment variables, and the
  agents see Claude Code's settings folder, which holds its sign-in and the
  history of your other sessions.

Until these are closed, run the agents only on data you would be comfortable
sharing with them, and keep credentials out of the environment you run them
from.
