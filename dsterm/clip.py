"""The desktop clipboard, folded to something a 42x24 ASCII screen can hold.

One direction only: desktop to handheld. Nothing comes up from the DS, and this
module has no way to write a clipboard even if something asked it to.

Three properties are load-bearing.

* **Concealed content never leaves this process.** A password manager marks the
  clipboard with ``ExcludeClipboardContentFromMonitorProcessing`` or
  ``CanIncludeInClipboardHistory``; well-behaved clipboard-sync tools test both
  and re-check as late as possible, immediately before sending. This matches
  that, and does the check in the SAME
  PowerShell invocation as the read: two invocations would leave a ~200 ms
  window -- measured, that is what one spawn costs here -- in which a password
  copied between the check and the read would be returned as ordinary text.
  ``Get-Clipboard`` alone does not consult those markers at all.
* **No heuristics.** Nothing here guesses whether text looks secret. That is
  outcome inference wearing a different hat and it is wrong in both directions:
  it leaks the password it did not recognise and hides the paragraph it did.
* **The text is never logged.** Not to the journal, not to the dashboard's event
  list, not in an exception message. Length, chunk count and flags only. The
  LAN exposure a user accepts with ``--clip`` is transient; a copy of every
  password in ``journalctl`` would be permanent and at rest, which nobody
  accepted.

Reading is slow -- 0.18-0.22 s per spawn, measured from WSL -- so it happens on
a worker thread, never on the frame loop. See ``ClipReader``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import unicodedata

from . import protocol

# Where powershell.exe actually is, when PATH does not say.
#
# This shipped broken. The systemd unit sets PATH explicitly -- that line exists
# so the child can find `claude` -- and the value it sets does not include the
# Windows interop directory. So "powershell.exe" did not resolve under the
# service, subprocess.run raised OSError, and the silence below swallowed it on
# every poll forever. It worked in every test and every interactive shell,
# because both have the interop path. Resolve it absolutely instead of trusting
# an environment this process does not control.
_POWERSHELL_CANDIDATES = (
    "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/Windows/system32/WindowsPowerShell/v1.0/powershell.exe",
    "/mnt/c/Program Files/PowerShell/7/pwsh.exe",
)


def powershell_path() -> str | None:
    """An absolute path to PowerShell, or None if this host has no interop."""
    found = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if found:
        return found
    for candidate in _POWERSHELL_CANDIDATES:
        if os.path.exists(candidate):
            return candidate
    return None

# One spawn costs ~0.2 s, so this is not free even off the loop: at 1 Hz it is a
# process every second forever on a machine that is mostly not copying anything.
POLL_INTERVAL_S = 3.0

# Long enough for a cold PowerShell on a busy box, short enough that a wedged
# one cannot pin a worker thread indefinitely.
_READ_TIMEOUT = 8.0

# PowerShell, doing the conceal check and the read as one atomic-enough step.
#
# The two format names are Windows' own, verbatim. RegisterClipboardFormat
# returns 0 only on failure, and a 0 id must never be treated as "available" or
# every read would look concealed -- so this guards it.
#
# Output contract: the first line is CONCEALED or CLEAR; everything after the
# first newline is the clipboard text. Nothing else is printed, so a stray
# warning cannot be mistaken for content.
_PROBE = r"""
$ErrorActionPreference = 'Stop'
# Declare UTF-8 before writing anything. Without this PowerShell emits the
# console code page, Python decodes it as UTF-8, and one non-ASCII byte raises
# UnicodeDecodeError -- observed live as 0xf2 at position 1095, which lost the
# whole clipboard. Worse, it fails on exactly the characters the fold below
# exists for: an em dash, a curly quote, an accented name. Plain ASCII worked
# forever and the first interesting paste broke it.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -Namespace DsTerm -Name Clip -MemberDefinition @'
[DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
public static extern uint RegisterClipboardFormatW(string lpszFormat);
[DllImport("user32.dll", SetLastError=true)]
public static extern bool IsClipboardFormatAvailable(uint format);
'@
$exclude = [DsTerm.Clip]::RegisterClipboardFormatW('ExcludeClipboardContentFromMonitorProcessing')
$history = [DsTerm.Clip]::RegisterClipboardFormatW('CanIncludeInClipboardHistory')
$concealed = ($exclude -ne 0 -and [DsTerm.Clip]::IsClipboardFormatAvailable($exclude)) -or
             ($history -ne 0 -and [DsTerm.Clip]::IsClipboardFormatAvailable($history))
if ($concealed) { Write-Output 'CONCEALED'; exit 0 }
Write-Output 'CLEAR'
$text = Get-Clipboard -Raw
if ($null -ne $text) { [Console]::Out.Write($text) }
"""

# UTF-8 that a 6x8 ASCII font cannot draw, folded to something that reads the
# same. Anything not here and not printable is dropped, and either way the
# TRANSLITERATED flag says the text is no longer byte-for-byte what was copied.
_FOLD = {
    "—": "-", "–": "-", "−": "-",
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"',
    "…": "...",
    " ": " ", " ": " ", " ": " ", " ": " ",
    "•": "*", "·": "*",
    "\t": "    ",
}


class Snapshot:
    """One reading of the clipboard, already converted and chunked.

    ``text`` is the converted text, truncated to CLIP_TEXT_MAX. ``total`` is the
    length of the converted text BEFORE truncation, which is what makes
    ``sum(len(chunk)) == min(total, CLIP_TEXT_MAX)`` assertable.
    """

    __slots__ = ("text", "total", "flags", "withheld")

    def __init__(self, text: str, total: int, flags: int,
                 withheld: bool = False) -> None:
        self.text = text
        self.total = total
        self.flags = flags
        self.withheld = withheld

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Snapshot):
            return NotImplemented
        return (self.text == other.text and self.total == other.total
                and self.flags == other.flags
                and self.withheld == other.withheld)

    def __repr__(self) -> str:
        # Deliberately does NOT include the text. This object ends up in
        # exception messages and debugger output, and the whole point is that
        # the clipboard never reaches a log.
        state = "withheld" if self.withheld else f"{len(self.text)}B"
        return f"<Snapshot {state} total={self.total} flags={self.flags:#04x}>"


EMPTY = Snapshot("", 0, 0)
WITHHELD = Snapshot("", 0, protocol.CLIP_FLAG_WITHHELD, withheld=True)


def convert(raw: str) -> Snapshot:
    """Fold arbitrary clipboard text into what the wire and the font allow.

    Newlines first: CRLF and a lone CR both become LF, because the card holds LF
    through the composer's ENT key and would otherwise draw a stray glyph.
    """
    text = raw.replace("\r\n", "\n").replace("\r", "\n")

    out: list[str] = []
    changed = False
    for ch in text:
        if ch == "\n":
            out.append(ch)
            continue
        if 0x20 <= ord(ch) <= 0x7E:
            out.append(ch)
            continue
        folded = _FOLD.get(ch)
        if folded is None:
            # Try the standard decomposition before giving up: "é" becomes "e"
            # rather than vanishing, which is a better answer than a hole.
            stripped = "".join(
                c for c in unicodedata.normalize("NFKD", ch)
                if 0x20 <= ord(c) <= 0x7E
            )
            folded = stripped
        changed = True
        out.append(folded)

    converted = "".join(out)
    total = len(converted.encode("ascii", errors="replace"))

    flags = protocol.CLIP_FLAG_TRANSLITERATED if changed else 0
    if total > protocol.CLIP_TEXT_MAX:
        converted = converted[:protocol.CLIP_TEXT_MAX]
        flags |= protocol.CLIP_FLAG_TRUNCATED
    return Snapshot(converted, total, flags)


def read(timeout: float = _READ_TIMEOUT) -> Snapshot | None:
    """The clipboard right now, or None if it could not be read.

    None means "no answer", which the caller treats as "keep the last one" --
    distinct from EMPTY, which is a real reading of an empty clipboard.
    """
    shell = powershell_path()
    if shell is None:
        return None
    try:
        out = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-Command", _PROBE],
            capture_output=True, text=True, timeout=timeout, check=False,
            # errors="replace" so the decode cannot raise. The probe declares
            # UTF-8 above, which is the actual fix; this is what stops a byte
            # that still slips through -- a console that ignored the request, a
            # PowerShell that failed before reaching that line -- from costing
            # the entire clipboard instead of one character. A replacement
            # character is folded away by convert() like any other non-ASCII.
            errors="replace",
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError, ValueError):
        # UnicodeDecodeError is neither OSError nor SubprocessError, which is
        # how the live failure escaped read() entirely and surfaced as "cannot
        # read the clipboard" with no indication that decoding was the cause.
        return None
    if out.returncode != 0:
        return None

    body = out.stdout
    marker, _, rest = body.partition("\n")
    marker = marker.strip()
    if marker == "CONCEALED":
        return WITHHELD
    if marker != "CLEAR":
        # An unrecognised first line means the probe did not run as written.
        # Refusing is the safe direction: a malformed answer must never be
        # mistaken for a clear clipboard.
        return None
    if not rest:
        return EMPTY
    return convert(rest)


def chunks(snapshot: Snapshot, seq: int) -> list[bytes]:
    """A snapshot as wire frames, staged the way SESSIONS and PROJECT are.

    An empty or withheld clipboard is one explicit empty frame rather than no
    frames, because "nothing is on the clipboard" and "this server does not
    report it" have to look different on the handheld -- and "it was withheld"
    has to look different from both.
    """
    if snapshot.withheld:
        return [protocol.encode_clip_empty(protocol.CLIP_FLAG_WITHHELD)]
    body = snapshot.text.encode("ascii", errors="replace")
    if not body:
        return [protocol.encode_clip_empty(snapshot.flags)]

    size = protocol.CLIP_CHUNK_MAX
    pieces = [body[i:i + size] for i in range(0, len(body), size)]
    count = len(pieces)
    return [
        protocol.encode_clip_chunk(i, count, seq, snapshot.flags,
                                   snapshot.total, piece)
        for i, piece in enumerate(pieces)
    ]


class ClipReader:
    """``read()`` on a worker thread, so the frame loop never waits for it.

    Deliberately parallel to ``project.BackgroundCollector`` rather than shared
    with it: the two have different cadences and different arguments, and the
    abstraction that would unify them is not worth inventing for two callers.
    If a third appears, extract it then.

    ``poll`` never blocks. It publishes a finished reading, starts the next when
    the slow clock says so, and returns True only when the clipboard actually
    changed -- an unchanged clipboard must not re-push, or every three seconds
    would put the same six frames on the wire.
    """

    def __init__(self, interval: float = POLL_INTERVAL_S,
                 reader=None, on_problem=None) -> None:
        self._interval = interval
        self._reader = reader or read
        self._on_problem = on_problem
        self._lock = threading.Lock()
        self._snapshot: Snapshot | None = None
        self._result: Snapshot | None = None
        self._worker: threading.Thread | None = None
        self._started_at: float | None = None
        self._stopped = False
        self._seq = 0
        # Silence is right for a hiccup and wrong for a broken deployment. The
        # PATH bug that shipped here was invisible because every failed read
        # looked like every other one: nothing at all, forever. So the FIRST
        # failure is announced and so is the recovery, and nothing in between,
        # which keeps a wedged host from writing a line every three seconds.
        self._failing = False

    @property
    def snapshot(self) -> Snapshot | None:
        """The last complete reading, or None if there has never been one."""
        with self._lock:
            return self._snapshot

    @property
    def seq(self) -> int:
        with self._lock:
            return self._seq

    def poll(self, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        changed = False
        worker: threading.Thread | None = None

        with self._lock:
            if self._stopped:
                return False
            if self._result is not None:
                fresh, self._result = self._result, None
                self._worker = None
                if fresh != self._snapshot:
                    self._snapshot = fresh
                    # Wraps at a byte, which is what the card expects; two
                    # snapshots 256 apart cannot be in flight together.
                    self._seq = (self._seq + 1) & 0xFF
                    changed = True
            due = (self._started_at is None
                   or stamp - self._started_at >= self._interval)
            if self._worker is None and due:
                self._started_at = stamp
                worker = threading.Thread(target=self._run,
                                          name="dsterm-clip", daemon=True)
                self._worker = worker

        if worker is not None:
            worker.start()
        return changed

    def stop(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._stopped = True
            worker = self._worker
            self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout)

    def _run(self) -> None:
        try:
            snapshot = self._reader()
        except Exception:
            # The exception is swallowed rather than logged: its message could
            # quote clipboard text, and a password in the journal is permanent.
            # That the read failed is reported below; what it said is not.
            snapshot = None

        announce = None
        with self._lock:
            if snapshot is None:
                self._worker = None
                if not self._failing:
                    self._failing = True
                    shell = powershell_path()
                    where = shell or "no powershell.exe on this host"
                    announce = f"clip: cannot read the clipboard ({where})"
            else:
                self._result = snapshot
                if self._failing:
                    self._failing = False
                    announce = "clip: reading the clipboard again"

        # Outside the lock: the callback belongs to the server and may do
        # anything, including take locks of its own.
        if announce is not None and self._on_problem is not None:
            self._on_problem(announce)
