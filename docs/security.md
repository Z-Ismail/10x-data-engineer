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
files, the programs it runs (Claude Code, Python and dbt) and, for the
engineer, the raw files, read-only. The
gate's expectations, the reference project the gate is tested against, the
gate's work and the other agents' reports are never visible. The agent's
processes have their own process ids and stop when the run stops. The tests
check this by having a stand-in agent try to read each of these by `cat`, by
Python, by `dbt show` with `read_text()` and by a dbt Python model, and run in
CI.

Claude Code's own config folder (`~/.claude`, or `CLAUDE_CONFIG_DIR`) and
`~/.claude.json` are not shown either. They hold your settings, including
hooks, which are commands Claude Code runs outside any sandbox, and the
history of your other sessions; an agent that could write there could leave
a hook that runs with your full rights the next time you start Claude Code.
Each sandboxed agent instead gets an empty config folder of its own, in the
temporary folder, which is deleted when the agent ends, also when it is
stopped. Only your sign-in file, `.credentials.json`, if you have one, is
shown inside it, read-write, so a token Claude Code refreshes during a run
is saved where your own Claude Code reads it. Your settings, hooks, plugins
and `CLAUDE.md` do not apply to sandboxed agents; managed settings kept
outside the home folder stay readable to them. If the runner itself is
killed outright, the folder (the agent's session files, never a copy of your
sign-in) is left in the temporary folder. The tests plant settings, another
session's transcript, `~/.claude.json` and a sign-in file in a home folder,
and check that a stand-in agent sees only the sign-in file, cannot read the
rest, cannot change your settings, that a write to the sign-in file reaches
yours, and that its folder is gone afterwards. An unisolated run uses your
own config folder.

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

When a graph run repairs a failed project (`--repairs`), the engineer is
told what the gate found, but never the rows the gate worked out from its
hidden reference query, only how many differ; a check that does not say
what may be shown is reported by name alone. The engineer's project is
copied into the repair's folder outside the sandbox, with any link it made
copied as a link and not followed.

When a graph run has the architect agent write a spec (`--architect`),
the spec's rules for leaving rows out are SQL the agent wrote, and the run
counts them on the raw files. DuckDB's own advice is to treat SQL like code
and not to run it from an untrusted source without a sandbox, so the counts
are made by a separate Python process in the same sandbox: it sees the raw
files read-only and nothing of the run, the repository or the gate's
expectations. Inside it, DuckDB may read only the raw folder and cannot
change its settings, each count must be a single `SELECT`, and the process
is stopped after five minutes. The tests check that a rule which adds a
statement of its own, reads a file outside the raw folder or writes into it
is refused inside the counting process; that with those guards taken out,
the sandbox alone shows the rule an empty file and keeps the raw folder
unchanged; and that the same rules read and write without either.

A run that waits for a person to review the spec (`--review-spec`) treats a
contract the person gives (`approve --contract`) the same way: it is checked
against the profiles and its rules are counted in that sandbox before it
becomes the engineer's `SPEC.md`, wherever the rules came from. An approval
is for the spec the run showed, by its SHA-256: if `SPEC.md` was changed on
disk after the run wrote it, the approval is refused and the run keeps
waiting, so the engineer never works from a spec that was not checked.

The review packet (`tenx/review.py`), which a reviewer will be given after
a gate pass, is copied from the engineer's folder outside the sandbox. The copy follows no link the engineer left (a link to the
declaration is left out and named, never read), opens only regular files
(a named pipe left in the models cannot stall it), and caps each file and
the whole packet. A packet file with the same bytes as the declaration or
any of the gate's working files refuses the packet. The tests plant links
to the declaration, the expected rows and the gate's folder in a project
the real gate passed, and check that none of them, no reference query and
no expected row reaches the packet; with links followed and the second
guard taken out, the declaration and its reference query do.

