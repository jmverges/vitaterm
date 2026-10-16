# Performance

What the server costs and how quickly it answers, measured under everything
at once and over twenty minutes; what the test suite guarantees on every
change; and what is still assumed rather than measured.

```sh
.venv/bin/python tools/measure.py                  # about a minute
.venv/bin/python tools/measure.py --soak 20        # twenty minutes, memory over time
.venv/bin/python tools/soak.py --hours 4           # watch a real DS session
```

## Three kinds of claim

| Kind | What it means here |
|---|---|
| **Guaranteed** | A test in the suite fails if it stops being true; CI runs it on every push |
| **Measured** | Numbers from a run on one machine on one date, below, with the tool that reproduces them |
| **Assumed** | Not yet measured, and said so |

**Guaranteed** (tests in brackets):

- Every message the server sends a card fits one 536-byte TCP segment,
  encrypted and sealed -- each type at its largest legal size -- except an
  extension's own `EXTENSION` frames, which the extension keeps inside it
  (`tests/test_protocol.py`).
- A card's PING is answered within 500 ms while the program on screen floods
  the terminal with `ESC # 8`, scroll counts in the tens of thousands, or
  `yes`, and while it prints seeded random noise through a real PTY
  (`tests/test_responsiveness.py`, `tests/test_fuzz.py`). Measured below it
  is under 35 ms; the bound is loose for a slow CI runner.
- Emulating output works to a budget: 64-byte slices, 8 ms a pass, the rest
  kept for the next pass. While 30 KB of `ESC # 8` drains, each pass is
  tested to return within the budget plus 100 ms, and a scroll count to cost
  at most a screen's height (`tests/test_responsiveness.py`).
- Random and malformed input from cards, from peers before authentication,
  from views and from the program on screen never stops the server, and a
  fresh card can still log in after it; the server's own terminal emulator
  is fuzzed directly (`tests/test_fuzz.py`).

**Assumed** (not yet measured):

- The DS's radio. Loopback takes the network out of everything below; on
  the DS add its own round trip, which its Control screen shows for the
  PING it sends every second.
- Hours on real hardware: reconnects, sleep and wake, the camera, memory.
  `tools/soak.py` records exactly this against a live server and a real DS;
  its results go here when a long session has been run.
- Memory beyond twenty minutes. Every buffer is bounded (below), which is
  why it should stay flat; a day-long run has not been made.

## How it is measured

`tools/measure.py` starts a real server in a process of its own, so its CPU
and memory are its own, on loopback. Its child echoes what is typed while
another process prints a line every 20 ms behind it, as a busy session
would. A test card logs in the way the DS does (HELLO, AUTH, every frame
sealed) and types a short line every 200 ms plus a random fraction of a
frame interval -- so keys land at every point between the server's ticks --
timing each one until it is on its own screen. The first line of each
connection is a warm-up (about 200 ms: the child's first echo and the first
frames) and is reported apart.

The combined run adds, at the same time, what a DSi does with the camera
open and a `dsterm view` watching: the camera's live view at 3 frames a
second, the card's bottom screen at 4 a second (its most), and a view
reading every screen the server sends it. Then a server is started for each
of three floods of costly output and timed with PINGs, the card is dropped
and reconnects five times, and the server is restarted three times, each
timed until the card has its screen back. `--soak` runs the combined
workload for as long as asked and samples the server's memory every half
second.

## Results

Measured 2026-10-15 on an Apple silicon Mac (macOS, arm64), Python 3.11,
with every frame encrypted (SEAL v3), by `tools/measure.py --seconds 60
--soak 20`; recovery by `tools/measure.py --seconds 10`, run apart (below).

**A typed line, from the key to the card's screen**, in milliseconds:

| Workload | Lines | p50 | p95 | p99 | max | over 2 ticks | lost |
|---|---:|---:|---:|---:|---:|---:|---:|
| Typing, output streaming behind it | 129 | 34.7 | 67.0 | 71.1 | 128.0 | 0 | 0 |
| The same, plus camera 3 fps, bottom screen 4/s, `dsterm view` watching, for 20 minutes | 5,121 | 38.6 | 68.4 | 71.9 | 134.2 | 1 | 0 |

**The server process during the 20-minute combined run:**

