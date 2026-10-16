// How long to wait between reconnection attempts.
//
// Its own file, with no libnds dependency, so the schedule can be tested on the
// dev machine. A wrong backoff is invisible until it matters and then it is one
// of two bad things: hammering a dead address sixty times a second, or making a
// routine server restart look like the handheld has crashed.

#ifndef DSTERM_BACKOFF_H
#define DSTERM_BACKOFF_H

// Frames at 60Hz.
//
// The first wait is short because the overwhelmingly common cause is the server
// being restarted, which takes about a second. The ceiling is fifteen seconds:
// long enough that a console left on with the PC off is not busy, short enough
// that walking back into the room and turning the PC on reconnects without
// anyone touching the handheld.
#define BACKOFF_FIRST 30
#define BACKOFF_MAX   900

// Wait before attempt `attempt` (1-based). Doubles, then holds at the cap.
unsigned backoff_frames(unsigned attempt);

#endif  // DSTERM_BACKOFF_H
