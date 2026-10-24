# vitaterm: the Vita app

How to use it is in the [main README](../README.md); this page is for building
it.

The app talks to the same server the DS card does: Claude Code runs on your
computer, the Vita is its screen and controller.

It is not the DS interface on a Vita. There is one 960x544 screen, a 120x32
terminal in an 8x16 font, the system keyboard for typing, and the camera for
sending Claude a picture. The protocol half (framing, the encrypted link,
pairing, discovery, profiles, sessions, the sounds) is the DS client's own
code in `dsclient/source`, compiled unchanged.

The computer runs `vitaterm`, which is the DS project's `dsterm` with the
Vita's grid (120x32) as its default (`dsterm/vita.py`).

## Buttons

| Button | Does |
| --- | --- |
| Cross | Enter (answers Claude's question when one is open) |
| Circle | Esc (stops Claude) |
| Square | The keyboard: what you type is sent, then Enter |
| Triangle | Menu: Claude sessions, slash commands, snippets, keys, camera, sound, another computer |
| D-pad | Arrow keys |
| L / R | Shift+Tab (Claude's mode) / Tab |
| Left or right stick, or a finger | Scroll back through the session |
| Select | Back to live |
| Start | Camera |
| L+R held | Show the buttons |

Settings and paired computers live in `ux0:data/vitaterm`. Snippets can go in
`ux0:data/vitaterm/snippets.txt` (the DS card's format), or the computer pushes
its own.

## Building

With [VitaSDK](https://vitasdk.org) (or the `vitasdk/vitasdk` Docker image):

    cd vita && mkdir build && cd build && cmake .. && make

`tools/genatlas.py` regenerates the terminal font (`src/atlas.h`) and
`tools/genlivearea.py` the LiveArea images; both need Pillow.

A build with `-DVITA_SCRIPT=1` reads timed button presses from
`ux0:data/vitaterm/script.txt` (one `frame hex-buttons` per line), for testing in
an emulator that takes no input.

## Fonts

`assets/font.ttf` is DejaVu Sans, and the terminal atlas is drawn from DejaVu
Sans Mono with GNU Unifont for the few glyphs it lacks; both are free to
redistribute under their own licenses (the DejaVu fonts license, and the SIL
OFL / GPL with font exception for Unifont).
