// The PHOTO screen: the DSi's sensor as QUICK's PHOTO row (photo.c).
//
// main.c opens it, hands it each pass of the main loop and every press while
// it is up, passes on the computer's camera messages, and decides where the
// screen goes when it closes.  Everything else -- the sensor, its DMA, the
// viewfinder, the preview's pacing and a picture's upload -- is photo.c's.
#ifndef DSTERM_PHOTO_H
#define DSTERM_PHOTO_H

#include <stdint.h>

#include "proto.h"

// Whether this card has a camera, decided once at boot.
void photo_boot(int available);
int photo_available(void);

// This connection's proof: CAMERA_READY landed, and whether the computer can
// keep pictures.  photo_forget() is the connection ending: the camera closes
// without a word to a link that is gone, and the proof goes with it.
void photo_ready(int can_save);
int photo_proved(void);
int photo_can_save(void);
void photo_forget(void);

// Open from QUICK's PHOTO row (only once proved), and close.  `announce`
// tells the computer the camera is off -- only on a live, proved link.
void photo_open(void);
void photo_close(int announce);

// The screen's state, for what main.c does around it.
int photo_is_open(void);
int photo_uploading(void);         // a picture's chunks are going out
int photo_saving(void);            // waiting for PHOTO_SAVED

// One pass of the main loop while open: the transfers, the preview on the
// wire, the upload.
void photo_tick(unsigned long *tx);

// A press while open.  The camera owns every control while it is up; this
// says whether to stay or where to go, and main.c goes there (after closing).
typedef enum {
    PHOTO_STAY = 0,
    PHOTO_EXIT_QUICK,      // back to the PHOTO row
    PHOTO_EXIT_CONTROL,
    PHOTO_EXIT_NEXT,       // SELECT: the next screen
} photo_exit_t;
photo_exit_t photo_input(unsigned raw, unsigned edge);

// CAMERA_ACK.  0 means the computer lost count of an upload: reconnect.
int photo_ack(uint8_t kind, uint32_t value);

// PHOTO_SAVED, in turn.  1 is saved: the caller records the id, closes the
// camera and composes the caption.  0 is a refusal, already on the strip.
int photo_saved_ok(const photo_saved_t *saved);

// The viewfinder's moment in frame_wait(): a fresh frame into the rows the
// strips do not own.
void photo_blit(void);

#endif  // DSTERM_PHOTO_H
