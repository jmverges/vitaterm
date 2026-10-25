"""`vitaterm`: the server for the PS Vita client (vita/ in this repository).

The same server as `dsterm`, sized for the Vita: its terminal is 96x28, so
`vitaterm serve` and the service `vitaterm setup` installs run at that grid
unless told otherwise.  Only the commands that make sense without a DS are
offered; the DS-only ones (card, build, view...) stay under `dsterm`.
"""

from __future__ import annotations

import os
import sys

from . import cli

VITA_GRID = "96x28"

USAGE = f"""usage: vitaterm COMMAND [options]

Claude Code on your computer, on a PS Vita.

  setup        install the server as a background service (once)
  pair         pair a Vita: compare the code on both screens
  devices      the Vitas (and DSs) paired with this computer
  doctor       check everything, and say how to fix what isn't right
  start | stop | restart | status | logs
               the background service
  serve        run the server in this terminal (grid {VITA_GRID})
  attach       open the Claude session in this terminal

On the Vita, install vitaterm.vpk and pick this computer from its start
screen.
"""

COMMANDS = ("setup", "pair", "devices", "doctor", "start", "stop", "restart",
            "status", "logs", "serve", "attach")


def has_grid(args: list[str]) -> bool:
    return any(a == "--grid" or a.startswith("--grid=") for a in args)


def ensure_serve_conf_grid() -> None:
    """Give the service the Vita's grid, unless serve.conf already sets one.

    The service runs a fixed command line and reads its options from
    serve.conf (see cli.SERVE_CONF), so that is where the grid goes."""
    path = cli.SERVE_CONF
    text = path.read_text() if path.exists() else ""
    words = [w for line in text.splitlines() for w in line.split("#", 1)[0].split()]
    if has_grid(words):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    note = "# The PS Vita's terminal (vitaterm).\n"
    path.write_text(text + ("" if not text or text.endswith("\n") else "\n")
                    + note + f"--grid {VITA_GRID}\n")
    print(f"vitaterm: {path} now runs the server at {VITA_GRID}")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(USAGE, end="")
        return 0
    if argv[0] in ("-V", "--version", "version"):
        from importlib.metadata import PackageNotFoundError, version
        try:
            print(f"vitaterm {version('ds-claude-terminal')}")
        except PackageNotFoundError:
            print("vitaterm (not installed: a checkout without its venv)")
        return 0
    name = argv[0]
    if name not in COMMANDS:
        print(f"vitaterm: unknown command '{name}' -- try: vitaterm --help",
              file=sys.stderr)
        return 2
    if name == "setup":
        ensure_serve_conf_grid()
    if name == "serve" and not has_grid(argv) and "DSTERM_COLS" not in os.environ:
        argv = [name, "--grid", VITA_GRID, *argv[1:]]
    return cli.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
