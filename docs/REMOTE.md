# REMOTE: other Claude sessions, and other computers

The Control screen's REMOTE tile opens one list with two halves:

* **On this computer** -- every Claude Code session running on the machine the
  card is connected to, as `claude agents --json` reports them: background
  agents, terminal sessions, sessions started elsewhere.  Each row says what
  the session is doing (WORKING, NEEDS YOU, DONE, IDLE ...).  Picking one
  brings it onto the handheld in a new window of the dsterm tmux session.
* **Other computers** -- dsterm servers on the same network, found the same
  way the boot menu finds them, plus the ones this card has saved.  Picking
  one moves the card there (pairing first if it has never met that server).

The second half is entirely the card's: discovery is UDP on the LAN and the
switch is an ordinary reconnect.  This document is mostly about the first
half, which is a small wire extension.

## What "open" means

The rules come from Claude Code's own documentation for `claude agents`,
`claude attach` and `claude --resume`:

| Row                                   | A on it does                                     |
|---------------------------------------|--------------------------------------------------|
| background session (it has an `id`)   | `claude attach <id>` in a new tmux window: live  |
| interactive session open elsewhere    | asks first, then `claude --resume <sessionId> --fork-session`: **a copy** |
| a session already open in a dsterm window | selects that window                          |
| `+ New Claude session`                | a new window running the server's own command   |

An interactive session that is running in another terminal is never resumed
plainly: two writers on one session interleave into one transcript.  The copy
is labelled as a copy.  To bring the *original* over, background it where it
runs (press Left on an empty prompt, or `/bg`) and it becomes a live row.

Detaching from an attached background session never stops it.

## Wire

Gated by the HELLO token `remote`.  As everywhere else in this protocol a new
client-to-server type would make an old server drop the connection, so the
card sends nothing below until it has *received* a REMOTE frame, which is the
server's proof that it understands them.

### Server to client

`MSG_REMOTE = 0x9F`, one row per frame, committed atomically when the last
row of a set arrives (the MSG_SESSIONS discipline).

Empty or status-only set, 2 bytes:

| offset | size | field |
|--------|------|-------|
| 0      | 1    | count = 0 |
| 1      | 1    | status: 0 not asked yet, 1 asked and there are none, 2 `claude` is not installed on the server, 3 the query failed |

A row, `count` 1..8:

| offset | size | field |
|--------|------|-------|
| 0      | 1    | count |
| 1      | 1    | index, 0..count-1 |
| 2      | 1    | kind: 0 background, 1 interactive |
| 3      | 1    | state: 0 unknown, 1 working, 2 needs you, 3 done, 4 idle, 5 failed, 6 stopped |
| 4      | 1    | flags: 0x01 HERE (already open in a dsterm window), 0x02 LIVE (can be attached), 0x04 COPY (can be opened as a copy) |
| 5      | 2    | age in minutes since it started, big-endian, capped at 65535 |
| 7      | 1    | name length, 0..24 |
| 8      | n    | name, printable ASCII |
| 8+n    | 1    | detail length, 0..28 |
| 9+n    | m    | detail, printable ASCII: the working directory's last component, and what it is waiting for when it is waiting |

Rows are sorted: NEEDS YOU first, then WORKING, then everything else by most
recent.  Anything outside printable ASCII is replaced by `?` before it is
sent, and the card clamps and replaces again on arrival.

`MSG_REMOTE_NOTE = 0xA0`: one line, printable ASCII, at most 40 bytes, the
outcome of a pick ("opening ds-claude-terminal", "that session is gone").

### Client to server

`MSG_REMOTE_OPEN = 0x18`, 1 byte: 1 when the REMOTE screen opens, 0 when it
closes.  The server asks `claude agents --json` only while at least one card
has the screen open -- every few seconds, on a worker thread, never in the
select loop -- and sends a fresh set after each answer that differs from the
last one sent.

`MSG_REMOTE_PICK = 0x19`, 4 bytes:

