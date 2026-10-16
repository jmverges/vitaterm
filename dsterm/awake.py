"""Keep the computer awake while a handheld is connected.

A computer that idles to sleep takes the session with it, and the DS can only
say the link dropped.  So while at least one card is connected the server
holds the operating system's own "not idle" switch, and lets go when the last
card leaves:

  macOS    `caffeinate -i -w PID`: no idle sleep, ended by the system the
           moment this server exits, however it exits.
  Linux    `systemd-inhibit --what=idle` around a `tail --pid=PID`, which
           ends with the server the same way.  Idle only: a suspend you ask
           for from the menu still happens.
  WSL      nothing: Windows decides when the machine sleeps, and nothing
           inside WSL can hold it off.

It only holds off *idle* sleep.  Closing a laptop's lid still sleeps it, which
is what a closed lid should do.  `dsterm serve --no-keep-awake` turns it off.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable


def is_wsl() -> bool:
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def inhibit_command(pid: int | None = None) -> list[str] | None:
    """The command that keeps this machine awake until it is stopped or this
    process ends, or None where there is no such thing."""
    pid = os.getpid() if pid is None else pid
    if sys.platform == "darwin":
        if shutil.which("caffeinate"):
            return ["caffeinate", "-i", "-w", str(pid)]
        return None
    if sys.platform.startswith("linux") and not is_wsl():
        if shutil.which("systemd-inhibit") and shutil.which("tail"):
            return ["systemd-inhibit", "--what=idle", "--who=dsterm",
                    "--why=a handheld is connected", "--mode=block",
                    "tail", f"--pid={pid}", "-f", "/dev/null"]
    return None


class KeepAwake:
    """Hold the machine awake while `hold()` has been called more recently
    than `release()`.  Never raises: being unable to keep the machine awake is
    worth one line in the log, not a server that will not start."""

    def __init__(self, enabled: bool = True,
                 command: list[str] | None = None,
                 spawn: Callable[..., subprocess.Popen] = subprocess.Popen,
                 on_event: Callable[[str], None] | None = None):
        self.enabled = enabled
        self._command = command if command is not None else inhibit_command()
        self._spawn = spawn
        self._emit = on_event or (lambda _msg: None)
        self._proc: subprocess.Popen | None = None
        self._said_unavailable = False

    @property
    def holding(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def hold(self) -> None:
        if not self.enabled or self.holding:
            return
        if not self._command:
            if not self._said_unavailable:
                self._said_unavailable = True
                self._emit("keep-awake: not available here; the computer may "
                           "sleep while the handheld is connected")
            return
        try:
            self._proc = self._spawn(self._command, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            self._emit("keep-awake: holding off idle sleep while the handheld "
                       "is connected")
        except OSError as exc:
            self._proc = None
            self._emit(f"keep-awake: could not start {self._command[0]}: {exc}")

    def release(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass
        self._emit("keep-awake: released")
