import curses
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from herdr_rest.config import Config
from herdr_rest import view
from herdr_rest.__main__ import main as internal_main
from herdr_rest.view import HEADINGS, Viewport, last_active_text, remaining_text, table_lines


class TestInactivityView(unittest.TestCase):
    def test_remaining_protects_done_and_every_focused_project_session(self):
        self.assertEqual(remaining_text({"agent_status": "done", "remaining_seconds": 1}), "protected")
        for state in ("idle", "done", "working", "blocked", "unknown", "sleeping"):
            self.assertEqual(remaining_text({"agent_status": state, "workspace_focused": True}), "protected")

    def test_remaining_formats_only_idle_countdowns(self):
        self.assertEqual(remaining_text({"agent_status": "idle", "remaining_seconds": 60.1}), "1m 1s")
        self.assertEqual(remaining_text({"agent_status": "idle", "remaining_seconds": 3601}), "1h 0m 1s")
        self.assertEqual(remaining_text({"agent_status": "idle", "remaining_seconds": -1}), "0s")
        self.assertEqual(remaining_text({"agent_status": "idle", "remaining_seconds": None}), "unknown")
        for state in ("sleeping", "working", "blocked", "unknown"):
            self.assertEqual(remaining_text({"agent_status": state, "remaining_seconds": 20}), "—")

    def test_table_has_exact_headings_and_titles_with_safe_truncation(self):
        rows = [{"workspace": "Website", "agent": "claude", "session_title": "Fix authentication " * 10, "agent_status": "done"}]
        lines = table_lines(rows, 90)
        self.assertEqual(tuple(re.split(r" {2,}", lines[0].strip())), HEADINGS)
        self.assertIn("claude", lines[2])
        self.assertIn("protected", lines[2])
        self.assertIn("…", lines[2])
        self.assertEqual(view._width(lines[0]), view._width(lines[2]))

    def test_table_keeps_columns_aligned_for_unicode_session_titles(self):
        rows = [{"workspace": "项目", "agent": "pi", "session_title": "修复测试" * 20, "agent_status": "idle", "remaining_seconds": 61}]
        lines = table_lines(rows, 90)
        self.assertEqual(view._width(lines[0]), view._width(lines[2]))

    def test_snapshot_is_fetched_once_before_display(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"HERDR_SOCKET_PATH": "/tmp/test.sock", "HERDR_PLUGIN_STATE_DIR": directory}
            rows = [{"agent": "codex", "agent_status": "done"}]
            with mock.patch.dict(os.environ, env), mock.patch.object(view.Config, "load", return_value=Config()), mock.patch.object(view, "agent_snapshot", return_value=rows) as snapshot, mock.patch.object(view, "show_table") as display:
                view.main()
            snapshot.assert_called_once()
            self.assertEqual(display.call_args.args[0], rows)

    def test_action_opens_a_managed_view_instead_of_printing_output(self):
        with mock.patch.object(view.Config, "load", return_value=Config()), mock.patch.object(view, "Herdr") as client:
            view.open_view()
        client.return_value.json.assert_called_once_with("plugin", "pane", "open", "--plugin", "herdr.rest", "--entrypoint", "inactivity")

    def test_manifest_exposes_only_the_embedded_view_action(self):
        root = Path(__file__).resolve().parents[1]
        with (root / "herdr-plugin.toml").open("rb") as stream:
            manifest = tomllib.load(stream)
        self.assertEqual([item["id"] for item in manifest["actions"]], ["inactivity"])
        self.assertEqual(manifest["panes"][0]["placement"], "popup")
        self.assertEqual(manifest["panes"][0]["command"], ["python3", "-m", "herdr_rest", "view"])
        self.assertEqual(manifest["startup"][0]["command"], ["python3", "-m", "herdr_rest", "start"])
        self.assertFalse((root / "herdr_rest" / "cli.py").exists())

    def test_removed_standalone_command_cannot_start_a_watcher(self):
        result = subprocess.run([sys.executable, "-m", "herdr_rest", "inactivity"], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid choice", result.stderr)

    def test_internal_parser_dispatches_start_and_managed_view_commands(self):
        with mock.patch("herdr_rest.daemon.start_process", return_value=0) as start:
            self.assertEqual(internal_main(["start"]), 0)
        start.assert_called_once()
        self.assertIsInstance(start.call_args.args[0], dict)

        with mock.patch("herdr_rest.view.open_view") as open_view:
            self.assertEqual(internal_main(["open-view"]), 0)
        open_view.assert_called_once_with()

        with mock.patch("herdr_rest.state.request_focus") as focus:
            self.assertEqual(internal_main(["focus"]), 0)
        focus.assert_called_once_with()

    def test_last_active_formats_a_local_timestamp_and_handles_unknown_values(self):
        with mock.patch("herdr_rest.view.time.localtime", return_value=time.gmtime(0)):
            self.assertEqual(last_active_text({"last_active_at": 0}), "1970-01-01 00:00:00")
        for timestamp in (None, True, float("nan"), float("inf")):
            self.assertEqual(last_active_text({"last_active_at": timestamp}), "unknown")


class TestViewport(unittest.TestCase):
    def setUp(self):
        self.viewport = Viewport()
        self.lines = ["x" * 100 for _ in range(100)]

    def press(self, key):
        return self.viewport.navigate(key, self.lines, 20, 40)

    def test_vim_motions_and_bounds(self):
        self.press(ord("j"))
        self.assertEqual(self.viewport.top, 1)
        self.press(ord("k"))
        self.press(ord("k"))
        self.assertEqual(self.viewport.top, 0)
        self.press(4)
        self.assertEqual(self.viewport.top, 10)
        self.press(6)
        self.assertEqual(self.viewport.top, 30)
        self.press(21)
        self.press(2)
        self.assertEqual(self.viewport.top, 0)
        self.press(ord("G"))
        self.assertEqual(self.viewport.top, 80)
        self.press(ord("j"))
        self.assertEqual(self.viewport.top, 80)
        self.press(ord("g"))
        self.assertEqual(self.viewport.top, 80)
        self.press(curses.KEY_RESIZE)
        self.press(ord("g"))
        self.assertEqual(self.viewport.top, 0)

    def test_horizontal_scroll_and_resize(self):
        self.press(ord("l"))
        self.assertEqual(self.viewport.left, 4)
        self.press(ord("h"))
        self.assertEqual(self.viewport.left, 0)
        self.press(ord("G"))
        self.viewport.navigate(curses.KEY_RESIZE, ["short"], 20, 40)
        self.assertEqual(self.viewport.top, 0)
        self.assertEqual(self.viewport.left, 0)

    def test_g_prefix_is_cancelled_and_quit_returns_false(self):
        self.press(ord("G"))
        self.press(ord("g"))
        self.press(ord("k"))
        self.press(ord("g"))
        self.assertEqual(self.viewport.top, 79)
        self.assertFalse(self.press(ord("q")))
        self.assertFalse(self.press(27))