| Measure | Value |
|---|---:|
| CPU | 9.6% of one core (5.7% for the same run before encryption) |
| Camera frames in | 3.0 a second, 17.9 KB/s |
| Bottom screens in | 4.0 a second, 23.5 KB/s |
| To `dsterm view` | 23.0 KB/s (9.4 top and 4.0 bottom screens a second) |

**Memory (RSS) over the 20 minutes**, in MB:

| Minute | 0 | 2 | 4 | 6 | 8 | 10 | 12 | 14 | 16 | 18 | 20 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| RSS | 25.0 | 41.2 | 42.6 | 39.3 | 43.1 | 43.1 | 43.1 | 43.1 | 28.5 | 30.0 | 39.5 |

**A PING while the program floods the terminal**, in milliseconds (40 each):

| Output | median | max |
|---|---:|---:|
| `ESC # 8` (fill the screen), without pause | 25 | 46 |
| `CSI 99999 S` (scroll), without pause | 15 | 21 |
| `yes` | 5 | 21 |

**Recovery**, until the card has its screen again, in milliseconds, with
and without encryption, back to back on the same machine:

| Event | Runs | median | max | before encryption: median, max |
|---|---:|---:|---:|---:|
| The card drops and reconnects | 5 | 68 | 69 | 67, 68 |
| The server restarts (process start to screen) | 3 | 147 | 180 | 155, 199 |

## Reading them

- **A key waits for the next tick.** The server turns the session into frames
  15 times a second (`DEFAULT_FPS`), so a key pressed at a random moment
  waits up to 66.7 ms for the next one: the median is about half a tick,
  p95 and p99 just over one. The camera, the bottom screen and a watching
  view together moved the median by 4 ms.
- **The tail is rare and not explained.** Over twenty minutes 1 line of
  5,121 took longer than two ticks, 134 ms; none was lost. Why it missed has
  not been measured.
- **Encryption costs the server a few percent of a core, and nothing else
  measurable.** ChaCha20 is pure Python, about 0.4 ms for a full 536-byte
  frame, so the combined run took 9.6% of a core instead of 5.7%. Typing,
  PINGs and reconnecting moved by a millisecond or two, within run-to-run
  noise. (The long run's own restart figures were left out: that checkout
  had no bytecode cache, so they timed compiling the server as well. The
  recovery table is a separate run of both versions with their caches.)
- **Memory fills, then holds.** It rose 16 MB in the first two minutes --
  most likely the 5,000-line scrollback filling at the workload's 50 lines a
  second, though the camera, the mirror and the view also started then, and
  the rise was not separated by cause -- and stayed between 39.3 and 43.1 MB
  to minute 14. It then fell to 28.5 MB and rose again; the run before
  encryption fell the same way only at its very end. Why it falls was not
  measured; it has never grown past 43.2 MB. Every buffer has a limit:
  - 5,000 lines of scrollback, and one 64 KB read of output waiting to be
    emulated;
  - per card: a 128 KB send backlog, and one picture being assembled
    (460,800 bytes at most);
  - 256 KB of typed input waiting for a child that is not reading;
  - per view (four at most): a 64 KB incoming line and a 1 MB backlog;
  - per camera-feed viewer (eight at most): a 256 KB backlog.
- **Output costs what the program chooses, so it is budgeted.** Per byte,
  emulating it varies about a hundredfold: a line of text ~3 ms a KB,
  `ESC # 8` 240-290 ms a KB. The server emulates in 64-byte slices to an
  8 ms budget a pass, keeps the rest for the next, and reads nothing more
  while a backlog waits -- the child is slowed instead of the server. Before
  this, ten bytes of `CSI 99999 S` held a PING for seconds.
- **What the DS receives is paced where it is large.** Each frame fits one
  536-byte TCP segment sealed (the DS's MSS). Screen updates and the
  `SESSIONS`, clipboard and `REMOTE` lists are released four messages a tick
  (about 32 KB/s at most); small replies and the snippet set go at once. A
  whole repaint is a run of those, never one large frame.
- **What the DS sends is budgeted.** The camera's live view is at most one
  frame in flight and one every 20 DS frames: 3 a second, about 18 KB/s. The
  bottom screen goes only while a view is open: on change, at most four a
  second, once a second during a picture's upload, and every two seconds
  regardless (6 KB each). With no view open it sends nothing.
