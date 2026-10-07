"""Configuration and Herdr plugin directory resolution."""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from pathlib import Path

from .utils import atomic_create


@dataclass(frozen=True)
class Config:
    idle_seconds: float = 900.0
    poll_seconds: float = 2.0
    focus_debounce_seconds: float = 0.15
    terminate_wait_seconds: float = 15.0
    herdr_binary: str = "herdr"

    @classmethod
    def load(cls, directory: Path) -> "Config":
        path = directory / "config.toml"
        if not path.exists():
            atomic_create(path, DEFAULT_CONFIG)
        try:
            import tomllib

            with path.open("rb") as stream:
                values = tomllib.load(stream).get("hibernate", {})
        except (OSError, ValueError, ImportError) as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc

        try:
            result = cls(
                float(values.get("idle_seconds", cls.idle_seconds)),
                float(values.get("poll_seconds", cls.poll_seconds)),
                float(values.get("focus_debounce_seconds", cls.focus_debounce_seconds)),
                float(values.get("terminate_wait_seconds", cls.terminate_wait_seconds)),
                str(values.get("herdr_binary", cls.herdr_binary)),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid hibernate configuration in {path}") from exc

        if not all(math.isfinite(value) for value in (
            result.idle_seconds,
            result.poll_seconds,
            result.focus_debounce_seconds,
            result.terminate_wait_seconds,
        )):
            raise ValueError("duration values must be finite")
        if result.idle_seconds <= 0 or result.poll_seconds <= 0 or result.terminate_wait_seconds <= 0:
            raise ValueError("idle_seconds, poll_seconds, and terminate_wait_seconds must be positive")
        if result.focus_debounce_seconds < 0 or not result.herdr_binary:
            raise ValueError("focus_debounce_seconds must be non-negative and herdr_binary cannot be empty")
        return result


def plugin_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env.get("HERDR_PLUGIN_ROOT", Path(__file__).resolve().parents[1]))


DEFAULT_CONFIG = """[hibernate]
idle_seconds = 900
poll_seconds = 2
focus_debounce_seconds = 0.15
terminate_wait_seconds = 15
herdr_binary = "herdr"
"""


def config_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    home = Path(env.get("HOME", str(Path.home())))
    root = Path(env.get("XDG_CONFIG_HOME", home / ".config"))
    return Path(env.get("HERDR_PLUGIN_CONFIG_DIR", root / "herdr" / "plugins" / "config" / "herdr.rest"))


def _socket_identity(env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env

    socket_path = env.get("HERDR_SOCKET_PATH")
    if not socket_path:
        raise ValueError("HERDR_SOCKET_PATH is required; refusing to use shared default state")

    return os.path.abspath(socket_path)


def state_dir(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    home = Path(env.get("HOME", str(Path.home())))
    state_home = Path(env.get("XDG_STATE_HOME", home / ".local" / "state"))
    root = Path(env.get("HERDR_PLUGIN_STATE_DIR", state_home / "herdr" / "plugins" / "herdr.rest"))
    key = hashlib.sha256(_socket_identity(env).encode()).hexdigest()[:20]

    path = root / key
    path.mkdir(parents=True, exist_ok=True)

    return path
