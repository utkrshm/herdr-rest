"""Read-only agent inactivity table, hosted by a Herdr plugin popup."""

from __future__ import annotations

import curses
import math
import os
import sys
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

from .config import Config, config_dir, state_dir
from .herdr import CliError, Herdr
from .presentation import agent_snapshot


HEADINGS = ("Project", "Agent", "Session", "State", "Remaining", "Last active")


def last_active_text(row: dict[str, Any]) -> str:
    timestamp = row.get("last_active_at")
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
        return "unknown"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except (OSError, OverflowError, ValueError):
        return "unknown"


def remaining_text(row: dict[str, Any]) -> str:
    if row.get("workspace_focused") is True or row.get("agent_status") == "done":
        return "protected"
    if row.get("agent_status") != "idle" or row.get("launch_pending"):
        return "—"
    remaining = row.get("remaining_seconds")
    if not isinstance(remaining, (int, float)) or not math.isfinite(remaining):
        return "unknown"
    seconds = max(0, math.ceil(remaining))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"


def _clean(value: Any) -> str:
    return " ".join("".join(char if char.isprintable() else " " for char in str(value or "—")).split())


def _width(text: str) -> int:
    return sum(0 if unicodedata.combining(char) else 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _cell(text: str, width: int) -> str:
    if _width(text) > width:
        clipped = ""
        for char in text:
            if _width(clipped + char) > width - 1:
                break
            clipped += char
        text = clipped + "…"
    return text + " " * max(0, width - _width(text))


def table_lines(rows: list[dict[str, Any]], width: int) -> list[str]:
    cells = [(
        _clean(row.get("workspace")),
        _clean(row.get("agent")),
        _clean(row.get("session_title")),
        _clean(row.get("agent_status") or "unknown"),
        remaining_text(row),
        last_active_text(row),
    ) for row in rows]
    widths = [
        max(len(HEADINGS[0]), min(24, max((_width(row[0]) for row in cells), default=16))),
        max(len(HEADINGS[1]), min(12, max((_width(row[1]) for row in cells), default=8))),
        24,
        max(len(HEADINGS[3]), max((_width(row[3]) for row in cells), default=8)),
        max(len(HEADINGS[4]), max((_width(row[4]) for row in cells), default=9)),
        max(len(HEADINGS[5]), max((_width(row[5]) for row in cells), default=19)),
    ]
    widths[2] = max(24, width - sum(widths[index] for index in (0, 1, 3, 4, 5)) - 10)
    render = lambda values: "  ".join(_cell(value, size) for value, size in zip(values, widths))
    return [render(HEADINGS), "  ".join("─" * size for size in widths), *[render(row) for row in cells]]


@dataclass
class Viewport:
    top: int = 0
    left: int = 0
    pending_g: bool = False

    def navigate(self, key: int, lines: list[str], rows: int, columns: int) -> bool:
        """Update scroll offsets; return False when the user closes the view."""
        rows = max(1, rows)
        max_top = max(0, len(lines) - rows)
        max_left = max(0, max((len(line) for line in lines), default=0) - max(1, columns))

        if key in (ord("q"), 27):
            return False

        if key == ord("g"):
            if self.pending_g:
                self.top = 0
            self.pending_g = not self.pending_g
            return True

        if key != curses.KEY_RESIZE:
            self.pending_g = False

        if key in (ord("j"), curses.KEY_DOWN):
            self.top += 1
        elif key in (ord("k"), curses.KEY_UP):
            self.top -= 1
        elif key in (ord("h"), curses.KEY_LEFT):
            self.left -= 4
        elif key in (ord("l"), curses.KEY_RIGHT):
            self.left += 4
        elif key in (4,):  # Ctrl-D
            self.top += max(1, rows // 2)
        elif key in (21,):  # Ctrl-U
            self.top -= max(1, rows // 2)
        elif key in (6, curses.KEY_NPAGE, ord(" ")):  # Ctrl-F
            self.top += rows
        elif key in (2, curses.KEY_PPAGE):  # Ctrl-B
            self.top -= rows
        elif key in (ord("G"), curses.KEY_END):
            self.top = max_top
        elif key == curses.KEY_HOME:
            self.top = 0
        elif key == ord("0"):
            self.left = 0

        self.top = max(0, min(self.top, max_top))
        self.left = max(0, min(self.left, max_left))
        return True


def _text(window: Any, row: int, text: str, width: int, attr: int = 0) -> None:
    # Avoid writing into the bottom-right cell, where curses raises on some TTYs.
    if width <= 1:
        return
    try:
        window.addnstr(row, 0, text, width - 1, attr)
    except curses.error:
        pass


def show_table(rows: list[dict[str, Any]], captured_at: str, error: str = "") -> None:
    def run(window: Any) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        window.keypad(True)
        viewport = Viewport()
        while True:
            height, width = window.getmaxyx()
            page_rows = max(1, height - 4)
            lines = table_lines(rows, width - 1)
            body = lines[2:] or [_cell("No agent sessions in this Herdr session.", _width(lines[0]))]
            viewport.navigate(curses.KEY_RESIZE, body, page_rows, width - 1)
            window.erase()
            _text(window, 0, f"Agent inactivity · Snapshot {captured_at}", width, curses.A_REVERSE)
            if height > 2:
                _text(window, 1, lines[0][viewport.left:], width, curses.A_BOLD)
                _text(window, 2, lines[1][viewport.left:], width, curses.A_DIM)

            for index, line in enumerate(body[viewport.top:viewport.top + page_rows]):
                if index + 3 < height - 1:
                    _text(window, index + 3, line[viewport.left:], width)

            footer = _clean(error) if error else f"Snapshot only · j/k h/l · gg/G · Ctrl-D/U · q/Esc close | {len(rows)} sessions"
            if height > 1:
                _text(window, height - 1, footer, width, curses.A_REVERSE)
            window.refresh()

            key = window.getch()
            if not viewport.navigate(key, body, page_rows, width - 1):
                return

    curses.wrapper(run)


def open_view() -> None:
    """Manifest action: open the Herdr-managed view without a shell command."""
    config = Config.load(config_dir())
    client = Herdr(os.environ.get("HERDR_BIN_PATH", config.herdr_binary))
    client.json("plugin", "pane", "open", "--plugin", "herdr.rest", "--entrypoint", "inactivity")


def main() -> None:
    """Manifest pane entry point; Herdr supplies its terminal and session context."""
    rows: list[dict[str, Any]] = []
    error = ""
    try:
        config = Config.load(config_dir())
        client = Herdr(os.environ.get("HERDR_BIN_PATH", config.herdr_binary))
        rows = agent_snapshot(client, state_dir())
    except (CliError, OSError, ValueError) as exc:
        error = f"Unable to read agent snapshot: {exc}"
    try:
        show_table(rows, time.strftime("%Y-%m-%d %H:%M:%S %Z"), error)
    except curses.error as exc:
        print(f"herdr-rest: unable to open Herdr's inactivity view: {exc}", file=sys.stderr)
        raise SystemExit(1)
