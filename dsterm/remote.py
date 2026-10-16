"""The other Claude Code sessions on this machine, for the card's REMOTE screen.

docs/REMOTE.md is the contract. This module is the server's half of it, minus
the wire plumbing that lives in server.py: what ``claude agents --json`` says,
what each row means on the handheld, which rows can be opened and how, and the
worker thread that asks.

Three rules shape everything here.

* **Nothing is taken from the wire.** A pick is a row index and an action
  number. The id handed to ``claude attach``, the session id handed to
  ``--resume``, the working directory and the command all come from this
  machine's own ``claude agents`` answer or this server's own configuration,
  and the ids are re-validated against conservative patterns before they reach
  an argv. Everything is passed as argv elements, never through a shell.

* **The select loop never waits for ``claude``.** One ``claude agents`` costs
  hundreds of milliseconds and is allowed eight seconds; inline, that is the
  handheld's screen frozen. It runs on a worker thread, only while some card
  has the screen open, and the tick harvests the result the same way it
  harvests the PROJECT page (dsterm/project.py's BackgroundCollector).

* **A detection that fails means a flag that is absent.** Whether a session is
  already open in one of this server's tmux windows (HERE) is worked out from
  tmux's pane list and the process tree. Either can fail, and failing must cost
  the HERE badge, never the list.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol
from .protocol import (
    REMOTE_COPY,
    REMOTE_DETAIL_MAX,
    REMOTE_HERE,
    REMOTE_KIND_BACKGROUND,
    REMOTE_KIND_INTERACTIVE,
    REMOTE_LIVE,
    REMOTE_NAME_MAX,
    REMOTE_NOTE_MAX,
    REMOTE_ROWS_MAX,
    REMOTE_STATE_DONE,
    REMOTE_STATE_FAILED,
    REMOTE_STATE_IDLE,
    REMOTE_STATE_NEEDS_YOU,
    REMOTE_STATE_STOPPED,
    REMOTE_STATE_UNKNOWN,
    REMOTE_STATE_WORKING,
    REMOTE_STATUS_FAILED,
    REMOTE_STATUS_NO_CLAUDE,
    REMOTE_STATUS_NONE,
    REMOTE_STATUS_NOT_ASKED,
    STATE_DETAIL_MAX,
    STATE_IDLE,
    STATE_NEEDS_APPROVAL,
    STATE_NEEDS_INPUT,
    STATE_UNKNOWN,
    STATE_WORKING,
)

# How often ``claude agents`` is asked while a card has the screen open. Agents
# change state on human timescales -- a prompt appears, a task finishes -- and
# each ask starts the whole CLI, so a few seconds is the honest cadence.
POLL_SECONDS = 3.0

# STATE's cadence (docs/REMOTE.md). Fast while something is happening -- the
# session on screen is working or waiting, or the card typed recently, which
# is when "it finished" or "it needs you" is about to be the next edge -- and
# slow otherwise, because an idle session's next edge is a person typing.
STATE_FAST_SECONDS = 1.5
STATE_SLOW_SECONDS = 5.0
STATE_TYPED_WINDOW = 30.0

# One ask may take this long before it counts as failed. Generous because a
# cold ``claude`` start on a loaded laptop is slow, and it is off the loop.
QUERY_TIMEOUT = 8.0

# tmux and ps are local and quick; these only bound a wedged one.
_TMUX_TIMEOUT = 2.0
_PS_TIMEOUT = 2.0

# An answer with more entries than this is not a list a person is choosing
# from. Bounding it bounds the work a strange ``claude`` can cause here.
_MAX_ITEMS = 256

# How far up the process tree a session's pid is walked looking for one of
# this server's panes. Real chains are a handful deep (pane shell -> claude, or
# pane -> node -> claude); the bound only stops a cycle in a racing ps snapshot.
_MAX_ANCESTRY = 64

# Ids that reach an argv. ``claude attach`` takes a short id; the pattern is
# deliberately narrower than "whatever claude printed" and must not start with
# a dash, so no value can ever be read as an option by the program it is
# handed to.
_AGENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SESSION_ID = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_WINDOW_ID = re.compile(r"^@[0-9]+$")

# What ``claude agents --json`` documents. Anything else is ignored rather than
# guessed at: an unknown state reads as UNKNOWN, an unknown waitingFor is left
# off the detail line.
_KINDS = frozenset({"background", "interactive"})
_BACKGROUND_STATE = {
    "working": REMOTE_STATE_WORKING,
    "blocked": REMOTE_STATE_NEEDS_YOU,
    "done": REMOTE_STATE_DONE,
    "failed": REMOTE_STATE_FAILED,
    "stopped": REMOTE_STATE_STOPPED,
}
_INTERACTIVE_STATUS = {
    "busy": REMOTE_STATE_WORKING,
    "waiting": REMOTE_STATE_NEEDS_YOU,
    "idle": REMOTE_STATE_IDLE,
}
_WAITING_FOR = frozenset({
    "permission prompt", "input needed", "sandbox request", "worker request",
    "dialog open",
})

UNTITLED = "untitled"


# -- what claude says -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Agent:
    """One entry of ``claude agents --json``, with every field optional.

    Optional because the documented shape is conditional -- ``id`` and
    ``state`` only for background sessions, ``pid`` and ``status`` only while
    the process is alive -- and because this is another program's output: a
    field that is missing or the wrong type is absent here, never a crash.
    """

    kind: str
    cwd: str = ""
    started_at_ms: int | None = None
    agent_id: str | None = None
    state: str | None = None
    pid: int | None = None
    status: str | None = None
    waiting_for: str | None = None
    session_id: str | None = None
    name: str | None = None
    # ``waitingFor`` exactly as printed, known value or not. REMOTE's detail
    # line shows only the documented values; STATE shows "what Claude Code
    # reports", and maps an unknown one to "needs input" per the contract.
    waiting_raw: str | None = None


def _int(value) -> int | None:
    # bool is an int in Python, and True is not a pid.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:          # NaN, negatives
        return None
    try:
        return int(value)
    except (OverflowError, ValueError):
        return None


def _str(value) -> str | None:
    return value if isinstance(value, str) else None


def parse_agents(text: str) -> tuple[Agent, ...] | None:
    """Parse ``claude agents --json``. None means the answer was not a list.

    Entries that are not objects, or whose ``kind`` is not one of the two
    documented, are skipped: a row that cannot say whether it is a background
    or an interactive session cannot say what opening it would do.
    """
    try:
        data = json.loads(text)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, list):
        return None
    out: list[Agent] = []
    for item in data[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in _KINDS:
            continue
        agent_id = _str(item.get("id"))
        session_id = _str(item.get("sessionId"))
        state = _str(item.get("state"))
        status = _str(item.get("status"))
        waiting = _str(item.get("waitingFor"))
        pid = _int(item.get("pid"))
        out.append(Agent(
            kind=kind,
            cwd=_str(item.get("cwd")) or "",
            started_at_ms=_int(item.get("startedAt")),
            agent_id=agent_id if agent_id and _AGENT_ID.fullmatch(agent_id) else None,
            state=state if state in _BACKGROUND_STATE else None,
            pid=pid if pid else None,
            status=status if status in _INTERACTIVE_STATUS else None,
            waiting_for=waiting if waiting in _WAITING_FOR else None,
            session_id=(session_id if session_id and _SESSION_ID.fullmatch(session_id)
                        else None),
            name=_str(item.get("name")),
            waiting_raw=waiting,
        ))
    return tuple(out)


# -- what the card is shown -------------------------------------------------------


def clamp(text: str, limit: int) -> str:
    """Printable ASCII, at most ``limit`` characters; anything else becomes '?'.

    Replaced rather than dropped (sessions.clamp_name drops) because a name
    here is the user's own label for a conversation, and "caf?" is still
    recognisably theirs where "caf" might be someone else's.
    """
    return "".join(ch if 0x20 <= ord(ch) <= 0x7E else "?" for ch in text[:limit])


def note_text(text: str) -> str:
    """A REMOTE_NOTE line: clamped, and never empty."""
    return clamp(text, REMOTE_NOTE_MAX) or "?"


def tmux_window_name(name: str) -> str:
    """A name safe to hand tmux's ``new-window -n``.

    Narrower than the wire's printable ASCII on purpose: tmux expands
    ``#{...}`` formats in that argument (measured: ``-n '#{pane_pid}'`` names
    the window after a pid), and a session name is text another program chose.
    """
    return re.sub(r"[^A-Za-z0-9._ -]", "-", name)[:REMOTE_NAME_MAX] or "claude"


def _state(agent: Agent) -> int:
    # Each kind has its own field; the other is a fallback, because the
    # documented combinations ("state without status", "status without state")
    # are exactly the cases where only one is there.
    if agent.kind == "background":
        first, second = (_BACKGROUND_STATE.get(agent.state or ""),
                         _INTERACTIVE_STATUS.get(agent.status or ""))
    else:
        first, second = (_INTERACTIVE_STATUS.get(agent.status or ""),
                         _BACKGROUND_STATE.get(agent.state or ""))
    if first is not None:
        return first
    if second is not None:
        return second
    return REMOTE_STATE_UNKNOWN


def _detail(agent: Agent) -> str:
    base = os.path.basename(agent.cwd.rstrip("/")) or ("/" if agent.cwd else "")
    if agent.status == "waiting" and agent.waiting_for:
        base = f"{base} - {agent.waiting_for}" if base else agent.waiting_for
    return clamp(base, REMOTE_DETAIL_MAX)


@dataclass(frozen=True, slots=True)
class Row:
    """One advertised row: what the card draws, and what a pick of it means.

    The identity fields never cross the wire. A pick names a row by position,
    and the server reads the id, session id and directory back from here.
    """

    kind: int
    state: int
    flags: int
    age_min: int
    name: str
    detail: str
    agent_id: str | None = None
    session_id: str | None = None
    cwd: str = ""
    here_window: str | None = None
    # The tmux lifetime ``here_window`` belongs to (a sessions.TmuxEpoch).
    # Window ids are reused by a new tmux server, so selecting one is only
    # safe against the lifetime it was observed in.
    here_epoch: object = None
    started_at_ms: int | None = None

    @property
    def signature(self) -> tuple:
        # Age is deliberately absent, as it is from SESSIONS' signature: it
        # ticks every minute and that is a refresh, not a new fact. Identity is
        # present, because a pick maps through it -- two rows that look alike
        # but attach to different sessions are different sets.
        return (self.kind, self.state, self.flags, self.name, self.detail,
                self.agent_id, self.session_id, self.cwd, self.here_window,
                self.here_epoch)

    @property
    def check(self) -> int:
        """What a REMOTE_PICK of this row must carry (docs/REMOTE.md).

        Over ``name`` and ``detail`` as they are, because these ARE the bytes
        make_set encodes: clamped and replaced before the Row exists, so what
        the card hashes and what this hashes cannot differ by a sanitising
        step one side forgot.
        """
        return protocol.remote_row_check(self.name, self.detail)


def build_rows(agents: tuple[Agent, ...], *, now_ms: int,
               here_by_pid: dict[int, str] | None = None,
               here_by_id: dict[str, str] | None = None,
               here_epoch: object = None,
               can_open: bool = True) -> tuple[Row, ...]:
    """Turn ``claude agents`` into at most eight sorted, clamped rows.

    ``here_by_pid`` maps a session's pid to the dsterm window it runs in;
    ``here_by_id`` maps a background id to the window this server attached it
    in. ``can_open`` is False when the server's child is not in tmux: then
    there is nowhere to open anything, and no row claims otherwise.
    """
    here_by_pid = here_by_pid or {}
    here_by_id = here_by_id or {}
    rows: list[Row] = []
    for a in agents:
        state = _state(a)
        here = None
        if a.pid is not None:
            here = here_by_pid.get(a.pid)
        if here is None and a.agent_id is not None:
            here = here_by_id.get(a.agent_id)
        flags = 0
        if can_open:
            if here is not None:
                flags |= REMOTE_HERE
            if a.kind == "background":
                # Stopped is attachable too: per `claude attach`, the id also
                # restarts a stopped session. Failed is the one that is not.
                if a.agent_id is not None and state != REMOTE_STATE_FAILED:
                    flags |= REMOTE_LIVE
            elif a.session_id is not None and here is None:
                # Never a plain --resume of a session another terminal is
                # writing to: two writers interleave into one transcript. A
                # session already in one of our windows needs no copy at all.
                flags |= REMOTE_COPY
        age = 0
        if a.started_at_ms is not None:
            age = max(0, min(0xFFFF, (now_ms - a.started_at_ms) // 60000))
        rows.append(Row(
            kind=(REMOTE_KIND_BACKGROUND if a.kind == "background"
                  else REMOTE_KIND_INTERACTIVE),
            state=state,
            flags=flags,
            age_min=age,
            name=clamp(a.name or UNTITLED, REMOTE_NAME_MAX) or UNTITLED,
            detail=_detail(a),
            agent_id=a.agent_id,
            session_id=a.session_id,
            cwd=a.cwd,
            here_window=here if can_open else None,
            here_epoch=here_epoch if can_open and here is not None else None,
            started_at_ms=a.started_at_ms,
        ))

    def order(r: Row):
        urgency = (0 if r.state == REMOTE_STATE_NEEDS_YOU else
                   1 if r.state == REMOTE_STATE_WORKING else 2)
        started = r.started_at_ms if r.started_at_ms is not None else -1
        return (urgency, -started)

    rows.sort(key=order)
    return tuple(rows[:REMOTE_ROWS_MAX])


@dataclass(frozen=True, slots=True)
class RemoteSet:
    """One complete answer for the card: its frames, rows and change-identity."""

    status: int
    rows: tuple[Row, ...]
    messages: tuple[bytes, ...]
    signature: tuple
    ages: tuple[int, ...]


def make_set(status: int, rows: tuple[Row, ...] = ()) -> RemoteSet:
    """Frame a set. Rows win over status: a list with rows is status 1."""
    if rows:
        status = REMOTE_STATUS_NONE
        count = len(rows)
        messages = tuple(
            protocol.encode_remote_row(i, count, r.kind, r.state, r.flags,
                                       r.age_min, r.name, r.detail)
            for i, r in enumerate(rows))
    else:
        messages = (protocol.encode_remote_empty(status),)
    return RemoteSet(status, rows, messages,
                     (status, tuple(r.signature for r in rows)),
                     tuple(r.age_min for r in rows))


NOT_ASKED = make_set(REMOTE_STATUS_NOT_ASKED)


def still_listed(row: Row, agents: tuple[Agent, ...]) -> bool:
    """Whether the session behind ``row`` is in a later answer."""
    for a in agents:
        if row.agent_id is not None and a.agent_id == row.agent_id:
            return True
        if (row.agent_id is None and row.session_id is not None
                and a.session_id == row.session_id):
            return True
    return False


# -- the session on screen ---------------------------------------------------------

# STATE's waitingFor values that are a yes/no for the user; any other wait is
# a question. From the contract's table, not inferred.
_APPROVALS = frozenset({"permission prompt", "sandbox request"})

# Which of several sessions in the one selected window speaks for it: the one
# that needs the person most. Two Claude sessions in one window is unusual (a
# split pane), and "one of them wants an approval" must not be hidden by "the
# other one is idle".
_STATE_URGENCY = {STATE_NEEDS_APPROVAL: 4, STATE_NEEDS_INPUT: 3, STATE_WORKING: 2,
                  STATE_IDLE: 1, STATE_UNKNOWN: 0}


def agent_state(agent: Agent) -> tuple[int, str]:
    """(STATE byte, detail) for one session, as docs/REMOTE.md maps them."""
    if agent.status == "busy":
        return STATE_WORKING, ""
    if agent.status == "idle":
        return STATE_IDLE, ""
    if agent.status == "waiting":
        detail = clamp(agent.waiting_raw or "", STATE_DETAIL_MAX)
        if agent.waiting_raw in _APPROVALS:
            return STATE_NEEDS_APPROVAL, detail
        return STATE_NEEDS_INPUT, detail
    # No live status: only a background session attached in a window gets
    # here, and its own state is the next best answer. "blocked" says it is
    # waiting on the user but not for what, which the contract files under
    # "anything else".
    if agent.status is None and agent.state is not None:
        if agent.state == "working":
            return STATE_WORKING, ""
        if agent.state == "blocked":
            return STATE_NEEDS_INPUT, ""
        return STATE_IDLE, ""
    return STATE_UNKNOWN, ""


def state_reading(agents: tuple[Agent, ...], here_by_pid: dict[int, str],
                  active_window: str | None,
                  attached: dict[str, str] | None = None) -> tuple[int, str]:
    """What the session in ``active_window`` is doing, as (state, detail).

    The session on screen is the one whose process descends from a pane of
    the selected window -- the HERE detection, asked about one window. A
    background session this server attached in that window is the fallback
    (``attached`` maps window id -> background id): the attach client is in
    the pane, but the session's own pid is the background worker's.
    """
    if active_window is None:
        return STATE_UNKNOWN, ""
    on_screen = [a for a in agents
                 if a.pid is not None and here_by_pid.get(a.pid) == active_window]
    if not on_screen and attached:
        wanted = attached.get(active_window)
        on_screen = [a for a in agents if wanted and a.agent_id == wanted]
    if not on_screen:
        return STATE_UNKNOWN, ""
    best = max(on_screen, key=lambda a: (_STATE_URGENCY[agent_state(a)[0]],
                                         a.started_at_ms or 0))
    return agent_state(best)


def poll_interval(*, remote_open: bool, state_clients: bool, reading: int,
                  typed_ago: float | None, failing: bool = False) -> float | None:
    """Seconds between asks, or None when nobody is owed an answer.

    The faster of what REMOTE and STATE each need: one ``claude agents``
    reading serves both, so a card with both never pays for two.

    ``failing`` -- every ask for a while has failed -- puts STATE on the slow
    cadence whatever the reading or the typing says: starting a broken CLI
    every 1.5 s finds nothing new faster, it only costs more. An open REMOTE
    screen keeps its own cadence, because someone is looking at a screen
    that says "failed" and is owed the moment it stops being true.
    """
    intervals = []
    if remote_open:
        intervals.append(POLL_SECONDS)
    if state_clients:
        active = reading in (STATE_WORKING, STATE_NEEDS_APPROVAL, STATE_NEEDS_INPUT)
        typed = typed_ago is not None and typed_ago < STATE_TYPED_WINDOW
        fast = (active or typed) and not failing
        intervals.append(STATE_FAST_SECONDS if fast else STATE_SLOW_SECONDS)
    return min(intervals) if intervals else None


# -- asking -------------------------------------------------------------------------


Runner = Callable[[list[str], float], "subprocess.CompletedProcess | None"]


def run(argv: list[str], timeout: float) -> subprocess.CompletedProcess | None:
    """Run argv without a shell; None for anything that is not an answer."""
    try:
        return subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def find_claude() -> str | None:
    """The ``claude`` this server would run, by PATH, as the card is told."""
    return shutil.which("claude")


@dataclass(frozen=True, slots=True)
class Observation:
    """One finished ask, handed from the worker to the select loop whole."""

    status: int
    agents: tuple[Agent, ...] = ()
    claude: str | None = None
    # Window ids that exist in the dsterm session, or None when tmux could not
    # be asked -- "unknown", which must not be read as "every window closed".
    windows: frozenset[str] | None = None
    here_by_pid: dict[int, str] = field(default_factory=dict)
    # The pid of the tmux server that answered: the window ids above mean
    # something only in that server's lifetime.
    tmux_pid: int | None = None
    started_at: float = 0.0


def pane_windows(socket: str, session: str, runner: Runner = run
                 ) -> tuple[int | None, dict[int, str]] | None:
    """(tmux server pid, {pane pid: window id}) for the dsterm session, or None.

    One subprocess for both, so the pane list and the lifetime it belongs to
    cannot come from two different tmux servers.
    """
    out = runner(["tmux", "-L", socket, "list-panes", "-s", "-t", session,
                  "-F", "#{pid} #{pane_pid} #{window_id}"], _TMUX_TIMEOUT)
    if out is None or out.returncode != 0:
        return None
    server_pid: int | None = None
    panes: dict[int, str] = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 3 or not _WINDOW_ID.fullmatch(parts[2]):
            continue
        try:
            pid, pane_pid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if server_pid is None:
            server_pid = pid
        elif pid != server_pid:
            return None
        panes[pane_pid] = parts[2]
    return server_pid, panes


def process_parents(runner: Runner = run) -> dict[int, int] | None:
    """{pid: ppid} for every process, from one ``ps``, or None."""
    out = runner(["ps", "-A", "-o", "pid=,ppid="], _PS_TIMEOUT)
    if out is None or out.returncode != 0:
        return None
    parents: dict[int, int] = {}
    for line in (out.stdout or "").splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            parents[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return parents


def here_by_ancestry(pids, panes: dict[int, str],
                     parents: dict[int, int]) -> dict[int, str]:
    """{session pid: window id} for each pid that descends from a dsterm pane.

    A pane's pid is the process tmux started in it -- the shell, or the
    command itself -- so a session counts as "in that window" when walking
    its parents reaches one.
    """
    found: dict[int, str] = {}
    for pid in pids:
        cursor, seen = pid, 0
        while cursor > 1 and seen < _MAX_ANCESTRY:
            window = panes.get(cursor)
            if window is not None:
                found[pid] = window
                break
            cursor = parents.get(cursor, 0)
            seen += 1
    return found


def observe(target: tuple[str, str] | None, *, runner: Runner = run,
            which: Callable[[], str | None] = find_claude,
            now: float | None = None) -> Observation:
    """One complete ask. Runs on the worker thread; never raises on bad input."""
    started = time.monotonic() if now is None else now
    claude = which()
    if not claude:
        return Observation(REMOTE_STATUS_NO_CLAUDE, started_at=started)
    out = runner([claude, "agents", "--json"], QUERY_TIMEOUT)
    if out is None or out.returncode != 0:
        return Observation(REMOTE_STATUS_FAILED, claude=claude, started_at=started)
    agents = parse_agents(out.stdout or "")
    if agents is None:
        return Observation(REMOTE_STATUS_FAILED, claude=claude, started_at=started)

    windows: frozenset[str] | None = None
    here: dict[int, str] = {}
    tmux_pid: int | None = None
    if target is not None:
        listed = pane_windows(*target, runner=runner)
        if listed is not None:
            tmux_pid, panes = listed
            windows = frozenset(panes.values())
            pids = [a.pid for a in agents if a.pid is not None]
            if panes and pids:
                parents = process_parents(runner)
                if parents is not None:
                    here = here_by_ancestry(pids, panes, parents)
    return Observation(REMOTE_STATUS_NONE, agents, claude, windows, here,
                       tmux_pid, started)


class RemotePoller:
    """``observe`` on a worker thread, harvested by the tick.

    The same shape as dsterm/project.py's BackgroundCollector, and for the same
    reason: a lock held only around handing a finished result over, a daemon
    worker bounded by its own timeouts, and a ``poll`` that never blocks. The
    difference is that this one only asks while it is wanted -- a card with the
    REMOTE screen open -- because each ask starts the whole CLI, and nobody
    looking means nobody to tell.
    """

    def __init__(self, interval: float = POLL_SECONDS, *,
                 runner: Runner | None = None,
                 which: Callable[[], str | None] | None = None) -> None:
        self._interval = interval
        self._runner = runner
        self._which = which
        self._lock = threading.Lock()
        self._result: Observation | None = None
        self._latest: Observation | None = None
        self._worker: threading.Thread | None = None
        self._started_at: float | None = None
        self._again = False
        self._stopped = False
        # How many asks have been started. For tests and the dashboard: the
        # property worth pinning is that this stays still while nobody looks.
        self.asks = 0

    @property
    def latest(self) -> Observation | None:
        """The last harvested answer, or None if there has never been one."""
        with self._lock:
            return self._latest

    def forget(self) -> None:
        """Drop the last answer, so nothing reads it as current.

        For when nobody has been asking for a while: a reading taken before
        the last card left says nothing about now.
        """
        with self._lock:
            self._latest = None

    def refresh(self) -> None:
        """Ask again at the next opportunity rather than at the next interval.

        If an ask is already in flight, the next one starts as soon as it is
        harvested -- its answer may predate whatever made this call necessary.
        """
        with self._lock:
            self._again = True

    def poll(self, wanted: bool, target: tuple[str, str] | None = None,
             now: float | None = None,
             interval: float | None = None,
             max_age: float | None = None) -> Observation | None:
        """Hand back a newly finished answer (once), and start the next if due.

        ``wanted`` gates only the start. A result that lands after the last
        card closed the screen is still harvested, so it is never left to be
        mistaken for a fresh one later. ``interval`` overrides the default for
        this call: the cadence is the caller's to choose (poll_interval), and
        it is applied at every call, so speeding up takes effect at once.

        ``max_age`` bounds how old a harvested answer may be, measured from
        when its ask STARTED. Older ones are thrown away unseen -- neither
        returned nor kept as ``latest``. "Harvested at the next poll" is only
        a promise while somebody polls: the server stops ticking this when
        its last card leaves, so an ask in flight at that moment lands in
        ``_result`` and sits there for however long the handheld is off. The
        next card's first tick would otherwise harvest it and publish an hour-
        old "working" as the present -- the stale edge STATE_STALE_SECONDS
        exists to prevent, arriving by the one road that rule did not watch.
        """
        stamp = time.monotonic() if now is None else now
        every = self._interval if interval is None else interval
        harvested: Observation | None = None
        worker: threading.Thread | None = None
        with self._lock:
            if self._stopped:
                return None
            if self._result is not None:
                harvested, self._result = self._result, None
                self._worker = None
                if max_age is not None and stamp - harvested.started_at > max_age:
                    harvested = None
                else:
                    self._latest = harvested
            due = (self._again or self._started_at is None
                   or stamp - self._started_at >= every)
            if wanted and self._worker is None and due:
                self._again = False
                self._started_at = stamp
                self.asks += 1
                worker = threading.Thread(target=self._run, args=(target,),
                                          name="dsterm-remote", daemon=True)
                self._worker = worker
        if worker is not None:
            worker.start()
        return harvested

    def stop(self, timeout: float = 2.0) -> None:
        """Stop asking and wait briefly for an ask in flight. Idempotent."""
        with self._lock:
            self._stopped = True
            worker = self._worker
            self._worker = None
        if worker is not None and worker.is_alive():
            worker.join(timeout)

    def _run(self, target: tuple[str, str] | None) -> None:
        kwargs = {}
        if self._runner is not None:
            kwargs["runner"] = self._runner
        if self._which is not None:
            kwargs["which"] = self._which
        try:
            result = observe(target, **kwargs)
        except Exception:  # noqa: BLE001 -- the screen says "failed", the loop lives
            result = Observation(REMOTE_STATUS_FAILED, started_at=time.monotonic())
        with self._lock:
            if self._stopped:
                self._worker = None
            else:
                self._result = result


# -- opening ----------------------------------------------------------------------


def tmux_word(word: str) -> str:
    """``word`` as one tmux command-line argument that means only itself.

    tmux re-parses its own argv for command lists: an argument ENDING in ';'
    ends the command there (measured, tmux 3.7b: ``-c 'dir;'`` opened a window
    running the default shell in the wrong directory, then failed on ``--`` as
    an unknown command). ``\\;`` at the end is tmux's escape for a literal
    one, and it composes -- ``a\\;`` goes as ``a\\\\;`` and arrives intact.
    """
    return word[:-1] + "\\;" if word.endswith(";") else word


def tmux_directory(path: str) -> str:
    """``path`` as ``new-window -c`` takes it: formats escaped, then a word.

    tmux expands formats in the start directory (measured: ``-c '/x#Sy'``
    became "/x<session name>y", which does not exist, so the window quietly
    opened in $HOME). A directory comes from ``claude agents`` -- another
    program's output -- and a '#' in it is just a character, so ``##``.
    """
    return tmux_word(path.replace("#", "##"))


def exec_argv(argv: list[str]) -> list[str]:
    """``argv`` in a shape tmux will exec as it stands.

    tmux runs a command of TWO or more words with execvp, but hands a single
    word to ``$SHELL -c`` -- which re-parses it: measured, a one-word command
    whose path has a space in it never ran. A one-word command is the common
    case here (``claude``, or a child command of one word), so it goes through
    ``/bin/sh -c 'exec "$0"' WORD``: the script is a constant, and the word
    reaches it as ``$0``, a parameter that is expanded but never re-parsed.
    Not the user's $SHELL, which may not be a POSIX shell at all. Two or more
    words are left exactly as they are.
    """
    if len(argv) == 1:
        return ["/bin/sh", "-c", 'exec "$0"', argv[0]]
    return list(argv)


def new_window(socket: str, session: str, name: str, cwd: str,
               argv: list[str], runner: Runner = run) -> str | None:
    """Open a detached window in the dsterm session; return its window id.

    Detached (-d) so that selecting it is done by the same path SESSION_PICK
    uses, which is what gives the card its switch barrier and repaint. The
    command goes after ``--`` as separate argv elements, which tmux executes
    directly rather than through ``sh -c`` -- nothing in it is ever parsed by a
    shell (exec_argv makes that true for a one-word command too). Every word
    tmux itself would re-read is escaped for it: the directory's formats, and a
    trailing ';' anywhere. ``session:`` (with the colon) means "the next free
    index of that session", never a window that happens to share the
    session's name.
    """
    if not argv:
        return None
    out = runner(["tmux", "-L", socket, "new-window", "-d", "-P",
                  "-F", "#{window_id}", "-t", f"{session}:",
                  "-n", tmux_window_name(name), "-c", tmux_directory(cwd), "--",
                  *(tmux_word(w) for w in exec_argv(argv))],
                 _TMUX_TIMEOUT)
    if out is None or out.returncode != 0:
        return None
    window_id = (out.stdout or "").strip()
    return window_id if _WINDOW_ID.fullmatch(window_id) else None


def valid_agent_id(value: str | None) -> bool:
    return bool(value) and _AGENT_ID.fullmatch(value) is not None


def valid_session_id(value: str | None) -> bool:
    return bool(value) and _SESSION_ID.fullmatch(value) is not None
