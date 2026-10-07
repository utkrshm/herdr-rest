"""Shared atomic file operations."""

import contextlib
import os
import tempfile
from pathlib import Path


def atomic_create(path: Path, content: str) -> None:
    """Publish a complete file only if the destination does not exist."""
    _atomic_write(path, content, replace=False)


def atomic_replace(path: Path, content: str) -> None:
    """Replace a destination with a complete, flushed file."""
    _atomic_write(path, content, replace=True)


def _atomic_write(path: Path, content: str, replace: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f"{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
