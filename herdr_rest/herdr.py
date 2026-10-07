"""Internal communication with the Herdr API through its installed binary."""

from __future__ import annotations

import json
import subprocess
from typing import Any, Callable


class CliError(RuntimeError):
    pass


class Herdr:
    def __init__(
        self,
        binary: str,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        timeout: float = 10.0,
    ):
        self.binary = binary
        self.runner = runner or subprocess.run
        self.timeout = timeout

    def json(self, *args: str) -> Any:
        # These subcommands print JSON responses by default. They do not accept
        # a --json option (only selected commands such as session list do).
        command = [self.binary, *args]

        try:
            result = self.runner(command, text=True, capture_output=True, check=False, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise CliError(f"timed out: {' '.join(command)}") from exc
        except OSError as exc:
            raise CliError(f"cannot run {' '.join(command)}: {exc}") from exc

        if result.returncode:
            raise CliError(result.stderr.strip() or f"{' '.join(command)} failed")

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise CliError(f"invalid JSON from {' '.join(command)}") from exc

    def agents(self) -> list[dict[str, Any]]:
        return _objects(self.json("agent", "list"), "agents")

    def panes(self) -> list[dict[str, Any]]:
        return _objects(self.json("pane", "list"), "panes")

    def workspaces(self) -> list[dict[str, Any]]:
        return _objects(self.json("workspace", "list"), "workspaces")

    def notification(self, title: str, body: str) -> dict[str, Any]:
        return _object(self.json("notification", "show", title, "--body", body), "result")

    def process_info(self, pane_id: str) -> dict[str, Any]:
        return _object(self.json("pane", "process-info", "--pane", pane_id), "process_info")

    def rename_pane(self, pane_id: str, label: str | None) -> None:
        self.json("pane", "rename", pane_id, "--clear" if label is None else label)

    def rename_agent(self, target: str, name: str | None) -> None:
        self.json("agent", "rename", target, "--clear" if name is None else name)

    def agent_read(self, target: str) -> str:
        command = [self.binary, "agent", "read", target, "--source", "visible"]
        try:
            result = self.runner(command, text=True, capture_output=True, check=False, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CliError(f"cannot read {target}: {exc}") from exc
        if result.returncode:
            raise CliError(result.stderr.strip() or f"{' '.join(command)} failed")
        return result.stdout

    def send_key(self, target: str, key: str) -> None:
        # Herdr validates the key name and resolves the live agent target. Send
        # one key per request so a future validation or focus failure cannot
        # allow a later key to reach a replacement process.
        if key != "ctrl+d":
            raise ValueError(f"unsupported native stop key: {key}")
        self.json("agent", "send-keys", target, key)

    def start(self, name: str, kind: str, pane_id: str, args: list[str]) -> None:
        command = [self.binary, "agent", "start", name, "--kind", kind, "--pane", pane_id, "--timeout", "30000"]
        if args:
            command += ["--", *args]

        try:
            result = self.runner(command, text=True, capture_output=True, check=False, timeout=35)
        except subprocess.TimeoutExpired as exc:
            raise CliError("timed out starting resumed agent") from exc
        except OSError as exc:
            raise CliError(f"cannot start resumed agent: {exc}") from exc

        if result.returncode:
            raise CliError(result.stderr.strip() or "agent start failed")


def _objects(value: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]

    if isinstance(value, dict):
        for candidate in (key, "result", "items"):
            if candidate in value:
                return _objects(value[candidate], key)

    return []


def _object(value: Any, key: str) -> dict[str, Any]:
    if isinstance(value, dict) and isinstance(value.get(key), dict):
        return value[key]

    if isinstance(value, dict) and isinstance(value.get("result"), dict):
        return _object(value["result"], key)

    return value if isinstance(value, dict) else {}
