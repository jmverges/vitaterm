# SEAL: every frame encrypted and signed after the handshake

The handshake proves the card holds the pairing key -- the server sends a
fresh nonce, the card answers with HMAC-SHA256(key, nonce) -- but that proves
nothing about what follows.  SEAL covers everything after it: from AUTH_OK on,
every frame in both directions is **encrypted** with ChaCha20 and carries an
8-byte **tag** over what was encrypted.

* Nobody on the network can read the session: not the terminal's text, not
  the keys typed into it, not the clipboard, the pictures or the bottom
  screen.  On WPA2 that keeps out everyone else who knows the network's
  password; on an open network, the only kind a DS or DS Lite can join, it
  keeps out everyone in radio range.
* Nobody can type into it either: a frame that was forged, replayed,
  reordered, dropped or altered fails its tag and ends the connection, and
  nothing in it is decrypted or acted on.

This is SEAL v3.  v2 (below, under History) signed every frame but encrypted
nothing.

## What is still visible

Encryption hides what the frames say, not that they exist.  Someone on the
network can still see:

* that a DS is talking to this computer, and the computer's name in its
  answer to a discovery probe;
* the handshake: the HELLO's capability list and device id, the nonces and
  the AUTH proof, none of which is reusable;
* each frame's type and length, which are in the header in the clear.  They
  show *when* you type and roughly how much of the screen changes, never
  what.

## Negotiation, such as it is

* A card announces the HELLO tokens `seal` and `seal3`, and `cn=` with 32
  lowercase hex digits, its nonce for this connection (below).
* The server requires `seal3` and a well-formed `cn`, and answers the AUTH
  with `AUTH_OK` whose payload is the five bytes `seal3`.  A HELLO without
  them is a card from before encryption, or one with the words taken out on
  the way; the server refuses it with an ERROR that says to copy the current
  `dsterm.nds` to the card, then closes.
* A card requires `AUTH_OK` `seal3`.  `seal` is a server from before
  encryption, which the card names ("the computer runs an older dsterm:
  update it there"); anything else is refused too.  `seal` stays in the
  card's HELLO for exactly that: an old server answers it in a way the card
  can explain.  The card never sends anything after such an AUTH_OK.
* Neither refusal is AUTH_FAIL, which means a wrong key.  A version mismatch
  never costs a pairing: the card keeps its profile, keeps retrying as it
  does after any refusal, and connects by itself once both ends are updated.
* There is no fallback, in either direction.  An earlier version fell back to
  unsealed frames when either side was older -- and on an open network an
  older peer and an attacker who deleted the word look exactly alike, so the
  fallback was a way to switch SEAL off.  It is gone, and v3 adds none.
* Pairing frames (PAIR_*), HELLO, NONCE, AUTH and AUTH_OK / AUTH_FAIL are
  never sealed, because they all come before AUTH_OK.  After it nothing is
  exempt by type: a frame of any of those types sent after AUTH_OK (the
  server accepts a later HELLO) is sealed like every other, and a second
  AUTH on a sealed connection ends it.
* The frame version byte stays 1, so a card from before encryption can still
  read and show the server's ERROR.

## Keys

    base  = HMAC-SHA256(key, "dsterm seal v3" || nonce || SHA-256(hello))
    k_mac = HMAC-SHA256(base, "mac")       the tags, both directions
    k_cs  = HMAC-SHA256(base, "enc C")     the cipher, card to server
    k_sc  = HMAC-SHA256(base, "enc S")     the cipher, server to card

`key` is the 16-byte key the AUTH was computed with: the device's own key
for a paired card, or the legacy PSK.  These are the keys pairing already
made; v3 adds no key material and changes no key file, so every pairing made
before it keeps working.  `nonce` is the 32 bytes of this connection's
NONCE, and `hello` is the HELLO payload exactly as the card sent it -- on
the server, the last HELLO before the AUTH.

* **The HELLO is in the key** because it travels before anything is signed:
  one edited on the way (a capability removed, a grid changed, the card
  nonce swapped) leaves the two ends with different keys, and the first
  sealed frame ends the connection.
* **Both sides add a fresh nonce.**  The server's NONCE is fresh per
  connection.  The card's `cn` is too, and it is what makes a replay useless
  against the card.  Without it, someone who recorded a connection could
  play the old NONCE and the old server frames back at the card: the key,
  the nonce and the HELLO would all match, so the card would rebuild the old
  keys and encrypt your new keystrokes under the old keystream, and the two
  recordings XORed together would give away what you typed.  With `cn`, the
  replay gives the card new keys, and the first replayed frame fails its
  tag.  `tests/test_encryption.py` makes that recording and plays it back.
* **One cipher key per direction.**  The card's frame 7 and the server's
  frame 7 share a sequence number; with their own keys they never share a
  keystream.

### The card's nonce

`cn` is 16 bytes from the boot menu's entropy pool (`entropy.h`), taken
afresh at every connection after stirring in the clock, the frame count, the
current scanline and a count of this boot's connections.  The pool already
holds the Wi-Fi MAC, the scan's signal strengths and the timing of the join,
and every button press and touch since power-on.

It need not be secret, only never repeated.  The count separates connections
within one boot.  Between boots the clock moves on.  On a console whose
clock battery has died, the clock restarts at the same time at every
power-on, but the frame and scanline at which the join completes, and the
signal strengths in the scan, still differ from boot to boot.  Autoconnect
reaches this with no button pressed, and needs nothing more.  Nothing is
written to the SD card per connection.

## Frames

A sealed frame is an ordinary frame whose payload is encrypted and whose
length field counts eight more bytes, which are the tag, after the payload:

    'D' 'S' version type  len_hi len_lo   ciphertext ...   tag[8]
                          (len = payload length + 8)

    ciphertext = payload XOR ChaCha20(k_dir, nonce = 0x00 * 8 || seq, counter = 0)
    tag        = HMAC-SHA256(k_mac, dir || seq || header || ciphertext)[0:8]

* ChaCha20 is RFC 8439's: 20 rounds, a 256-bit key, a 96-bit nonce and a
  32-bit block counter from 0.  `k_dir` is `k_cs` for what the card sends
  and `k_sc` for what the server sends.
* `dir` is one byte: `C` (0x43) for card to server, `S` (0x53) for server to
  card, so a frame cannot be reflected back at its sender.
* `seq` is a 32-bit big-endian counter per direction, starting at 0 for the
  first frame after AUTH_OK and incremented by one per frame.  It is not
  sent; each side keeps its own, which is what makes a dropped, repeated or
  reordered frame fail.  It is also the cipher's nonce, so one key never
  sees the same nonce twice.  Neither side seals or opens frame
  2^32 - 1: the connection ends first (the card reconnects in a second), so
  a nonce never comes round again.
* `header` is the six header bytes exactly as sent (with the +8 length).
* A stream cipher adds nothing to the length.  A sealed frame is exactly as
  long as a v2 one, so every message the server sends still fits one
  536-byte TCP segment (`tests/test_protocol.py`).

The receiver checks the tag in constant time over the ciphertext **before**
it decrypts a byte or acts on the type (the card's frame reader does check
that the type is one it knows, and refuses the frame -- ending the
connection -- if not).  It decrypts only a frame that passed, and strips the
tag, so everything above the framing sees the same payloads as before.  A
wrong tag ends the connection; the card reconnects, which costs a second.

Eight bytes of HMAC is 64 bits of tag: a forger has one guess per connection
(a miss ends it) and fresh keys every time.

Why these choices:

* **ChaCha20, not AES.**  A DS has no AES hardware.  Software AES is slower
  on an ARM9, and its table lookups leak timing.  ChaCha20 is additions,
  rotations and XORs on 32-bit words: constant time with no effort, and
  cheaper per byte than the SHA-256 the card already runs for the tag.
* **Encrypt-then-MAC with the existing tag, not Poly1305.**  Poly1305's
  16-byte tag would add eight bytes to every frame and break the 536-byte
  budget, and it would be new code on the card.  HMAC-SHA256 over the
  ciphertext, checked before decrypting, gives the same guarantee with the
  code and the sizes that were already there.

## Cost

Per frame each way: one ChaCha20 pass over the payload and one HMAC-SHA256.
Per connection: one SHA-256 of the HELLO, four HMACs and one entropy take,
well under a millisecond's worth of the DS's time.  There is no public-key
operation per connection, which is why reconnecting is as quick as before.

The server's ChaCha20 is pure Python (`dsterm/chacha20.py`, no compiled
dependency): about 0.4 ms for a full 536-byte frame on an Apple silicon Mac.
The link carries tens of kilobytes a second at most, and a picture crosses in
chunks of a kilobyte, so no single frame holds the loop.  What it costs in
the server's CPU under load is in [PERFORMANCE.md](PERFORMANCE.md).

