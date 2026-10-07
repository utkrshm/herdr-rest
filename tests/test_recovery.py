import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_rest.config import Config
from herdr_rest.daemon import Daemon
from herdr_rest.presentation import agent_snapshot
from herdr_rest.state import Registry
from tests.test_plugin import Clock, FakeCli, agent


class TestRestartRecovery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.cli = FakeCli()
        self.clock = Clock()
        self.daemon = Daemon(self.cli, Registry(self.path), Config(focus_debounce_seconds=0), self.path / "mutations.lock", self.clock)
        self.record = {
            "terminal_id": "old-terminal", "pane_id": "w:p1", "workspace_id": "w", "tab_id": "w:t1",
            "cwd": "/project", "kind": "opencode", "session": "native-session", "session_ref_kind": "id",
            "session_source": "herdr:opencode", "name": "worker", "session_title": "Fix tests",
            "sleeping_label": "[sleeping] opencode: Fix tests", "original_label": None,
            "last_active_at": 1234, "process": {"pid": 999, "start_time": "old-start"},
        }
        self.pane = {
            "terminal_id": "new-terminal", "pane_id": "w:p1", "workspace_id": "w", "tab_id": "w:t1",
            "cwd": "/project", "label": self.record["sleeping_label"], "focused": False,
        }
        self.cli.panes_now = [self.pane]
        self.cli.process = {
            "shell_pid": 40, "foreground_process_group_id": 40,
            "foreground_processes": [{"pid": 40, "name": "zsh", "argv": ["/bin/zsh"]}],
        }

    def test_restored_terminal_rebinds_saved_session_and_remains_visible(self):
        records = {"old-terminal": dict(self.record)}
        with mock.patch("herdr_rest.lifecycle.os.kill") as signal:
            self.daemon._reconcile(records, [], self.cli.panes_now)
        signal.assert_not_called()
        self.assertEqual(set(records), {"new-terminal"})
        rebound = records["new-terminal"]
        self.assertEqual(rebound["session"], "native-session")
        self.assertEqual(rebound["last_active_at"], 1234)
        self.assertNotIn("process", rebound)
        row = agent_snapshot(self.cli, self.path)[0]
        self.assertEqual(row["agent_status"], "sleeping")
        self.assertEqual(row["session_title"], "Fix tests")

        self.pane["focused"] = True
        self.daemon._resume_focused(records, self.cli.panes_now, 0)
        self.assertEqual(self.cli.started, [("worker", "opencode", "w:p1", ["--session", "native-session"])])
        self.assertIsNone(self.pane["label"])

    def test_missing_pane_archives_resume_information_and_recovers_when_restore_finishes(self):
        records = {"old-terminal": dict(self.record)}
        self.daemon._reconcile(records, [], [])
        self.assertEqual(records, {})
        saved = Registry(self.path, "orphaned.json").load()
        self.assertEqual(saved["old-terminal"]["session"], "native-session")
        self.daemon._reconcile(records, [], self.cli.panes_now)
        self.assertEqual(set(records), {"new-terminal"})
        self.assertEqual(Registry(self.path, "orphaned.json").load(), {})

    def test_reused_or_changed_pane_does_not_receive_an_old_session(self):
        for override in ({"cwd": "/other"}, {"foreground_cwd": "/other"}, {"label": "Another pane"}, {"workspace_id": "another"}, {"tab_id": "w:t2"}):
            with self.subTest(override=override):
                self.cli.panes_now = [{**self.pane, **override}]
                records = {"old-terminal": dict(self.record)}
                self.daemon._reconcile(records, [], self.cli.panes_now)
                self.assertEqual(records, {})
                self.assertEqual(self.cli.started, [])
                self.assertEqual(Registry(self.path, "orphaned.json").load()["old-terminal"]["session"], "native-session")

    def test_busy_or_exec_replaced_shell_defers_rebinding(self):
        self.cli.process["foreground_process_group_id"] = 41
        records = {"old-terminal": dict(self.record)}
        self.daemon._reconcile(records, [], self.cli.panes_now)
        self.assertEqual(records, {})
        self.cli.process["foreground_process_group_id"] = 40
        self.cli.process["foreground_processes"][0]["name"] = "vim"
        self.daemon._reconcile(records, [], self.cli.panes_now)
        self.assertEqual(records, {})
        self.cli.process["foreground_processes"][0]["name"] = "zsh"
        self.daemon._reconcile(records, [], self.cli.panes_now)
        self.assertEqual(set(records), {"new-terminal"})

    def test_conflicting_saved_sessions_never_choose_a_resume_target(self):
        records = {
            "old-terminal": dict(self.record),
            "another-old-terminal": {**self.record, "terminal_id": "another-old-terminal", "session": "different-session"},
        }
        self.daemon._reconcile(records, [], self.cli.panes_now)
        self.assertEqual(records, {})
        self.assertEqual(len(Registry(self.path, "orphaned.json").load()), 2)
        self.assertEqual(self.cli.started, [])

    def test_native_restore_of_the_same_session_cleans_up_without_starting_it_twice(self):
        live = agent(terminal="new-terminal", kind="opencode")
        live["agent_session"] = {"source": "herdr:opencode", "agent": "opencode", "kind": "id", "value": "native-session"}
        self.cli.agents_now = [live]
        records = {"old-terminal": dict(self.record)}
        self.daemon._reconcile(records, self.cli.agents_now, self.cli.panes_now)
        self.assertEqual(records, {})
        self.assertEqual(self.cli.started, [])
        self.assertIsNone(self.pane["label"])
