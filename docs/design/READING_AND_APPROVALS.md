# The reading and approval client (milestone 5)

> **Design notes.** Written while this part was being built and kept for
> the reasoning in it. Details may describe an earlier version; the code
> and the guides in `docs/` describe the current one.

Written 2026-08-10, after M4 gave the handheld a keyboard and Ship 1 made
scrollback report its position honestly.

M4 made the DS able to *type*. This makes it able to *read* and to *answer* —
the two things you actually do with a Claude Code session you are not sitting
in front of. Everything here is client-side. **No wire-format change**: the
server and an older card, and this card and an older server, remain compatible
in both directions.

## Decisions, and why

### The approval bytes: A = `\r`, X = `\x1b`

The earlier plan was `A = y\r` and `B = n\r`. That is not merely clumsy, it is
backwards: Claude Code's permission prompt is a **numbered select list**, not a
y/n question. The literal `n` is discarded as an unmatched key and the `\r`
behind it accepts whatever option is highlighted — so the reject button
approves. A reject control that approves is worse than no reject control,
because it is trusted.

So: **A sends CR** (accept the highlighted option) and **X sends ESC** (escape
the prompt). The rule that this client sends generic key bytes and never reads
the terminal's text is kept, and is precisely *why* the bytes have to be the
ones a real keyboard would send.

An intermediate version used **B** for reject, and made B mean Backspace at the
keyboard and Escape on the Control screen. That was a mistake and is gone: the
same physical button sent a different byte depending on a mode with no visible
indicator, on the one control where being wrong matters most. X already sent ESC
in every mode, so reject cost nothing to move there, and **no button in this
program is now mode-dependent**.

### The selection problem: Left/Right move the selection

Control mode spends Up/Down on scrolling, which is what that screen is for. That
leaves the selection un-movable unless something else carries it, and a Control
mode that can only accept the default is worse than no Control mode.

**Left and Right send `\x1b[D` / `\x1b[C`.** A touch control returns to live.

> **Not verified on hardware.** If a permission list turns out to honour only
> the vertical arrows, this is a one-line change and `dpad_send()` already
> sends `\x1b[A` / `\x1b[B`: route Left/Right to those two strings instead. The
> comment in `main.c` at the Control-mode d-pad block says so at the site.

### Scrolling is an armed modifier, not a held chord

Reading happens on the Control screen, where Up/Down scroll directly. From the
keyboard, **arm Shift with R, then press Up or Down**.

It used to be L *held* plus a direction. That asked for exactly the grip the
one-shot modifiers exist to avoid: M4 chose arm-then-press because a modifier
and a key "cannot be pressed together" on this hardware — the shoulders are
under the index fingers and everything else is under the thumbs. A held chord
for scrolling had the same problem and no excuse, since the armed mechanism was
already there. The two touch UP/DOWN tiles are gone with it; they were a third
way to do the same thing.

### The composer sends without a protocol change

A 255-byte local buffer, drawn as a top-screen overlay, with the lower screen
still a keyboard. **Nothing reaches the PTY while editing** — while the composer
or the snippet list is open, the only thing in the program that can send is an
explicit Send.

On Send the body goes out as consecutive `KEY` frames of ≤64 bytes with **the
carriage return as its own final frame**. `MAX_KEY_PAYLOAD` and
`PROTO_MAX_KEY_PAYLOAD` stay at 64. No server change, no card/server lockstep,
no flag day.

The CR being last and alone is the entire point: an interrupted burst leaves an
unsubmitted line on the far end that the user can see and clear, rather than a
truncated prompt that has already been submitted. Snippets take the same path —
picking one loads it straight into the composer, so the next press is Send.
Picking is not sending.

### Round-trip clock: no hardware timer

The 1 Hz probe is a `PING` with no payload; the server answers `PONG`. There is
no correlation id in the protocol and none was added: at most one probe is
outstanding, so the next PONG is unambiguously the answer to the last PING. An
unanswered probe clears the reading rather than freezing it — `--ms` is
information and a stale number is not.

Elapsed time comes from the VBlank frame counter refined by `REG_VCOUNT`
(16715 µs per frame, scaled across 263 scanlines), **not** from `timerStart()`.
dswifi's half of the machine already owns hardware timers, and racing it for one
to drive a cosmetic readout is exactly the kind of fault that gets attributed to
the network for a week.

### Battery: both consoles, honestly

`getBatteryLevel()` already normalises the two: on a DSi it is the hardware's
0-15 reading, and on a DS/DS Lite — which only distinguishes high from low —
libnds returns 15 or 3 to say so.

So this is **not DSi-only**, but the DS Lite number is a two-state reading
wearing a scale. Printing `3/15` there would invent twelve levels of precision
the hardware does not have, so the DS Lite path prints `bat low` / `bat high`
and only DSi mode prints `bat n/15`. The charger bit
(`BATTERY_CHARGER_CONNECTED`) shows as a trailing `+` on both.

### Colour: hue first, and a tenth entry for Claude's own

