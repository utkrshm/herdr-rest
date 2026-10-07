import json
import os
import signal
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from herdr_rest.agents import (
    _linux_start_time_from_stat,
    process_identity,
    resume_args,
    same_process,
    valid_session,
)
from herdr_rest.config import Config, config_dir, state_dir
from herdr_rest.daemon import Daemon
from herdr_rest.herdr import CliError, Herdr
from herdr_rest.presentation import agent_snapshot
from herdr_rest.state import FileLock, Registry, request_focus


class Completed:
    def __init__(self, output, returncode=0):
        self.returncode = returncode
        self.stdout = json.dumps(output)
        self.stderr = ""


def agent(status="idle", seq=1, focused=False, terminal="t1", pane="w:p1", kind="claude"):
    return {
        "terminal_id": terminal,
        "name": "worker",
        "agent": kind,
        "agent_status": status,
        "agent_session": {"source": "herdr:test", "agent": kind, "kind": "id", "value": "s1"},
        "workspace_id": "w",
        "tab_id": "w:t1",
        "pane_id": pane,
        "focused": focused,
        "launch_pending": False,
        "interactive_ready": True,
        "state_change_seq": seq,
    }


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class FakeCli:
    def __init__(self):
        self.agents_now = []
        self.panes_now = []
        self.process = {"pane_id": "w:p1", "foreground_processes": []}
        self.started = []
        self.workspaces_now = [{"workspace_id": "w", "label": "Project", "focused": False}]
        self.notifications = []
        self.notification_result = {"shown": True}
        self.agent_screen = ""
        self.sent_keys = []
        self.renamed = []

    def agents(self):
        return list(self.agents_now)

    def panes(self):
        return list(self.panes_now)

    def workspaces(self):
        return list(self.workspaces_now)

    def notification(self, title, body):
        self.notifications.append((title, body))
        return self.notification_result

    def process_info(self, pane_id):
        return self.process

    def rename_pane(self, pane_id, label):
        self.renamed.append((pane_id, label))
        for pane in self.panes_now:
            if pane.get("pane_id") == pane_id:
                pane["label"] = label

    def start(self, name, kind, pane_id, args):
        self.started.append((name, kind, pane_id, args))

    def agent_read(self, target):
        return self.agent_screen

    def send_key(self, target, key):
        self.sent_keys.append((target, key))


class RecordingDaemon(Daemon):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hibernated = []

    def _hibernate(self, agent_info, records):
        self.hibernated.append(agent_info["terminal_id"])


