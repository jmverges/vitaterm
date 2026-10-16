# Contributing

Issues and pull requests are welcome.

## The most useful thing you can do first

Install it on your own console and computer, following only the guides, and
file an **Install report** (the issue template asks the right questions):
what went wrong, what nearly did, and what was only confusing. Every guide
here was written by someone who already knew the answer; a report from
someone who did not is how they get fixed.

## Setting up to work on it

```sh
git clone <this repository> && cd ds-claude-terminal
python3.11 -m venv .venv                  # 3.11 or newer; the venv must be at the repo root
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q tests       # the server's suite
dsclient/run_tests.sh                     # the client's host suite, and the C/Python cross-check
make -C dsclient                          # the ROM, with BlocksDS installed (BLOCKSDS=... if not in /opt)
tools/screenshots.sh                      # redraw docs/screens/ after a UI change
```

None of this installs a service; `.venv/bin/dsterm serve` runs the server in
the terminal you are in. CI runs the same steps.

## The rules

A few rules this codebase lives by, and why, so a change does not have to
rediscover them:

- **CI is the gate.** `dsterm test` runs both suites locally. CI also runs the
  C suite under a real gcc with ASan and UBSan, at both grid sizes, and builds
  the ROM in the BlocksDS container. Green there is what done means.
- **Both geometries.** The grid size is a compile-time contract with the
  server; the host suite runs at 42x24 and 64x32, and so must anything you add.
- **No dynamic allocation on the DS once it is running, and every length
  from the wire is bounds-checked before it is used.** The console has 4 MB
  of RAM and no MMU: an overrun there does not crash, it corrupts. (The one
  exception is loading custom sounds at boot, under a fixed budget.)
- **Never `swiWaitForVBlank()` once Wi-Fi is up.** The network stack is a
  cooperative thread; wait with `cothread_yield_irq(IRQ_VBLANK)` (in practice,
  `frame_wait()` / `host_frame_wait()`). See the comment at `frame_wait()`.
- **Pure where it can be.** Logic that does not need libnds lives in its own
  file with no libnds include and is host-tested (`dsclient/tests/`). The
  libnds glue stays thin.
- **Two implementations of one protocol drift.** Anything that crosses the wire
  gets an `INTEROP` line in the C suite and an entry in `tools/crosscheck.py`.
- **No weakening.** A test is not changed to pass; the code is. A new test
  should fail without the change it pins (try it).
- **Look at what you changed.** `tools/screenshots.sh` draws every screen with
  the DS's own drawing code into `docs/screens/`. Run it after a UI change and
  commit the pictures with it; the README shows them.
- **Sounds are generated.** `dsclient/source/sfx_assets.[ch]` come from
  `dsclient/sounds/` through `tools/sfx_build.py`; edit the WAVs or
  `sounds.txt` and regenerate, never the C.
- **The DS gets the PTY and nothing else.** `tests/test_protocol.py` pins the
  list of message types. Adding one means writing down, in that test, why it
  cannot run a command, read a file or take a path from the card; a
  server-to-client type also goes in that test's segment table, at its
  largest legal size, so it is checked against the DS's 536-byte segment.
- **Each feature owns its state.** On the server a feature's state is set in
  its `_init_<feature>()` and each card's in its part of `ClientState`;
  `tests/test_layout.py` fails when another feature reaches in, and lists the
  method calls between features that are allowed, each with its reason.

Building the ROM needs [BlocksDS](https://blocksds.skylyrac.net/docs/setup/);
`dsterm build` finds it through `$BLOCKSDS` or `/opt/blocksds/core`, or in a
Wonderful toolchain under `$WONDERFUL_TOOLCHAIN`, `~/wonderful`,
`/opt/wonderful` or `/usr/local/wonderful`.
