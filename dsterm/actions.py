"""What this project can be told to do, discovered from what is lying around.

The PROJECT page's DO tab is a row of buttons. Four of them are compiled into
the card and are the same everywhere -- clear, git status, git log, ls. These
are the other kind: the ones that know you are in a Node project with a `test`
script, or a repository with a Makefile, and offer that instead of making you
type it on a touchscreen keyboard.

The safety argument, in full, because this module composes text that can end up
in a shell:

* **The card shows the command next to the label.** A button is a label and the
  exact text it will type, and the text is on the top screen beside it. Nothing
  is hidden behind a friendly name.
* **The card will not run it unaided.** Pressing a button loads the command into
  the composer, where a second deliberate press sends it -- the same gate a
  pushed snippet passes. God mode removes that gate, but only the person
  holding the device can arm it, from a page that says in red that it is armed.
* **Names are validated, not quoted.** A script called ``test; rm -rf ~`` would
  compose a command that means something other than it appears to. Rather than
  quoting our way out of that, a name that is not plainly a name is skipped.
  Refusing the weird case costs one button and removes the whole class.
* **Read-only, bounded, and silent on failure.** Files are read with a size cap,
  parsed defensively, and any problem at all yields no actions.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import protocol

# The most any project may contribute. The card shows eight buttons and keeps
# four for its own, so anything past this could never be pressed anyway -- and
# a list that silently runs past the end of the screen is worse than a short
# one, which is why the card says how many it left out.
MAX_ACTIONS = 6

# No file worth reading for this is large. A package.json in the megabytes is
# not a package.json we want to parse on the server's clock.
_READ_LIMIT = 256 * 1024

# What a name has to look like to be spliced into a command.
#
# This is the whole defence and it is deliberately narrow: letters, digits, and
# the four punctuation marks that real script and target names use. A name with
# a space, a quote, a semicolon, a backtick or a dollar in it is not rejected
# because we could not escape it -- it is rejected because a button whose text
# needs escaping to be safe is a button nobody can check by reading.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")

# Makefile targets that are not things a person means to run from a button.
_SKIP_TARGETS = frozenset({
    ".PHONY", ".DEFAULT", ".SUFFIXES", ".PRECIOUS", ".INTERMEDIATE",
    ".SECONDARY", ".DELETE_ON_ERROR", ".IGNORE", ".SILENT", ".EXPORT_ALL_VARIABLES",
})

_MAKE_TARGET = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_.:-]*)\s*:(?!=)")


def _read(path: Path) -> str | None:
    """A bounded read. None for anything that is not a readable small file."""
    try:
        if not path.is_file():
            return None
        if path.stat().st_size > _READ_LIMIT:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _safe(name: str) -> bool:
    return bool(_SAFE_NAME.match(name))


def _label(name: str) -> str:
    """A name shortened to fit a button, keeping the informative end.

    `test:integration` becomes `~ntegration` rather than `test:integ`, because
    the half that tells two buttons apart is usually the tail.
    """
    limit = protocol.PROJECT_LABEL_MAX
    if len(name) <= limit:
        return name
    return "~" + name[-(limit - 1):]


def _npm_scripts(cwd: Path) -> list[tuple[str, str]]:
    text = _read(cwd / "package.json")
    if text is None:
        return []
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return []
    if not isinstance(data, dict):
        return []
    scripts = data.get("scripts")
    if not isinstance(scripts, dict):
        return []
    out: list[tuple[str, str]] = []
    for name in scripts:
        if not isinstance(name, str) or not _safe(name):
            continue
        body = f"npm run {name}"
        if len(body) <= protocol.PROJECT_VALUE_MAX:
            out.append((_label(name), body))
    return out


def _make_targets(cwd: Path) -> list[tuple[str, str]]:
    for filename in ("Makefile", "makefile", "GNUmakefile"):
        text = _read(cwd / filename)
        if text is None:
            continue
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for line in text.splitlines():
            # A recipe line starts with a tab; a target does not. Checking that
            # first keeps a shell line inside a recipe from being read as one.
            if not line or line[0].isspace():
                continue
            match = _MAKE_TARGET.match(line)
            if not match:
                continue
            name = match.group(1)
            if name in _SKIP_TARGETS or name in seen or not _safe(name):
                continue
            seen.add(name)
            body = f"make {name}"
            if len(body) <= protocol.PROJECT_VALUE_MAX:
                out.append((_label(name), body))
        return out
    return []


def _python_tests(cwd: Path) -> list[tuple[str, str]]:
    """One button, and only when there is obviously something to run."""
    has_tests = (cwd / "tests").is_dir() or (cwd / "test").is_dir()
    marked = any((cwd / f).is_file()
                 for f in ("pyproject.toml", "setup.cfg", "pytest.ini", "tox.ini"))
    if has_tests and marked:
        return [("pytest", "pytest -q")]
    return []


# Your own buttons.  One `label = command` per line, `#` for comments -- the
# snippets format.  A project's own file (in the directory the server runs in)
# comes first, then the one in your config directory, then whatever was
# discovered above, up to MAX_ACTIONS in all.  See docs/CUSTOMIZING.md.
PROJECT_BUTTONS = ".dsterm-buttons"
USER_BUTTONS = Path.home() / ".config" / "ds-claude-terminal" / "buttons"


def _user_buttons(path: Path) -> list[tuple[str, str]]:
    """Buttons somebody wrote down on purpose.

    These are the person's own words, so they are not held to the discovered
    names' alphabet -- `git push` is a fine command to want a button for.
    What they are held to is what a button can show honestly: printable
    ASCII, a label that fits the tile, a command that fits the line beside it.
    A line that breaks those is skipped rather than clipped, because a clipped
    command is a different command.
    """
    text = _read(path)
    if text is None:
        return []
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        label, _, body = line.partition("=")
        label, body = label.strip(), body.strip()
        if not label or not body:
            continue
        if not all(0x20 <= ord(ch) <= 0x7E for ch in label + body):
            continue
        if (len(label) > protocol.PROJECT_LABEL_MAX
                or len(body) > protocol.PROJECT_VALUE_MAX):
            continue
        out.append((label, body))
    return out


# Claude Code's prefix for "run this line as a shell command".  What the DO
# buttons type into is Claude's prompt, where a bare `npm run test` is a
# message to Claude; with the prefix it runs, and its output joins the
# conversation.  Your own buttons are typed exactly as you wrote them -- add
# the `!` yourself for a command, or write a prompt or a /command instead.
SHELL_PREFIX = "!"


def discover(cwd: str, user_file: Path | None = None) -> tuple[tuple[str, str], ...]:
    """Up to MAX_ACTIONS (label, command) pairs for this directory.

    Order is by how likely the button is to be the one wanted: the buttons you
    wrote yourself first, then a project's own scripts before its Makefile,
    and the generic test runner last.
    """
    try:
        root = Path(cwd)
        if not root.is_dir():
            return ()
    except OSError:
        return ()

    found: list[tuple[str, str]] = []
    seen_bodies: set[str] = set()
    mine = (*_user_buttons(root / PROJECT_BUTTONS),
            *_user_buttons(user_file if user_file is not None else USER_BUTTONS))
    found_here = [(label, SHELL_PREFIX + body)
                  for label, body in (*_npm_scripts(root), *_make_targets(root),
                                      *_python_tests(root))
                  if len(SHELL_PREFIX + body) <= protocol.PROJECT_VALUE_MAX]
    for candidate in (*mine, *found_here):
        label, body = candidate
        if body in seen_bodies:
            continue
        seen_bodies.add(body)
        found.append(candidate)
        if len(found) >= MAX_ACTIONS:
            break
    return tuple(found)


# Kept here rather than in project.py so the discovery and the thing that
# decides it is worth showing stay next to each other.
def rows(cwd: str) -> list[tuple[str, str]]:
    """The DO tab's rows: label, and the command as the value.

    The value being the command is not a shortcut. It is what puts the exact
    text on the top screen next to the button, so the person pressing it can
    read what it types.
    """
    return [(label, body) for label, body in discover(cwd)]


def project_root(cwd: str) -> str:
    """The directory discovery should look in. The server's cwd, normalised."""
    return os.path.abspath(cwd)