| offset | size | field |
|--------|------|-------|
| 0      | 1    | row index, or 0xFF for a new session |
| 1      | 1    | action: 0 open the row (live attach, or select the window it is already in), 1 open a copy, 2 a new session |
| 2      | 2    | check, big-endian: which row the card means (below); 0 for a new session |

The index means only "the row you last sent me": the server maps it back to
the session it advertised and re-checks the action against that row's flags.
It never takes an id, a path or a command from the wire.

The check is there because the list moves.  It is re-sorted on every answer
-- NEEDS YOU first -- so a session that starts waiting jumps to the top while
a thumb is on its way to the button, and "row 1" then names a different
session from the one the person read.  So the card also sends a 16-bit hash
of the row it drew:

    check = fold16(FNV-1a-32(name + 0x00 + detail))
    FNV-1a-32: h = 0x811C9DC5; for each byte b: h = (h ^ b) * 0x01000193 mod 2^32
    fold16(h) = (h >> 16) ^ (h & 0xFFFF)

over the row's name and detail bytes exactly as the server sent them (after
its clamping and replacement; the card hashes what it received, before any
clamping of its own).  The NUL keeps ("ab", "c") apart from ("a", "bc").  The
server recomputes the check for the row at that index in the set it last
sent this connection; if they differ it does nothing, answers REMOTE_NOTE
"the list changed; pick again", and sends the list again.  It is a guard
against a list that moved, not against an attacker: a client can always send
the check of the row it names.

A pick of any other length -- the 2-byte index-and-action pick this used to
be included -- is a malformed frame and drops the connection, like any other.
The card and the server ship together, and a pick without a check is exactly
the one that can open the wrong session.

Vector: row `fix-tests` / `app - permission prompt` has check 0x10BB, so
opening it at index 1 is the frame `44 53 01 19 00 04 01 00 10 BB`; a new
session is `44 53 01 19 00 04 FF 02 00 00`.

A server answers both with nothing but REMOTE / REMOTE_NOTE / the ordinary
SESSIONS and screen traffic that a window switch produces.

## STATE: what the session on screen is doing

The same `claude agents --json` reading answers a second question, and it is
the one the card's sounds and status line most need: is the Claude session
*on screen* working, finished, or waiting for you -- and if waiting, for an
approval or for an answer?  Without it the card can only infer from traffic
("the screen stopped changing"), which cannot tell a permission prompt from a
finished answer.

Gated by the HELLO token `state`.

`MSG_STATE = 0xA1`, server to client, sent once right after authentication
(the current reading, or 0) and then only when the reading changes:

| offset | size | field |
|--------|------|-------|
| 0      | 1    | state: 0 unknown (no Claude session in the selected window, or `claude` not found), 1 idle, 2 working, 3 needs approval, 4 needs input |
| 1      | 1    | detail length, 0..24 |
| 2      | n    | detail, printable ASCII: `waitingFor` as Claude Code reports it ("permission prompt", "input needed" ...), empty otherwise |

Mapping from `status` / `waitingFor` of the session whose process lives in
the dsterm tmux window that is currently selected:

* `busy` -> 2 working
* `idle` -> 1 idle
* `waiting` with `permission prompt` or `sandbox request` -> 3 needs approval
* `waiting` with anything else (`input needed`, `dialog open`, `worker request`) -> 4 needs input
* no such session -> 0

The server polls every 1.5 s while the reading is 2, 3 or 4 or a card has
typed in the last 30 s, and every 5 s otherwise -- only while a card that
announced `state` is connected and has been heard from (it pings every
second) in the last 10 s, so a DS switched off on a half-open connection does
not keep `claude` running forever.  One reading serves both STATE and an open
REMOTE screen.  If every ask fails for 30 s, the reading becomes 0 and the
slow cadence applies until one succeeds; a reading taken before a card
connected is never offered to it as current.

The card plays its cues from these edges when it has them: THINK on idle to
working, APPROVE on anything to needs-approval, QUESTION on anything to
needs-input, DONE on working or waiting to idle.  Without STATE (an older
server) it falls back to reading traffic.
