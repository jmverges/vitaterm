#!/bin/bash
# Host tests for the platform-independent half of the DS client.
#
# The DS toolchain is not needed: framing, bounds checking, and HMAC are plain C
# with no libnds dependency, and they are exactly the parts where a bug is either
# a security hole or an unreproducible glitch on a handheld.
set -euo pipefail
cd "$(dirname "$0")"

mkdir -p build

# ASAN's runtime is incompatible with some hosts -- Apple clang 17 on macOS 26
# traps at startup (SIGILL, empty stderr) right after "libc interceptors
# initialized", even for a `return 0` binary. Probe it once so we do not either
# fail every run or -- worse -- build with ASAN, have the binary die before
# main(), and read the empty tee'd output as an all-green pass. A working host
# keeps the full check; a broken one degrades to UBSAN-only and says so loudly.
#
# The probe must bound its own runtime. That same host later stopped trapping
# and started *spinning*: build/asan_probe -- a `return 0` program -- never
# exited, so the `&&` waited forever and the whole script hung with no output,
# which reads as a slow build rather than a broken one. Four of them were still
# parked when this was found. A probe that can hang is not a probe, and a host
# where a `return 0` binary cannot exit in five seconds is a broken ASAN host by
# definition -- exactly the case this degrades to UBSAN for.
asan_works() {
    printf 'int main(void){return 0;}' \
        | gcc -x c -fsanitize=address -o build/asan_probe - 2>/dev/null || return 1

    ./build/asan_probe >/dev/null 2>&1 &
    local probe=$!
    # Watchdog rather than a kill -0 poll: a child that has exited is a zombie
    # until it is waited for, so polling would report it alive for the full
    # timeout and call a working host broken.
    ( sleep 5; kill -9 "$probe" 2>/dev/null ) &
    local guard=$!
    local rc=0
    wait "$probe" 2>/dev/null || rc=$?
    kill "$guard" 2>/dev/null || true
    wait "$guard" 2>/dev/null || true
    return "$rc"
}

# The other half of "this host cannot see everything", and the one that has
# actually cost time.
#
# `gcc` here may be Apple clang wearing the name. clang does not implement
# -Wformat-truncation or -Wformat-overflow at all, so a truncating snprintf
# that gcc refuses outright compiles clean on this machine. That split has now
# hidden a broken build from this host three times -- most recently a tip that
# passed 13,368 checks here and did not produce a binary on the gate machine.
#
# There is nothing to degrade to: the diagnostics do not exist in this
# compiler. So this only says so, loudly, the way the ASAN probe does. A green
# run here is not a green build; a Linux host with real gcc is the authority.
if ! gcc --version 2>/dev/null | head -1 | grep -qiE '^(gcc|.*Free Software Foundation)'; then
    echo "warning: this host's gcc is $(gcc --version 2>/dev/null | head -1)" >&2
    echo "warning: gcc-only diagnostics (-Wformat-truncation, -Wformat-overflow) are NOT checked here -- a clean run here is not a clean build under real gcc" >&2
fi

# The sanitizers must be fatal, not advisory. UBSAN's default is to print a
# finding and carry on, and the suite's "N checks, 0 failures" line counts
# only its own asserts -- so an undefined-behaviour finding used to print and
# still report green. That is how an out-of-bounds write in focus.c reached
# the card: the WSL run printed it, and the suite still passed.
#
# -fno-sanitize-recover=all makes the instrumented code abort on the first
# finding, and it travels with the binary rather than depending on the
# environment surviving into the test process. The *_OPTIONS below are belt
# and braces on top.
SAN=(-fsanitize=undefined -fno-sanitize-recover=all)
if asan_works; then
    SAN=(-fsanitize=address,undefined -fno-sanitize-recover=all)
else
    echo "warning: ASAN unavailable on this host (probe failed or hung); using UBSAN only" >&2
fi

# Up to two suites, from two source lists.  The terminal's (tests/sources.txt)
# is linked with ext_none.c, so its tests see exactly the build that ships when
# nothing is attached.  An extension's (tests/sources.ext.txt) names its own
# test entry point, the extension implementation that replaces ext_none.c, and
# its sources; it is built on top of the terminal's list.  Neither list is a
# literal here, so an extension adds a suite without editing this script.
CORE_SOURCES=$(grep -v '^#' tests/sources.txt | tr '\n' ' ')
# The second list is usually absent -- nothing attached -- and under pipefail a
# missing file would otherwise end the script here, silently.
EXT_SOURCES=$({ grep -v '^#' tests/sources.ext.txt 2>/dev/null || true; } | tr '\n' ' ')

build_suite() {
    local out="$1"; shift
    # shellcheck disable=SC2086 -- the lists are whitespace-separated on purpose
    gcc -std=c11 -Wall -Wextra -Werror -O1 "${SAN[@]}" -o "$out" "$@"
}

# pipefail (set above) is what makes this honest: the binary's exit code, not
# tee's, decides whether the script continues. A sanitizer abort or a failing
# CHECK now stops the run instead of teeing empty output and "passing".
#
# The environment pins make the runtime abort (exit 1) on the first finding
# even if the binary was built without -fno-sanitize-recover; ASAN's default
# exit code is also pinned, because some hosts report a sanitizer failure as
# exit 0.
run_suite() {
    UBSAN_OPTIONS=halt_on_error=1:abort_on_error=1:exitcode=1 \
    ASAN_OPTIONS=abort_on_error=1:exitcode=1 \
        "./$1" | tee "$2"
}

# The grid geometry is a build-time contract with the server, and the whole
# suite must hold at the zoomed-out shape too: PROTO_COLS/ROWS drive every
# cell array on a device with no MMU.  A second host binary at 64x32 is the
# cheapest place an out-of-bounds write from a stale 42x24 assumption shows
# up before the card does.
for geom in 42x24 64x32; do
    # The empty-array expansion below is the bash 3.2 (macOS) idiom: under
    # set -u a plain "${defs[@]}" of an empty array is an unbound variable.
    defs=()
    [ "$geom" = 64x32 ] && defs=(-DPROTO_COLS=64 -DPROTO_ROWS=32)
    echo
    echo "=== the terminal's suite, $geom ==="
    # shellcheck disable=SC2086
    build_suite "build/test_core_$geom" ${defs[@]+"${defs[@]}"} \
        tests/test_core.c tests/test_connect.c tests/test_sound_snes.c tests/test_remote.c tests/test_seal.c tests/test_camera.c tests/test_fuzz.c tests/support.c source/ext_none.c \
        $CORE_SOURCES
    run_suite "build/test_core_$geom" "build/test_core_$geom.txt"
    if [ -n "$EXT_SOURCES" ]; then
        echo
        echo "=== the extension's suite, $geom ==="
        # shellcheck disable=SC2086
        build_suite "build/test_ext_$geom" ${defs[@]+"${defs[@]}"} \
            tests/support.c $CORE_SOURCES $EXT_SOURCES
        run_suite "build/test_ext_$geom" "build/test_ext_$geom.txt"
    fi
done

# Cross-check against the server's own implementation. Two implementations of
# one protocol drift; a mismatch here is a DS that cannot authenticate.
if [ -x ../.venv/bin/python ]; then
    echo
    echo "=== interop vs the Python server ==="
    (cd .. && PYTHONPATH=. .venv/bin/python tools/crosscheck.py < dsclient/build/test_core_42x24.txt)
else
    echo
    echo "(skipping interop check: ../.venv not found)"
fi
