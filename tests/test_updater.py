from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "lib"))
import updater
from worker_control import WorkerError


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "BRIGADE_TEST_REPORT": str(self.root / "validation-report"),
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.remote = self.root / "origin"
        self.remote.mkdir()
        self.git(self.remote, "init", "-q", "-b", "master")
        for name in ("bin/brigade", "bin/5stack", "scripts/worker.py", "lib/worker_control.py", "lib/updater.py"):
            destination = self.remote / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, destination)
        (self.remote / ".gitignore").write_text("__pycache__/\nprivate.txt\n")
        (self.remote / "value.txt").write_text("original\n")
        (self.remote / "scripts/check.sh").write_text("#!/bin/sh\nexit 0\n")
        self.original = self.commit(self.remote)
        self.installation = self.clone("installed brigade")

    def git(self, directory, *arguments):
        result = subprocess.run(["git", *arguments], cwd=directory, capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def clone(self, name):
        destination = self.root / name
        self.git(self.root, "clone", "--quiet", str(self.remote), str(destination))
        return destination

    def commit(self, repository):
        self.git(repository, "add", "--all")
        self.git(repository, "-c", "user.name=Update test", "-c", "user.email=update@example.invalid", "commit", "-qm", "Fixture revision")
        return self.git(repository, "rev-parse", "HEAD")

    def new_revision(self, check="#!/bin/sh\nexit 0\n"):
        (self.remote / "value.txt").write_text("updated\n")
        (self.remote / "scripts/check.sh").write_text(check)
        return self.commit(self.remote)

    def update(self, repository=None):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            updater.update(repository or self.installation)
        return output.getvalue()

    def assert_original(self, repository=None):
        self.assertEqual(self.git(repository or self.installation, "rev-parse", "HEAD"), self.original)

    def test_validates_target_in_disposable_checkout_before_fast_forward(self):
        target = self.new_revision('''#!/bin/sh
set -eu
test "$(cat value.txt)" = updated
printf '%s\\n' "$PWD" > "$BRIGADE_TEST_REPORT"
git rev-parse HEAD >> "$BRIGADE_TEST_REPORT"
''')
        output = self.update()
        candidate, validated = (self.root / "validation-report").read_text().splitlines()
        self.assertEqual(validated, target)
        self.assertNotEqual(Path(candidate), self.installation)
        self.assertFalse(Path(candidate).exists())
        self.assertEqual(self.git(self.installation, "rev-parse", "HEAD"), target)
        self.assertEqual(self.git(self.installation, "status", "--porcelain"), "")
        self.assertIn(target[:12], output)
        self.assertIn("fresh agent session", output)

    def test_current_checkout_needs_no_validation(self):
        output = self.update()
        self.assertIn("already up to date", output)
        self.assertNotIn("Checking candidate", output)
        self.assert_original()

    def test_local_edits_staged_changes_and_untracked_files_stop_before_fetch(self):
        target = self.new_revision()
        installations = [(kind, self.clone(kind)) for kind in ("unstaged", "staged", "untracked")]
        self.remote.rename(self.root / "unavailable-origin")
        for kind, installation in installations:
            with self.subTest(kind=kind):
                # This clone starts at the newer remote revision; local changes must still stop an update.
                name = "extra.txt" if kind == "untracked" else "value.txt"
                (installation / name).write_text("keep my edits\n")
                if kind == "staged":
                    self.git(installation, "add", name)
                before = self.git(installation, "status", "--porcelain")
                with self.assertRaisesRegex(WorkerError, "local edits"):
                    self.update(installation)
                self.assertEqual(self.git(installation, "rev-parse", "HEAD"), target)
                self.assertEqual(self.git(installation, "status", "--porcelain"), before)
                self.assertEqual((installation / name).read_text(), "keep my edits\n")

    def test_feature_branch_and_detached_head_are_not_updated(self):
        self.new_revision()
        for kind in ("feature", "detached"):
            with self.subTest(kind=kind):
                installation = self.clone(kind)
                self.git(installation, "checkout", "-qb", "feature") if kind == "feature" else self.git(installation, "checkout", "--detach")
                before = self.git(installation, "rev-parse", "HEAD")
                with self.assertRaisesRegex(WorkerError, "requires master"):
                    self.update(installation)
                self.assertEqual(self.git(installation, "rev-parse", "HEAD"), before)

    def test_local_commits_are_preserved(self):
        self.new_revision()
        (self.installation / "value.txt").write_text("local commit\n")
        local = self.commit(self.installation)
        with self.assertRaisesRegex(WorkerError, "commits outside"):
            self.update()
        self.assertEqual(self.git(self.installation, "rev-parse", "HEAD"), local)
        self.assertEqual((self.installation / "value.txt").read_text(), "local commit\n")

    def test_failed_candidate_checks_leave_installation_unchanged(self):
        self.new_revision("#!/bin/sh\necho candidate-failed >&2\nexit 1\n")
        with self.assertRaisesRegex(WorkerError, "Candidate checks failed.*") as raised:
            self.update()
        self.assertIn("candidate-failed", str(raised.exception))
        self.assert_original()
        self.assertEqual((self.installation / "value.txt").read_text(), "original\n")

    def test_missing_candidate_check_script_is_rejected(self):
        (self.remote / "scripts/check.sh").unlink()
        self.commit(self.remote)
        with self.assertRaisesRegex(WorkerError, "no scripts/check.sh"):
            self.update()
        self.assert_original()

    def test_unfinished_merge_stops_update(self):
        self.new_revision()
        (self.installation / ".git/MERGE_HEAD").write_text(self.original + "\n")
        with self.assertRaisesRegex(WorkerError, "current Git operation"):
            self.update()
        self.assert_original()

    def test_edits_made_during_validation_are_not_overwritten(self):
        self.new_revision('''#!/bin/sh
set -eu
printf '%s\\n' 'concurrent local edit' > "$BRIGADE_TEST_INSTALL/value.txt"
''')
        with mock.patch.dict(os.environ, {"BRIGADE_TEST_INSTALL": str(self.installation)}):
            with self.assertRaisesRegex(WorkerError, "local edits"):
                self.update()
        self.assert_original()
        self.assertEqual((self.installation / "value.txt").read_text(), "concurrent local edit\n")

    def test_ignored_file_is_not_overwritten_by_a_new_tracked_file(self):
        (self.installation / "private.txt").write_text("keep local data\n")
        (self.remote / "private.txt").write_text("remote tracked data\n")
        self.git(self.remote, "add", "--force", "private.txt")
        self.commit(self.remote)
        with self.assertRaises(WorkerError):
            self.update()
        self.assert_original()
        self.assertEqual((self.installation / "private.txt").read_text(), "keep local data\n")

    def test_network_failure_does_not_change_installed_files(self):
        self.remote.rename(self.root / "unavailable-origin")
        with self.assertRaises(WorkerError):
            self.update()
        self.assert_original()
        self.assertEqual((self.installation / "value.txt").read_text(), "original\n")

    def test_installed_symlink_updates_its_own_checkout_from_unrelated_directory(self):
        target = self.new_revision()
        binary = self.root / "brigade"
        binary.symlink_to(self.installation / "bin/brigade")
        unrelated = self.root / "unrelated"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("keep\n")
        result = subprocess.run([str(binary), "update"], cwd=unrelated, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git(self.installation, "rev-parse", "HEAD"), target)
        self.assertEqual(binary.resolve(), self.installation / "bin/brigade")
        self.assertEqual((unrelated / "keep.txt").read_text(), "keep\n")
        self.assertFalse((unrelated / ".git").exists())


if __name__ == "__main__":
    unittest.main()
