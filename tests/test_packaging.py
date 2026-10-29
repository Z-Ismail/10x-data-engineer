"""The built package holds every module of the source tree."""

import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def source_modules() -> set[str]:
    """Every Python file under ``tenx/``, as a path inside the package."""
    return {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tenx").rglob("*.py")
        if "__pycache__" not in path.parts
    }


class WheelTest(unittest.TestCase):
    def test_wheel_holds_every_module(self):
        with tempfile.TemporaryDirectory() as folder:
            # Built from a copy, so that an earlier build left in the tree cannot
            # add files of its own to the wheel.
            source = Path(folder) / "source"
            source.mkdir()
            for name in ("pyproject.toml", "README.md", "LICENSE"):
                shutil.copy2(ROOT / name, source / name)
            shutil.copytree(
                ROOT / "tenx", source / "tenx", ignore=shutil.ignore_patterns("__pycache__")
            )
            built = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "wheel",
                    "--no-deps",
                    "-q",
                    "-w",
                    folder,
                    str(source),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=300,
            )
            self.assertEqual(built.returncode, 0, built.stderr)
            (wheel,) = Path(folder).glob("tenx-*.whl")
            with zipfile.ZipFile(wheel) as archive:
                packaged = {name for name in archive.namelist() if name.startswith("tenx/")}
        expected = source_modules()
        self.assertIn("tenx/adapters/duckdb.py", expected)
        self.assertEqual(packaged, expected)


if __name__ == "__main__":
    unittest.main()
