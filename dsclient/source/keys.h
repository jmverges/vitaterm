// Key sequences the handheld sends as a keyboard would.
//
// These live in a header rather than beside their one call site so a host test
// can assert on them. That is not ceremony: the first version of the scroll
// gesture passed a hand-written length of 3 for a 4-byte sequence, the host
// suite had nothing that could see it, and the truncation reached the card.
//
// A CSI without its final byte is worse than a key that does nothing. The
// receiving terminal stays mid-sequence and consumes the next keystroke trying
// to finish it, so the failure presents as the keyboard going dead rather than
// as a scroll that did not work -- which sends anyone debugging it toward the
// input path instead of toward the four bytes that were actually wrong.
#ifndef DSTERM_KEYS_H
#define DSTERM_KEYS_H

// Confirmed against a live Claude Code session; see docs/design/SCROLLBACK_MEASUREMENT.md.
#define SEQ_PAGE_UP   "\x1b[5~"
#define SEQ_PAGE_DOWN "\x1b[6~"

// Taken from the literal so it can never drift from it again.
#define SEQ_PAGE_LEN (sizeof(SEQ_PAGE_UP) - 1)

_Static_assert(sizeof(SEQ_PAGE_UP) == sizeof(SEQ_PAGE_DOWN),
               "page keys must be the same length for SEQ_PAGE_LEN to serve both");

#endif
