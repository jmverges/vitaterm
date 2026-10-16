#include "log.h"

#include <stdio.h>
#include <string.h>

// The last LOG_ROWS lines. Once `count` reaches LOG_ROWS every append shifts
// the oldest line out; `count` then stays fixed and indexing is unchanged.
static char ring[LOG_ROWS][LOG_COLS + 1];
static int count = 0;

void log_init(void)
{
    memset(ring, 0, sizeof(ring));
    count = 0;
}

// line is exactly LOG_COLS + 1 bytes: the ring is written whole, never
// byte-granular, so a blank line is indistinguishable from a short one and no
// entry ever carries stale bytes past its NUL.
static void append_line(const char *line)
{
    if (count < LOG_ROWS) {
        memcpy(ring[count], line, LOG_COLS + 1);
        count++;
    } else {
        memmove(ring[0], ring[1], (LOG_ROWS - 1) * (LOG_COLS + 1));
        memcpy(ring[LOG_ROWS - 1], line, LOG_COLS + 1);
    }
}

void log_vprintf(const char *fmt, va_list ap)
{
    char line[LOG_COLS + 1];
    memset(line, 0, sizeof(line));
    vsnprintf(line, sizeof(line), fmt, ap);   // truncated to LOG_COLS, NUL-padded
    append_line(line);
}

void log_blank(void)
{
    char line[LOG_COLS + 1];
    memset(line, 0, sizeof(line));
    append_line(line);
}

void log_printf(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    log_vprintf(fmt, ap);
    va_end(ap);
}

int log_count(void)
{
    return count;
}

const char *log_get(int i)
{
    if (i < 0 || i >= count) return "";
    return ring[i];
}