class TestPlugin(unittest.TestCase):
    def test_session_title_is_captured_independently_of_custom_pane_label(self):
        daemon = self.make_daemon(FakeCli())
        item = agent(kind="opencode") | {"terminal_title_stripped": "OC | Fix authentication tests"}
        self.assertEqual(daemon._session_title(item, {"label": "My custom label"}), "Fix authentication tests")

    def test_old_sleeping_marker_migrates_without_changing_original_label(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = FakeCli()
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "label": "[sleeping] opencode"}]
            daemon = self.make_daemon(cli, directory=directory)
            records = {"t1": {
                "terminal_id": "t1", "pane_id": "w:p1", "kind": "opencode", "session": "s1",
                "sleeping_label": "[sleeping] opencode", "original_label": None,
                "session_title": "Fix authentication tests",
            }}
            daemon._reconcile(records, [], cli.panes_now)
            self.assertEqual(cli.panes_now[0]["label"], "[sleeping] opencode: Fix authentication tests")
            self.assertIsNone(records["t1"]["original_label"])
            daemon._restore_sleeping_label(records["t1"])
            self.assertIsNone(cli.panes_now[0]["label"])

    def test_sleeping_label_added_once_and_restored_after_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = FakeCli()
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "label": "Auth tests", "focused": False}]
            daemon = self.make_daemon(cli, Config(focus_debounce_seconds=0), directory=directory)
            records = {"t1": {"terminal_id": "t1", "pane_id": "w:p1", "name": "worker", "kind": "claude", "session": "s1"}}
            daemon._reconcile(records, [], cli.panes_now)
            self.assertEqual(cli.renamed, [("w:p1", "[sleeping] claude: Auth tests")])
            daemon._reconcile(records, [], cli.panes_now)
            self.assertEqual(len(cli.renamed), 1)

            cli.panes_now[0]["focused"] = True
            daemon._resume_focused(records, cli.panes_now, 0)
            self.assertEqual(cli.renamed[-1], ("w:p1", "Auth tests"))
            self.assertEqual(records, {})

    def test_sleeping_label_restores_automatic_label_and_keeps_user_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = FakeCli()
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "focused": False}]
            daemon = self.make_daemon(cli, directory=directory)
            records = {"t1": {"terminal_id": "t1", "pane_id": "w:p1", "kind": "pi", "session": "s1"}}
            daemon._reconcile(records, [], cli.panes_now)
            self.assertEqual(cli.panes_now[0]["label"], "[sleeping] pi: s1")
            self.assertTrue(daemon._restore_sleeping_label(records["t1"]))
            self.assertEqual(cli.renamed[-1], ("w:p1", None))

            daemon._reconcile(records, [], cli.panes_now)
            cli.panes_now[0]["label"] = "My new name"
            daemon._reconcile(records, [], cli.panes_now)
            self.assertEqual(cli.panes_now[0]["label"], "[sleeping] pi: My new name")
            self.assertTrue(daemon._restore_sleeping_label(records["t1"]))
            self.assertEqual(cli.panes_now[0]["label"], "My new name")

    def test_live_failed_exit_is_not_marked_sleeping_and_external_resume_clears_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = FakeCli()
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "label": "Worker"}]
            daemon = self.make_daemon(cli, directory=directory)
            records = {"t1": {"terminal_id": "t1", "pane_id": "w:p1", "kind": "claude", "session": "s1", "stop_failed": True}}
            daemon._reconcile(records, [agent()], cli.panes_now)
            self.assertEqual(cli.renamed, [])

            with mock.patch("herdr_rest.lifecycle.same_process", return_value=False):
                daemon._reconcile(records, [], cli.panes_now)
            self.assertEqual(cli.panes_now[0]["label"], "[sleeping] claude: Worker")
            daemon._reconcile(records, [agent()], cli.panes_now)
            self.assertEqual(cli.panes_now[0]["label"], "Worker")
            self.assertEqual(records, {})

    def test_label_restoration_does_not_modify_reused_pane_or_overwrite_user_edit(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = FakeCli()
            daemon = self.make_daemon(cli, directory=directory)
            record = {"terminal_id": "t1", "pane_id": "w:p1", "sleeping_label": "[sleeping] Old", "original_label": "Old"}
            cli.panes_now = [{"terminal_id": "replacement", "pane_id": "w:p1", "label": "[sleeping] Old"}]
            daemon._restore_sleeping_label(dict(record))
            self.assertEqual(cli.renamed, [])

            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "label": "User edit during wake"}]
            daemon._restore_sleeping_label(record)
            self.assertEqual(cli.renamed, [])

    def test_pane_label_cli_can_set_and_clear(self):
        runner = mock.Mock(return_value=Completed({"result": {"type": "pane_renamed"}}))
        cli = Herdr("fake-herdr", runner)
        cli.rename_pane("w:p1", "[sleeping] Worker")
        self.assertEqual(runner.call_args.args[0], ["fake-herdr", "pane", "rename", "w:p1", "[sleeping] Worker"])
        cli.rename_pane("w:p1", None)
        self.assertEqual(runner.call_args.args[0], ["fake-herdr", "pane", "rename", "w:p1", "--clear"])

    def test_direct_and_plugin_commands_resolve_same_state_and_config_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {"HOME": directory, "HERDR_SOCKET_PATH": "/tmp/herdr-test.sock"}
            explicit = {
                **env,
                "HERDR_PLUGIN_STATE_DIR": str(Path(directory) / ".local/state/herdr/plugins/herdr.rest"),
                "HERDR_PLUGIN_CONFIG_DIR": str(Path(directory) / ".config/herdr/plugins/config/herdr.rest"),
            }
            self.assertEqual(state_dir(env), state_dir(explicit))
            self.assertEqual(config_dir(env), config_dir(explicit))

            xdg = {**env, "XDG_STATE_HOME": str(Path(directory) / "state"), "XDG_CONFIG_HOME": str(Path(directory) / "config")}
            self.assertEqual(config_dir(xdg), Path(directory) / "config/herdr/plugins/config/herdr.rest")
            self.assertEqual(state_dir(xdg).parent, Path(directory) / "state/herdr/plugins/herdr.rest")

    def test_agent_snapshot_reports_per_agent_timer(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cli, clock = FakeCli(), Clock()
            cli.agents_now = [agent()]
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "workspace_id": "w"}]
            daemon = self.make_daemon(cli, Config(idle_seconds=900), clock, directory)
            daemon.run_once()
            clock.value = 125
            daemon.run_once()

            with FileLock(directory / "daemon.lock"):
                report = agent_snapshot(cli, directory, now=127)[0]
                self.assertEqual(report["inactive_seconds"], 127)
                self.assertEqual(report["remaining_seconds"], 773)
                self.assertEqual(report["observation_age_seconds"], 2)

    def test_inactivity_refocus_overrides_snapshot_and_stopped_watcher_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cli = FakeCli()
            cli.agents_now = [agent()]
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "workspace_id": "w"}]
            Registry(directory, "inactivity.json").save({
                "sampled_at": 100, "idle_seconds": 900, "poll_seconds": 2,
                "agents": {"t1": {"idle_since": 0, "fingerprint": {}}},
            })

            self.assertIsNone(agent_snapshot(cli, directory, now=101)[0]["inactive_seconds"])
            self.assertIn("watcher_not_running", agent_snapshot(cli, directory, now=101)[0]["timer_reason"])
            with FileLock(directory / "daemon.lock"):
                self.assertIsNone(agent_snapshot(cli, directory, now=200)[0]["inactive_seconds"])
                cli.workspaces_now[0]["focused"] = True
                self.assertEqual(agent_snapshot(cli, directory, now=101)[0]["inactive_seconds"], 0)

    def test_agent_snapshot_includes_sleepers_and_saved_titles(self):
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            cli = FakeCli()
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "workspace_id": "w"}]
            Registry(directory).save({"t1": {"name": "worker", "pane_id": "w:p1", "session": "s1", "kind": "claude", "session_title": "Fix authentication"}})
            report = agent_snapshot(cli, directory, now=0)[0]
            self.assertEqual(report["pane_id"], "w:p1")
            self.assertEqual(report["agent_status"], "sleeping")
            self.assertEqual(report["session_title"], "Fix authentication")

    def make_daemon(self, cli, config=None, clock=None, directory=None):
        directory = Path(directory or tempfile.mkdtemp())
        return RecordingDaemon(
            cli,
            Registry(directory),
            config or Config(idle_seconds=10, poll_seconds=1),
            directory / "mutations.lock",
            clock or Clock(),
        )

    def test_config_rejects_non_finite_duration(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "config.toml").write_text("[hibernate]\nidle_seconds = inf\n")
            with self.assertRaises(ValueError):
                Config.load(Path(directory))

    def test_config_is_created_once_in_shared_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "config.toml")
            self.assertEqual(Config.load(Path(directory)), Config())
            original = path.read_text()
            path.write_text("[hibernate]\nidle_seconds = 42\n")
            self.assertEqual(Config.load(Path(directory)).idle_seconds, 42)
            self.assertNotEqual(path.read_text(), original)

    def test_config_creation_is_atomic_under_concurrency(self):
        with tempfile.TemporaryDirectory() as directory:
            errors = []

            def load():
                try:
                    Config.load(Path(directory))
                except Exception as error:  # pragma: no cover - diagnostic path
                    errors.append(error)

            threads = [threading.Thread(target=load) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertIn("[hibernate]", Path(directory, "config.toml").read_text())

    def test_existing_config_wins_over_concurrent_default_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory, "config.toml")
            contents = "[hibernate]\nidle_seconds = 42\n"
            path.write_text(contents)
            errors = []

            def load():
                try:
                    self.assertEqual(Config.load(Path(directory)).idle_seconds, 42)
                except Exception as error:  # pragma: no cover - diagnostic path
                    errors.append(error)

            threads = [threading.Thread(target=load) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(path.read_text(), contents)

    def test_herdr_uses_real_json_by_default_and_authoritative_shapes(self):
        calls = []

        def runner(command, **kwargs):
            calls.append((command, kwargs))
            if command[1:3] == ["agent", "list"]:
                return Completed({"result": {"agents": [agent()]}})
            return Completed({"result": {"process_info": {"pane_id": "w:p1", "shell_pid": 10, "foreground_process_group_id": 11, "foreground_processes": []}}})

        cli = Herdr("fake-herdr", runner)
        self.assertEqual(cli.agents()[0]["terminal_id"], "t1")
        self.assertEqual(cli.process_info("w:p1")["shell_pid"], 10)
        self.assertEqual(calls[0][0], ["fake-herdr", "agent", "list"])
        self.assertEqual(calls[1][0], ["fake-herdr", "pane", "process-info", "--pane", "w:p1"])
        self.assertEqual(calls[0][1]["timeout"], 10.0)

    def test_herdr_workspace_and_notification_commands(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if command[1:3] == ["workspace", "list"]:
                return Completed({"result": {"workspaces": [{"workspace_id": "w", "focused": False}]}})
            return Completed({"result": {"type": "notification_show", "shown": True, "reason": "shown"}})

        cli = Herdr("fake-herdr", runner)
        self.assertEqual(cli.workspaces()[0]["workspace_id"], "w")
        self.assertTrue(cli.notification("title", "body")["shown"])
        self.assertEqual(calls[0], ["fake-herdr", "workspace", "list"])
        self.assertEqual(calls[1], ["fake-herdr", "notification", "show", "title", "--body", "body"])

    def test_herdr_converts_binary_failures_to_cli_errors(self):
        def runner(*args, **kwargs):
            raise OSError("missing binary")

        with self.assertRaises(CliError) as error:
            Herdr("fake-herdr", runner).agents()
        self.assertIn("cannot run", str(error.exception))

    def test_agent_activity_restarts_only_that_agents_timer(self):
        cli, clock = FakeCli(), Clock()
        cli.agents_now = [agent(seq=1)]
        daemon = self.make_daemon(cli, clock=clock)
        daemon.run_once()
        clock.value = 9
        daemon.run_once()
        self.assertEqual(daemon.hibernated, [])
        cli.agents_now = [agent(seq=2)]
        clock.value = 10
        daemon.run_once()
        self.assertEqual(daemon.hibernated, [])
        clock.value = 20
        daemon.run_once()
        self.assertEqual(daemon.hibernated, ["t1"])

    def test_focused_workspace_protects_all_agents(self):
        cli, clock = FakeCli(), Clock()
        daemon = self.make_daemon(cli, clock=clock)
        cli.agents_now = [agent(focused=False)]
        cli.workspaces_now = [{"workspace_id": "w", "label": "Project", "focused": True}]
        daemon.run_once()
        clock.value = 100
        daemon.run_once()  # ready transition starts a fresh baseline
        self.assertEqual(daemon.hibernated, [])

    def test_fresh_focus_observation_resets_all_agent_timers(self):
        cli, clock = FakeCli(), Clock()
        cli.agents_now = [agent()]
        daemon = self.make_daemon(cli, clock=clock)
        daemon.run_once()
        clock.value = 5
        cli.workspaces_now[0]["focused"] = True
        self.assertFalse(daemon._fresh_agent_ready("t1", daemon._fingerprint(agent()), "w"))
        self.assertNotIn("t1", daemon.agent_timers)
        cli.workspaces_now[0]["focused"] = False
        clock.value = 6
        self.assertFalse(daemon._fresh_agent_ready("t1", daemon._fingerprint(agent()), "w"))
        clock.value = 15
        self.assertFalse(daemon._fresh_agent_ready("t1", daemon._fingerprint(agent()), "w"))
        clock.value = 16
        self.assertTrue(daemon._fresh_agent_ready("t1", daemon._fingerprint(agent()), "w"))

    def test_agents_in_one_unfocused_workspace_have_independent_timers(self):
        cli, clock = FakeCli(), Clock()
        second = agent(terminal="t2", pane="w:p2")
        cli.agents_now = [agent(), second]
        daemon = self.make_daemon(cli, Config(idle_seconds=10), clock)
        daemon.run_once()
        clock.value = 5
        cli.agents_now = [agent(seq=2), second]
        daemon.run_once()
        clock.value = 10
        daemon.run_once()
        self.assertEqual(daemon.hibernated, ["t2"])
        clock.value = 15
        cli.agents_now = [agent(seq=2), second | {"agent_status": "working"}]
        daemon.run_once()
        self.assertEqual(daemon.hibernated, ["t2", "t1"])

    def test_working_to_idle_starts_a_fresh_timer(self):
        cli, clock = FakeCli(), Clock()
        cli.agents_now = [agent(status="working")]
        daemon = self.make_daemon(cli, Config(idle_seconds=10), clock)
        daemon.run_once()
        clock.value = 20
        cli.agents_now = [agent(status="idle", seq=2)]
        daemon.run_once()
        self.assertEqual(daemon.hibernated, [])
        clock.value = 30
        daemon.run_once()
        self.assertEqual(daemon.hibernated, ["t1"])

    def test_warning_is_once_cancelled_and_rearmed_for_new_session(self):
        cli, clock = FakeCli(), Clock()
        cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "label": "Claude pane"}]
        cli.agents_now = [agent()]
        daemon = self.make_daemon(cli, Config(idle_seconds=10), clock)
        daemon.run_once()
        clock.value = 1
        daemon.run_once()
        daemon.run_once()
        self.assertEqual(len(cli.notifications), 1)
        self.assertIn("Claude pane", cli.notifications[0][0])
        self.assertIn("Project", cli.notifications[0][1])
        cli.workspaces_now[0]["focused"] = True
        clock.value = 2
        daemon.run_once()
        cli.workspaces_now[0]["focused"] = False
        cli.agents_now = [agent() | {"agent_session": {"agent": "claude", "kind": "id", "value": "new"}}]
        clock.value = 3
        daemon.run_once()
        self.assertEqual(len(cli.notifications), 2)

    def test_failed_notification_retries_but_does_not_change_timeout(self):
        cli, clock = FakeCli(), Clock()
        cli.notification_result = {"shown": False, "reason": "busy"}
        cli.agents_now = [agent()]
        daemon = self.make_daemon(cli, Config(idle_seconds=3), clock)
        daemon.run_once()
        for value in (0.5, 1, 2, 3):
            clock.value = value
            daemon.run_once()
        self.assertEqual(len(cli.notifications), 3)
        self.assertEqual(daemon.hibernated, ["t1"])

    def test_refocus_before_signal_aborts(self):
        cli, clock = FakeCli(), Clock()
        cli.agents_now = [agent()]
        daemon = Daemon(cli, Registry(Path(tempfile.mkdtemp())), Config(idle_seconds=1), Path(tempfile.mkdtemp()) / "mutations.lock", clock)
        daemon.run_once()
        clock.value = 2
        original = cli.workspaces
        calls = 0

        def workspaces():
            nonlocal calls
            calls += 1
            if calls >= 2:
                return [{"workspace_id": "w", "label": "Project", "focused": True}]
            return original()

        cli.workspaces = workspaces
        with mock.patch("herdr_rest.lifecycle.os.kill") as kill:
            daemon.run_once()
        kill.assert_not_called()

    def test_done_working_blocked_unknown_and_launching_agents_are_never_candidates(self):
        cli, clock = FakeCli(), Clock()
        daemon = self.make_daemon(cli, clock=clock)
        for item in (agent(focused=True), agent(status="done"), agent(status="working"), agent(status="blocked"), agent(status="unknown"), agent() | {"launch_pending": True}):
            cli.agents_now = [item]
            daemon.run_once()
            clock.value += 100
            daemon.run_once()
        self.assertEqual(daemon.hibernated, [])

    def test_done_clears_timer_and_idle_starts_fresh_countdown(self):
        cli, clock = FakeCli(), Clock()
        daemon = self.make_daemon(cli, clock=clock)
        cli.agents_now = [agent()]
        daemon.run_once()
        clock.value = 9
        cli.agents_now = [agent(status="done", seq=2)]
        daemon.run_once()
        self.assertEqual(daemon.agent_timers, {})
        cli.notifications.clear()

        clock.value = 100
        daemon.run_once()
        self.assertEqual(daemon.hibernated, [])
        self.assertEqual(cli.notifications, [])

        cli.agents_now = [agent(status="idle", seq=3)]
        daemon.run_once()
        clock.value = 109
        daemon.run_once()
        self.assertEqual(daemon.hibernated, [])
        clock.value = 110
        daemon.run_once()
        self.assertEqual(daemon.hibernated, ["t1"])

    def test_idle_agents_with_omitted_readiness_are_hibernation_candidates(self):
        cli, clock = FakeCli(), Clock()
        item = agent()
        item.pop("interactive_ready")
        item.pop("launch_pending")
        cli.agents_now = [item]
        daemon = self.make_daemon(cli, clock=clock)
        daemon.run_once()
        clock.value = 10
        daemon.run_once()
        self.assertEqual(daemon.hibernated, ["t1"])

        # The API's omitted false and an explicitly returned false agree.
        self.assertTrue(daemon._eligible(item | {"interactive_ready": False}))

    def test_fingerprint_keeps_pre_stop_safety_fields(self):
        daemon = self.make_daemon(FakeCli())
        first = daemon._fingerprint(agent())
        changed = daemon._fingerprint(agent(seq=2, status="working", focused=True) | {"interactive_ready": False, "launch_pending": True})
        self.assertNotEqual(first, changed)

    def test_agent_title_precedence_and_fallbacks(self):
        daemon = self.make_daemon(FakeCli())
        self.assertEqual(daemon._agent_title(agent() | {"title": "Agent title"}, {"label": "Pane label"}), "Pane label")
        self.assertEqual(daemon._agent_title(agent() | {"title": "Agent title"}, {}), "Agent title")
        self.assertEqual(daemon._agent_title(agent() | {"terminal_title_stripped": "Terminal"}, {}), "Terminal")

    def test_valid_config_changes_request_internal_reload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            Config.load(path)
            daemon = Daemon(FakeCli(), Registry(path), Config(), path / "mutations.lock")
            daemon.reload_directory = path
            daemon._reload_signature = daemon._runtime_signature()
            (path / "config.toml").write_text("[hibernate]\nidle_seconds = 123\n")
            with mock.patch("herdr_rest.daemon.subprocess.run", return_value=Completed({})) as validate:
                self.assertTrue(daemon._reload_changed_files())
            validate.assert_called_once()

    def test_last_active_tracks_work_and_changes_but_not_idle_polls_or_project_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            cli, clock, wall = FakeCli(), Clock(), Clock()
            path = Path(directory)
            daemon = Daemon(cli, Registry(path), Config(), path / "mutations.lock", clock, wall_clock=wall)
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "workspace_id": "w", "focused": False}]
            cli.agents_now = [agent(status="working")]
            wall.value = 1000
            daemon.run_once()
            self.assertEqual(daemon.activity_history["t1"]["last_active_at"], 1000)

            cli.agents_now = [agent(status="idle", seq=2)]
            wall.value = 1010
            daemon.run_once()
            wall.value = 1020
            cli.workspaces_now[0]["focused"] = True
            daemon.run_once()
            self.assertEqual(daemon.activity_history["t1"]["last_active_at"], 1010)

            cli.panes_now[0]["focused"] = True
            wall.value = 1030
            daemon.run_once()
            self.assertEqual(daemon.activity_history["t1"]["last_active_at"], 1030)

    def test_last_active_survives_reload_and_is_available_for_sleeping_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            cli, clock = FakeCli(), Clock()
            path = Path(directory)
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "workspace_id": "w"}]
            cli.agents_now = [agent(status="working")]
            daemon = Daemon(cli, Registry(path), Config(), path / "mutations.lock", clock, wall_clock=lambda: 1000)
            daemon.run_once()
            cli.agents_now = [agent(status="idle", seq=2)]
            daemon.wall_clock = lambda: 1010
            daemon.run_once()

            reloaded = Daemon(cli, Registry(path), Config(), path / "mutations.lock", clock, wall_clock=lambda: 2000)
            reloaded.run_once()
            self.assertEqual(reloaded.activity_history["t1"]["last_active_at"], 1010)
            Registry(path).save({"t1": {"pane_id": "w:p1", "kind": "claude", "session": "s1"}})
            cli.agents_now = []
            reloaded.run_once()
            self.assertEqual(agent_snapshot(cli, path)[0]["last_active_at"], 1010)

    def test_preexisting_idle_sessions_have_unknown_activity_and_replacements_do_not_inherit_it(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = FakeCli()
            path = Path(directory)
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "workspace_id": "w"}]
            cli.agents_now = [agent()]
            daemon = Daemon(cli, Registry(path), Config(), path / "mutations.lock", wall_clock=lambda: 1000)
            daemon.run_once()
            self.assertIsNone(daemon.activity_history["t1"]["last_active_at"])
            cli.agents_now = [agent(seq=2)]
            daemon.run_once()
            self.assertEqual(daemon.activity_history["t1"]["last_active_at"], 1000)
            cli.agents_now[0]["agent_session"]["value"] = "different-session"
            daemon.run_once()
            self.assertIsNone(daemon.activity_history["t1"]["last_active_at"])

    def test_invalid_config_changes_keep_current_watcher_running(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            Config.load(path)
            daemon = Daemon(FakeCli(), Registry(path), Config(), path / "mutations.lock")
            daemon.reload_directory = path
            daemon._reload_signature = daemon._runtime_signature()
            (path / "config.toml").write_text("[hibernate]\nidle_seconds = -1\n")
            with mock.patch("herdr_rest.daemon.subprocess.run") as validate, mock.patch("builtins.print") as log:
                self.assertFalse(daemon._reload_changed_files())
                self.assertFalse(daemon._reload_changed_files())
            validate.assert_not_called()
            log.assert_called_once()

    def test_focus_request_is_atomic_and_coalesced_per_session(self):
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "HERDR_SOCKET_PATH": "/tmp/herdr-focus.sock",
                "HERDR_PLUGIN_STATE_DIR": directory,
            }
            request_focus(env)
            request_focus(env)
            runtime = state_dir(env)
            self.assertTrue((runtime / "wake.request").exists())

            daemon = Daemon(FakeCli(), Registry(runtime), Config(), runtime / "mutations.lock")
            self.assertTrue(daemon.wake_requested())
            self.assertFalse(daemon.wake_requested())

    def test_focus_event_runs_before_next_normal_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            request = path / "wake.request"
            request.touch()
            clock = Clock()
            daemon = Daemon(FakeCli(), Registry(path), Config(poll_seconds=2), path / "mutations.lock", clock)
            calls = []

            def run_once():
                calls.append(clock())
                daemon.stop.set()

            daemon.run_once = run_once
            daemon.loop()
            self.assertEqual(calls, [0.0])

    def test_focus_deadline_is_rechecked_before_normal_poll(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            daemon = Daemon(FakeCli(), Registry(Path(directory)), Config(poll_seconds=2), Path(directory) / "mutations.lock", clock)
            calls = []

            def run_once():
                calls.append(clock())
                if len(calls) == 1:
                    daemon.focus_deadline = clock() + 0.15
                else:
                    daemon.stop.set()

            def wait(seconds):
                clock.value += seconds
                return False

            daemon.run_once = run_once
            daemon.waiter = wait
            daemon.loop()
            self.assertEqual(calls, [0.0, 0.15])

    def test_api_failure_clears_pending_focus_deadline_to_avoid_busy_retry_loop(self):
        with tempfile.TemporaryDirectory() as directory:
            cli, clock = FakeCli(), Clock()
            daemon = self.make_daemon(cli, clock=clock, directory=directory)
            daemon.focus_deadline = 0
            daemon.focus_since["t1"] = 0
            daemon.focus_retry_at["t1"] = 0
            with mock.patch.object(cli, "agents", side_effect=CliError("server unavailable")):
                daemon.run_once()
            self.assertIsNone(daemon.focus_deadline)
            self.assertEqual(daemon.focus_since, {})
            self.assertEqual(daemon.focus_retry_at, {})

    def test_failed_focus_resume_waits_for_normal_poll_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            cli, clock = FakeCli(), Clock()
            cli.start = mock.Mock(side_effect=CliError("busy"))
            daemon = Daemon(cli, Registry(Path(directory)), Config(poll_seconds=2, focus_debounce_seconds=0), Path(directory) / "mutations.lock", clock)
            records = {"t1": {"terminal_id": "t1", "pane_id": "w:p1", "name": "worker", "kind": "claude", "session": "s1"}}
            panes = [{"terminal_id": "t1", "pane_id": "w:p1", "focused": True}]
            cli.panes_now = panes
            daemon._resume_focused(records, panes, clock())
            clock.value = 0.05
            daemon._resume_focused(records, panes, clock())
            self.assertEqual(cli.start.call_count, 1)
            self.assertEqual(daemon.focus_deadline, 2.0)

    def test_focus_debounce_requires_continuous_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            registry = Registry(Path(directory))
            registry.save({"t1": {"terminal_id": "t1", "pane_id": "w:p1", "name": "worker", "kind": "claude", "session": "s1"}})
            cli, clock = FakeCli(), Clock()
            daemon = self.make_daemon(cli, Config(focus_debounce_seconds=2), clock, directory)
            cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "focused": True}]
            daemon._resume_focused(registry.load(), cli.panes_now, clock())
            clock.value = 1
            daemon._resume_focused(registry.load(), cli.panes_now, clock())
            cli.panes_now[0]["focused"] = False
            clock.value = 2
            daemon._resume_focused(registry.load(), cli.panes_now, clock())
            cli.panes_now[0]["focused"] = True
            clock.value = 3
            daemon._resume_focused(registry.load(), cli.panes_now, clock())
            clock.value = 5
            daemon._resume_focused(registry.load(), cli.panes_now, clock())
            self.assertEqual(cli.started, [("worker", "claude", "w:p1", ["--resume", "s1"])])

    def test_term_only_never_kill_and_requires_precise_process(self):
        with tempfile.TemporaryDirectory() as directory:
            cli, clock = FakeCli(), Clock()
            cli.agents_now = [agent()]
            cli.process = {"pane_id": "w:p1", "foreground_processes": [{"pid": 4321, "name": "claude", "argv0": "claude"}]}
            daemon = Daemon(cli, Registry(Path(directory)), Config(idle_seconds=1, terminate_wait_seconds=0.01), Path(directory) / "mutations.lock", clock)
            daemon.run_once()
            clock.value = 2
            with mock.patch("herdr_rest.lifecycle.process_identity", return_value={"pid": 4321, "name": "claude", "start_time": "1"}), mock.patch("herdr_rest.lifecycle.same_process", return_value=True), mock.patch("herdr_rest.lifecycle.os.kill") as kill:
                daemon.run_once()
            self.assertEqual(kill.call_args.args, (4321, signal.SIGTERM))
            self.assertNotIn(signal.SIGKILL, [call.args[1] for call in kill.call_args_list])
            self.assertTrue(Registry(Path(directory)).load()["t1"]["stop_failed"])

    def test_delayed_registration_cleanup_does_not_mark_successful_exit_failed(self):
        with tempfile.TemporaryDirectory() as directory:
            cli, clock = FakeCli(), Clock()
            cli.agents_now = [agent()]
            daemon = Daemon(cli, Registry(Path(directory)), Config(idle_seconds=1), Path(directory) / "mutations.lock", clock)
            daemon.run_once()
            clock.value = 2
            calls_after_signal = 0
            signalled = False

            def terminate(*args):
                nonlocal signalled
                signalled = True

            def agents():
                nonlocal calls_after_signal
                if signalled:
                    calls_after_signal += 1
                    return [agent()] if calls_after_signal == 1 else []
                return [agent()]

            cli.agents = agents
            with mock.patch("herdr_rest.lifecycle.process_identity", return_value={"pid": 4321}), mock.patch("herdr_rest.lifecycle.same_process", side_effect=lambda *args: not signalled), mock.patch("herdr_rest.lifecycle.os.kill", side_effect=terminate):
                daemon.run_once()
            self.assertNotIn("stop_failed", daemon.registry.load()["t1"])

    def test_failed_exit_is_reconciled_when_registration_eventually_clears(self):
        with tempfile.TemporaryDirectory() as directory:
            daemon = self.make_daemon(FakeCli(), directory=directory)
            records = {"t1": {"pane_id": "w:p1", "kind": "claude", "session": "s1", "stop_failed": True}}
            panes = [{"terminal_id": "t1", "pane_id": "w:p1"}]
            with mock.patch("herdr_rest.lifecycle.same_process", return_value=False):
                result = daemon._reconcile(records, [], panes)
            self.assertNotIn("stop_failed", result["t1"])

    def test_process_schema_is_exact_and_pid_identity_is_not_substring_matching(self):
        with mock.patch("herdr_rest.agents._linux_start_time", return_value="44"):
            self.assertIsNotNone(process_identity({"foreground_processes": [{"pid": 42, "name": "claude"}]}, "claude"))
            self.assertIsNone(process_identity({"foreground_processes": [{"pid": 42, "name": "claude-wrapper"}]}, "claude"))
            self.assertIsNone(process_identity({"foreground_processes": [{"pid": 42, "name": "claude"}, {"pid": 43, "name": "claude"}]}, "claude"))

    def test_codex_and_pi_session_refs_and_resume_argv_are_native(self):
        codex = agent(kind="codex") | {
            "agent_session": {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "c1"}
        }
        pi = agent(kind="pi") | {
            "agent_session": {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/tmp/pi-session.jsonl"}
        }
        self.assertTrue(valid_session(codex))
        self.assertTrue(valid_session(pi))
        self.assertFalse(valid_session(agent(kind="codex")))
        self.assertEqual(resume_args({"kind": "codex", "session": "c1"}), ["resume", "c1"])
        self.assertEqual(resume_args({"kind": "pi", "session": "/tmp/pi-session.jsonl"}), ["--session", "/tmp/pi-session.jsonl"])
        self.assertFalse(valid_session(pi | {"agent_session": pi["agent_session"] | {"value": "relative.jsonl"}}))

    def test_pi_process_profile_requires_known_node_package_script(self):
        with mock.patch("herdr_rest.agents._linux_start_time", return_value="44"):
            known = {
                "pid": 42,
                "name": "node",
                "argv0": "node",
                "argv": ["/usr/bin/node", "/home/me/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"],
            }
            self.assertIsNotNone(process_identity({"foreground_processes": [known]}, "pi"))
            for script in ("/tmp/pi-cli.js", "/home/me/node_modules/pi-coding-agent/dist/cli.js", "/home/me/node_modules/@earendil-works/pi-coding-agent/bin/cli.js"):
                self.assertIsNone(process_identity({"foreground_processes": [known | {"argv": ["node", script]}]}, "pi"))

    def test_codex_draft_never_receives_key_or_signal_when_not_proven_empty(self):
        cli, clock = FakeCli(), Clock()
        item = agent(kind="codex") | {
            "agent_session": {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "c1"}
        }
        cli.agents_now = [item]
        cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "workspace_id": "w"}]
        cli.process = {"pane_id": "w:p1", "foreground_processes": [{"pid": 4321, "name": "codex", "argv0": "codex"}]}
        daemon = Daemon(cli, Registry(Path(tempfile.mkdtemp())), Config(idle_seconds=1, terminate_wait_seconds=0.01), Path(tempfile.mkdtemp()) / "mutations.lock", clock)
        daemon.run_once()
        clock.value = 2
        cli.agent_screen = "history\n› Ask Codex to do anything\n› draft text"
        with mock.patch("herdr_rest.lifecycle.process_identity", return_value={"pid": 4321, "start_time": "1"}), mock.patch("herdr_rest.lifecycle.same_process", return_value=True), mock.patch("herdr_rest.lifecycle.os.kill") as kill:
            daemon.run_once()
        kill.assert_not_called()
        self.assertEqual(cli.sent_keys, [])
        self.assertNotIn("t1", Registry(daemon.registry.path.parent).load())

    def test_codex_empty_composer_sends_one_key_then_resumes_after_real_exit(self):
        cli, clock = FakeCli(), Clock()
        item = agent(kind="codex") | {
            "agent_session": {"source": "herdr:codex", "agent": "codex", "kind": "id", "value": "c1"}
        }
        cli.agents_now = [item]
        cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "workspace_id": "w"}]
        cli.process = {"pane_id": "w:p1", "foreground_processes": [{"pid": 4321, "name": "codex", "argv0": "codex"}]}
        cli.agent_screen = "history\n› Ask Codex to do anything\n› draft text"
        exited = False

        def same(*_args):
            return not exited

        def send_key(*args):
            nonlocal exited
            exited = True
            cli.sent_keys.append(args)
            cli.agents_now = []

        cli.send_key = send_key
        directory = Path(tempfile.mkdtemp())
        daemon = Daemon(cli, Registry(directory), Config(idle_seconds=1), directory / "mutations.lock", clock)
        daemon.run_once()
        clock.value = 2
        with mock.patch("herdr_rest.lifecycle.process_identity", return_value={"pid": 4321, "start_time": "1"}), mock.patch("herdr_rest.lifecycle.same_process", side_effect=same), mock.patch("herdr_rest.lifecycle.os.kill") as kill:
            daemon.run_once()
            self.assertEqual(cli.sent_keys, [])
            cli.agent_screen = "› Ask Codex to do anything"
            daemon.run_once()
        kill.assert_not_called()
        self.assertEqual(cli.sent_keys, [("w:p1", "ctrl+d")])
        record = Registry(directory).load()["t1"]
        self.assertNotIn("stop_failed", record)
        cli.panes_now[0]["focused"] = True
        daemon.focus_since["t1"] = clock() - daemon.config.focus_debounce_seconds
        daemon._resume_focused(Registry(directory).load(), cli.panes_now, clock())
        self.assertEqual(cli.started, [("worker", "codex", "w:p1", ["resume", "c1"])])

    def test_pi_term_exit_then_focus_resume_uses_path_reference(self):
        cli, clock = FakeCli(), Clock()
        item = agent(kind="pi") | {
            "agent_session": {"source": "herdr:pi", "agent": "pi", "kind": "path", "value": "/tmp/pi-session.jsonl"}
        }
        cli.agents_now = [item]
        cli.panes_now = [{"terminal_id": "t1", "pane_id": "w:p1", "workspace_id": "w"}]
        cli.process = {"pane_id": "w:p1", "foreground_processes": [{"pid": 4321, "name": "node", "argv0": "node", "argv": ["node", "/home/me/node_modules/@mariozechner/pi-coding-agent/dist/cli.js"]}]}
        exited = False

        def same(*_args):
            return not exited

        def terminate(*_args):
            nonlocal exited
            exited = True
            cli.agents_now = []

        directory = Path(tempfile.mkdtemp())
        daemon = Daemon(cli, Registry(directory), Config(idle_seconds=1), directory / "mutations.lock", clock)
        daemon.run_once()
        clock.value = 2
        with mock.patch("herdr_rest.lifecycle.process_identity", return_value={"pid": 4321, "start_time": "1"}), mock.patch("herdr_rest.lifecycle.same_process", side_effect=same), mock.patch("herdr_rest.lifecycle.os.kill", side_effect=terminate) as kill:
            daemon.run_once()
        kill.assert_called_once_with(4321, signal.SIGTERM)
        cli.panes_now[0]["focused"] = True
        daemon.focus_since["t1"] = clock() - daemon.config.focus_debounce_seconds
        daemon._resume_focused(Registry(directory).load(), cli.panes_now, clock())
        self.assertEqual(cli.started, [("worker", "pi", "w:p1", ["--session", "/tmp/pi-session.jsonl"])])

    def test_resume_args_rejects_corrupt_agent_kind(self):
        with self.assertRaises(ValueError):
            resume_args({"kind": "unknown", "session": "s1"})

    def test_latest_session_swap_aborts_before_process_targeting(self):
        with tempfile.TemporaryDirectory() as directory:
            cli, clock = FakeCli(), Clock()
            cli.agents_now = [agent() | {"agent_session": {"agent": "claude", "kind": "id", "value": "different"}}]
            daemon = Daemon(cli, Registry(Path(directory)), Config(), Path(directory) / "mutations.lock", clock)
            with mock.patch.object(cli, "process_info") as process_info:
                daemon._hibernate(agent(), {})
            process_info.assert_not_called()

    def test_linux_stat_parser_handles_spaces_in_comm(self):
        raw = "123 (agent with spaces) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23"
        self.assertEqual(_linux_start_time_from_stat(raw), "19")

    def test_session_state_isolated_by_socket_path(self):
        with tempfile.TemporaryDirectory() as directory:
            base = {"HERDR_PLUGIN_STATE_DIR": directory, "HERDR_SOCKET_PATH": "/tmp/herdr-a.sock"}
            first = state_dir(base)
            second = state_dir({**base, "HERDR_SOCKET_PATH": "/tmp/herdr-b.sock"})
            self.assertNotEqual(first, second)

    def test_reconcile_prunes_closed_and_replaced_panes_but_recovers_matching_record(self):
        with tempfile.TemporaryDirectory() as directory:
            cli, clock = FakeCli(), Clock()
            daemon = self.make_daemon(cli, clock=clock, directory=directory)
            records = {
                "closed": {"terminal_id": "closed", "pane_id": "gone", "kind": "claude", "session": "s"},
                "replaced": {"terminal_id": "replaced", "pane_id": "p2", "kind": "claude", "session": "old"},
                "recover": {"terminal_id": "recover", "pane_id": "p3", "kind": "claude", "session": "s3"},
                "moved": {"terminal_id": "moved", "pane_id": "old-pane", "kind": "claude", "session": "s4"},
            }
            panes = [
                {"terminal_id": "replaced", "pane_id": "p2"},
                {"terminal_id": "recover", "pane_id": "p3"},
                {"terminal_id": "moved", "pane_id": "new-pane"},
            ]
            agents = [agent(terminal="replaced", pane="p2") | {"agent_session": {"agent": "claude", "value": "new"}}]
            result = daemon._reconcile(records, agents, panes)
            self.assertEqual(set(result), {"recover", "moved"})
            self.assertEqual(result["moved"]["pane_id"], "new-pane")

    def test_run_once_loads_registry_after_acquiring_mutation_lock(self):
        class LockCheckingRegistry(Registry):
            def load(self):
                try:
                    lock = FileLock(self.path.parent / "mutations.lock", blocking=False)
                    lock.__enter__()
                except TimeoutError:
                    self.was_locked = True
                else:
                    self.was_locked = False
                    lock.__exit__(None, None, None)
                return {}

        with tempfile.TemporaryDirectory() as directory:
            cli, clock = FakeCli(), Clock()
            registry = LockCheckingRegistry(Path(directory))
            daemon = Daemon(cli, registry, Config(), Path(directory) / "mutations.lock", clock)
            daemon.run_once()
            self.assertTrue(registry.was_locked)

    def test_missing_session_and_native_session_fail_closed(self):
        with self.assertRaises(ValueError):
            state_dir({"HERDR_PLUGIN_STATE_DIR": tempfile.mkdtemp()})
        self.assertFalse(valid_session({"terminal_id": "t1", "pane_id": "p1"}))


if __name__ == "__main__":
    unittest.main()