Reported as "the crab is white", and it was. Claude Code paints its mark, its
box rules and its prompt in 256-colour 174 — `#d78787` — and the quantiser
picked the palette entry at the shortest Euclidean distance in RGB. White is
light and so is the mark, so white won. Every coloured thing on screen that was
not fully saturated arrived colourless.

Distance has no idea what a colour *is*. Hue does, so hue decides now, and
brightness is handled separately: only a colour with no hue may become white or
black. Adding orange then exposed the same flaw from the other side — orange
sits near the middle of the cube, so on plain distance it became the nearest
entry to a *pale green*. Hue fixes both.

The tenth entry is Claude's own. Eight ANSI slots contain nothing within reach
of that orange, and the attribute byte already carried four bits per colour with
only nine spoken for — so it costs no wire and no protocol version, and a card
built before it renders index 9 as default rather than mis-colouring it.

One special case, and it is deliberate: a red below 60% saturation becomes
orange rather than red. `#d78787` is a *desaturated red* — its hue is exactly 0
— and the closest thing this palette has to a salmon is its orange, which is
where Claude's colours live anyway. A saturated hue-0 is still red.

Verified through the whole stack, not just the unit: `tests/test_endtoend.py`
runs a child inside tmux that emits the real SGR sequence and asserts the cell
arrives at the client as orange.

### Sound, backlight, lid

- **Four cues, not four beeps.** `sound.c` owns note tables and a sequencer;
  `main.c` owns the two hardware calls, so the cues are host-tested — a cue
  that never ends would hold the PSG channel open for the rest of the session,
  which on a handheld means a stuck tone with no fix but a power cycle.

  | Cue | Notes | Meaning |
  |---|---|---|
  | SENT | C5 → G5 | a composed prompt left the console |
  | DONE | C5 → E5 → G5 | output settled: the answer is ready |
  | DROP | A4 → D4 | the link went away |
  | LINK | D4 → A4 → D5 | the link came up, at boot or after a reconnect |

  One key, short notes, warm mid register, nothing urgent. Rising when
  something arrives, falling when something goes away, and the last note of
  each cue held longest so it resolves instead of merely stopping — a cue
  ending on its shortest note sounds cut off, which reads as a fault even when
  nothing is wrong. The intervals are the design, and the host test pins the
  direction so a retune cannot quietly invert the meaning.

  Driven from `frame_wait()`, not the main loop: `reconnect()` spends its
  backoff inside `frame_wait()`, so a cue started as the link dropped would
  otherwise never advance a note — the one sound you most need to hear would be
  the one that never plays.

- **The typing cue is the user's own feedback.** Every byte-producing keypress
  (touch keyboard, d-pad focus, composer, physical B/Y) voices a two-frame,
  half-volume tick — one pitch for a character, a lower one for delete. Enter
  is excluded because a submitted CR already owns SENT; arrows, ESC and mode
  chords never type. It lives in **every profile, including Studio**: Studio's
  sparseness governs how much the *model* may speak, never whether the user's
  own fingers answer — and the complaint that started this feature was a
  console silent under its own keys. It has its own 2-frame cooldown (shared
  between the two variants) and sits just above the murmur texture and below
  every named cue: it may replace a murmur, never a phrase. Every started cue
  writes one line to the boot log, so the STATUS screen can say whether the
  conductor is firing even when the speaker is not.

- **SWITCH is Companion now, FOCUS is not.** A session switch is a deliberate,
  consequential action, and a silent one feels broken in the warm profile, so
  SWITCH voices under Companion and Field. FOCUS stays Field-only: per-move
  ticking while walking a list is exactly the tactile distinction Field exists
  for. Both stay out of Studio.

- **DONE without reading the terminal.** The obvious way to chime "your answer
  is ready" is to notice a finished reply, which means recognising terminal
  text — something this client is not allowed to do and should not start doing
  for a chime. So it uses what it legitimately knows: screen-changing messages
  stopped arriving. Cursor blink does not count (`MSG_CURSOR` is its own type
  and does not change the grid), and Claude Code's spinner animates the grid
  while it thinks, so a long pause mid-answer keeps the counter alive rather
  than firing early.

  It is a heuristic, and it answers "the screen stopped changing", which is not
  identical to "Claude is waiting for you" — a finished command chimes too.
  2.5s of quiet, at least four messages, and silent in the two cases where it
  would most likely be wrong: while the view is scrolled back, and after a
  `VIEW` request, whose resync burst is this client's own doing.

  What it still cannot do is tell you a **permission prompt** is waiting, which
  is the one worth having. That needs a server-side signal — see
  `docs/design/SESSIONS_WIRE.md` §5, where the same flag was requested and declined
  until it can be derived without screen-shape heuristics.
- **Mute is session-scoped by construction** — a variable, not a setting written
  to the card. A muted console is one power cycle from audible again, and no
  state is left behind that makes a silent handheld look broken. It lives in
  `sound.c` and nowhere else; muting mid-cue abandons that cue rather than
  letting it finish silently and resurface on unmute.
- **No automatic dimming anywhere.** A screen that darkens on its own while
  someone is reading a long answer is a bug that looks like a hardware fault.
