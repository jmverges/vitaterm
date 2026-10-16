# Snippets over the wire — approved; the argument corrected

> **Design notes.** Written while this part was being built and kept for
> the reasoning in it. Details may describe an earlier version; the code
> and the guides in `docs/` describe the current one.

Status: approved for implementation. The wire format below is the contract;
the two halves must not drift. The earlier draft argued that a snippet
message was safe because the server could type the same text into the PTY
itself. That reasoning is withdrawn: it reasons about the wrong adversary
and would license a direct-send v2 that is genuinely dangerous.

## The threat this has to survive

The link had no encryption and no per-message integrity after the handshake
when this was written. It now has integrity (docs/SEAL.md) for cards and
servers that both speak it, but not encryption, and an older card still
connects without it -- so the argument below still stands. The adversary is not the server — the server
is trusted. The adversary is anyone on the LAN who can inject into the
server->client stream, which today buys them nothing worse than drawing on
the screen. A message that carries snippet text changes that: it lets an
injector plant text that the user's own thumb later fires into the PTY. That
is a real escalation — from "can deceive the display" to "can stage an
action" — and it is exactly the class the no-side-channels rule exists to
catch.

## Why it is still acceptable — the argument to keep

Because picking is not sending. A chosen snippet is loaded into the composer
(main.c: snippet_input -> composer_load), where the body is displayed in
full on the top screen and needs a second, deliberate commit. A planted
snippet cannot fire silently — only deceptively, against a user who is
reading the text first. That is strictly weaker than what an injector can
already do by manipulating the screen the user is reading anyway.

The composer gate is therefore the entire safety case for this feature. A
future change that routes a snippet straight to send deletes the gate and
must fail its test and this doc. There is no v2 that sends directly; a
design that ever needs one needs a per-message integrity scheme first.

## The wire contract

- HELLO token `snippets`, gated exactly like `mode`: the server sends
  nothing to a card that did not announce it, so an old card never halts
  (an old card halts on unknown types). The flag lives on ClientState and
  resets on reconnect; the server re-pushes on every authenticated connect.
- MSG_SNIPPETS = 0x98. M6 SESSIONS now uses 0x97; see
  `docs/design/SESSIONS_WIRE.md` for its coordinated-rollout contract.
- Chunked: one message per snippet, because every server->client message
  must stay under 536 bytes (asserted on real traffic in test_server.py)
  and a whole list can exceed that.
- Payload: `u8 index, u8 total, u8 label_len, label[label_len],
  u16 body_len, body[body_len]` (lengths big-endian). Maximum payload:
  1 + 1 + 1 + 15 + 2 + 255 = 275 bytes.
- Validation, identical on both sides, mirroring dsclient/source/snippets.c:
  total 1..8, index < total, label_len 1..15, body_len 0..255, body content
  printable ASCII plus \n and \t only, and the declared length must equal
  the payload length exactly.
- Partial sets: the client applies the push only when every chunk of the
  set has arrived; the card-file list stands until then. A malformed chunk,
  a duplicate index, or a mismatched total discards the whole in-flight
  push (the card list is unaffected). On completion the pushed list replaces
  the in-RAM list; the card file remains the durable store and the fallback
  when the server sends none.
- No card writes, ever, in v1.

## Tests that carry the case

- Client: decode bounds (every field), the old-card reader regression
  (0x98 must reach the application, not halt it), accumulation (partial,
  out-of-order, completion, discard), and the composer gate: pushed snippets
  can only reach the PTY as the composer's send plan (body chunks plus a
  lone CR), `snippet_load_picked` is the only API that accepts a snippet
  body, and a repo test fails loudly if any line of main.c touches
  snippet items outside the composer load or the label draw path, or if
  snippets.h grows an API the whitelist does not name.

## The split

Server side: MsgType 0x98, the token gate, parser and validator, chunked
encoder, re-push per connect. Client side: decode, bounds, the partial-set
rule, list replacement, card fallback, the composer-gate tests.
