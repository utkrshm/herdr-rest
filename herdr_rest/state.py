"""Atomic persistence, process locks, and watcher control requests."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

from .config import state_dir
from .utils import atomic_create, atomic_replace


class Registry:
    def __init__(self, directory: Path, filename: str = "registry.json"):
        self.path = directory / filename

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}

        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, dict[str, Any]]) -> None:
        atomic_replace(self.path, json.dumps(data, indent=2, sort_keys=True) + "\n")


class FileLock:
    def __init__(self, path: Path, blocking: bool = True):
        self.path, self.blocking, self.handle = path, blocking, None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")

        try:
            import fcntl

            flags = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
            fcntl.flock(self.handle, flags)
        except (ImportError, BlockingIOError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in (errno.EACCES, errno.EAGAIN):
                raise

            self.handle.close()
            self.handle = None
            raise TimeoutError(f"lock busy: {self.path}") from exc

        return self

    def __exit__(self, *_: object) -> None:
        if self.handle:
            self.handle.close()
            self.handle = None


class Singleton(FileLock):
    def __enter__(self) -> "Singleton":
        super().__enter__()

        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()

        return self


def request_focus(env: dict[str, str] | None = None) -> None:
    """Write the current Herdr session's coalesced focus wake request."""
    atomic_create(state_dir(env) / "wake.request", "")
