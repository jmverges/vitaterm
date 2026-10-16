"""Create the server's legacy key (`dsterm setup` does this for you).

Consoles pair by comparing a code (`dsterm pair`) and each gets a key of its own, so
this key is only for a card set up by hand from it -- the older way, kept for
cards that still use it. Printed only when run directly."""

from __future__ import annotations

import sys

from .auth import DEFAULT_KEY_PATH, format_for_entry, generate_key, save_key


def main(quiet: bool = False) -> int:
    if DEFAULT_KEY_PATH.exists():
        print(f"a key already exists at {DEFAULT_KEY_PATH}")
        print("delete it first if you want a new one (cards set up with it "
              "must be set up again)")
        return 1
    key = generate_key()
    path = save_key(key)
    print(f"key: written to {path} (mode 600)")
    if quiet:
        # From setup: consoles pair with `dsterm pair`, so printing a
        # key with "enter this on the DS" would send people the long way.
        return 0
    print("\n  " + format_for_entry(key) + "\n")
    print("To set a card up by hand with it, choose \"Type the 32-character key")
    print("instead\" when the DS offers to pair. Pairing (`dsterm pair`)")
    print("is easier. The key never crosses the network: the DS answers a")
    print("challenge with it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
