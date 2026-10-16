#ifndef DSTERM_SNIPPETS_H
#define DSTERM_SNIPPETS_H

#include <stddef.h>

#include "composer.h"

#define SNIPPETS_MAX 8
#define SNIPPET_LABEL_MAX 15
// A snippet body's own cap, deliberately not the composer's.
//
// These shared one constant until the composer had to hold a pasted desktop
// clipboard and grew to 1024. A snippet is a line somebody typed into a text
// file on the card, or a short push from the server; it has never wanted a
// kilobyte, and following the composer up would have cost eight kilobytes of
// static RAM and widened what the wire accepts for no reason anybody asked
// for. Two different questions that happened to have the same answer once.
#define SNIPPET_BODY_MAX 255
#define SNIPPETS_FILE_MAX 4096

typedef struct {
    char label[SNIPPET_LABEL_MAX + 1];
    char body[SNIPPET_BODY_MAX + 1];
} snippet_t;

typedef struct {
    snippet_t items[SNIPPETS_MAX];
    int count;
} snippets_t;

void snippets_init(snippets_t *s);
int snippets_parse(const char *text, snippets_t *s, char *err, size_t err_len);
int snippets_load(const char *path, snippets_t *s, char *err, size_t err_len);

// Apply one chunk of a pushed snippet set (MSG_SNIPPETS, docs/design/SNIPPETS_PUSH
// .md). Returns 1 when the set is complete and the in-RAM list has been
// replaced, 0 while chunks are still accumulating, and -1 when the chunk is
// invalid -- in which case the whole in-flight push is discarded and the
// current list is untouched. The card-file list therefore stands until a
// complete, valid set arrives; the pushed list is RAM-only, and the card
// file remains the durable store and the fallback.
int snippets_apply_pushed(snippets_t *s, unsigned index, unsigned total,
                          const char *label, size_t label_len,
                          const char *body, size_t body_len);

// The only way a snippet body may enter the send path: load it into the
// composer, where it is displayed in full and needs a second, deliberate
// commit. Picking is not sending -- that is the entire safety case for
// pushed snippets (docs/design/SNIPPETS_PUSH.md), and a direct-send variant is
// forbidden: test_repo.py fails loudly if one appears. Returns 1 on success.
int snippet_load_picked(const snippets_t *s, int pick, composer_t *c);

#endif
