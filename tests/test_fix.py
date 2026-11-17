"""The proposed fix as a diff: exact text, applies with git, never follows links."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from tenx.fix import MAX_TEXT_BYTES, proposed_diff

HEADER = (
    "# The fix proposed for the project: what the engineer changed in a copy of it.\n"
    "# Your project was not changed. Review this, then apply it in the project's\n"
    "# folder with: git apply FIX.diff (or: patch -p1 < FIX.diff)\n"
)
LEAVE_OUT = ("target", "*.duckdb", "GOAL.md", "source_profiles")


def text_files(folder: Path) -> dict[str, str]:
    return {
        path.relative_to(folder).as_posix(): path.read_text()
        for path in sorted(folder.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }


class DiffTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tenx-fix-"))
        self.addCleanup(shutil.rmtree, self.root)
        self.before = self.root / "before"
        (self.before / "models").mkdir(parents=True)
        (self.before / "dbt_project.yml").write_text("name: shop\n")
        (self.before / "models" / "orders.sql").write_text(
            "select\n  id,\n  amount * 100\nfrom raw\n"
        )
        (self.before / "models" / "old.sql").write_text("select 1\n")
        self.after = self.root / "after"
        shutil.copytree(self.before, self.after)

    def diff(self) -> str:
        return proposed_diff(self.before, self.after, LEAVE_OUT)

    def test_an_edit_an_added_file_and_a_removed_file_written_by_hand(self):
        (self.after / "models" / "orders.sql").write_text("select\n  id,\n  amount\nfrom raw\n")
        (self.after / "models" / "old.sql").unlink()
        (self.after / "models" / "new.sql").write_text("select 2")
        self.assertEqual(
            self.diff(),
            HEADER + "--- /dev/null\n"
            "+++ b/models/new.sql\n"
            "@@ -0,0 +1 @@\n"
            "+select 2\n"
            "\\ No newline at end of file\n"
            "--- a/models/old.sql\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-select 1\n"
            "--- a/models/orders.sql\n"
            "+++ b/models/orders.sql\n"
            "@@ -1,4 +1,4 @@\n"
            " select\n"
            "   id,\n"
            "-  amount * 100\n"
            "+  amount\n"
            " from raw\n",
        )

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_the_diff_applies_with_git_and_gives_the_fix(self):
        (self.after / "models" / "orders.sql").write_text("select\n  id,\n  amount\nfrom raw")
        (self.after / "models" / "old.sql").unlink()
        (self.after / "models" / "marts").mkdir()
        (self.after / "models" / "marts" / "totals.sql").write_text("select sum(amount)\n")
        target = self.root / "applied"
        shutil.copytree(self.before, target)
        (target / "FIX.diff").write_text(self.diff())
        applied = subprocess.run(
            ["git", "apply", "FIX.diff"], cwd=target, capture_output=True, text=True, check=False
        )
        self.assertEqual(applied.returncode, 0, applied.stderr)
        (target / "FIX.diff").unlink()
        self.assertEqual(text_files(target), text_files(self.after))

    def test_a_link_is_named_and_never_followed(self):
        secret = self.root / "declaration.json"
        secret.write_text("the hidden checks\n")
        (self.after / "models" / "peek.sql").symlink_to(secret)
        (self.after / "models" / "old.sql").unlink()
        (self.after / "models" / "old.sql").symlink_to(secret)
        diff = self.diff()
        self.assertNotIn("hidden checks", diff)
        self.assertEqual(
            diff,
            HEADER + "# Changes not in the diff below:\n"
            f"#   added, a link to {secret} (not followed): models/old.sql\n"
            f"#   added, a link to {secret} (not followed): models/peek.sql\n"
            "--- a/models/old.sql\n"
            "+++ /dev/null\n"
            "@@ -1 +0,0 @@\n"
            "-select 1\n",
        )

    def test_a_linked_folder_is_not_walked(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "secret.sql").write_text("select 'hidden'\n")
        (self.after / "models" / "more").symlink_to(outside)
        diff = self.diff()
        self.assertNotIn("hidden", diff)
        self.assertIn(f"added, a link to {outside} (not followed): models/more", diff)

    def test_binary_large_and_special_files_are_named_only(self):
        (self.after / "models" / "orders.sql").write_bytes(b"select \xff\n")
        (self.after / "big.csv").write_text("x" * (MAX_TEXT_BYTES + 1))
        (self.after / "nul.bin").write_bytes(b"a\0b")
        os.mkfifo(self.after / "pipe")
        self.assertEqual(
            self.diff(),
            HEADER + "# Changes not in the diff below:\n"
            "#   added, not shown (not text or over 256 KiB): big.csv\n"
            "#   changed, not shown (not text or over 256 KiB): models/orders.sql\n"
            "#   added, not shown (not text or over 256 KiB): nul.bin\n"
            "#   added, not a file, folder or link: pipe\n",
        )

    def test_builds_and_inputs_are_left_out_and_no_change_says_so(self):
        (self.after / "target").mkdir()
        (self.after / "target" / "run_results.json").write_text("{}\n")
        (self.after / "dev.duckdb").write_text("db\n")
        (self.after / "GOAL.md").write_text("Build orders.\n")
        (self.after / "source_profiles").mkdir()
        (self.after / "source_profiles" / "orders.txt").write_text("table orders\n")
        self.assertEqual(self.diff(), HEADER + "# The engineer changed nothing.\n")


if __name__ == "__main__":
    unittest.main()
