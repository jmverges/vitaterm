# Session switching: the wire contract (milestone 6)

> **Design notes.** Written while this part was being built and kept for
> the reasoning in it. Details may describe an earlier version; the code
> and the guides in `docs/` describe the current one.

This document is the authoritative server-side M6 Sessions contract. It
supersedes the earlier client-design proposal: there is no list request, no
stable project ID, no unseen-line count, and no client capability token for
Sessions.

## 1. Compatibility and scope

`SESSIONS` (`0x97`) is a coordinated rollout. An older card cannot parse this
server-to-client message and must not connect to a server that sends it, so
the card and the server were updated together.

There is intentionally no Sessions HELLO token and no list-request message.
After authentication, the server sends a complete snapshot. Receiving that
snapshot is the card's proof that this connection understands `SESSION_PICK`.
The card must only send a pick after it has assembled one complete snapshot.

A session is a live tmux window in the dedicated `tmux -L dsterm` server. The
server never accepts a tmux target, name, path, command, or durable session ID
from the handheld.

## 2. Wire format

```
0x97  SESSIONS      server -> client
0x15  SESSION_PICK  client -> server
```

A `SESSIONS` frame carries exactly one row. A snapshot is either one empty
frame, or a contiguous run of one frame for every row, indexed `0..count-1`.

```
empty snapshot
  count:u8 = 0

non-empty row
  count:u8       1..8; total rows in this snapshot
  index:u8       0..count-1; wire row index
  flags:u8       bit 0 RUNNING
                 bit 1 ACTIVE
                 bit 2 NEW_OUTPUT
                 bits 3..7 reserved and zero
  idle_s:u16be   seconds since observed output, capped at 65535
  name_len:u8    1..24
  name:bytes     printable ASCII
```

The empty form is valid only with a one-byte payload. Every non-empty payload
is exactly `6 + name_len` bytes. A decoder must reject malformed lengths,
out-of-range count/index/name values, and nonzero reserved flag bits. Names are
short printable ASCII; the server clamps tmux names before encoding them.

`SESSION_PICK` has exactly one byte:

```
index:u8
```

The index is meaningful only against the most recently advertised complete row
mapping for that exact client. It is not a tmux window index, target, path,
command, name, or identity that survives a later advertisement.

## 3. Server semantics

### Authoritative window identity

The server discovers each live window's tmux `window_id`, current display
index, name, active state, and activity marker. A window is identified by:

```
(tmux-server-epoch, window_id)
```

The display index is presentation only. If the tmux server changes or restarts,
its epoch changes and the server invalidates every advertised row mapping and
output tracker before publishing further rows. A pick is revalidated by this
identity and selected by verified `window_id`, never by an updated display
index.

Only discoverable live tmux windows are advertised as `RUNNING` in v1. A server
with no authoritative configured catalog omits vanished windows rather than
inventing stopped rows. A stale, missing, stopped, or unattachable row is
refused with `ERROR`; the server never silently redirects a pick to another
window.

### Output, new output, and idle age

Output state is per window identity and view state is per client:

- The server keeps an output revision and monotonic last-output timestamp for
  each live window.
- Each client keeps the last viewed revision for each window identity.
- `NEW_OUTPUT` is true exactly when `output_revision > last_viewed_revision`.
- Output in the active window advances that client's viewed revision. Switching
  to a window also advances its viewed revision to the current output revision.
  Later output while it is inactive is what makes `NEW_OUTPUT` true.
- `idle_s` is `monotonic_now - last_output_monotonic`, capped at `65535`.
  Wall-clock time, terminal text, prompt shapes, and guessed input state are
  not signals for this feature.

The server initializes unseen windows during discovery and records activity at
the polling boundary. This makes tmux's coarse activity signal usable without
pretending it is terminal-content tracking.

### Snapshot publication

The server publishes a complete snapshot after authentication, after a
successful switch, and after meaningful session-state changes. It refreshes
idle ages at the normal one-second session poll cadence.

Frames belonging to one snapshot are queued as a FIFO batch. The server does
not begin a later snapshot until the previous `0..count-1` batch is entirely in
the outbound queue; a refresh that arrives while a batch is pending is
coalesced. The client row mapping is promoted with the corresponding completed
advertisement, so a pick can never resolve through another client's list or a
partly replaced snapshot.

## 4. Selecting a session

On a valid `SESSION_PICK`, the server:

1. Re-lists and validates the exact mapped `(epoch, window_id)`.
2. Selects that verified tmux window.
3. Resets the picking client's view offset to live (`0`).
4. At the next ordinary render/update tick, queues a forced full terminal
   resynchronization and cursor, then fresh `VIEW_POS`, forced fresh `MODE`,
   and a complete fresh `SESSIONS` snapshot.

The required serialized order is:

```
FRAME_DIFF resynchronization / CURSOR -> VIEW_POS -> MODE -> complete SESSIONS
```

The resynchronization uses the normal bounded `FRAME_DIFF` sequence rather than
an oversized literal `FRAME_FULL`, preserving the 536-byte message ceiling. An
ordering barrier prevents metadata for the new view from getting ahead of the
terminal repaint, without waiting indefinitely for tmux to paint.

A valid pick proves `VIEW_POS` support for this coordinated M6 card. All picks
are still fully validated; malformed payloads are protocol errors and stale
mappings receive `ERROR` without switching the active terminal.

## 5. Deliberately excluded signals

M6 does not advertise "waiting for input" or infer it from terminal text,
prompt shape, spinner state, or idle time. `NEW_OUTPUT` means only that output
revision advanced while the client was not viewing that window. This distinction
is deliberate: a false readiness indication is worse than no indication.

## 6. Hardware verification after approved deployment

Verify on a card that switching between a shell and a Claude
window resets scrollback to live, updates the view readout, repaints before its
metadata, and clears/sets background-output indication only as output revisions
change. Also verify the dedicated tmux session retains its per-window manual
sizing. Do not change the DS ROM, SD card, tmux configuration, or active service
as part of server implementation or host testing.
