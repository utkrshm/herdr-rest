"""Agent hibernation, recovery reconciliation, and focus-driven resume."""

from __future__ import annotations

import os
import logging
import re
import signal
import time
from typing import Any

from .agents import process_identity, resume_args, same_process
from .herdr import CliError
from .recovery import rebind_record, restored_pane
from .state import Registry


logger = logging.getLogger(__name__)


class AgentLifecycleMixin:
    """Lifecycle methods operating on the state owned by Daemon."""

    def _reconcile(self, records: dict[str, dict[str, Any]], agents: list[dict[str, Any]], panes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        pane_by_terminal = {p.get("terminal_id"): p for p in panes}
        agent_by_terminal = {a.get("terminal_id"): a for a in agents}
        changed = False
        archive = Registry(self.mutation_lock.parent, "orphaned.json")
        orphaned = archive.load()
        archive_changed = False
        claimants = [*records.values(), *orphaned.values()]

        # An old watcher can observe the new server before all restored panes
        # are available. Retry archived bindings rather than losing them.
        for terminal, record in list(orphaned.items()):
            pane = restored_pane(record, panes, agents, self.cli, claimants)
            if pane is None or pane["terminal_id"] in records:
                continue
            records[pane["terminal_id"]] = rebind_record(record, pane)
            orphaned.pop(terminal)
            changed = archive_changed = True
            logger.info("Recovered archived sleeper %s on %s", record.get("pane_id"), pane["terminal_id"])

        for terminal, record in list(records.items()):
            pane = pane_by_terminal.get(terminal)
            live = agent_by_terminal.get(terminal)

            if not pane:
                pane = restored_pane(record, panes, agents, self.cli, claimants)
                if pane is not None and pane["terminal_id"] not in records:
                    rebound = rebind_record(record, pane)
                    records.pop(terminal)
                    records[pane["terminal_id"]] = rebound
                    self.focus_since.pop(terminal, None)
                    self.focus_retry_at.pop(terminal, None)
                    terminal, record = pane["terminal_id"], rebound
                    live = agent_by_terminal.get(terminal)
                    changed = True
                    logger.info("Rebound sleeper %s to restored terminal %s", record["pane_id"], terminal)
                else:
                    # Preserve resume information even for closed, temporarily
                    # unavailable, or unverifiable panes. Never guess a target.
                    orphaned[terminal] = {**record, "recovery_reason": "pane_identity_unavailable"}
                    archive.save(orphaned)
                    logger.info("Archived unmatched sleeper %s instead of deleting its resume information", record.get("pane_id"))
                    records.pop(terminal, None)
                    self.focus_since.pop(terminal, None)
                    self.focus_retry_at.pop(terminal, None)
                    changed = True
                    continue

            if pane.get("pane_id") != record.get("pane_id"):
                record.update({key: pane[key] for key in ("pane_id", "workspace_id", "tab_id") if key in pane})
                changed = True

            if record.get("terminal_id") != terminal:
                record["terminal_id"] = terminal
                changed = True

            if live:
                saved_record = dict(record)
                if not self._restore_sleeping_label(record):
                    continue
                session = live.get("agent_session")

                if (
                    not isinstance(session, dict)
                    or session.get("value") != record.get("session")
                    or session.get("agent") != record.get("kind")
                    or session.get("kind") != record.get("session_ref_kind", "id")
                    or (record.get("session_source") is not None and session.get("source") != record.get("session_source"))
                ):
                    orphaned[terminal] = {**saved_record, "recovery_reason": "agent_session_replaced"}
                    archive.save(orphaned)
                    records.pop(terminal, None)
                    self.focus_since.pop(terminal, None)
                    self.focus_retry_at.pop(terminal, None)
                    changed = True
                elif record.get("stop_failed"):
                    # Keep a failed termination visible for manual recovery;
                    # never repeatedly signal the same live process.
                    continue
                else:
                    records.pop(terminal, None)
                    self.focus_since.pop(terminal, None)
                    self.focus_retry_at.pop(terminal, None)
                    changed = True
            elif not same_process(record.get("process", {}), record.get("kind", "")):
                # Herdr can clear its registration after the exit timeout.
                # Reconcile that eventual success instead of retaining a false
                # failure forever.
                if record.pop("stop_failed", None) is not None:
                    changed = True
                self._sync_sleeping_label(record, pane, records)

        if changed:
            self.registry.save(records)
        if archive_changed:
            archive.save(orphaned)

        return records

    def _hibernate(self, agent: dict[str, Any], records: dict[str, dict[str, Any]]) -> None:
        terminal, pane_id = agent.get("terminal_id"), agent.get("pane_id")
        workspace_id = agent.get("workspace_id")

        try:
            expected = self._fingerprint(agent)
            if expected is None:
                return

            if not self._fresh_agent_ready(terminal, expected, workspace_id):
                return

            latest = next((item for item in self.cli.agents() if item.get("terminal_id") == terminal), None)
            latest_pane = next((item for item in self.cli.panes() if item.get("terminal_id") == terminal), None)
            if not latest or latest.get("workspace_id") != workspace_id or self._fingerprint(latest) != expected or not self._eligible(latest, latest_pane):
                return

            process = process_identity(self.cli.process_info(pane_id), latest["agent_session"]["agent"])
            if not process or not same_process(process, latest["agent_session"]["agent"]):
                return

            latest_after_info = next((item for item in self.cli.agents() if item.get("terminal_id") == terminal), None)
            latest_after_pane = next((item for item in self.cli.panes() if item.get("terminal_id") == terminal), None)
            if not latest_after_info or self._fingerprint(latest_after_info) != expected or not self._eligible(latest_after_info, latest_after_pane):
                return

            session = latest_after_info["agent_session"]
            record = {
                "terminal_id": terminal,
                "pane_id": pane_id,
                "workspace_id": latest_after_info.get("workspace_id"),
                "tab_id": latest_after_info.get("tab_id"),
                "name": latest_after_info.get("name") or f"hibernate_{terminal[-8:].lower()}",
                "kind": session["agent"],
                "session": session["value"],
                "session_ref_kind": session.get("kind", "id"),
                "session_source": session.get("source"),
                "cwd": latest_after_info.get("cwd"),
                "process": process,
                "state_change_seq": latest.get("state_change_seq"),
                "original_label": latest_after_pane.get("label") if latest_after_pane else None,
                "sleep_label_base": self._agent_title(latest_after_info, latest_after_pane),
                "session_title": self._session_title(latest_after_info, latest_after_pane),
                "last_active_at": self.activity_history.get(terminal, {}).get("last_active_at"),
            }
            records[terminal] = record
            self.registry.save(records)

            # Recheck the exact process immediately before the only stop action.
            if not self._fresh_agent_ready(terminal, expected, workspace_id) or not same_process(process, session["agent"]):
                records.pop(terminal, None)
                return

            if session["agent"] == "codex":
                if not self._codex_editor_empty(terminal, pane_id, expected, process):
                    # No input was sent. Do not pin a live agent in recovery;
                    # the next poll may retry after its draft is cleared.
                    records.pop(terminal, None)
                    self.registry.save(records)
                    return
                self.cli.send_key(pane_id, "ctrl+d")
                record["stop_method"] = "herdr_agent_send_keys_ctrl_d"
            else:
                # Pi's current interactive mode owns SIGTERM/SIGHUP cleanup and
                # stops its TUI; OpenCode and Claude retain their established
                # TERM behavior. Never escalate to SIGKILL.
                os.kill(process["pid"], signal.SIGTERM)
                record["stop_method"] = "sigterm"
            deadline = time.monotonic() + self.config.terminate_wait_seconds

            while time.monotonic() < deadline:
                process_running = same_process(process, session["agent"])
                registered = any(item.get("terminal_id") == terminal for item in self.cli.agents())
                if not process_running and not registered:
                    break
                time.sleep(min(0.1, self.config.poll_seconds))

            # Never escalate to SIGKILL. The durable record remains if TERM did
            # not settle the agent; resume is guarded by a live-agent check.
            if same_process(process, session["agent"]) or any(item.get("terminal_id") == terminal for item in self.cli.agents()):
                record["stop_failed"] = True
                self.registry.save(records)
                return

            self.registry.save(records)
            sleeping_pane = next((item for item in self.cli.panes() if item.get("terminal_id") == terminal and item.get("pane_id") == pane_id), None)
            if sleeping_pane is not None:
                self._sync_sleeping_label(record, sleeping_pane, records)
        except (CliError, KeyError, OSError):
            return

    def _codex_editor_empty(self, terminal: str, pane_id: str, expected: dict[str, Any], process: dict[str, Any]) -> bool:
        """Return true only for Codex's identifiable empty-composer prompt.

        A missing/changed screen is deliberately treated as non-empty. This is
        conservative: an unrecognized composer is deferred for a later poll.
        Codex's native Ctrl-D handler provides the final empty-editor check;
        no prompt text or Enter key is ever sent.
        """
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", self.cli.agent_read(pane_id))
        prompt_rows = [line.strip() for line in text.splitlines() if line.strip().startswith("› ")]
        if not prompt_rows or prompt_rows[-1] != "› Ask Codex to do anything":
            return False
        latest = next((item for item in self.cli.agents() if item.get("terminal_id") == terminal), None)
        latest_pane = next((item for item in self.cli.panes() if item.get("terminal_id") == terminal), None)
        if not latest or self._fingerprint(latest) != expected or not self._eligible(latest, latest_pane):
            return False
        return same_process(process, "codex")

    def _fresh_agent_ready(self, terminal: Any, expected: dict[str, Any], workspace_id: Any) -> bool:
        now = self.clock()
        try:
            workspaces = self._workspace_map(self.cli.workspaces())
            agents = self.cli.agents()
            panes = self.cli.panes()
        except CliError:
            self._reset_inactivity()
            return False
        if workspaces is None:
            self._reset_inactivity()
            return False
        if not isinstance(workspace_id, str) or workspace_id not in workspaces:
            return False
        if workspaces[workspace_id]["focused"]:
            self._reset_workspace_agents(agents, workspace_id)
            return False
        self._update_agent_timers(agents, workspaces, now, panes)
        latest = next((item for item in agents if item.get("terminal_id") == terminal), None)
        if not latest or latest.get("workspace_id") != workspace_id:
            return False
        latest_pane = next((item for item in panes if item.get("terminal_id") == terminal), None)
        if self._fingerprint(latest) != expected or not self._eligible(latest, latest_pane):
            return False
        timer = self.agent_timers.get(terminal)
        return (
            timer is not None
            and timer.get("idle_since") is not None
            and now - timer["idle_since"] >= self.config.idle_seconds
        )

    def _resume_focused(self, records: dict[str, dict[str, Any]], panes: list[dict[str, Any]], now: float) -> None:
        self.focus_deadline = None
        for terminal, record in list(records.items()):
            pane = next((item for item in panes if item.get("pane_id") == record.get("pane_id") and item.get("terminal_id") == terminal), None)
            if not pane or not pane.get("focused"):
                self.focus_since.pop(terminal, None)
                self.focus_retry_at.pop(terminal, None)
                continue

            since = self.focus_since.setdefault(terminal, now)
            deadline = since + self.config.focus_debounce_seconds
            retry_at = self.focus_retry_at.get(terminal, deadline)
            if now < deadline or now < retry_at:
                self.focus_deadline = min(
                    deadline if now < deadline else retry_at,
                    self.focus_deadline if self.focus_deadline is not None else float("inf"),
                )
                continue

            try:
                if any(item.get("terminal_id") == terminal for item in self.cli.agents()):
                    if not self._restore_sleeping_label(record):
                        continue
                    records.pop(terminal, None)
                    self.focus_since.pop(terminal, None)
                    self.focus_retry_at.pop(terminal, None)
                    continue

                latest_pane = next((item for item in self.cli.panes() if item.get("pane_id") == record.get("pane_id") and item.get("terminal_id") == terminal), None)
                if not latest_pane or not latest_pane.get("focused"):
                    self.focus_since.pop(terminal, None)
                    self.focus_retry_at.pop(terminal, None)
                    continue

                self.cli.start(record["name"], record["kind"], record["pane_id"], resume_args(record))
            except (CliError, KeyError, ValueError):
                # A failed start is retried by the ordinary poll cadence, not
                # by the fast event/deadline loop on every 50 ms tick.
                self.focus_retry_at[terminal] = now + self.config.poll_seconds
                self.focus_deadline = min(
                    self.focus_retry_at[terminal],
                    self.focus_deadline if self.focus_deadline is not None else float("inf"),
                )
                continue

            if not self._restore_sleeping_label(record):
                self.focus_retry_at[terminal] = now + self.config.poll_seconds
                self.focus_deadline = self.focus_retry_at[terminal]
                continue
            records.pop(terminal, None)
            self.focus_since.pop(terminal, None)
            self.focus_retry_at.pop(terminal, None)
