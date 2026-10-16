"""The slash commands this computer's Claude Code offers, for QUICK's list.

The handheld has a few built in (/compact, /context, /clear ...). These are
the ones particular to this computer: the project's own commands and skills,
and your personal ones --

    <project>/.claude/commands/**/*.md      /name   (the file's name)
    ~/.claude/commands/**/*.md              /name
    <project>/.claude/skills/<name>/SKILL.md /name
    ~/.claude/skills/<name>/SKILL.md         /name

Only names cross the wire, never a file's contents or a path, and a name has
to look like one (letters, digits, and _ - . :) or it is skipped rather than
escaped -- the same rule actions.py keeps for the DO tab. On the card a name
goes into the composer, so nothing runs until the person presses A.

Read-only and bounded: a few directories, a capped walk, no file opened.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

# The card shows its six built-ins first and keeps 24 in all.
MAX_COMMANDS = 18
NAME_MAX = 30
# How many directory entries one walk may look at before giving up on the
# rest: a home directory with an enormous ~/.claude is not worth a stall.
_WALK_LIMIT = 2000
_SAFE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]*$")


def _ok(name: str) -> bool:
    return 0 < len(name) <= NAME_MAX and bool(_SAFE.match(name))


def _command_files(root: Path) -> list[str]:
    """The .md files under `root`, by name, without following links out."""
    found: list[str] = []
    if not root.is_dir():
        return found
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        for f in sorted(filenames):
            seen += 1
            if seen > _WALK_LIMIT:
                return found
            if f.endswith(".md"):
                found.append(f[:-3])
        if len(Path(dirpath).relative_to(root).parts) >= 3:
            dirnames[:] = []
    return found


def _skills(root: Path) -> list[str]:
    found: list[str] = []
    if not root.is_dir():
        return found
    try:
        entries = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return found
    for entry in entries[:_WALK_LIMIT]:
        try:
            if entry.is_dir(follow_symlinks=False) and (Path(entry.path) / "SKILL.md").is_file():
                found.append(entry.name)
        except OSError:
            continue
    return found


def discover(cwd: str, home: Path | None = None) -> tuple[str, ...]:
    """The computer's commands, project first, as "/name": at most
    MAX_COMMANDS of them, and no more than fit in one COMMANDS frame (a
    sealed 536-byte segment), so a list of long names stops early. Never
    raises."""
    from .protocol import COMMANDS_PAYLOAD_MAX
    home = Path.home() if home is None else home
    project = Path(cwd)
    names: list[str] = []
    try:
        groups = (
            _command_files(project / ".claude" / "commands"),
            _skills(project / ".claude" / "skills"),
            _command_files(home / ".claude" / "commands"),
            _skills(home / ".claude" / "skills"),
        )
    except OSError:
        return ()
    size = 0                                   # the payload so far, newlines included
    for group in groups:
        for name in group:
            if not _ok(name):
                continue
            slash = "/" + name
            if slash in names:
                continue
            grown = size + len(slash) + (1 if names else 0)
            if grown > COMMANDS_PAYLOAD_MAX:
                return tuple(names)
            names.append(slash)
            size = grown
            if len(names) >= MAX_COMMANDS:
                return tuple(names)
    return tuple(names)
