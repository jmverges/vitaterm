# Input and the two-screen UI (milestone 4)

> **Design notes.** Written while this part was being built and kept for
> the reasoning in it. Details may describe an earlier version; the code
> and the guides in `docs/` describe the current one.

Design for the handheld's input layer. Written 2026-08-10, after M3 rendered a live
Claude Code session on the top screen.

## What is already true

Do not redesign these; they work and are measured.

- Top screen is a **42x24 terminal** at 6x8 cells, drawn into the main engine's LCD
  framebuffer (`MODE_FB0` + `VRAM_A_LCD`). Cells arrive as diffs; a whole screen is
  12 paced messages.
- Bottom screen is currently `consoleDemoInit()` showing the boot log.
- Physical buttons already map to real key bytes and the server writes them into the
  PTY. `A`→`\r`, `B`→`\x7f`, `X`→`\x1b`, `Y`→`\t`, `START`→`\x03`, d-pad→`\x1b[A/B/C/D`.
- The protocol is length-prefixed, 6-byte header `>2sBBH`. The framing layer on both
  sides rejects unknown message types rather than ignoring them, so a new
  server→client type is only backward compatible with clients that know it: the card
  and server are updated together, never one ahead of the other.

## STEP 0 — verify the transport before building anything

The build that fixed lwIP thread starvation had not yet run on hardware when this was written. Everything
below assumes bytes can leave the DS. Confirm that first:

1. Flash a build with the thread-starvation fix.
2. Boot. The sub screen must show an `L<number>` line that climbs. If it does not, the
   card did not take the build.
3. Press A. The server must log received bytes beyond the 53-byte handshake
   (`ss -tni | grep 8347`, watch `bytes_received`).

If input works after this, the rest of M4 is ergonomics rather than repair. If it does
not, fix that before writing UI code — do not layer a keyboard on an unverified path.

## Physical buttons

| Button   | Action                        | Bytes sent        |
|----------|-------------------------------|-------------------|
| D-pad    | arrows, with key repeat       | `\x1b[A/B/C/D`    |
| A        | Enter                         | `\r`              |
| B        | Backspace                     | `\x7f`            |
| X        | Escape                        | `\x1b`            |
| Y        | Tab, or Shift-Tab if R armed  | `\t` / `\x1b[Z`   |
| L        | arm Ctrl for the next key     | none              |
| R        | arm Shift for the next key    | none              |
| START    | Interrupt                     | `\x03`            |
| SELECT   | toggle bottom screen mode     | none, local only  |

Key repeat: 400 ms delay, then every 60 ms. Applies to the d-pad and to touch keys,
not to A/X/Y/START — a repeated Enter or interrupt is never what anyone meant.

Modifiers are **sticky and one-shot**: pressing L arms Ctrl, the next character key
sends `byte & 0x1f` and disarms it. Pressing L twice disarms. This is the only design
that works when a modifier and a letter cannot be pressed together — L is on the
shoulder and the letters are on the touchscreen.

Shift-Tab matters: it is how Claude Code cycles modes.

### Focused keyboard shortcuts

When Focus is active **on the keyboard key grid**, its nearby-action layout
overrides the ordinary physical-button map: `A` presses the highlighted key,
`B` inserts a space, `Y` sends Backspace, and one press of `R` sends Enter.
`B` and `Y` keep the normal typing repeat cadence; `R` is edge-only and never
repeats. The on-screen Focus footer names this layout.

These overrides are deliberately limited to the active key grid. The Control
tiles retain `B` as Back, the composer remains its separate all-local editor,
and the non-Focus keyboard retains the physical map above. `L+R` still only
enters or leaves Focus; it can never submit an Enter as a side effect.

## Bottom screen

The sub engine has **no LCD framebuffer mode** — `MODE_FB0` is main-engine only. Use an
extended-rotoscale bitmap background instead:

```c
videoSetModeSub(MODE_5_2D);
vramSetBankC(VRAM_C_SUB_BG);
int bg = bgInitSub(3, BgType_Bmp16, BgSize_B16_256x256, 0, 0);
u16 *sub = bgGetGfxPtr(bg);
```

