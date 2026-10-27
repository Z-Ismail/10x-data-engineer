import unittest
from pathlib import Path

from tenx import engineer, ports
from tenx.ports import (
    OVERLAP,
    PROFILE,
    AgentBackend,
    AgentRun,
    BuildResult,
    SourceProfiler,
    TransformEngine,
    UnsupportedCapability,
    Validator,
    require,
)


class ProfilesOnly:
    """A profiler for a platform that cannot compare columns across tables."""

    name = "flat files"

    def capabilities(self) -> frozenset[str]:
        return frozenset({PROFILE})

    def profile(self, raw: Path, out: Path) -> list[dict]:
        return []

    def overlap(self, raw: Path, out: Path) -> dict:
        require(self, OVERLAP)
        return {}


class NoMethods:
    name = "nothing"

    def capabilities(self) -> frozenset[str]:
        return frozenset()


class CapabilityTest(unittest.TestCase):
    def test_missing_capability_is_refused_with_the_reason(self):
        profiler = ProfilesOnly()
        self.assertEqual(profiler.profile(Path("raw"), Path("out")), [])
        with self.assertRaises(UnsupportedCapability) as caught:
            profiler.overlap(Path("raw"), Path("out"))
        self.assertEqual(str(caught.exception), "flat files cannot overlap")
        self.assertEqual(caught.exception.capability, "overlap")
        self.assertEqual(caught.exception.implementation, "flat files")

    def test_declared_capability_passes(self):
        require(ProfilesOnly(), PROFILE)

    def test_capability_names_are_distinct(self):
        names = [
            ports.PROFILE,
            ports.OVERLAP,
            ports.SCAFFOLD,
            ports.BUILD,
            ports.VALIDATE,
            ports.RUN_AGENT,
            ports.STRUCTURED_ANSWER,
        ]
        self.assertEqual(len(set(names)), len(names))


class ProtocolTest(unittest.TestCase):
    def test_a_profiler_written_elsewhere_satisfies_the_interface(self):
        self.assertIsInstance(ProfilesOnly(), SourceProfiler)

    def test_an_object_without_the_methods_does_not(self):
        for interface in (SourceProfiler, TransformEngine, Validator, AgentBackend):
            with self.subTest(interface=interface.__name__):
                self.assertNotIsInstance(NoMethods(), interface)


class SharedTypesTest(unittest.TestCase):
    def test_the_runner_uses_the_same_types(self):
        self.assertIs(engineer.AgentRun, AgentRun)
        self.assertIs(engineer.Role, ports.Role)
        self.assertIsInstance(engineer.ENGINEER, ports.Role)

    def test_agent_run_descriptions(self):
        self.assertEqual(
            AgentRun(None, None, timed_out_after=1800).describe(),
            "stopped: did not finish within 30 minutes",
        )
        self.assertEqual(
            AgentRun(None, None, timed_out_after=2.5).describe(),
            "stopped: did not finish within 2.5 seconds",
        )
        self.assertEqual(
            AgentRun(1, None, message="boom").describe(), "exited 1 without a report: boom"
        )
        self.assertEqual(
            AgentRun(0, {"subtype": "success", "num_turns": 7}).describe(),
            "success after 7 turns (exit 0)",
        )

    def test_build_result_ok_only_on_exit_zero(self):
        self.assertTrue(BuildResult(0, None, "").ok)
        self.assertFalse(BuildResult(1, {}, "error").ok)
        self.assertFalse(BuildResult(2, None, "").ok)


if __name__ == "__main__":
    unittest.main()
