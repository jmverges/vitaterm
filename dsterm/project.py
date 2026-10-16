"""Facts about the repository the child is working in, for the PROJECT page.

The handheld cannot see a filesystem and must never be able to name one. This
module is the whole of what it learns: a fixed set of read-only questions asked
of ``git`` in the server's own working directory, turned into finished label and
value strings that the card draws and does not interpret.

Three properties are load-bearing and none of them is an accident:

* **Read-only.** Every command below is a query. Nothing here writes, checks
  out, stages, or fetches, and the argument lists are literals -- no value from
  the wire, the page, or the repository is ever spliced into one.
* **Facts, not verdicts.** "quiet for 40s" is a fact. "the agent is stuck" is a
  guess about someone else's state, and this server does not make those; the
  same rule that keeps it from inferring success or failure from the shape of
  the terminal applies to the page built beside it.
* **Failure is silence.** A directory that is not a repository, a git that is
  missing, a command that hangs: all of them produce no rows. The card shows
  "this server does not report it", which is true, rather than a stale answer.

The card renders whatever it is sent, so every string that leaves here is
already clipped to the widths in ``protocol.py``.
"""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field

from . import actions as dsactions
from . import protocol

# Long enough for a cold cache on a large repository, short enough that a
# wedged git cannot stall the frame loop it is polled from.
_GIT_TIMEOUT = 3.0

# Git is forked to answer these, so it is asked on its own slow clock rather
# than on the server's 15 fps tick or even the once-a-second session poll.
# Nothing on this page changes faster than this is worth noticing.
POLL_INTERVAL_S = 3.0

# How many changed files the DIFF tab lists. The page has eighteen rows and the
# rest of the answer is the summary line above them.
_DIFF_FILES_MAX = 12

# Palette indices, mirrored from render.h. Named here so the wording and the
# colour of a row are decided in the same place.
_PLAIN = 0
_RED = 2
_GREEN = 3
_ORANGE = 9


@dataclass(slots=True)
class Row:
    """One line of the page: which tab, what it says, and what colour."""

    tab: int
    label: str
    value: str
    fg: int = _PLAIN


@dataclass(slots=True)
class ProjectState:
    """The last snapshot, and when it was taken."""

    rows: tuple[Row, ...] = ()
    taken_at: float = 0.0
    # Set once we know the cwd is not a git worktree, so a non-repository
    # directory costs one git invocation rather than one every three seconds.
    not_a_repo: bool = False
    _pending: list[Row] = field(default_factory=list)