That gives a 256x256 16-bit surface written exactly like the top screen's framebuffer,
so `render.c`'s 6x8 font routine can be reused. **Generalise `render_cell()` to take a
target pointer and stride** rather than duplicating the glyph loop.

Dropping `consoleDemoInit()` means `printf` logging goes away. Keep a
`char log[24][43]` ring buffer and redraw it in STATUS mode, so the boot log survives
a mode switch and remains photographable — it has been the most valuable debugging
surface in this project.

### Two modes, toggled by SELECT

**KEYBOARD** (default)

```
 y   0..15   two status lines, 42 cols at 6x8
 y  18..167  keyboard, 5 rows of 27px
 y 170..191  hint line and modifier indicators
```

**STATUS**

Full-screen log ring buffer plus the counters that already exist
(`rx` bytes, `msg` count, `tx` count, loop count, `raw`/`lib` key words).

### Keyboard grid

10 columns x 5 rows. Key `n` at `x = 4 + col*25`, `y = 20 + row*27`, size 24x26.

Base layer:

```
1 2 3 4 5 6 7 8 9 0
q w e r t y u i o p
a s d f g h j k l ;
z x c v b n m , . /
SHFT CTRL ESC TAB [SPACE x2] - = ENT BKSP
```

Shift layer:

```
! @ # $ % ^ & * ( )
Q W E R T Y U I O P
A S D F G H J K L :
Z X C V B N M < > ?
SHFT CTRL ESC S-TAB [SPACE x2] _ + ENT BKSP
```

Sym layer (press SHFT twice) replaces row 0 only:

```
` [ ] \ ' | { } ~ "
```

Rows 1–4 are unchanged in the sym layer. Between the three layers every character
needed to type a prompt and a shell command is reachable.

### Touch

- `scanKeys()`, then `keysHeld() & KEY_TOUCH`, then `touchRead(&pos)`.
- Ignore the first frame of a touch. The DS touchscreen reports a garbage coordinate on
  contact and this is the single most common source of phantom keypresses.
- Emit on **press**, not release, and invert the key's cell while held so there is
  visible feedback.
- Apply key repeat to character keys and backspace only.

## Cursor — new protocol work

There is no cursor on the top screen today. `MsgType` has no CURSOR and
`Session.snapshot()` returns `(grid, changes, stats)` without one. Typing blind is
unacceptable once there is a keyboard, so add it:

- `MsgType.CURSOR = 0x94`, payload 3 bytes: `row`, `col`, `visible`.
- Server reads `screen.cursor.x/y` and `screen.cursor.hidden` from pyte and sends the
  message when any of the three changes.
- Client draws the cell at that position inverted, and **repaints the cell it vacated**
  when the cursor moves. Getting that wrong leaves a trail of inverted cells.
- No version bump needed: the wire version stays 1 and CURSOR is display state, not a
  side channel. But the card and server must be reflashed/restarted together: a
  pre-CURSOR client halts on the first CURSOR frame instead of ignoring it.

## Order of work

1. STEP 0 above. Nothing else until input is proven on hardware.
2. Cursor: protocol, server emission, client rendering. Small, self-contained, and it
   makes every later step verifiable by eye.
3. Sub-screen bitmap, font generalisation, log ring buffer, STATUS mode. At this point
   nothing has regressed and the boot log still works.
4. Keyboard layers and touch handling.
5. Modifiers and the physical button remap.

## Tests

Host tests (`dsclient/tests/test_host.c`, runs on the dev machine, no DS needed):

- every key position in every layer maps to a defined byte
- no two keys occupy the same cell, and no key falls outside 256x192
- the three layers have identical geometry
- Ctrl folding: `L` then `c` produces `0x03`; `L` then `[` produces `0x1b`
- key repeat timing is a pure function of elapsed frames and is tested as one

Python tests:

- CURSOR is emitted when the cursor moves and not when it does not
- a client that ignores CURSOR still reconstructs the grid exactly

## Constraints that still hold

- The DS gets the PTY and nothing else. No new message type may name a file, a path or
  a command. CURSOR is display state, which is why it is allowed.
- No stubs, no `#[ignore]`, no weakening the gate.
- Never claim hardware behaviour that has not been observed. Say NOT TESTED and give
  the procedure.
