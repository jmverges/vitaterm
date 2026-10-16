# Design notes

Written while each part was being built, and kept for the reasoning in them:
what was measured, what was tried, and why the code is the shape it is. They
describe the design as it was decided; the guides in [`docs/`](..) and the
code describe it as it is now.

The project was built in milestones, and the notes refer to them by number:

| Milestone | What it added |
|---|---|
| M1 | Measuring what a Claude Code session repaints, cell by cell (`tools/measure_cells.py`) |
| M2 | The wire format: cells, diffs, scrolls |
| M3 | A live session on the DS's top screen |
| M4 | Input: the touch keyboard and the two-screen UI |
| M5 | Reading back and answering approvals from the DS |
| M6 | Several sessions, and switching between them |

"Ship 1" and "Ship 2" in the M5 note are the two batches it was delivered in.

| Note | About |
|---|---|
| [INPUT_AND_KEYBOARD.md](INPUT_AND_KEYBOARD.md) | The keyboard's layout, sticky and one-shot modifiers, key repeat |
| [READING_AND_APPROVALS.md](READING_AND_APPROVALS.md) | Scrolling back, and answering Claude from the buttons |
| [SESSIONS_WIRE.md](SESSIONS_WIRE.md) | The SESSIONS and SESSION_PICK messages, and their ordering rules |
| [SNIPPETS_PUSH.md](SNIPPETS_PUSH.md) | Why pushed text is shown in full and never typed without a second press |
| [SCROLLBACK_MEASUREMENT.md](SCROLLBACK_MEASUREMENT.md) | What scrollback can show through tmux, measured |
| [SCROLLBACK_DRAIN_RATE.md](SCROLLBACK_DRAIN_RATE.md) | Why history depends on how fast the server reads |