def _git(cwd: str, *args: str) -> str | None:
    """One read-only git query. None on any failure at all.

    ``args`` is always a literal at every call site below; this signature takes
    them varargs so that stays visible rather than being assembled somewhere.
    """
    try:
        out = subprocess.run(
            ["git", "--no-optional-locks", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()


def _ago(committed_at: int, now: float) -> str:
    """A commit age that changes when the answer does, not once a second.

    This row used to be ``git log -1 --pretty=%cr`` straight onto the page, and
    %cr is a live clock: "3 seconds ago", then "6 seconds ago". Every poll
    therefore changed the row, which changed the signature ``_advertise_project``
    compares, which re-sent the entire six-frame page -- every three seconds,
    forever, on a repository nobody had touched. Measured: 6 of 6 polls re-sent
    it. ``refresh`` promises that returning "changed" rather than "collected" is
    what keeps a quiet page off the wire, and against any repo with a recent
    commit that promise was never once kept.

    Freezing it was not the answer either: an age that never moved would be
    wrong within the minute. So the value is bucketed. It holds still for the
    whole first minute -- the window somebody is most likely to be watching --
    then changes at a boundary that means something, and the tilde says the
    number is coarse, exactly as the "quiet for" row already does.

    The alternative was to send the absolute timestamp and let the card render
    it, which is the better split and is a wire change. Not taken unilaterally
    while the client half is mid-pass; this needs no wire change at all.
    """
    delta = max(0, int(now - committed_at))
    if delta < 60:
        return "just now"
    if delta < 3600:
        return f"~{delta // 60}m ago"
    if delta < 86_400:
        return f"~{delta // 3600}h ago"
    return f"~{delta // 86_400}d ago"


def _clip(text: str, limit: int) -> str:
    """Printable ASCII, clipped. The encoder enforces both; doing it here too
    means the value the page shows is the value this module decided on, rather
    than whatever survived a truncation it could not see."""
    safe = "".join(c if 0x20 <= ord(c) <= 0x7E else "?" for c in text)
    return safe[:limit]


def collect(cwd: str, quiet_s: int | None = None) -> tuple[Row, ...]:
    """Every row of one snapshot, or () when there is nothing honest to say."""
    toplevel = _git(cwd, "rev-parse", "--show-toplevel")
    if not toplevel:
        return ()

    rows: list[Row] = []
    now = protocol.PROJECT_TAB_NOW
    diff = protocol.PROJECT_TAB_DIFF
    v_max = protocol.PROJECT_VALUE_MAX

    name = toplevel.rsplit("/", 1)[-1] or toplevel
    rows.append(Row(now, "repo", _clip(name, v_max), _ORANGE))

    # A detached HEAD reports the short sha instead, which is the honest answer
    # rather than an empty branch line.
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if branch == "HEAD":
        branch = _git(cwd, "rev-parse", "--short", "HEAD")
        if branch:
            branch = f"detached at {branch}"
    if branch:
        rows.append(Row(now, "branch", _clip(branch, v_max), _ORANGE))

    porcelain = _git(cwd, "status", "--porcelain")
    if porcelain is not None:
        lines = [ln for ln in porcelain.splitlines() if ln.strip()]
        if not lines:
            rows.append(Row(now, "state", "clean", _GREEN))
        else:
            untracked = sum(1 for ln in lines if ln.startswith("??"))
            tracked = len(lines) - untracked
            parts = []
            if tracked:
                parts.append(f"{tracked} changed")
            if untracked:
                parts.append(f"{untracked} new")
            rows.append(Row(now, "state", _clip(", ".join(parts), v_max), _RED))

    # Ahead/behind is only meaningful with an upstream, and plenty of branches
    # have none. No upstream is not a problem to report, so it is simply absent.
    counts = _git(cwd, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    if counts:
        try:
            behind_s, ahead_s = counts.split()
            behind, ahead = int(behind_s), int(ahead_s)
        except ValueError:
            behind = ahead = 0
        if ahead or behind:
            rows.append(Row(now, "upstream",
                            _clip(f"{ahead} ahead, {behind} behind", v_max),
                            _ORANGE))

    subject = _git(cwd, "log", "-1", "--pretty=%s")
    if subject:
        rows.append(Row(now, "last commit", _clip(subject, v_max)))
    # %ct, the absolute commit time, and bucketed here -- not %cr, which is a
    # clock that re-sent this whole page every three seconds. See _ago.
    when = _git(cwd, "log", "-1", "--pretty=%ct")
    if when:
        try:
            committed_at = int(when)
        except ValueError:
            committed_at = 0
        if committed_at:
            rows.append(Row(now, "committed",
                            _clip(_ago(committed_at, time.time()), v_max)))

    # A fact, deliberately, and not a verdict. How long the terminal has been
    # quiet is something this server measured; whether that means the agent is
    # stuck is a guess about someone else's state, and it does not make those.
    if quiet_s is not None:
        # Rounded to five seconds and marked approximate, because the number is
        # sampled when the collection starts: by the time the row is drawn it
        # can be a whole snapshot older than POLL_INTERVAL_S already made it,
        # and a bare "43s" invites a precision it does not have. Under five
        # seconds says so rather than rounding down to a "0s" that would read
        # as "producing output right now", which is the opposite of the truth.
        quiet_text = ("<5s" if quiet_s < 5
                      else f"~{int(round(quiet_s / 5.0)) * 5}s")
        rows.append(Row(now, "quiet for", _clip(quiet_text, v_max)))

    # DO: what this project can be told to do.  The value is the command
    # itself, deliberately -- that is what puts the exact text on the page
    # beside the button, so it can be read before it is pressed.  See
    # actions.py for why a script name that is not plainly a name is skipped
    # rather than quoted.
    for label, body in dsactions.rows(cwd):
        # Clipped like every other value here even though actions.py already
        # bounds it: the encoder raises on an over-long or non-ASCII value, and
        # that raise would come out inside the tick that queues the snapshot.
        # Nothing should be able to reach the wire without passing through the
        # same gate the rest of the page does.
        rows.append(Row(protocol.PROJECT_TAB_DO, _clip(label,
                                                       protocol.PROJECT_LABEL_MAX),
                        _clip(body, v_max), _ORANGE))

    # DIFF: what the working tree has that HEAD does not.
    numstat = _git(cwd, "diff", "--numstat", "HEAD")
    if numstat is not None:
        entries: list[tuple[str, str, str]] = []
        added = removed = 0
        for line in numstat.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            plus, minus, path = parts
            entries.append((plus, minus, path))
            if plus.isdigit():
                added += int(plus)
            if minus.isdigit():
                removed += int(minus)
        if not entries:
            rows.append(Row(diff, "working", "nothing uncommitted", _GREEN))
        else:
            rows.append(Row(diff, "working",
                            _clip(f"{len(entries)} files  +{added} -{removed}",
                                  v_max),
                            _ORANGE))
            for plus, minus, path in entries[:_DIFF_FILES_MAX]:
                # The path is the label so the numbers line up down the page,
                # and the tail of a long path is the informative half.
                short = path if len(path) <= protocol.PROJECT_LABEL_MAX else \
                    "~" + path[-(protocol.PROJECT_LABEL_MAX - 1):]
                rows.append(Row(diff, _clip(short, protocol.PROJECT_LABEL_MAX),
                                _clip(f"+{plus} -{minus}  {path}", v_max)))
            if len(entries) > _DIFF_FILES_MAX:
                rows.append(Row(diff, "and",
                                f"{len(entries) - _DIFF_FILES_MAX} more"))

    return tuple(rows[:protocol.PROJECT_ROWS_MAX])


def refresh(state: ProjectState, cwd: str, quiet_s: int | None = None,
            now: float | None = None) -> bool:
    """Re-collect if the slow clock says it is time. True if the rows changed.

    Returning "changed" rather than "collected" is what keeps the page off the
    wire when nothing has happened, which on a repository nobody is touching is
    almost always.
    """
    stamp = time.monotonic() if now is None else now
    if state.not_a_repo:
        return False
    if state.taken_at and stamp - state.taken_at < POLL_INTERVAL_S:
        return False
    state.taken_at = stamp
    rows = collect(cwd, quiet_s)
    if not rows and not state.rows and not state.taken_at:
        state.not_a_repo = True
    changed = rows != state.rows
    state.rows = rows
    return changed


class BackgroundCollector:
    """``collect`` on a worker thread, so the frame loop never waits for git.

    ``refresh`` above is the synchronous answer and remains correct for anything
    that can afford to block. The server's tick loop cannot. Measured against
    the working directory this server actually runs in -- a Windows path reached
    over WSL's drvfs -- one snapshot costs 1.59 s, of which ``status
    --porcelain`` is 0.70 s and ``diff --numstat`` 0.56 s, on a repository of
    only 269 tracked files. Polled inline every ``POLL_INTERVAL_S`` that is over
    half of wall-clock time with no frames reaching the handheld, and eight
    queries each hitting ``_GIT_TIMEOUT`` would be a 24 s freeze. On a developer
    checkout on ext4 the same snapshot is an order of magnitude cheaper, so the
    stall is invisible exactly where the code gets written.

    ``poll`` is the whole interface and it never blocks: it hands back a
    finished snapshot if the worker has one, starts a new one when the slow
    clock says so, and returns immediately either way.

    Two deliberate differences from ``refresh``:

    * **No ``not_a_repo`` latch.** A directory that is not a worktree costs one
      ``rev-parse`` per interval, which is ~16 ms and now off the loop entirely.
      Latching would also mean a git that was briefly missing silences the page
      until the server restarts, and this way it heals by itself.
    * **``quiet_s`` is sampled when the collection starts,** not when it lands,
      so that row can be one collection older than the rest. The value was
      already up to ``POLL_INTERVAL_S`` stale by design; this widens that by the
      length of one snapshot.
    """

    def __init__(self, cwd: str, interval: float = POLL_INTERVAL_S) -> None:
        self._cwd = cwd
        self._interval = interval
        self._lock = threading.Lock()
        self._rows: tuple[Row, ...] = ()
        self._result: tuple[Row, ...] | None = None
        self._worker: threading.Thread | None = None
        self._started_at: float | None = None
        self._stopped = False

    @property
    def rows(self) -> tuple[Row, ...]:
        """The last complete snapshot. Never a half-gathered one."""
        with self._lock:
            return self._rows

    def poll(self, quiet_s: int | None = None,
             now: float | None = None) -> bool:
        """Publish a finished snapshot and start the next. True if rows changed.

        Safe to call every tick; the interval is enforced here, not by the
        caller. Returning "changed" rather than "collected" is what keeps a
        quiet repository off the wire, exactly as ``refresh`` does.
        """
        stamp = time.monotonic() if now is None else now
        changed = False
        worker: threading.Thread | None = None

        with self._lock:
            if self._stopped:
                return False

            # Harvest first: a snapshot that finished during the last tick is
            # already paid for, and publishing it frees the slot for the next.
            if self._result is not None:
                rows, self._result = self._result, None
                self._worker = None
                if rows != self._rows:
                    self._rows = rows
                    changed = True

            due = (self._started_at is None
                   or stamp - self._started_at >= self._interval)
            if self._worker is None and due:
                self._started_at = stamp
                worker = threading.Thread(
                    target=self._run, args=(quiet_s,),
                    name="dsterm-project", daemon=True)
                self._worker = worker

        # Started outside the lock: the worker's first act is to want it.
        if worker is not None:
            worker.start()
        return changed

    def stop(self, timeout: float = 2.0) -> None:
        """Stop polling and wait briefly for a running collection. Idempotent.

        The worker is a daemon and every git call is bounded by ``_GIT_TIMEOUT``,
        so a wedged one cannot hold the process open even if this times out.
        """
        with self._lock:
            self._stopped = True
            worker = self._worker
            self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout)

    def _run(self, quiet_s: int | None) -> None:
        """One collection, on the worker thread. Failure is silence."""
        try:
            # Resolved here rather than captured in __init__ so that patching
            # the module function is honoured -- and so a future caller can.
            rows: tuple[Row, ...] | None = collect(self._cwd, quiet_s)
        except Exception:
            # The page keeps its last honest answer, and the next tick tries
            # again. A collector that dies on one bad git is a page that goes
            # silent until the server restarts.
            rows = None
        with self._lock:
            if rows is None:
                self._worker = None
            else:
                self._result = rows


def encode(rows: tuple[Row, ...]) -> list[bytes]:
    """The snapshot as wire frames, one row each.

    An empty snapshot is one explicit empty message rather than no messages,
    because the card distinguishes "nothing to report" from "never told".
    """
    if not rows:
        return [protocol.encode_project_empty()]
    count = len(rows)
    return [
        protocol.encode_project_row(i, count, r.tab, r.fg, r.label, r.value)
        for i, r in enumerate(rows)
    ]
