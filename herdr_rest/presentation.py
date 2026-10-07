"""Session titles, sleeping labels, warnings, and inactivity reports."""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any

from .herdr import CliError, Herdr
from .agents import same_process
from .policy import ELIGIBLE, activity_identity, session_identity
from .state import FileLock, Registry


class PresentationMixin:
    """Presentation methods operating on the state owned by Daemon."""

    def _agent_title(self, agent: dict[str, Any], pane: dict[str, Any] | None) -> str:
        if isinstance(pane, dict):
            value = pane.get("label")
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("title", "terminal_title_stripped", "terminal_title", "name"):
            value = agent.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return "agent"

    @staticmethod
    def _session_title(agent: dict[str, Any], pane: dict[str, Any] | None) -> str:
        for key in ("title", "terminal_title_stripped", "terminal_title"):
            value = agent.get(key)
            if isinstance(value, str) and value.strip():
                title = " ".join(value.split())
                if agent.get("agent") == "opencode" and title.startswith("OC | "):
                    title = title[5:]
                return title

        label = pane.get("label") if pane else None
        if isinstance(label, str) and label.strip() and not label.startswith("[sleeping]"):
            return " ".join(label.split())
        return "Untitled session"

    def _warn_if_needed(self, agent: dict[str, Any], workspace: dict[str, Any], now: float) -> None:
        workspace_id = agent.get("workspace_id")
        terminal = agent.get("terminal_id")
        session = agent.get("agent_session")
        if not isinstance(workspace_id, str) or not isinstance(terminal, str) or not isinstance(session, dict):
            return
        timer = self.agent_timers.get(terminal)
        if timer is None:
            return
        remaining = self.config.idle_seconds - (now - timer["idle_since"])
        if not 0 < remaining < 30:
            return
        key = (workspace_id, terminal, str(session.get("value")))
        attempts = self.warning_state.get(key, 0)
        if attempts >= 3:
            return
        pane = next((item for item in self._last_panes if item.get("terminal_id") == terminal), None)
        title = f"Herdr Rest: {self._agent_title(agent, pane)}"
        body = f"Agent will sleep in {max(1, math.ceil(remaining))} seconds in {workspace.get('label') or workspace_id}"
        self.warning_state[key] = attempts + 1
        try:
            result = self.cli.notification(title[:80], body[:240])
            if result.get("shown") is True or result.get("result", {}).get("shown") is True:
                self.warning_state[key] = 3
        except CliError:
            pass

    def _restore_sleeping_label(self, record: dict[str, Any]) -> bool:
        marker = record.get("sleeping_label")
        if marker is None:
            return True

        try:
            pane = next((item for item in self.cli.panes() if item.get("pane_id") == record.get("pane_id") and item.get("terminal_id") == record.get("terminal_id")), None)
            if pane is not None and pane.get("label") == marker:
                self.cli.rename_pane(pane["pane_id"], record.get("original_label"))
        except CliError:
            return False

        # A label edited by the user is never overwritten by restoration.
        record.pop("sleeping_label", None)
        return True

    def _sync_sleeping_label(self, record: dict[str, Any], pane: dict[str, Any], records: dict[str, dict[str, Any]]) -> None:
        if pane.get("terminal_id") != record.get("terminal_id") or pane.get("pane_id") != record.get("pane_id"):
            return

        label = pane.get("label")
        # Save label ownership before renaming, so a crash can still restore it.
        # This also adopts a new custom label if the user renamed a sleeping pane.
        owned = record.get("sleeping_label") is not None and label == record["sleeping_label"]
        if not owned:
            record["original_label"] = label
        title = (
            record.get("session_title")
            or record.get("sleep_label_base")
            or record.get("original_label")
            or record.get("session")
            or "Untitled session"
        )
        title = " ".join(str(title).split())
        if record.get("kind") == "opencode" and title.startswith("OC | "):
            title = title[5:]
        marker = f"[sleeping] {record.get('kind') or 'agent'}: {title}"
        if owned and label == marker:
            return
        record["sleeping_label"] = marker
        self.registry.save(records)

        try:
            self.cli.rename_pane(pane["pane_id"], marker)
        except CliError:
            # The next reconciliation retries without affecting sleep/resume.
            pass


