"""Small internal command dispatcher for Herdr's manifest entry points."""

import argparse
import os


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Herdr Rest internal entry points")
    parser.add_argument("command", nargs="?", default="run", choices=("start", "run", "focus", "open-view", "view"))
    command = parser.parse_args(argv).command

    if command == "start":
        from .daemon import start_process

        return start_process(dict(os.environ))
    if command == "run":
        from .daemon import daemon_process

        return daemon_process()
    if command == "focus":
        from .state import request_focus

        request_focus()
    elif command == "open-view":
        from .view import open_view

        open_view()
    else:
        from .view import main as view_main

        view_main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
