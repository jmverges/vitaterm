# Why scrollback shows only past sent text

> **Design notes.** Written while this part was being built and kept for
> the reasoning in it. Details may describe an earlier version; the code
> and the guides in `docs/` describe the current one.

A measurement taken on a WSL host against a
live, authenticated Claude Code session. Written down because an observation
nobody records is one we make again.

## The numbers

    tmux -L dsterm display-message -p '#{history_size}'

    before the prompt          0
    after an 80-line answer    0
    history_limit              5000
    pane                       42x24

The prompt was "print a numbered list from 1 to 80, one short line each" — no
tools, no file changes, chosen so the output length was known in advance. The
answer rendered in full: item 80 was on screen. Nothing scrolled off.

## Why it is flat

    tmux -L dsterm display-message -p '#{alternate_on}'   ->  1

Claude Code runs on the **alternate screen**. The alt screen has no scrollback
by definition — that is what it is for, the same as `vim` or `less`. So:

  - `history_size` is 0 and stays 0 no matter how long the answer is.
  - pyte's history is empty for the same reason. Nothing ever scrolls; the app
    repaints the same 24 rows.
  - tmux copy-mode cannot reach it either. There is nothing behind the window.

This is the third case in the client-side survey (37/37, 22/22, 0/0) and it is
the 0/0 one. The mechanism is now named: not "content repainted in place" as a
general phenomenon, but specifically DECSET 1049.

The earlier conclusion therefore holds and hardens: `tmux capture-pane` would
not have recovered this, and neither will any emulator change. The content the
handheld wants to scroll through never becomes history anywhere. It existed
only as pixels in a 24-row window that was subsequently overwritten.

## What does work

Claude Code scrolls its own transcript. Verified on the live session:

    tmux -L dsterm send-keys -t dsterm PageUp     screen changes, shows earlier
                                                  output and the prompt echo
    tmux -L dsterm send-keys -t dsterm PageDown   returns to live; the captured
                                                  pane hashes identical to before

Round-tripped to the same md5, so the test left the session exactly as it found
it, unsent input line included.

The application that owns the screen owns the history. That points the fix at
the key path rather than at a server-side view — but it is a client decision and
it is not made here.

## Server-side consequence, for whoever picks this up

pyte does record the mode even though it does not implement the buffer swap.
Private modes are stored shifted left by five:

    1049 << 5 == 33568 in screen.mode   <->  the child is on the alternate screen

Confirmed against pyte 0.8.2 by feeding `ESC[?1049h` to a bare HistoryScreen.
So the server can tell a client which regime it is in, without asking tmux and
without assuming the child is Claude Code. That matters because the existing
history path is still correct for children that do not use the alt screen — a
plain shell run with `--` scrolls normally and the MSG_VIEW machinery works on
it exactly as designed. Whatever gets built, it should be selected by the mode,
not by hardcoding one child.

Not built. Not proposed as a wire change yet. Recorded so the next person starts
from the measurement instead of the assumption.

## Correction: the alternate screen was not the whole reason

Taken while building the MODE flag, and it moved the ground under it. Four
configurations, same 90-line child, `Session` driven directly:

    child        via tmux   alt_screen   pyte history
    plain        no         False        67
    alt-screen   no         True         67
    plain        yes        False         0
    alt-screen   yes        False         0

Two things neither of us had measured:

  1. **Through tmux the emulator gets no history at all** — 0 lines, whatever
     the child does. A multiplexer repaints its pane by addressing cells rather
     than letting lines scroll off, so nothing is ever pushed above the top.
  2. **tmux does not forward DECSET 1049 outward.** The inner application's
     alternate screen is tmux's business; the outer terminal never sees it.

So in the configuration we actually ship — `serve.py --tmux`, which is what the
systemd unit runs — `alt_screen` is False while the history is empty. A flag
derived from `alt_screen` alone would have reported "the server has scrollback,
ask it for a view" on every production connection, and the view would have come
back 0 of 0 forever. That is the wrong-button failure the flag exists to
prevent, and it would have shipped.

