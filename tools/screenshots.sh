#!/bin/bash
# Photograph every screen of the card with the card's own drawing code.
#
#   tools/screenshots.sh            # writes docs/screens/*.png
#
# Compiles render.c and subscreen.c against dsclient/tests/shot/nds.h (two
# arrays standing in for VRAM) together with the pure modules, runs
# dsclient/tests/screens.c, and converts its PPMs to PNG with tools/ppm2png.py
# (standard library only, and no metadata in the files).
set -euo pipefail
cd "$(dirname "$0")/../dsclient"

mkdir -p build/screens ../docs/screens
CORE=$(grep -v '^#' tests/sources.txt | tr '\n' ' ')
# shellcheck disable=SC2086
gcc -std=c11 -O1 -Wall -Wextra -Werror -Itests/shot \
    -o build/screens/shoot tests/screens.c source/render.c source/subscreen.c \
    source/ext_none.c $CORE
(cd . && ./build/screens/shoot) > build/screens/list.txt

while read -r ppm; do
    name=$(basename "$ppm" .ppm)
    python3 ../tools/ppm2png.py "$ppm" "../docs/screens/$name.png"
    echo "docs/screens/$name.png"
done < build/screens/list.txt
