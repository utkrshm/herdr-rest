"""Supported agent sessions, resume arguments, and process identity checks."""

from __future__ import annotations

import subprocess
import re
import sys
from pathlib import Path
from typing import Any


SUPPORTED = {"opencode", "claude", "codex", "pi"}
PROCESS_NAMES = {
    "opencode": {"opencode", "opencode2", "open-code"},
    "claude": {"claude", "claude-code"},
    "codex": {"codex", "codex-x86_64-unknown-linux-musl", "codex-aarch64-unknown-linux-musl"},
    "pi": {"pi"},
}
PI_PACKAGE_NAMES = {"@mariozechner/pi-coding-agent", "@earendil-works/pi-coding-agent"}
PI_RUNTIME_NAMES = {"node", "nodejs", "bun"}


def original_agent_name(record: dict[str, Any]) -> str | None:
    """Use captured user names; older records used generated names as fallbacks."""
    if "original_agent_name" in record:
        return record["original_agent_name"]
    name = record.get("name")
    if not isinstance(name, str) or re.fullmatch(r"hibernate_[0-9a-f]{8}", name):
        return None
    return name


def valid_session(agent: dict[str, Any]) -> bool:
    session = agent.get("agent_session")
    kind = session.get("agent") if isinstance(session, dict) else None
    source = session.get("source") if isinstance(session, dict) else None
    ref_kind = session.get("kind") if isinstance(session, dict) else None
    source_ok = (kind in {"codex", "pi"} and source == f"herdr:{kind}") or kind in {"opencode", "claude"}
    ref_ok = ref_kind == "id" or (kind == "pi" and ref_kind == "path" and Path(str(session.get("value", ""))).is_absolute())
    return (
        isinstance(agent.get("terminal_id"), str)
        and bool(agent["terminal_id"])
        and isinstance(agent.get("pane_id"), str)
        and isinstance(session, dict)
        and kind in SUPPORTED
        and source_ok
        and ref_ok
        and isinstance(session.get("value"), str)
        and bool(session["value"])
    )


def _linux_start_time(pid: int) -> str | None:
    try:
        return _linux_start_time_from_stat(Path(f"/proc/{pid}/stat").read_text())
    except (OSError, IndexError):
        return None


def _linux_start_time_from_stat(raw: str) -> str | None:
    closing = raw.rfind(")")
    if closing < 0:
        return None

    fields = raw[closing + 2 :].split()
    return fields[19]  # field 22 overall: starttime


def _basename(value: str) -> str:
    return value.rsplit("/", 1)[-1].lower()


def _process_name(item: dict[str, Any]) -> str | None:
    candidates: list[str] = []

    if isinstance(item.get("name"), str):
        candidates.append(item["name"])
    if isinstance(item.get("argv0"), str):
        candidates.append(item["argv0"])

    argv = item.get("argv")
    if isinstance(argv, list) and argv and isinstance(argv[0], str):
        candidates.append(argv[0])

    return _basename(candidates[0]) if candidates else None


def _argv(item: dict[str, Any]) -> list[str]:
    value = item.get("argv")
    return [part for part in value if isinstance(part, str)] if isinstance(value, list) else []


def _is_pi_wrapper(item: dict[str, Any]) -> bool:
    argv = _argv(item)
    runtime = _basename(argv[0]) if argv else _process_name(item)
    if runtime not in PI_RUNTIME_NAMES or len(argv) < 2:
        return False
    script = Path(argv[1])
    if not script.is_absolute() or script.name != "cli.js":
        return False
    parts = script.parts
    return any(
        len(parts) >= len(package.split("/")) + 3
        and parts[-(len(package.split("/")) + 3)] == "node_modules"
        and tuple(parts[-(len(package.split("/")) + 2) : -2]) == tuple(package.split("/"))
        and parts[-1] == "cli.js"
        and parts[-2] == "dist"
        for package in PI_PACKAGE_NAMES
    )


def _process_matches(item: dict[str, Any], agent: str) -> bool:
    if agent == "pi" and _is_pi_wrapper(item):
        return True
    return _process_name(item) in PROCESS_NAMES.get(agent, set())


def process_identity(info: dict[str, Any], agent: str) -> dict[str, Any] | None:
    processes = info.get("foreground_processes")
    if not isinstance(processes, list):
        return None

    matches = []

    for item in processes:
        if not isinstance(item, dict) or not isinstance(item.get("pid"), int) or item["pid"] <= 1:
            continue
        if not _process_matches(item, agent):
            continue

        # Herdr's authoritative schema has no start time. On Linux, require
        # the kernel identity as an additional PID-reuse guard.
        start_time = _linux_start_time(item["pid"]) if sys.platform.startswith("linux") else None
        if sys.platform.startswith("linux") and start_time is None:
            continue
        matches.append({
            "pid": item["pid"],
            "name": item.get("name"),
            "argv0": item.get("argv0"),
            "argv": item.get("argv"),
            "cmdline": item.get("cmdline"),
            "cwd": item.get("cwd"),
            "start_time": start_time,
        })

    return matches[0] if len(matches) == 1 else None


def same_process(identity: dict[str, Any], agent: str) -> bool:
    pid = identity.get("pid")
    if not isinstance(pid, int) or pid <= 1:
        return False

    try:
        if sys.platform.startswith("linux"):
            if identity.get("start_time") is None or _linux_start_time(pid) != identity["start_time"]:
                return False

            raw = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            values = [part.decode(errors="replace") for part in raw if part]
            return _process_matches({"argv": values, "argv0": values[0] if values else ""}, agent)

        result = subprocess.run(["ps", "-p", str(pid), "-o", "command="], text=True, capture_output=True, check=False, timeout=2)
        command = result.stdout.strip().split()
        if not command:
            return False
        return _process_matches({"name": command[0], "argv0": command[0], "argv": command}, agent)
    except (OSError, subprocess.TimeoutExpired):
        return False


def resume_args(record: dict[str, Any]) -> list[str]:
    commands = {
        "opencode": ["--session", record["session"]],
        "claude": ["--resume", record["session"]],
        "codex": ["resume", record["session"]],
        "pi": ["--session", record["session"]],
    }
    try:
        return commands[record["kind"]]
    except (KeyError, TypeError) as exc:
        raise ValueError("unsupported or corrupt hibernated agent kind") from exc
