"""Watcher scheduling, orchestration, and detached process startup."""

from __future__ import annotations

import os
import logging
import subprocess
import sys
import time
from pathlib import Path
from threading import Event
from typing import Any, Callable

from .config import Config, config_dir, plugin_dir, state_dir
from .herdr import CliError, Herdr
from .lifecycle import AgentLifecycleMixin
from .policy import IdlePolicyMixin, is_trackable_agent
from .presentation import PresentationMixin
from .state import FileLock, Registry, Singleton


class Daemon(IdlePolicyMixin, PresentationMixin, AgentLifecycleMixin):
    def __init__(
        self,
        cli: Herdr,
        registry: Registry,
        config: Config,
        mutation_lock: Path,
        clock: Callable[[], float] = time.monotonic,
        waiter: Callable[[float], bool] | None = None,
        wall_clock: Callable[[], float] = time.time,
    ):
        self.cli, self.registry, self.config, self.mutation_lock, self.clock = cli, registry, config, mutation_lock, clock

        self.stop = Event()
        self.waiter = waiter or self.stop.wait
        self.wall_clock = wall_clock
        activity = Registry(mutation_lock.parent, "inactivity.json").load().get("activity", {})
        self.activity_history: dict[str, dict[str, Any]] = activity if isinstance(activity, dict) else {}
        self.focus_since: dict[str, float] = {}
        self.focus_retry_at: dict[str, float] = {}
        self.focus_deadline: float | None = None
        self.agent_timers: dict[str, dict[str, Any]] = {}
        self.warning_state: dict[tuple[str, str, str], int] = {}
        self._last_panes: list[dict[str, Any]] = []
        self.reload_directory: Path | None = None
        self._reload_signature: tuple | None = None
        self._rejected_reload_signature: tuple | None = None
        self._reload_check_at = 0.0

    def _runtime_signature(self) -> tuple:
        paths = [self.reload_directory / "config.toml", *sorted(Path(__file__).parent.glob("*.py"))]
        result = []
        for path in paths:
            try:
                stat = path.stat()
                result.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                result.append((str(path), None, None))
        return tuple(result)

    def _reload_changed_files(self) -> bool:
        signature = self._runtime_signature()
        if signature in (self._reload_signature, self._rejected_reload_signature):
            return False
        try:
            Config.load(self.reload_directory)
            # Validate the new modules before handing off a working watcher.
            result = subprocess.run(
                [sys.executable, "-c", "import herdr_rest.__main__, herdr_rest.daemon, herdr_rest.view"],
                cwd=str(plugin_dir()), text=True, capture_output=True, timeout=10,
            )
            if result.returncode:
                raise ValueError(result.stderr.strip() or "module validation failed")
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            self._rejected_reload_signature = signature
            print(f"herdr-rest: reload deferred: {exc}", file=sys.stderr, flush=True)
            return False
        return True

    def run_once(self) -> None:
        now = self.clock()

        try:
            agents = self.cli.agents()
            panes = self.cli.panes()
            workspaces = self._workspace_map(self.cli.workspaces())
        except CliError:
            self._reset_inactivity()
            self._publish_inactivity()
            return
        if workspaces is None:
            self._reset_inactivity()
            self._publish_inactivity()
            return
        self._last_panes = panes
        self._update_activity(agents, panes)
        self._update_agent_timers(agents, workspaces, now, panes)

        with FileLock(self.mutation_lock):
            # Read and update recovery records under the same lifecycle lock.
            records = self.registry.load()
            records = self._reconcile(records, agents, panes)
            self._resume_focused(records, panes, now)

            for agent in agents:
                terminal = agent.get("terminal_id")
                if not terminal or not is_trackable_agent(agent) or terminal in records:
                    continue

                # A live agent cannot still be waiting on a hibernated
                # record's focus debounce after reconciliation.
                self.focus_since.pop(terminal, None)
                self.focus_retry_at.pop(terminal, None)

                workspace_id = agent.get("workspace_id")
                workspace = workspaces.get(workspace_id)
                if not workspace or workspace["focused"]:
                    continue

                pane = next((item for item in panes if item.get("terminal_id") == terminal), None)
                if self._eligible(agent, pane):
                    self._warn_if_needed(agent, workspace, now)
                    timer = self.agent_timers.get(terminal)
                    if timer and now - timer["idle_since"] >= self.config.idle_seconds:
                        self._hibernate(agent, records)

            for terminal in (set(self.focus_since) | set(self.focus_retry_at)) - set(records):
                self.focus_since.pop(terminal, None)
                self.focus_retry_at.pop(terminal, None)

            self.registry.save(records)

        self._publish_inactivity()

    def wake_requested(self) -> bool:
        """Consume one coalesced focus wake request without invoking Herdr."""
        try:
            (self.mutation_lock.parent / "wake.request").unlink()
        except FileNotFoundError:
            return False
        except OSError:
            return False
        return True

    def loop(self) -> bool:
        next_poll = self.clock()
        while not self.stop.is_set():
            now = self.clock()
            if self.reload_directory is not None and now >= self._reload_check_at:
                self._reload_check_at = now + self.config.poll_seconds
                if self._reload_changed_files():
                    return True
            if self.wake_requested():
                # Focus events are merely invalidation signals. The full,
                # lock-protected read remains the authority and coalesced
                # requests cannot launch the same record twice.
                self.run_once()
                next_poll = self.clock() + self.config.poll_seconds
                continue

            deadline = self.focus_deadline
            if deadline is not None and now >= deadline:
                self.run_once()
                next_poll = self.clock() + self.config.poll_seconds
                continue

            if now >= next_poll:
                self.run_once()
                next_poll = self.clock() + self.config.poll_seconds
                continue

            wake_at = min(next_poll, deadline) if deadline is not None else next_poll
            self.waiter(min(0.05, max(0.0, wake_at - now)))
        return False


def daemon_process(env: dict[str, str] | None = None) -> int:
    env = dict(os.environ) if env is None else env
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        directory = state_dir(env)
        config = Config.load(config_dir(env))
    except (OSError, ValueError) as exc:
        print(f"herdr-rest: {exc}", file=sys.stderr)
        return 1

    cli = Herdr(env.get("HERDR_BIN_PATH", config.herdr_binary))
    daemon = Daemon(cli, Registry(directory), config, directory / "mutations.lock")

    restart = False
    try:
        with Singleton(directory / "daemon.lock", blocking=False):
            daemon.reload_directory = config_dir(env)
            daemon._reload_signature = daemon._runtime_signature()
            restart = daemon.loop()
    except TimeoutError:
        return 0

    if restart:
        return start_process(env)

    return 0


def start_process(env: dict[str, str]) -> int:
    try:
        directory = state_dir(env)
        Config.load(config_dir(env))
        log = (directory / "daemon.log").open("a", buffering=1)
    except (OSError, ValueError) as exc:
        print(f"herdr-rest: {exc}", file=sys.stderr)
        return 1

    child_env = dict(env)

    try:
        subprocess.Popen(
            [sys.executable, "-m", "herdr_rest", "run"],
            cwd=str(plugin_dir(env)),
            env=child_env,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    except OSError as exc:
        log.close()
        print(f"herdr-rest: cannot start watcher: {exc}", file=sys.stderr)
        return 1

    log.close()
    return 0