The camera's frames are the big ones on the card, so they were estimated from
the instruction count by `tools/camera_budget.py`.  The tag is SHA-256:

* A preview frame (128x96 greys, 6,147 bytes) is 100 SHA-256 blocks,
  estimated at 9.4-12.7 ms at 67 MHz in the shipped Thumb build
  (`sha256_compress` is about 6,250 instructions a block; the ARM state build
  would be about 3,360).  ChaCha20 adds 97 blocks of 64 bytes, each about
  2,500 instructions in the same build (`chacha20_block` and its XOR):
  about 40% on top, for an estimated 13-18 ms a preview frame, 4-5% of the
  ARM9 at 3 fps.
* A FULL picture is 9,000 SHA-256 blocks and 7,200 ChaCha20 blocks, about
  1.1-1.5 s of CPU spread over the upload.

The console prints the measured figure, tag and cipher together, in its
STATUS log the first time the camera opens (`seal: N.N ms/frame`), which is
the figure to believe.

## Not yet

* **Forward secrecy.**  The keys come from the pairing key, so someone who
  recorded your traffic *and later* stole the key off your SD card (or the
  computer) could decrypt the recording.  A fresh X25519 exchange per
  connection would close that, but the card's X25519 is built for once per
  pairing, and per connection it would cost an estimated second or more of
  every reconnect (not yet measured).  Treat the SD card like a password,
  as [SECURITY.md](../SECURITY.md) says.
* Hiding frame types and lengths, and the discovery answer.

## History

* **v2** signed every frame (the same HMAC tag, over the plaintext) under
  `HMAC(key, "dsterm seal v2" || nonce || SHA-256(hello))`, with HELLO token
  and AUTH_OK payload `seal`.  Integrity, not secrecy: the text crossed the
  air in the clear.
* **v3** encrypts, and adds the card's nonce, without which encryption would
  have been replayable into a reused keystream.
