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
Each build runs in its own process group and is stopped after 20 minutes
(`--build-timeout`), so a model that never finishes cannot hold the gate or
the run; whatever a build leaves running is killed when it ends, and in the
sandbox nothing survives bubblewrap. The tests stop real builds whose Python
model sleeps and starts another process, both with and without the sandbox.
Each build also starts with a limit on the memory it can address
(`RLIMIT_AS`): half the computer's memory, at least 4 GiB
(`--build-memory GIB`). Every process the build starts inherits it, inside
the sandbox too, and shared memory counts, so an allocation past it fails
inside the build (a Python model gets `MemoryError`, DuckDB an out-of-memory
error) and the project fails `build`, rather than the computer running out
of memory and its out-of-memory killer stopping one of your programs.
DuckDB is told to keep within a quarter of the limit, so a large query
writes part of its work to disk instead of reaching it. The tests build a
real project whose Python model asks for more than the limit, in the
sandbox.

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

A socket can also be bound to an abstract name instead of a file. It has no
file to cover, and it belongs to the network, which agents share with the
computer, so an X server, an older D-Bus session bus or any other program
listening on such a name would answer an agent. Where the kernel offers
Landlock's scoping (Linux 6.12 and later), each agent starts through a
short launcher, run with the runner's own Python inside the sandbox, that
restricts itself so that it and every process it starts can connect only to
abstract sockets made inside that restriction, and then becomes the agent.
If the restriction cannot be set, the launcher stops without starting the
agent, and the start-up check runs it once before any agent. The gate's
builds and the counting process need none of this: without the network they
cannot reach these sockets at all. In the tests, a process in the real
sandbox with the network kept cannot connect to a listener on an abstract
name outside, and can without the launcher; its own abstract socket between
its own processes, a socket file in a folder it is shown and a listener on
the host's network still answer it; and a launcher that cannot set the
restriction starts nothing. On older kernels agents run without it, and
`python -m tenx.engineer` says in its sandbox line that they are not kept
from abstract sockets.

Covering socket files one at a time still leaves gaps: a socket a service
makes again while an agent runs, or one that first appears after it
starts, a socket file outside `/run`, and abstract sockets on kernels
without Landlock's scope. Every one of them needs a new socket, so on
x86_64 and aarch64 the same launcher also installs a seccomp filter, which
the kernel applies to the agent and to every process it starts and which
cannot be removed. The filter refuses `socket()` for Unix sockets (with
"permission denied"), and Unix socket pairs other than stream pairs, which
programs use to talk to their own children and which reach nothing else (a
datagram pair can send to another socket by address). It also refuses
io_uring, which can open a socket without calling `socket()`, and any call
made through another architecture's entry, such as the 32-bit one a 64-bit
x86 program can still use, whose numbers differ. A name lookup cannot ask
a lookup service over a socket (nscd, systemd-resolved) either, so it
depends on the other sources your system lists, such as `/etc/resolv.conf`.
If the filter cannot be installed, the launcher starts
nothing, and the start-up check tries it once before any agent. In the
tests, the filter is run by an interpreter written apart from it on each
call that matters, and copies broken on purpose (no architecture check, no
x32 check, io_uring allowed, the socket type not masked) fail; installed in
a real process, it refuses Unix sockets, datagram pairs, io_uring and the
32-bit entry, in that process and its children, while network sockets and
stream pairs work; and in the real sandbox a process with the network kept
can no longer reach a socket file in a folder it is shown or an abstract
socket outside, even with Landlock's scope off, and can without the filter.
On other machines agents run without the filter, and `python -m
tenx.engineer` says so in its sandbox line.

The gate's builds and the counting process have no network and no `/run`,
but a socket file in a folder they are shown (the project, the raw data,
the Python install, or a system folder outside `/run`) would still answer a
dbt Python model or anything else they run. So on x86_64 and aarch64 they
start through the same launcher with the filter alone (Landlock's scope
adds nothing without the network), and the start-up check starts its
offline probe the same way, so a machine that cannot install the filter
fails before any build. dbt's build and the counting script were traced to
make no Unix socket, socket pair or io_uring call of their own. In the
tests, a dbt Python model in the gate's build and a stand-in for the
counting script, in the real sandbox, cannot reach a socket in the raw data
folder, and can without the filter; the build's process shows the filter
in force.

What the sandbox does not cover yet:

- The agents keep network access, which Claude Code needs, so an agent
  could send out the raw data it can read.
- On machines other than x86_64 and aarch64, a socket straight in `/run`
  that a service makes again while an agent runs (Docker restarted, say) is
  visible to that agent from then on, as is any socket that first appears
  after the agent started, and socket files outside `/run` (in a folder of
  yours that is not hidden) can be reached. On those machines with kernels
  before Linux 6.12, or where Landlock is turned off, an agent can also
  connect to sockets bound to abstract names outside the sandbox.
- On machines other than x86_64 and aarch64, the gate's builds and the
  counting process, which have no network and no `/run`, run without the
  filter, so a dbt Python model in the gate's build could still reach a
  socket file outside `/run` and your home folder.
- The names of abstract sockets stay readable in `/proc/net/unix`.
- A gate build's memory limit applies to each of its processes, not to all
  of them together: a hostile model that starts many processes can still
  take more in all. Its disk space is not limited, nor is the sandbox's
  `/tmp`, which is held in memory, so a hostile model can still fill either
  until the build is stopped. The contract's counting process is limited in
  time but not in memory.
- An agent can read and change its own sign-in file, as Claude Code itself
  must, and with network access could send the token out.

Until these are closed, run the agents only on data you would be comfortable
sharing with them.