The diagnosis packet (`tenx/diagnosis.py`), which the Diagnostics agent
is given after a gate failure, is made the same way, and also carries
dbt's results and log from the gate's first build. That build ran in the
sandbox with the declaration and expected rows hidden, so its files cannot
hold them; the same second guard applies. The folders those files name
(the project, the raw files and the gate's work folder) are replaced by
neutral names, and a test checks that no folder of a real run is named in
any packet file.

The reviewer agent (`--reviewer`) runs in the same sandbox as the
cross-source agent: read-only tools, its own folder holding only the
packet, no raw files, and the repository, the run folder and the
declaration hidden. It has no shell and no access to the built database.
Its findings are checked against the packet's files before they are kept,
and they are advice: nothing it says changes the gate's verdict.

The Diagnostics agent (`python -m tenx.run diagnose`) runs in the same
sandbox as the reviewer: read-only tools, its own folder holding only the
packet, no raw files, no shell and no access to the built database. Its
facts and causes are checked against the packet's files and the gate's
results before they are kept, and they never change the gate's verdict.
The project it is pointed at is copied into the run's folder before
anything reads it, outside the sandbox, so the copy follows no link (a
link is copied as a link) and opens nothing that is not a regular file;
the gate builds the copy in the sandbox, and the project itself is never
written. When the diagnosis is measured on the failing bed (`make
diagnosis-rates`), or fixes with and without it (`make fix-rates`), the
bed with its answers and the measurement's folder, which holds the other
runs' diagnoses and fixes, are hidden from the gate's builds and from the
agents as well.

With `--fixes`, the engineer that fixes a diagnosed project runs in the
same sandbox as in a build run, on the run's copy, and the project itself
is still never written. The run's own `profiles.yml` replaces any the
project carries, so the agent's builds go to a DuckDB file in its folder,
never to a connection the project names. The proposed change, `FIX.diff`,
is written outside the sandbox from the agent's files, so it follows no
link and opens nothing that is not a regular file; applying it is left to
you.

If bubblewrap is missing or cannot set up a sandbox, the runner refuses to
start the agents and says how to install or allow it. On Ubuntu 24.04 and
later, AppArmor allows bubblewrap to set up a sandbox only with a profile
that permits it; [the CI workflow](../.github/workflows/ci.yml) shows one. On macOS and Windows
there is no tested sandbox yet, so the runner refuses there too.
`--unisolated` runs the agents without a sandbox; the run's report and
`usage.json` then say so.

The graph run (`python -m tenx.run`) uses the same sandbox for its agents
and the gate's builds. Its checkpoint file, attempt manifests and spending
records are in the run folder, which no agent can see. If the run is
killed, bubblewrap stops the agent it had started (`--die-with-parent`); a
run started with `--unisolated` leaves that agent running until it ends.

Environment variables are kept out of the sandbox unless they are on a
short list, because a model the agent writes can read any variable it is
given (dbt's `env_var`, a Python model, a shell command). Every sandboxed
process, the gate's builds and the contract's counting included, gets
`PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LANGUAGE`, `TZ` and the
`LC_` variables. The agents also get what Claude Code needs: variables
starting `ANTHROPIC_` or `CLAUDE_`, the proxy variables (`HTTPS_PROXY`,
`HTTP_PROXY`, `NO_PROXY`, either case), `NODE_EXTRA_CA_CERTS`,
`SSL_CERT_FILE`, `SSL_CERT_DIR`, `DISABLE_TELEMETRY`,
`DISABLE_ERROR_REPORTING`, `DISABLE_AUTOUPDATER`, `DO_NOT_TRACK` and
`API_TIMEOUT_MS`. The temporary-folder variables are left out, so programs
use the sandbox's own empty `/tmp`. To pass another variable, list its name
in `TENX_SANDBOX_ENV` (names separated by commas); anything passed this way
can be read by the agent and by the models it writes. Cloud credentials for
Claude Code on Amazon Bedrock, Google Cloud or Microsoft Foundry are not on
the list, for that reason. An unisolated run passes your whole environment.
The tests plant secrets in the runner's environment and check that a
stand-in agent, dbt's `env_var` in its build and in the gate's builds, and
the counting process cannot read them, and that they can without the
sandbox.

The gate's builds and the contract's counting process run agent-written
code but need nothing from outside, so they run without the network: they
get a network of their own with only a loopback interface
(`--unshare-net`), and `/run`, where the system keeps its sockets (Docker's,
the D-Bus system bus), is an empty folder for them, because a socket file
can still be reached through a read-only mount and from another network.
The start-up check sets up such a sandbox, so a machine where bubblewrap
cannot make one is refused before any agent runs. dbt loads a `.env` file
from the folder it starts in or any folder above it; the gate's builds run
with `PYTHON_DOTENV_DISABLED=1`, sandboxed or not, so a `.env` beside the
project or above the gate's work folder cannot set variables for them. In
the tests, a model in the gate's builds and a stand-in counting process try
to connect to a listener on the host, which works only without the sandbox;
a sandboxed process with the network cut cannot reach a socket file in the
folder standing for `/run`, and reaches it with the network; and the gate's
builds read variables from `.env` files only when the switch is taken away.

The agents keep the network, and `/run` too, since on some systems
`/etc/resolv.conf` leads into it. Instead, every socket found in `/run` and
`/var/run` when an agent starts is covered: a socket in a folder of its own
(containerd's under `/run/containerd`, the D-Bus system bus under `/run/dbus`) by
emptying that folder, so a service that makes its socket again while the
agent runs stays hidden; one straight in `/run` (`/run/docker.sock`), or
beside a name-lookup folder, by an empty file. A link to a socket covers
the socket it leads to. The sockets that answer name lookups,
`/run/systemd/resolve` and `/run/nscd`, stay. In the tests, a process in
the real sandbox with the network kept connects to a listener on the host
but not to a socket in a folder standing for `/run`, nor to one made again
there while it runs; it reaches a socket in a name-lookup folder and one
outside the sockets folders; and no socket of the computer running the
tests is left in its `/run` but the name-lookup ones.

What the sandbox does not cover yet:

- The agents keep network access, which Claude Code needs, so an agent
  could send out the raw data it can read.
- A socket straight in `/run` that a service makes again while an agent
  runs (Docker restarted, say) is visible to that agent from then on, as is
  any socket that first appears after the agent started.
- Socket files outside `/run` (in a folder of yours that is not hidden)
  can be reached from any sandboxed process.
- An agent can read and change its own sign-in file, as Claude Code itself
  must, and with network access could send the token out.

Until these are closed, run the agents only on data you would be comfortable
sharing with them.
