#!/bin/sh
# The card zip for a release: unzip onto the SD card root and boot.
#
#   sh tools/release_zip.sh v1.0.0      (inside the BlocksDS container, or with
#                                        BLOCKSDS pointing at a toolchain)
#
# dsterm.nds is the 42x24 build every card should start with; the 64x32 build
# is alongside for anyone who wants the smaller font, and needs the server run
# with `--grid 64x32` (in ~/.config/ds-claude-terminal/serve.conf) to match.
set -eu
version="${1:-dev}"
here="$(cd "$(dirname "$0")/.." && pwd)"
out="$here/dist/dsterm-$version"
rm -rf "$out" && mkdir -p "$out/dsterm"

# dsterm.nds is the boot image the Makefile refreshes from whatever it just
# built, so copying it after each build is copying exactly that build.
make -C "$here/dsclient" GRID_COLS=64 GRID_ROWS=32 >/dev/null
cp "$here/dsclient/dsterm.nds" "$out/dsterm/dsterm-64x32.nds"
make -C "$here/dsclient" >/dev/null
cp "$here/dsclient/dsterm.nds" "$out/dsterm.nds"

cat > "$out/dsterm/README.txt" <<TXT
DS Claude Terminal $version

1. Copy dsterm.nds (and this dsterm folder) to the root of the SD card.
2. Start dsterm.nds from your launcher:
     DSi / DSi XL     Unlaunch or TWiLight Menu++
     3DS / 2DS        TWiLight Menu++ (Run in: DSi mode, the default)
     DS / DS Lite     your flashcart's menu (open Wi-Fi networks only)
3. On the computer, run \`dsterm pair\`. On the DS, choose "Find servers on
   this network", pick your computer, then "Pair". If both screens show the
   same six-digit code, press A on the DS and type y on the computer.

The computer needs the server: pipx install the wheel from the same release,
then run \`dsterm setup\`.

Guides for every step: the project's page (the README's Setup section).
TXT

# Written outside the folder being summed, so the list never includes itself.
(cd "$out" && find . -type f | sort | xargs sha256sum > "$out.sums")
mv "$out.sums" "$out/dsterm/SHA256SUMS"
(cd "$here/dist" && rm -f "dsterm-$version.zip" && \
 cd "dsterm-$version" && zip -qr "../dsterm-$version.zip" .)
echo "$here/dist/dsterm-$version.zip"
