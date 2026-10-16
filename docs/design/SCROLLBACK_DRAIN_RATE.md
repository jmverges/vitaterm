# History through tmux depends on how fast we read, not on the multiplexer

> **Design notes.** Written while this part was being built and kept for
> the reasoning in it. Details may describe an earlier version; the code
> and the guides in `docs/` describe the current one.

Written after a change derived `app_scrolls = multiplexed or alt_screen` from a
table showing zero pyte history through tmux in every configuration. That table
does not reproduce here, and the reason it does not matters more than the table.

## The measurement

Same 90-line child throughout, pyte history read off `Session.scrollback_lines`,
tmux 3.7b, `-f dsterm.tmux.conf` (so `status off`, pane is the full 42x24).

The only variable is how the reader drains the PTY:

    reader                                   pyte history
    pump() with time.sleep(0.033) between       0
    pump() in a tight loop                     67

Same child, same tmux, same config, same everything else. Reproduced across
repeated runs in both directions; it is not a race.

The production server is the tight case. It pumps on selector readability
(`server.py:339`), not on a timer, so the shipped configuration is the row that
yields 67 -- and a bare `Session` driven by a polling loop is the row that
yields 0. A probe harness that sleeps between reads measures its own harness.

The mechanism is inferred, not measured: tmux appears to coalesce a burst into a
full-screen repaint when the terminal drains slowly, and to emit incremental
line-at-a-time output -- which scrolls the outer terminal and therefore feeds
pyte's history -- when it drains quickly. What is measured is that the drain rate
changes the outcome. That is enough to disqualify anything derived from argv.

## The four configurations, at production drain rate

    child            via tmux   pyte alt bit   multiplexed   pyte history
    scroller         no         False          False          67
    scroller         yes        True           True           67
    alt-screen app   no         True           False           0
    alt-screen app   yes        True           True            0

Read the second row against the flag as built. `multiplexed` is true and
`alt_screen` is true, so `app_scrolls` is true -- and there are 67 lines of
perfectly good history sitting there that the handheld would then refuse to
scroll, because it would be sending PageUp into a bash prompt instead.

Two separate problems, either one fatal:

  - `multiplexed` is true for every session we ship, so it cannot distinguish
    anything. It is a constant, not a signal.
  - The pyte `alt` bit is true for *every* child under tmux on this tmux, because
    tmux enters the alternate screen of the outer terminal on its own account.
    It reports tmux's state, never the inner application's. On tmux 3.4 it
    reportedly reads False under tmux instead -- which is the same defect with
    the opposite sign, and means the bit is version-dependent as well as wrong.

## What the signal has to be

The inner application's alternate-screen state is the thing that actually
predicts an empty history, and it is not visible in the byte stream, because
tmux does not forward DECSET 1049 outward. It is visible to tmux, which is how
it was found in the first place:

    tmux -L dsterm display-message -p '#{alternate_on}'

That queries the pane, so it reports the inner application. It is authoritative
where the derived bit is a guess, it costs one cheap call, and it is correct on
all four rows above on both tmux versions.

`scrollback_lines == 0` is tempting as a pure-measurement alternative and is
correct on all four rows, but it fails the mixed case: a shell that scrolls, and
*then* launches Claude Code, has a non-empty history that belongs to the shell.
The handheld would scroll the pre-Claude transcript while Claude is on screen.

## Consequence for the two end-to-end tests

`test_scrollback_reaches_back_past_the_screen` and
`test_the_view_position_is_reported_and_returns_to_live` pass here, at that change,
against real tmux -- 67 lines of history, four passed. They assert the property
the production server actually has, which is why they were written through the
Server rather than against a bare Session.

They are not rewritten and should not be. If they fail on another box, the
question that needs answering first is whether that box's harness drains on
readability or on a timer, because this document is what the difference looks
like.
