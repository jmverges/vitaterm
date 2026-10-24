# Claude Terminal for PS Vita

A Vita client for the same `dsterm` server the DS card talks to: Claude Code
runs on your computer, the Vita is its screen and controller.

It is not the DS interface on a Vita. There is one 960x544 screen, a 120x32
terminal in an 8x16 font, the system keyboard for typing, and the camera for
sending Claude a picture. The protocol half (framing, the encrypted link,
pairing, discovery, profiles, sessions, the sounds) is the DS client's own
code in `dsclient/source`, compiled unchanged.

## On the computer

The Vita's terminal is 120x32, so the server has to match:

    dsterm serve --grid 120x32

Then `dsterm pair` when the Vita asks for it.

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

Settings and paired computers live in `ux0:data/dsterm`. Snippets can go in
`ux0:data/dsterm/snippets.txt` (the DS card's format), or the computer pushes
its own.

## Building

With [VitaSDK](https://vitasdk.org) (or the `vitasdk/vitasdk` Docker image):

    cd vita && mkdir build && cd build && cmake .. && make

`tools/genatlas.py` regenerates the terminal font (`src/atlas.h`) and
`tools/genlivearea.py` the LiveArea images; both need Pillow.

A build with `-DVITA_SCRIPT=1` reads timed button presses from
`ux0:data/dsterm/script.txt` (one `frame hex-buttons` per line), for testing in
an emulator that takes no input.

## Fonts

`assets/font.ttf` is DejaVu Sans, and the terminal atlas is drawn from DejaVu
Sans Mono with GNU Unifont for the few glyphs it lacks; both are free to
redistribute under their own licenses (the DejaVu fonts license, and the SIL
OFL / GPL with font exception for Unifont).