Hence `Session.app_scrolls`, which is `multiplexed or alt_screen`, and hence the
wire bit is named MODE_APP_SCROLLS rather than MODE_ALT_SCREEN. The name is the
consequence, not the mechanism, because there turned out to be two mechanisms.
`multiplexed` is read off argv[0] rather than sniffed from the output: it is a
fact we already hold, and it is true from the first byte instead of after
enough of them.

Note what this costs. The MSG_VIEW history path is now dead for every child we
run in production, not merely for Claude Code — a plain shell inside tmux has
the same empty history. The path is still correct and still tested for an
unwrapped child, which is why it stays.

### Two tests in test_endtoend.py cannot pass, and were not failing by accident

    TestThroughTmux::test_scrollback_reaches_back_past_the_screen
    TestThroughTmux::test_the_view_position_is_reported_and_returns_to_live

Both assert that scrolling back works through tmux. Per the table above that
cannot hold: `scrollback_lines` is structurally 0 there. Verified as pre-existing
by running them at an earlier commit in a clean worktree, before any of this landed — they
fail identically. They would have been skipped on a machine without tmux, which
is why they were believed to pass.

Deliberately not rewritten here. They protect scrollback, which is a real
property worth protecting, and the house rule is to say which test is wrong
rather than quietly edit it.

## Correction to the correction: that table is this box, not the world

The section above is right about this machine and wrong to generalise. See
SCROLLBACK_DRAIN_RATE.md for the counter-measurement, and:

  - On tmux 3.7b a scrolling child inside tmux DOES feed our history — 67 lines
    — provided the PTY is drained tightly. A probe that sleeps between reads
    gets 0 and is measuring the probe.
  - On tmux 3.4, drain rate changes nothing: 0 lines either way. Re-run with a
    tight loop to confirm, both directions, same child.
  - The outer alt bit is likewise version-dependent: True under 3.7b for any
    tmux child, because tmux takes the outer terminal's alternate screen on its
    own account; False under 3.4. It reports tmux, never the inner application,
    so it is wrong in both directions rather than merely blind.

So neither `multiplexed` nor the pyte alt bit can derive the flag. `multiplexed`
is a constant over everything we ship and would claim a shell owns scrolling
while 67 good lines sat behind it. The alt bit answers a different question and
disagrees with itself across versions.

`app_scrolls` therefore asks tmux about the pane — `#{alternate_on}` — which
reports the inner application, is correct on both versions and all four rows,
and is polled at 1 Hz because the regime changes on human timescales. The
non-multiplexed branch still reads the emulator, so a server run without
`--tmux` needs no tmux installed.

The two end-to-end tests are consequently guarded rather than rewritten: they
assert a property the production server genuinely has on a box whose tmux
scrolls, so they now skip where it does not. The guard measures the property
directly instead of comparing version strings, because nobody has located the
boundary between 3.4 and 3.7b and a proxy would be a third guess in a file
already containing two.

The mixed case — shell scrolls, then an alt-screen application starts, flag
flips, the shell's history stays put — is pinned by
test_mode.py::TestTheFlagFollowsTheInnerApplication. That is the case both cheap
derivations fail, and it is the one to keep.

## Correction: Claude Code moved to the main screen (2.1.x)

Claude Code 2.1.270 runs on the **main** screen, so `#{alternate_on}` is 0 and
the card is in the VIEW regime — which reads history from pyte. Through tmux,
pyte receives almost none: measured live, `alternate_on=0` with
`history_size` 305 and 209 on the two panes while the emulator held
`scrollback_lines: 0` and every VIEW answered `0 of 0`. Intermittent because
whether pyte catches lines at all depends on the tmux version and the drain
rate, exactly as this file measured.

The fix: when the child is multiplexed, history comes from tmux's pane
history, not the emulator. The 1 Hz pane query also reads `#{history_size}`;
a VIEW is served by a worker that runs `capture-pane -p -e -S -<offset>
-E <rows-1-offset>` and replays the capture into a fresh pyte screen for the
same folding, and the view is anchored to an absolute history line so what is
being read stays put while new output arrives. The end-to-end tests now run
unconditionally wherever tmux exists.
