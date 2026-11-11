"""Offline git integration tests: no real remote, installation, or process changes."""
import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("local_install", Path(__file__).resolve().parents[1] / "scripts/local-install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class LocalUpdateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "checkout"
        installer.run("git", "init", "-b", "main", str(self.repo))
        installer.git(self.repo, "config", "user.name", "Test")
        installer.git(self.repo, "config", "user.email", "test@example.org")
        self.commit(self.repo, "shared", "base")
        for remote in ("origin", "upstream"):
            path = self.root / remote
            installer.run("git", "clone", str(self.repo), str(path))
            installer.git(path, "config", "user.name", "Test")
            installer.git(path, "config", "user.email", "test@example.org")
            installer.git(self.repo, "remote", "add", remote, str(path))

    def commit(self, repo, filename, content):
        (repo / filename).write_text(content)
        installer.git(repo, "add", filename)
        installer.git(repo, "commit", "-m", f"Change {filename}")
        return installer.git(repo, "rev-parse", "HEAD")

    def test_merges_both_remotes_and_keeps_local_commits(self):
        self.commit(self.repo, "local", "local")
        fork = self.commit(self.root / "origin", "fork", "fork")
        upstream = self.commit(self.root / "upstream", "upstream", "upstream")
        self.assertEqual(installer.merge_checkout(self.repo), (fork, upstream))
        for name in ("local", "fork", "upstream"):
            self.assertEqual((self.repo / name).read_text(), name)
        before = installer.git(self.repo, "rev-parse", "HEAD")
        installer.merge_checkout(self.repo)
        self.assertEqual(installer.git(self.repo, "rev-parse", "HEAD"), before)

    def test_conflict_leaves_original_checkout_untouched(self):
        self.commit(self.repo, "shared", "local")
        self.commit(self.root / "origin", "fork", "fork")
        self.commit(self.root / "upstream", "shared", "upstream")
        before = installer.git(self.repo, "rev-parse", "HEAD")
        with self.assertRaises(installer.MergeConflict) as failure:
            installer.merge_checkout(self.repo)
        self.assertEqual(failure.exception.files, ["shared"])
        self.assertEqual(installer.git(self.repo, "rev-parse", "HEAD"), before)
        self.assertEqual((self.repo / "shared").read_text(), "local")
        self.assertFalse((self.repo / "fork").exists())
        self.assertEqual(installer.git(self.repo, "status", "--porcelain"), "")
        self.assertEqual(len(installer.git(self.repo, "worktree", "list").splitlines()), 1)

    def test_untracked_file_refuses_update(self):
        (self.repo / "unsaved").write_text("do not overwrite")
        before = installer.git(self.repo, "rev-parse", "HEAD")
        with self.assertRaisesRegex(RuntimeError, "uncommitted"):
            installer.merge_checkout(self.repo)
        self.assertEqual(installer.git(self.repo, "rev-parse", "HEAD"), before)

    def test_install_preserves_state_and_generates_launchers(self):
        source = Path(__file__).resolve().parents[1]
        prefix = self.root / "prefix with spaces"
        app = prefix / "share/dsqrd"
        app.mkdir(parents=True)
        (app / "prefs-dsqrd.json").write_text('{"sidebarCollapsed": true}')
        interpreter = prefix / "share/dsqrd-venv/bin/python3"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(__import__("sys").executable)
        real_run = installer.run
        real_subprocess_run = subprocess.run
        def run(*args, **kwargs):
            if len(args) > 1 and args[1] == "-c":
                return ""  # Dependency imports are checked by the real installer.
            return real_run(*args, **kwargs)
        def subprocess_run(args, **kwargs):
            if args[1:4] == ["-m", "pip", "install"]:
                return subprocess.CompletedProcess(args, 0)
            return real_subprocess_run(args, **kwargs)
        with patch.object(installer, "run", run), patch.object(installer, "stop_processes") as stop, \
                patch("subprocess.run", subprocess_run):
            installer.install(source, prefix, restart=False)
        stop.assert_called_once_with(app)
        self.assertEqual((app / "prefs-dsqrd.json").read_text(), '{"sidebarCollapsed": true}')
        for name in ("dsqrd", "dsqrd-client", "dsqrd-update"):
            path = prefix / "bin" / name
            subprocess.run(["bash", "-n", str(path)], check=True)
            self.assertNotIn("@PREFIX@", path.read_text())
        daemon = (prefix / "bin/dsqrd").read_text()
        self.assertIn("DSQRD_FORK_REV", daemon)
        self.assertIn("DSQRD_UPSTREAM_REV", daemon)
        self.assertTrue((prefix / "share/.dsqrd-previous/prefs-dsqrd.json").exists())

    def test_no_automatic_push(self):
        self.commit(self.root / "upstream", "upstream", "upstream")
        origin_before = installer.git(self.root / "origin", "rev-parse", "HEAD")
        installer.merge_checkout(self.repo)
        self.assertEqual(installer.git(self.root / "origin", "rev-parse", "HEAD"), origin_before)


if __name__ == "__main__":
    unittest.main()
