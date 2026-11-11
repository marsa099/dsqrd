"""Herdr handoff contract tests. Never starts a real agent or modifies layout."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("local_install", Path(__file__).resolve().parents[1] / "scripts/local-install.py")
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class ConflictHandoffTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.conflict = installer.MergeConflict(self.root / "repo with spaces", "fork-sha", "upstream-sha", ["dsqrd.py"])
        self.calls = []
        self.has_workspace = True
        self.has_tab = False
        self.which = patch.object(installer.shutil, "which", return_value="/bin/mock")
        self.which.start()
        self.addCleanup(self.which.stop)

    def request(self, *args, env=None):
        self.calls.append((args, env))
        if args[:2] == ("workspace", "list"):
            return {"workspaces": [{"workspace_id": "workspace-id", "label": "arch"}] if self.has_workspace else []}
        if args[:2] == ("workspace", "create"):
            return {"workspace": {"workspace_id": "created-workspace"}}
        if args[:2] == ("tab", "list"):
            return {"tabs": [{"label": "dsqrd-update"}] if self.has_tab else []}
        if args[:2] == ("tab", "create"):
            return {"root_pane": {"pane_id": "returned-pane"}}
        return {}

    def open(self):
        with patch.object(installer, "herdr_request", self.request):
            installer.open_conflict_assistant(self.conflict, self.root / "prefix", self.root,
                                              {"socket": "/saved/herdr.sock"})

    def test_existing_arch_creates_named_tab_in_repo_and_exact_model(self):
        with patch.dict(os.environ, {"HERDR_PANE_ID": "unrelated-pane"}):
            self.open()
        args = [call[0] for call in self.calls]
        self.assertNotIn(("workspace", "create"), [call[:2] for call in args])
        tab = next(call for call in args if call[:2] == ("tab", "create"))
        self.assertEqual(tab[tab.index("--workspace") + 1], "workspace-id")
        self.assertEqual(tab[tab.index("--label") + 1], "dsqrd-update")
        self.assertEqual(tab[tab.index("--cwd") + 1], str(self.conflict.repo))
        self.assertIn("--no-focus", tab)
        start = next(call for call in args if call[:2] == ("agent", "start"))
        self.assertIn("returned-pane", start)
        self.assertEqual(start[start.index("--") + 1:],
                         ("--provider", "openai", "--model", "gpt-6.1-sol", "--name", "dsqrd-update"))
        prompt = next(call for call in args if call[:2] == ("agent", "prompt"))
        self.assertEqual(prompt[2], "returned-pane")
        for _, env in self.calls:
            self.assertEqual(env["HERDR_SOCKET_PATH"], "/saved/herdr.sock")
            self.assertNotIn("HERDR_PANE_ID", env)
        instructions = (self.root / "merge-instructions.md").read_text()
        for required in ("fork-sha", "upstream-sha", "dsqrd.py", "requirements.txt", "--no-assistant", "Do not push"):
            self.assertIn(required, instructions)
        self.assertEqual((self.root / "merge-instructions.md").stat().st_mode & 0o777, 0o600)

    def test_missing_arch_is_created_in_repo(self):
        self.has_workspace = False
        self.open()
        create = next(call[0] for call in self.calls if call[0][:2] == ("workspace", "create"))
        self.assertEqual(create[create.index("--cwd") + 1], str(self.conflict.repo))
        tab = next(call[0] for call in self.calls if call[0][:2] == ("tab", "create"))
        self.assertIn("created-workspace", tab)

    def test_existing_conflict_tab_prevents_competing_agents(self):
        self.has_tab = True
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.open()
        self.assertFalse(any(call[0][:2] == ("agent", "start") for call in self.calls))
        self.assertFalse(any(call[0][:2] == ("tab", "create") for call in self.calls))

    def test_missing_tools_fails_without_claiming_success(self):
        with patch.object(installer.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "need herdr and pi"):
                self.open()
        self.assertEqual(self.calls, [])

    def test_failed_agent_start_does_not_submit_prompt(self):
        def fail(*args, **kwargs):
            if args[:2] == ("agent", "start"):
                raise RuntimeError("agent_not_ready")
            return self.request(*args, **kwargs)
        with patch.object(installer, "herdr_request", fail):
            with self.assertRaisesRegex(RuntimeError, "agent_not_ready"):
                installer.open_conflict_assistant(self.conflict, self.root, self.root, {})
        self.assertFalse(any(call[0][:2] == ("agent", "prompt") for call in self.calls))


if __name__ == "__main__":
    unittest.main()
