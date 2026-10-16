#include "backoff.h"

unsigned backoff_frames(unsigned attempt)
{
    if (attempt == 0) return BACKOFF_FIRST;

    // Shift rather than repeated multiplication, and bail before the shift
    // width reaches the width of the type: at 60Hz the cap is reached by the
    // sixth attempt, so anything beyond that is arithmetic that only exists to
    // overflow. A console left on overnight against a powered-off PC reaches
    // attempt numbers in the thousands.
    if (attempt >= 32) return BACKOFF_MAX;

    const unsigned long long scaled = (unsigned long long)BACKOFF_FIRST << (attempt - 1);
    return scaled >= BACKOFF_MAX ? BACKOFF_MAX : (unsigned)scaled;
}
