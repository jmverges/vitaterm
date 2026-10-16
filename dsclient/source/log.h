// Boot log ring buffer for the sub screen.
//
// The sub screen's text console is gone (replaced by a bitmap, see subscreen.c),
// so the log that used to scroll via printf now lives here: a ring of
// LOG_ROWS lines that the sub screen paints from. Pure C with no libnds
// dependency so it can be host-tested -- a wrap bug would silently drop the
// most valuable debugging surface this program has.

#ifndef DSTERM_LOG_H
#define DSTERM_LOG_H

#include <stdarg.h>

#define LOG_ROWS 24
#define LOG_COLS 42          // characters per line, one per 6x8 cell

void log_init(void);

// Append one line. Any format is accepted; the line is truncated to LOG_COLS
// characters, and once the ring is full the oldest line is dropped.
void log_printf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
void log_vprintf(const char *fmt, va_list ap);

// Append an empty line. Separate from log_printf("") so a blank separator
// never reads as a zero-length format string (a compiler warning).
void log_blank(void);

// Number of retained lines, 0..LOG_ROWS.
int log_count(void);

// Line i, oldest (0) to newest (log_count()-1). Out of range returns "".
const char *log_get(int i);

#endif  // DSTERM_LOG_H
