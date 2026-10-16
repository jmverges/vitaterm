// HIDE ADDRESSES: for showing this to somebody -- a stream, a video, a photo
// for a post -- without showing where you are.
//
// While it is on, every IPv4 address the card draws has its digits drawn as
// stars, so the layout does not move and it is plain that something was
// hidden: "192.0.2.10:8347" is drawn "***.*.*.**:8347".  A MAC address is
// hidden the same way.  It applies where text is drawn -- the bottom screen,
// the overlays and the two pages -- so the menus, the log, REMOTE and the
// boot screens are all covered by the same few lines, and nothing that is
// stored or sent changes.  The terminal itself is Claude's screen and is
// drawn as it is.
//
// Pure C, host-tested.

#ifndef DSTERM_PRIVACY_H
#define DSTERM_PRIVACY_H

#include <stddef.h>

void privacy_set_hide(int hide);
int privacy_hiding(void);

// Hide every address in `text`, in place.  Does nothing while hiding is off.
void privacy_mask(char *text);

// `text` as it should be drawn: itself while hiding is off, otherwise a
// masked copy in one of a few rotating buffers (so two calls in one
// expression are both safe).  Text longer than a buffer is cut, which no
// line on either screen is.
const char *privacy_text(const char *text);

#endif  // DSTERM_PRIVACY_H