- **Lid behaviour is untouched.** Nothing in this program handles `IRQ_LID` and
  nothing here changed that. It is on the hardware checklist to *observe* before
  anyone decides it needs changing.

### The bottom screen paints once a frame, and clears only on a layout change

Observed on hardware: the bottom screen flashed constantly, and again on every
interaction. Both were the same bug.

`subscreen_update()` blanks 256x192 and redraws into the bitmap the LCD is
showing — there is no back buffer, so every repaint is visible. It was called
synchronously from `status()` (once per log line), from every `VIEW_POS`, from
every touch, and once a second. One scroll press produces a `VIEW_POS` *and* a
~12-message resync, so a single press could repaint the screen a dozen times in
one frame.

Two changes. Callers now mark the screen dirty and return; `frame_wait()` paints
at most once per frame, just after the VBlank yield — the one moment the bitmap
can be rewritten without tearing, and a bound that holds no matter how many
things asked. Coalescing is the fix; painting less often would only have made
the flash rarer.

And the full-screen clear is gone from the repaint path. Every draw path already
covers its own area — `draw_line()` pads to full width, `draw_tile()` fills its
rect — so the clear was only ever covering debris left when one layout gives way
to another. It now runs on layout change only. If a layout leaves debris after
that, its draw path is incomplete; fix the path rather than clearing every
frame.

### Control tile geometry lives in `kbd.c`

The tile rectangles were originally written twice — once in
`draw_control_screen()` and again in the hit test — with nothing keeping them in
agreement. That is how a MUTE button comes to send an escape, and on the
approval pair that failure is not cosmetic.

The geometry now lives in `kbd.c` beside the keyboard's, as `kbd_control_rect()`
/ `kbd_control_hit()`: `subscreen.c` draws from that table and `main.c`
hit-tests against the same one. `kbd.c` has no libnds dependency, so it is
host-tested — no overlaps, everything on screen, every centre hits its own
control, edges exclusive, and a bad id fails closed with an empty rect.

## The three modes

SELECT cycles **CONTROL → KEYS → STATUS**. Control is the default: it is the
screen the handheld is most often picked up to use, and the only one where a
mis-touch cannot type into the terminal.

| Mode | Top rows | Body |
|---------|-----------------------------|--------------------------------|
| CONTROL | link line, scrollback readout | four touch tiles, two hint rows |
| KEYS | two counter lines | keyboard, footer with the scroll gesture |
| STATUS | — | full-screen log, counters pinned last |

Control-mode buttons: **A accepts, X rejects** — both global, neither
mode-dependent. D-pad: Up/Down read, Left/Right move the selection.

Four touch tiles, and only things no button can do: **COMPOSE, LIVE, SNIPPETS,
MUTE**. There were seven. Four of them were labelled with buttons — `A ENTER`,
`B ESC`, `X COMPOSE`, `Y KEYS` — and two of those labels were false, since X
sends ESC and Y sends Tab. A screen that names a button has to be right about
what that button does.

Composer keys: **A sends, B deletes, X cancels**, and the on-screen ENT key
inserts a newline in the body. There is no REVIEW state — the overlay shows the
body the whole time it is edited, so a separate confirm screen displayed what
was already on screen and cost A a second meaning. A on an empty body closes
without sending, because the send plan ends in a lone CR and a stray CR is what
accepts a permission prompt.

## Tests

Host (`dsclient/tests/test_host.c`, 2797 checks, no DS needed):

- composer bounds, editing, and the exact send plan at 0/1/63/64/65/128/255
  bytes — every body frame ≤ 64, final frame a lone CR, 255 bytes → five frames
- snippet parsing: escapes, duplicates, malformed lines, over-long bodies
- the shipped `dsterm.snippets.example` parses and ships no snippet bodies
- Control tile geometry: overlaps, bounds, centres, exclusive edges, fail-closed
- the link line at both battery scales, unknown RTT/RSSI, and mute

Python: unchanged and still green — this shipment touches no server code.

## Hardware — NOT TESTED

Nothing below has been observed on the console. Build and flash are verified
(a flash log recorded commit, size, and verified-flash per flash); the
behaviour is not.

Ship 1, in order:

1. `0 of 0` — an empty session reads `no history`, not a blank line.
2. `available` climbs while a long answer streams.
3. Scroll up, read, return to live.

Ship 2:

4. Control and Status screens render; SELECT cycles all three.
5. `L` climbing, RTT populating once a second.
6. Battery and RSSI plausible; charger `+` appears when plugged in.
7. A harmless composer submission (`echo hello`), then a snippet.
8. All four cues: LINK at boot, SENT on a composer submission, DONE ~2.5s
   after a long answer settles, DROP on pulling the server down. Mute
   silences them and survives nothing — a power cycle restores sound.
9. A disconnect/reconnect: RTT blanks, then recovers.
10. **A lid close/open — observe before changing anything about it.**

The approval pair is the one to watch: confirm on a real permission prompt that
A accepts the highlighted option and B dismisses it *without* accepting, and
that Left/Right actually move the selection. If they do not, see the one-line
change noted above.