def agent_snapshot(cli: Herdr, directory: Path, now: float | None = None) -> list[dict[str, Any]]:
    now = time.monotonic() if now is None else now
    snapshot = Registry(directory, "inactivity.json").load()
    records = Registry(directory).load()
    agents = cli.agents()
    panes = cli.panes()
    workspaces = {item["workspace_id"]: item for item in cli.workspaces() if isinstance(item.get("workspace_id"), str)}

    # A stale snapshot must not be mistaken for a live countdown after exit.
    running = False
    try:
        with FileLock(directory / "daemon.lock", blocking=False):
            pass
    except TimeoutError:
        running = True

    sampled_at = snapshot.get("sampled_at")
    poll_seconds = snapshot.get("poll_seconds", 2)
    age = now - sampled_at if isinstance(sampled_at, (int, float)) else None
    fresh = (
        running
        and age is not None
        and 0 <= age <= max(30, 3 * poll_seconds)
    )
    entries = {item.get("terminal_id"): item for item in agents}
    live_terminals = set(entries)
    for terminal, record in records.items():
        if terminal not in entries:
            entries[terminal] = {
                "terminal_id": terminal,
                "pane_id": record.get("pane_id"),
                "name": record.get("name"),
                "agent": record.get("kind"),
                "title": record.get("session_title") or record.get("sleep_label_base") or record.get("session"),
                "agent_status": "sleeping" if not record.get("stop_failed") and not same_process(record.get("process", {}), record.get("kind", "")) else "unknown",
                "agent_session": {"value": record.get("session"), "agent": record.get("kind"), "kind": record.get("session_ref_kind", "id")},
            }

    results = []
    for terminal, entry in entries.items():
        pane = next((item for item in panes if item.get("terminal_id") == terminal and item.get("pane_id") == entry.get("pane_id")), None)
        if pane is None:
            continue
        workspace_id = pane.get("workspace_id") or entry.get("workspace_id")
        workspace = workspaces.get(workspace_id, {})
        focused = workspace.get("focused")
        pane_focused = pane.get("focused") is True
        timer = snapshot.get("agents", {}).get(terminal, {})
        since = timer.get("idle_since")
        session = entry.get("agent_session") or {}
        threshold = snapshot.get("idle_seconds") if fresh else None
        history = snapshot.get("activity", {}).get(terminal, {})
        last_active = history.get("last_active_at") if isinstance(history, dict) and history.get("identity") == session_identity(entry) else None
        if last_active is None and terminal not in live_terminals:
            last_active = records.get(terminal, {}).get("last_active_at")
        inactive = None
        timer_reason = "agent_not_observed"
        if focused is True or pane_focused:
            inactive = 0.0
            timer_reason = "workspace_focused"
        elif entry.get("agent_status") == "sleeping":
            timer_reason = "sleeping"
        elif focused is False and entry.get("agent_status") not in ELIGIBLE:
            inactive = 0.0
            timer_reason = "agent_not_idle"
        elif focused is False and fresh and timer.get("fingerprint") != activity_identity(entry, workspace_id):
            timer_reason = "agent_identity_changed"
        elif focused is False and fresh and isinstance(since, (int, float)):
            inactive = max(0.0, now - since)
            timer_reason = "available"
        elif not running:
            timer_reason = "watcher_not_running"
        elif sampled_at is None:
            timer_reason = "snapshot_missing"
        elif not fresh:
            timer_reason = "snapshot_stale"
        elif not isinstance(focused, bool):
            timer_reason = "workspace_focus_unknown"

        results.append({
            "pane_id": entry.get("pane_id"),
            "name": entry.get("name"),
            "agent": entry.get("agent") or session.get("agent") or "unknown",
            "session_title": PresentationMixin._session_title(entry, pane),
            "session_id": session.get("value"),
            "workspace_id": workspace_id,
            "workspace": workspace.get("label") or workspace_id,
            "workspace_focused": focused,
            "inactive_seconds": inactive,
            "remaining_seconds": max(0.0, threshold - inactive) if threshold is not None and inactive is not None else None,
            "idle_seconds": threshold,
            "observation_age_seconds": age,
            "watcher_running": running,
            "timer_available": inactive is not None,
            "timer_reason": timer_reason,
            "agent_status": entry.get("agent_status"),
            "launch_pending": bool(entry.get("launch_pending")),
            "last_active_at": last_active,
        })

    workspace_order = {workspace_id: index for index, workspace_id in enumerate(workspaces)}
    pane_order = {pane.get("pane_id"): index for index, pane in enumerate(panes)}
    results.sort(key=lambda row: (workspace_order.get(row["workspace_id"], len(workspaces)), pane_order.get(row["pane_id"], len(panes))))
    return results
