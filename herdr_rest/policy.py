"""Idle eligibility and per-agent timer behavior used by the watcher."""

from __future__ import annotations

from typing import Any

from .agents import valid_session
from .state import Registry


ELIGIBLE = {"idle"}


def session_identity(agent: dict[str, Any]) -> dict[str, str] | None:
    session = agent.get("agent_session")
    if not isinstance(session, dict):
        return None
    kind, value = session.get("agent"), session.get("value")
    if not isinstance(kind, str) or not kind or not isinstance(value, str) or not value:
        return None
    return {"kind": kind, "session": value, "session_ref_kind": session.get("kind", "id")}


def activity_identity(agent: dict[str, Any], workspace_id: str | None = None) -> dict[str, Any]:
    session = agent.get("agent_session")
    session = session if isinstance(session, dict) else {}
    return {
        "workspace_id": workspace_id if workspace_id is not None else agent.get("workspace_id"),
        "session": session.get("value"),
        "kind": session.get("agent"),
        "session_ref_kind": session.get("kind"),
        "session_source": session.get("source"),
        "state_change_seq": agent.get("state_change_seq"),
        "agent_status": agent.get("agent_status"),
        "launch_pending": agent.get("launch_pending"),
    }


def is_trackable_agent(agent: dict[str, Any]) -> bool:
    sequence = agent.get("state_change_seq")
    return valid_session(agent) and isinstance(sequence, int) and not isinstance(sequence, bool)


class IdlePolicyMixin:
    """Idle-timer and observed-activity policy for Daemon."""

    def _reset_inactivity(self) -> None:
        self.agent_timers.clear()
        self.warning_state.clear()
        self.focus_since.clear()
        self.focus_retry_at.clear()
        self.focus_deadline = None

    def _publish_inactivity(self) -> None:
        # Publish timer identities and activity history for the read-only view.
        Registry(self.mutation_lock.parent, "inactivity.json").save({
            "sampled_at": self.clock(),
            "idle_seconds": self.config.idle_seconds,
            "poll_seconds": self.config.poll_seconds,
            "activity": self.activity_history,
            "agents": {
                terminal: {
                    "idle_since": timer["idle_since"],
                    "fingerprint": timer["fingerprint"],
                }
                for terminal, timer in self.agent_timers.items()
            },
        })

    def _update_activity(self, agents: list[dict[str, Any]], panes: list[dict[str, Any]]) -> None:
        """Track observed agent activity independently of idle protection/reset."""
        wall_now = self.wall_clock()
        pane_by_terminal = {pane.get("terminal_id"): pane for pane in panes}
        for agent in agents:
            terminal = agent.get("terminal_id")
            identity = session_identity(agent)
            if not isinstance(terminal, str) or identity is None:
                continue
            previous = self.activity_history.get(terminal, {})
            same_session = isinstance(previous, dict) and previous.get("identity") == identity
            last_active = previous.get("last_active_at") if same_session else None
            state, sequence = agent.get("agent_status"), agent.get("state_change_seq")
            changed = same_session and (
                sequence != previous.get("state_change_seq")
                or state != previous.get("agent_status")
            )
            focused = agent.get("focused") or pane_by_terminal.get(terminal, {}).get("focused")
            if state == "working" or focused or changed:
                last_active = wall_now
            self.activity_history[terminal] = {
                "identity": identity,
                "state_change_seq": sequence,
                "agent_status": state,
                "last_active_at": last_active,
            }

        for terminal in set(self.activity_history) - set(pane_by_terminal):
            self.activity_history.pop(terminal, None)

    def _workspace_map(self, workspaces: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
        result: dict[str, dict[str, Any]] = {}
        for workspace in workspaces:
            workspace_id = workspace.get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id or not isinstance(workspace.get("focused"), bool):
                return None
            if workspace_id in result:
                return None
            result[workspace_id] = workspace
        return result

    def _activity_identity(self, agent: dict[str, Any]) -> dict[str, Any] | None:
        return activity_identity(agent) if is_trackable_agent(agent) else None

    def _clear_warning(self, terminal: str) -> None:
        self.warning_state = {key: value for key, value in self.warning_state.items() if key[1] != terminal}

    def _reset_timer(self, terminal: str) -> None:
        self.agent_timers.pop(terminal, None)
        self._clear_warning(terminal)

    def _reset_workspace_agents(self, agents: list[dict[str, Any]], workspace_id: str) -> None:
        for agent in agents:
            if agent.get("workspace_id") != workspace_id:
                continue
            terminal = agent.get("terminal_id")
            if isinstance(terminal, str):
                self._reset_timer(terminal)
                self.focus_since.pop(terminal, None)
                self.focus_retry_at.pop(terminal, None)

    def _update_agent_timers(self, agents: list[dict[str, Any]], workspaces: dict[str, dict[str, Any]], now: float, panes: list[dict[str, Any]] | None = None) -> None:
        pane_by_terminal = {pane.get("terminal_id"): pane for pane in panes or []}
        seen: set[str] = set()
        for agent in agents:
            terminal = agent.get("terminal_id")
            identity = self._activity_identity(agent)
            if not isinstance(terminal, str) or identity is None:
                continue
            seen.add(terminal)
            workspace = workspaces.get(agent.get("workspace_id"))
            if workspace is None or workspace["focused"] or not self._eligible(agent, pane_by_terminal.get(terminal)):
                self._reset_timer(terminal)
                continue
            previous = self.agent_timers.get(terminal)
            if previous is None or previous.get("fingerprint") != identity:
                self.agent_timers[terminal] = {
                    "fingerprint": identity,
                    "idle_since": now,
                }
                self._clear_warning(terminal)

        for terminal in set(self.agent_timers) - seen:
            self._reset_timer(terminal)

    def _fingerprint(self, agent: dict[str, Any]) -> dict[str, Any] | None:
        if not is_trackable_agent(agent):
            return None

        return {
            "pane_id": agent.get("pane_id"),
            **activity_identity(agent),
            "focused": agent.get("focused"),
            "interactive_ready": agent.get("interactive_ready"),
        }

    def _eligible(self, agent: dict[str, Any], pane: dict[str, Any] | None = None) -> bool:
        # interactive_ready is a launch/readiness hint, not a prerequisite for
        # an already-running agent. Herdr omits it when false, including for
        # ordinary idle agents started directly in a shell. Semantic idle
        # state plus launch_pending is the eligibility authority here.
        return (
            agent.get("agent_status") in ELIGIBLE
            and not agent.get("focused")
            and not agent.get("launch_pending")
            and not (isinstance(pane, dict) and pane.get("focused"))
        )
