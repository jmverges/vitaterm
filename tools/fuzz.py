#!/usr/bin/env python3
"""Run tests/test_fuzz.py again and again with fresh seeds, for as long as
you like, and print the seed of anything that fails so it can be replayed.

    tools/fuzz.py              # ten minutes
    tools/fuzz.py 3600         # an hour
    DSTERM_FUZZ_SEED=1234 DSTERM_FUZZ_SCALE=3 .venv/bin/python -m pytest tests/test_fuzz.py
                               # replay one seed

Each round runs the decoders, the terminal-output grid, and a live server
taking noise from cards (before and after authentication) and from views.
The child under that server never reads its input, so the random keystrokes
go nowhere.
"""

import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def main() -> int:
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 600.0
    python = REPO / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    end = time.monotonic() + seconds
    rounds = failures = 0
    while time.monotonic() < end:
        seed = secrets.token_hex(4)
        env = dict(os.environ, DSTERM_FUZZ_SEED=seed, DSTERM_FUZZ_SCALE="3")
        run = subprocess.run([str(python), "-m", "pytest", "-q", "-p", "no:cacheprovider",
                              "-x", "tests/test_fuzz.py"], cwd=REPO, env=env,
                             capture_output=True, text=True)
        rounds += 1
        if run.returncode != 0:
            failures += 1
            print(f"FAILED with DSTERM_FUZZ_SEED={seed} DSTERM_FUZZ_SCALE=3")
            print(run.stdout[-3000:])
        else:
            print(f"round {rounds}: seed {seed} clean", flush=True)
    print(f"{rounds} rounds, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
