// Replay a captured post-auth server byte stream through the card's own
// platform-independent consumption path, under fatal sanitizers.
//
// This is the host-side half of a hardware data-abort investigation: if this
// binary survives the exact bytes the live server sent -- across chunk sizes
// and hundreds of repetitions -- then the parse/apply layer is exonerated and
// the fault must live in code the host cannot compile (OAM, VRAM, IRQ,
// cothread, SD).
//
//   gcc ... -o build/replay_stream tests/replay_stream.c source/proto.c
//       source/snippets.c source/sessions.c source/composer.c
//   ./build/replay_stream /tmp/stream.bin
//
// Not part of run_tests.sh: the stream file is a capture, not a fixture.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../source/proto.h"
#include "../source/sessions.h"
#include "../source/snippets.h"

static uint8_t *stream;
static size_t stream_len;

// The same consumption state main.c keeps, minus everything libnds.
static grid_t grid;
static cursor_t cursor;
static view_pos_t view_pos;
static sessions_t sessions;
static snippets_t snippets;
static uint8_t mode_flags;

static unsigned counts[256];

static void apply(msg_type_t type, const uint8_t *payload, uint16_t len)
{
    counts[(unsigned)type & 0xFF]++;
    switch (type) {
    case MSG_FRAME_FULL:
        if (proto_apply_full(&grid, payload, len) != PROTO_OK) {
            fprintf(stderr, "FULL rejected\n");
            exit(2);
        }
        break;
    case MSG_FRAME_DIFF:
        if (proto_apply_diff(&grid, payload, len) != PROTO_OK) {
            fprintf(stderr, "DIFF rejected\n");
            exit(2);
        }
        break;
    case MSG_SCROLL: {
        int lines = 0;
        if (proto_apply_scroll(&grid, payload, len, &lines) != PROTO_OK) {
            fprintf(stderr, "SCROLL rejected\n");
            exit(2);
        }
        break;
    }
    case MSG_CURSOR:
        if (proto_apply_cursor(&cursor, payload, len) != PROTO_OK) {
            fprintf(stderr, "CURSOR rejected\n");
            exit(2);
        }
        break;
    case MSG_VIEW_POS:
        if (proto_apply_view_pos(&view_pos, payload, len) != PROTO_OK) {
            fprintf(stderr, "VIEW_POS rejected\n");
            exit(2);
        }
        break;
    case MSG_MODE:
        if (proto_apply_mode(&mode_flags, payload, len) != PROTO_OK) {
            fprintf(stderr, "MODE rejected\n");
            exit(2);
        }
        break;
    case MSG_SNIPPETS: {
        unsigned index = 0, total = 0;
        char label[SNIPPET_LABEL_MAX + 1];
        char body[SNIPPET_BODY_MAX + 1];
        size_t label_len = 0, body_len = 0;
        if (proto_apply_snippet(&index, &total,
                                label, sizeof(label), &label_len,
                                body, sizeof(body), &body_len,
                                payload, len) != PROTO_OK) {
            fprintf(stderr, "SNIPPET rejected\n");
            exit(2);
        }
        (void)snippets_apply_pushed(&snippets, index, total,
                                    label, label_len, body, body_len);
        break;
    }
    case MSG_SESSIONS: {
        session_chunk_t chunk;
        if (proto_apply_sessions(&chunk, payload, len) != PROTO_OK) {
            fprintf(stderr, "SESSIONS rejected\n");
            exit(2);
        }
        (void)sessions_apply_chunk(&sessions, &chunk);
        break;
    }
    case MSG_EXTENSION:
        // The terminal hands the body to whatever asked for it and reads
        // none of it; a replay has nothing to check but that the frame
        // itself was sound, and the reader already did that.
        break;
    case MSG_PONG:
    case MSG_ERROR:
        break;
    default:
        // main.c halts on an unknown type; here that is a finding, not a halt.
        fprintf(stderr, "unknown type 0x%02X\n", (unsigned)type);
        exit(2);
    }
}

// One full pass over the stream, delivered in `chunk`-sized recv()s.
static void replay(size_t chunk)
{
    framereader_t reader;
    framereader_init(&reader);
    size_t off = 0;
    while (off < stream_len) {
        size_t n = stream_len - off;
        if (n > chunk) n = chunk;
        size_t space = framereader_space(&reader);
        if (n > space) n = space;
        if (n == 0) {
            fprintf(stderr, "reader wedged with no space\n");
            exit(2);
        }
        if (framereader_feed(&reader, stream + off, n) != PROTO_OK) {
            fprintf(stderr, "feed failed\n");
            exit(2);
        }
        off += n;
        for (;;) {
            msg_type_t type;
            const uint8_t *payload;
            uint16_t len;
            proto_result_t r = framereader_next(&reader, &type, &payload, &len);
            if (r == PROTO_NEED_MORE) break;
            if (r != PROTO_OK) {
                fprintf(stderr, "framereader_next: %s\n", proto_strerror(r));
                exit(2);
            }
            apply(type, payload, len);
        }
    }
}

int main(int argc, char **argv)
{
    if (argc != 2) {
        fprintf(stderr, "usage: %s stream.bin\n", argv[0]);
        return 1;
    }
    FILE *f = fopen(argv[1], "rb");
    if (!f) { perror("open"); return 1; }
    fseek(f, 0, SEEK_END);
    long size = ftell(f);
    fseek(f, 0, SEEK_SET);
    if (size <= 0) { fprintf(stderr, "empty stream\n"); return 1; }
    stream = malloc((size_t)size);
    if (!stream || fread(stream, 1, (size_t)size, f) != (size_t)size) {
        fprintf(stderr, "read failed\n");
        return 1;
    }
    fclose(f);
    stream_len = (size_t)size;

    grid_clear(&grid);
    memset(&cursor, 0, sizeof(cursor));
    memset(&view_pos, 0, sizeof(view_pos));
    sessions_init(&sessions);
    snippets_init(&snippets);
    mode_flags = 0;

    // The MSS the DS reports is 536; 1 byte and 7 bytes force header splits;
    // the whole stream at once is the localhost shape. 200 passes of each is
    // ~4.3 MB through every path, far beyond the seconds the card survives.
    static const size_t chunks[] = {1, 7, 536, 65536};
    for (unsigned pass = 0; pass < 200; pass++) {
        for (unsigned c = 0; c < sizeof(chunks) / sizeof(chunks[0]); c++) {
            replay(chunks[c]);
        }
    }

    printf("replayed %zu bytes x 800 passes clean\n", stream_len);
    printf("frame counts per pass set of 4:\n");
    for (unsigned i = 0; i < 256; i++) {
        if (counts[i]) printf("  0x%02X: %u\n", i, counts[i] / 800);
    }
    free(stream);
    return 0;
}
