// The desktop's clipboard, arriving on the handheld.
//
// The desktop is often one peer of a LAN clipboard-sync tool -- typically
// encrypted, peer to peer -- spanning several machines. The DS does not join
// that network: it has no business carrying X25519 and ChaCha20 and an mDNS
// responder, and it does not need to, because the machine running the dsterm
// server is already a peer. What it copies is already on that machine. This
// is the last hop.
//
// One direction only. Nothing goes up from the handheld, and not for want of a
// use: the handshake is one-way -- the card announces its HELLO tokens and the
// server announces nothing back -- so a new client->server type would break a
// new card against an old server. That stays off the table until the server
// learns to announce its own capabilities.
//
// -- what this is not allowed to become ------------------------------------
//
// A clipboard is server-supplied text, and now it is server-supplied text
// originating on *any machine synced to that clipboard*, which is a wider trust
// boundary than the one server this card talks to. So the rule snippets.h
// states applies here with more force, not less: text that arrives here is
// loaded into the composer and needs a second deliberate press. There is no
// path from a received clipboard to the PTY that does not go through a person
// reading it. God mode does not reach this -- it gates the PROJECT page's
// discovered actions, which are a different thing.
//
// The link is also worth naming plainly. dsterm frames are magic, version,
// type, length, payload -- the HMAC is in the AUTH handshake and nothing after
// it is encrypted. A clipboard-sync tool encrypts precisely so a clipboard is
// not readable on the LAN, so this hop is the weak one. That is why the server
// ships the feature off by default; the card cannot enforce that and does not
// pretend to.
//
// Pure C with no libnds dependency: the staging machine, the invariant check,
// and the wrapping are all host-tested. The wire layout is mirrored in
// dsterm/protocol.py and pinned by the INTEROP clip vector, which exists
// because a comment saying two encoders agree is what once let a wire-format
// bug reach hardware.

#ifndef DSTERM_CLIP_H
#define DSTERM_CLIP_H

#include <stddef.h>
#include <stdint.h>

// Mirrored in dsterm/protocol.py. The chunk cap is 192 rather than the 521 the
// 536-byte MSS ceiling would allow, because this buffer was sized for 192 and
// one side raising a shared constant on its own is how halves drift.
#define CLIP_CHUNK_MAX 192
#define CLIP_TEXT_MAX 1024
#define CLIP_CHUNKS_MAX 6      // ceil(CLIP_TEXT_MAX / CLIP_CHUNK_MAX)

// The clipboard was longer than CLIP_TEXT_MAX; `total` says how much longer.
#define CLIP_FLAG_TRUNCATED 0x01
// At least one byte changed in the fold to printable ASCII. Said out loud for
// the reason the DO tab drops an over-long action instead of shortening it: a
// paste that silently became different text does something other than what was
// copied.
#define CLIP_FLAG_TRANSLITERATED 0x02
// The copy was concealed -- a password manager marked it -- and the server
// declined to send it. Only ever on an empty frame, and deliberately distinct
// from a genuinely empty clipboard: a blank pane after copying a password
// invites copying it again.
#define CLIP_FLAG_WITHHELD 0x04

// Newline survives the server's fold; every other control byte is refused at
// both ends.
#define CLIP_LF 0x0A

// One display line of the preview. The lower screen is 256 pixels wide at
// every top-screen geometry, so this is 42 columns of 6px whether the terminal
// is 42x24 or 64x32.
#define CLIP_LINE_COLS 42

typedef struct {
    // The committed snapshot: what the card is willing to show and paste.
    char text[CLIP_TEXT_MAX + 1];
    uint16_t len;
    // Length of the converted text before truncation. Not the raw clipboard
    // length -- the server folds to ASCII and normalises newlines first, and
    // measuring after that is what makes the invariant below checkable.
    uint32_t total;
    uint8_t flags;
    uint8_t have;          // a complete snapshot has arrived at least once

    // The snapshot in flight. Same discipline as SESSIONS and PROJECT: stage
    // the lot, commit only when it is complete, never show half an update.
    char stage[CLIP_TEXT_MAX];
    uint16_t stage_len;
    uint32_t stage_total;
    uint8_t stage_count;
    uint8_t stage_seen;
    uint8_t stage_seq;
    uint8_t stage_flags;
    uint8_t staging;
} clip_t;

void clip_init(clip_t *clip);

// Feed one decoded MSG_CLIP frame. Returns 1 on the frame that completes a
// snapshot, at which point the staged text has been committed and the panes
// are worth redrawing.
//
// `count` of zero is an empty frame: the clipboard is empty, or `flags` says
// it was withheld. Either way it commits immediately -- "nothing to show" is a
// complete answer, and one the panes render differently from silence.
//
// A frame whose seq differs from the one in flight discards the staged set,
// and so does index 0, so a snapshot interrupted by a newer copy can never
// splice two clipboards together. Both rules are here rather than one, because
// two rules that must agree are cheaper to keep agreeing than to reconcile.
//
// The completing frame is checked against the invariant the wire is built to
// carry: the bytes actually received must equal min(total, CLIP_TEXT_MAX). A
// snapshot that fails it is discarded whole. That is the one assertion that
// would catch the two encoders drifting apart, which is the failure this
// protocol's history says to expect.
int clip_wire_chunk(clip_t *clip, unsigned count, unsigned index,
                    unsigned seq, unsigned flags, uint32_t total,
                    const char *bytes, unsigned len);

// The link went away. Forget the clipboard entirely, staged and committed
// both. A clipboard is the most perishable thing this device displays -- it is
// whatever somebody last copied, on a machine this card can no longer hear
// from -- and offering a stale one to paste is worse than offering none.
void clip_wire_reset(clip_t *clip);

static inline int clip_have(const clip_t *clip) { return clip->have != 0; }
static inline int clip_withheld(const clip_t *clip)
{
    return (clip->flags & CLIP_FLAG_WITHHELD) != 0;
}
// Is there something a paste could actually put in the composer?
static inline int clip_pastable(const clip_t *clip)
{
    return clip->have && clip->len > 0;
}

// Can this clipboard be typed straight at the PTY without submitting itself?
//
// Only when it holds no newline. The rule this whole module is written under
// is that server-supplied text never executes on its own, and text with no
// newline in it does not: it lands on the prompt line and waits for the person
// to press the submit key. One that contains a newline would submit at that
// newline, unread, which is the exact thing the rule forbids -- so it goes
// through the composer instead and gets looked at first.
//
// A withheld or empty clipboard answers 0: there is nothing to paste either
// way, and the caller should say which.
int clip_paste_is_inert(const clip_t *clip);

// One finished line saying what is on the clipboard and what was done to it:
// "1.0K of 4.1K, shortened" or "62 bytes" or "withheld: concealed copy".
// Written into `out`, always NUL-terminated, never longer than CLIP_LINE_COLS.
void clip_summary(const clip_t *clip, char *out, size_t out_len);

// Wrap the text into display lines for the preview pane. Newlines break a
// line; anything longer than CLIP_LINE_COLS is hard-wrapped rather than
// word-wrapped, because a clipboard is as likely to hold a path or a base64
// blob as a sentence, and word-wrapping those reads worse than not.
//
// Returns how many lines were written, capped at `max`. A line that had to be
// cut is still exact: this never inserts a character the clipboard did not
// contain, so what you read is what you would paste.
unsigned clip_wrap(const clip_t *clip, char lines[][CLIP_LINE_COLS + 1],
                   unsigned max);

#endif  // DSTERM_CLIP_H
