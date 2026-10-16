"""Authoritative discovery and selection of the server's tmux windows.

Wire row numbers are deliberately short-lived. tmux window *indices* can be
reused or shifted, so the server resolves a row through the ``window_id`` that
was advertised with it and also records which tmux server produced that id.
Neither value crosses the DS wire.
"""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass


MAX_SESSIONS = 8
NAME_MAX = 24

_TMUX_TIMEOUT = 2.0
_WINDOW_ID = re.compile(r"^@[0-9]+$")


@dataclass(frozen=True, slots=True)
class TmuxEpoch:
    """Identity of one lifetime of the dedicated tmux server."""

    server_pid: int
    session_created: int
    session_id: str


@dataclass(frozen=True, slots=True)
class Window:
    """One currently discoverable tmux window.

    ``index`` is display order only. ``window_id`` is revalidated before a
    previously advertised row can be selected.
    """

    window_id: str
    index: int
    name: str
    active: bool
    activity: int


@dataclass(frozen=True, slots=True)
class WindowSnapshot:
    epoch: TmuxEpoch
    windows: tuple[Window, ...]


def _run(args: list[str]) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=_TMUX_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None


def clamp_name(raw: str) -> str:
    """Return printable ASCII at most ``NAME_MAX`` bytes long."""
    kept = "".join(ch for ch in raw if 0x20 <= ord(ch) <= 0x7E)
    return kept[:NAME_MAX] or "?"


def list_windows(socket: str, target: str) -> WindowSnapshot | None:
    """Return live windows for ``target`` and the tmux-server epoch.

    ``None`` means tmux/the target could not be queried. The caller must
    invalidate previously advertised rows rather than guess a replacement.
    """
    # All fields come from one tmux subprocess, so a replacement cannot make
    # us compare a list from one server lifetime with an epoch from another.
    # Keep the potentially user-controlled name last and split at most seven
    # separators: a tab in a tmux name is then merely non-printable text to
    # clamp, never a shifted identity field.
    fmt = (
        "#{pid}\t#{session_created}\t#{session_id}\t#{window_id}\t"
        "#{window_index}\t#{window_active}\t#{window_activity}\t#{window_name}"
    )
    out = _run(["tmux", "-L", socket, "list-windows", "-t", target, "-F", fmt])
    if out is None or out.returncode != 0:
        return None

    epoch: TmuxEpoch | None = None
    windows: list[Window] = []
    for line in out.stdout.splitlines():
        parts = line.split("\t", 7)
        if len(parts) != 8:
            continue
        try:
            candidate_epoch = TmuxEpoch(int(parts[0]), int(parts[1]), parts[2])
            window_id, index, name, active, activity = (
                parts[3], int(parts[4]), parts[7], parts[5] == "1", int(parts[6])
            )
        except ValueError:
            continue
        if not _WINDOW_ID.fullmatch(window_id):
            continue
        if epoch is None:
            epoch = candidate_epoch
        elif epoch != candidate_epoch:
            # A mixed snapshot is never a safe source of row mappings.
            return None
        windows.append(Window(window_id, index, clamp_name(name), active, activity))
        if len(windows) >= MAX_SESSIONS:
            break

    if epoch is None:
        return None
    return WindowSnapshot(epoch, tuple(windows))


def select(socket: str, target: str, window_id: str) -> bool:
    """Select exactly ``window_id`` in ``target``. Never select by row/index."""
    if not _WINDOW_ID.fullmatch(window_id):
        return False
    out = _run(["tmux", "-L", socket, "select-window", "-t",
                f"{target}:{window_id}"])
    return out is not None and out.returncode == 0


def idle_seconds(last_output_monotonic: float, now: float | None = None) -> int:
    """Elapsed observed-output age, capped for the u16 wire field."""
    now = time.monotonic() if now is None else now
    return max(0, min(0xFFFF, int(now - last_output_monotonic)))
