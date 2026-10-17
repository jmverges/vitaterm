# Changelog

## 1.0.1 (2026-10-17)

### Approvals are tied to the prompt they answer

Before, pressing **A** sent an Enter that wasn't tied to any particular
permission prompt. A double press, a frame delayed on the way, or a prompt
answered at the computer while the press was in flight could land that Enter
on the *next* prompt. Now:

- **Every permission prompt has an id.** The computer sends it to the DS
  with the "needs your OK" state. An Enter pressed while a prompt is open
  carries that id, and the computer delivers it only if the same prompt is
  still waiting.
- **Late, crossed and double presses are held back.** This covers an Enter
  sent before the DS had heard of a prompt, and an Enter pressed within a
  moment of the last one, while the computer checks whether a new prompt is
  waiting behind it. The DS says *"not sent: the screen changed -- look, then
  press again"*.
- **X (decline), the d-pad and typing are never held back.** Declining is
  safe wherever it lands, and an arrow only moves the highlight.
- **Nothing times out.** A prompt waits in Claude Code until a key reaches
  it, so a lost connection never approves or declines anything.
- **Old DS apps and old computers work as before.** The feature switches on
  only when both sides have it.

It is pinned C against Python, tested against random interleavings of
prompts, presses, late readings and answers at the computer
(`tests/test_answers_random.py`), and run on a DSi. The one case it can't
cover is in [PROTOCOL.md](docs/PROTOCOL.md#answers): a prompt answered at the
computer, with the next one shown within half a second of an arrow pressed on
the DS.

## 1.0.0

The first release: your live Claude Code session on a DS, approvals on a
button, QUICK, REMOTE, the DSi camera, `dsterm view`, pairing by a six-digit
code, and an encrypted, signed link. The downloads are on the repository's
Releases page.
