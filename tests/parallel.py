"""Run the test suite one module per process, several modules at a time.

    python tests/parallel.py [--jobs N] [--timeout SECONDS] [--folder tests]

Each module runs as `python -m unittest discover -s <folder> -p <module>.py`
from the current folder, which imports it exactly as `make test` does. Its
output goes to a file rather than a pipe, so a test that leaves a process
holding the output open cannot keep the run waiting; each module runs in its
own process group, which is stopped when the module ends or passes the time
limit.

Before starting, every module's tests are counted in this process, and a
module passes only if it exits 0 and ran exactly that many tests. The run
passes only if every module passes.
"""

from __future__ import annotations

import argparse
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

PATTERN = "test*.py"
RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
GRACE = 5.0

# Modules that took most of a minute or more, longest first. They start
# first so that none of them is left to start last; every other module runs
# after them, in name order.
SLOW_FIRST = (
    "test_gate",
    "test_equivalence",
    "test_engineer",
    "test_gate_sandbox",
    "test_sandbox",
    "test_adapter_equivalence",
    "test_paired_runners",
    "test_repair_runs",
)


@dataclass
class Outcome:
    module: str
    expected: int
    ran: int | None
    returncode: int | None
    seconds: float
    output: str
    timed_out: bool = False

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out and self.ran == self.expected

    def summary(self) -> str:
        if self.timed_out:
            state = "TIMED OUT"
        elif self.passed:
            state = "OK"
        elif self.returncode == 0:
            state = f"FAILED (ran {self.ran} of {self.expected} tests)"
        else:
            state = f"FAILED (exit {self.returncode})"
        return f"{self.module}: {state}, {self.ran or 0} tests, {self.seconds:.1f} s"


def modules(folder: Path) -> list[str]:
    """Test modules in the folder, slow ones first, the rest in name order."""
    found = sorted(path.stem for path in folder.glob(PATTERN) if path.is_file())
    slow = [name for name in SLOW_FIRST if name in found]
    return slow + [name for name in found if name not in slow]


def count(folder: Path, module: str) -> int:
    """Number of tests unittest finds in one module, loaded here."""
    loader = unittest.TestLoader()
    return loader.discover(str(folder), pattern=f"{module}.py").countTestCases()


def _stop(group: int) -> None:
    try:
        os.killpg(group, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_module(folder: Path, module: str, expected: int, timeout: float) -> Outcome:
    command = [sys.executable, "-m", "unittest", "discover", "-s", str(folder)]
    command += ["-p", f"{module}.py", "-v"]
    started = time.monotonic()
    timed_out = False
    with tempfile.TemporaryFile() as log:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=GRACE)
            except subprocess.TimeoutExpired:
                pass
        # Whatever the module started and left behind goes with it.
        _stop(process.pid)
        process.wait()
        seconds = time.monotonic() - started
        log.seek(0)
        output = log.read().decode("utf-8", errors="replace")
    found = RAN.findall(output)
    ran = int(found[-1]) if found else None
    return Outcome(module, expected, ran, process.returncode, seconds, output, timed_out)


def run(folder: Path, jobs: int, timeout: float, out=None) -> list[Outcome]:
    out = out or sys.stdout
    names = modules(folder)
    expected = {name: count(folder, name) for name in names}
    outcomes = []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(run_module, folder, name, expected[name], timeout) for name in names]
        for future in as_completed(futures):
            outcome = future.result()
            outcomes.append(outcome)
            print(f"===== {outcome.summary()}", file=out)
            print(outcome.output, file=out, flush=True)
    return outcomes


def report(outcomes: list[Outcome], seconds: float) -> str:
    lines = [outcome.summary() for outcome in sorted(outcomes, key=lambda o: -o.seconds)]
    ran = sum(outcome.ran or 0 for outcome in outcomes)
    expected = sum(outcome.expected for outcome in outcomes)
    failed = [outcome.module for outcome in outcomes if not outcome.passed]
    lines.append(f"Ran {ran} of {expected} tests in {len(outcomes)} modules in {seconds:.1f} s")
    lines.append(f"FAILED: {', '.join(sorted(failed))}" if failed else "OK")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--folder", type=Path, default=Path("tests"))
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds per module")
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.timeout <= 0:
        parser.error("--jobs and --timeout must be positive")
    # Import from the current folder first, as `python -m unittest` does, so
    # that counting here finds what the module processes find.
    sys.path.insert(0, os.getcwd())
    started = time.monotonic()
    outcomes = run(args.folder, args.jobs, args.timeout)
    print(report(outcomes, time.monotonic() - started), flush=True)
    return 0 if outcomes and all(outcome.passed for outcome in outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
