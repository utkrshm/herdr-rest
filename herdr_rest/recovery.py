"""Rebind restored panes without trusting terminal IDs across server restarts."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .agents import SUPPORTED, _process_name
from .herdr import CliError, Herdr


SHELL_NAMES = {"sh", "bash", "zsh", "fish", "dash", "ksh", "mksh", "nu", "nushell", "xonsh", "elvish", "pwsh", "powershell"}


def restored_pane(record: dict[str, Any], panes: list[dict[str, Any]], agents: list[dict[str, Any]], client: Herdr, claimants: Sequence[dict[str, Any]] = ()) -> dict[str, Any] | None:
    if record.get("kind") not in SUPPORTED or not isinstance(record.get("session"), str) or not record["session"] or not isinstance(record.get("cwd"), str) or not record["cwd"]:
        return None
    candidates = [pane for pane in panes if (
        pane.get("pane_id") == record.get("pane_id")
        and pane.get("cwd") == record["cwd"]
        and (pane.get("foreground_cwd") or pane.get("cwd")) == record["cwd"]
        and isinstance(pane.get("terminal_id"), str)
        and pane.get("terminal_id")
        and all(record.get(key) is None or pane.get(key) == record[key] for key in ("workspace_id", "tab_id"))
    )]
    if len(candidates) != 1:
        return None
    pane = candidates[0]
    live = next((agent for agent in agents if agent.get("terminal_id") == pane["terminal_id"]), None)
    if live:
        session = live.get("agent_session")
        if isinstance(session, dict) and (
            session.get("agent") == record["kind"]
            and session.get("value") == record["session"]
            and session.get("kind") == record.get("session_ref_kind", "id")
            and (record.get("session_source") is None or session.get("source") == record["session_source"])
        ):
            return pane
        return None

    # A label by itself is never a resume instruction. It must exactly match
    # a saved record, the persistent layout IDs, cwd, and an available shell.
    marker = record.get("sleeping_label")
    if not isinstance(marker, str) or not marker.startswith("[sleeping] ") or pane.get("label") != marker:
        return None
    identity = (record["kind"], record.get("session_ref_kind", "id"), record["session"])
    for other in claimants:
        if all(other.get(key) == record.get(key) for key in ("pane_id", "cwd", "sleeping_label")) and (
            other.get("kind"), other.get("session_ref_kind", "id"), other.get("session")
        ) != identity:
            return None
    try:
        info = client.process_info(pane["pane_id"])
    except CliError:
        return None
    shell = info.get("shell_pid")
    processes = info.get("foreground_processes", [])
    if isinstance(shell, int) and shell > 1 and info.get("foreground_process_group_id") == shell and isinstance(processes, list) and any(process.get("pid") == shell and _process_name(process) in SHELL_NAMES for process in processes if isinstance(process, dict)):
        return pane
    return None


def rebind_record(record: dict[str, Any], pane: dict[str, Any]) -> dict[str, Any]:
    rebound = dict(record)
    rebound.update({key: pane[key] for key in ("terminal_id", "pane_id", "workspace_id", "tab_id") if key in pane})
    # PIDs and their start times belong to the old terminal/server instance.
    rebound.pop("process", None)
    rebound.pop("stop_failed", None)
    rebound.pop("recovery_reason", None)
    return rebound
