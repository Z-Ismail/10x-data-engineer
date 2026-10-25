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

If bubblewrap is missing or cannot set up a sandbox, the runner refuses to
start the agents and says how to install or allow it. On Ubuntu 24.04 and
later, AppArmor allows bubblewrap to set up a sandbox only with a profile
that permits it; [the CI workflow](../.github/workflows/ci.yml) shows one. On macOS and Windows
there is no tested sandbox yet, so the runner refuses there too.
`--unisolated` runs the agents without a sandbox; the run's report and
`usage.json` then say so.

What the sandbox does not cover yet:

- The gate builds the agent's project with dbt outside the sandbox, so a
  dbt Python model the agent wrote runs with your access during the gate.
- The agents keep network access, which Claude Code needs, so an agent could
  send out the raw data it can read.
- The agents see your environment variables, and Claude Code's settings
  folder, which holds its sign-in and the history of your other sessions.

Until these are closed, run the agents only on data you would be comfortable
sharing with them, and keep credentials out of the environment you run them
from.
