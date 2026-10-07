import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdr_rest.agents import original_agent_name
from herdr_rest.config import Config
from herdr_rest.daemon import Daemon
from herdr_rest.herdr import CliError, Herdr
from herdr_rest.state import Registry
from tests.test_plugin import Completed, FakeCli, agent


class TestResumeNames(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.cli = FakeCli()
        self.cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "focused": True}]
        self.daemon = Daemon(self.cli, Registry(self.path), Config(focus_debounce_seconds=0), self.path / "mutations.lock")
        self.record = {
            "terminal_id": "t1", "pane_id": "w:p1", "kind": "claude", "session": "s1",
            "name": "hibernate_deadbeef", "original_agent_name": None,
        }

    def test_resume_clears_temporary_name_and_does_not_change_pane_label(self):
        def start(name, kind, pane_id, args):
            self.cli.started.append((name, kind, pane_id, args))
            self.cli.agents_now = [agent() | {"name": name}]
        self.cli.start = start
        self.cli.panes_now[0]["label"] = "My pane"
        records = {"t1": dict(self.record)}
        self.daemon._resume_focused(records, self.cli.panes_now, 0)
        self.assertEqual(self.cli.agent_renamed, [("w:p1", None)])
        self.assertIsNone(self.cli.agents_now[0]["name"])
        self.assertEqual(self.cli.agents_now[0]["agent"], "claude")
        self.assertEqual(self.cli.panes_now[0]["label"], "My pane")
        self.assertEqual(records, {})

    def test_explicit_user_names_are_preserved_even_when_they_resemble_generated_names(self):
        for name in ("reviewer", "hibernate_deadbeef"):
            with self.subTest(name=name):
                record = {**self.record, "name": name, "original_agent_name": name}
                self.cli.agents_now = [agent() | {"name": name}]
                self.assertTrue(self.daemon._restore_agent_name(record))
                self.assertEqual(self.cli.agent_renamed, [])

    def test_user_edit_or_replacement_session_is_not_renamed(self):
        self.cli.agents_now = [agent() | {"name": "User edit"}]
        self.assertTrue(self.daemon._restore_agent_name(self.record))
        self.cli.agents_now = [agent() | {"name": "hibernate_deadbeef", "agent_session": {"agent": "claude", "value": "other-session"}}]
        self.assertTrue(self.daemon._restore_agent_name(self.record))
        self.assertEqual(self.cli.agent_renamed, [])

    def test_blocked_startup_without_session_report_still_clears_our_name(self):
        live = agent(status="blocked") | {"name": "hibernate_deadbeef"}
        live.pop("agent_session")
        self.cli.agents_now = [live]
        self.assertTrue(self.daemon._restore_agent_name(self.record, live))
        self.assertEqual(self.cli.agent_renamed, [("w:p1", None)])

    def test_cleanup_error_keeps_record_for_retry(self):
        def start(*args):
            self.cli.agents_now = [agent() | {"name": "hibernate_deadbeef"}]
        self.cli.start = start
        records = {"t1": dict(self.record)}
        with mock.patch.object(self.cli, "rename_agent", side_effect=CliError("temporarily unavailable")):
            self.daemon._resume_focused(records, self.cli.panes_now, 0)
        self.assertIn("t1", records)
        self.daemon._reconcile(records, self.cli.agents_now, self.cli.panes_now)
        self.assertEqual(records, {})
        self.assertEqual(self.cli.agent_renamed, [("w:p1", None)])

    def test_legacy_generated_names_and_custom_names_are_distinguished(self):
        self.assertIsNone(original_agent_name({"name": "hibernate_a462983b"}))
        self.assertEqual(original_agent_name({"name": "reviewer"}), "reviewer")

    def test_clear_name_uses_agent_rename_not_pane_rename(self):
        runner = mock.Mock(return_value=Completed({"result": {}}))
        Herdr("fake-herdr", runner).rename_agent("w:p1", None)
        self.assertEqual(runner.call_args.args[0], ["fake-herdr", "agent", "rename", "w:p1", "--clear"])
