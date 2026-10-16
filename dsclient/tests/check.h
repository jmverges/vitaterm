// The one assertion every host test uses, and the two counters behind it.
//
// Shared by the terminal's suite and an extension's; each binary defines the
// counters once (support.c) and prints them at the end of its main().
#ifndef DSTERM_TESTS_CHECK_H
#define DSTERM_TESTS_CHECK_H

#include <stdio.h>

extern int failures;
extern int checks;

#define CHECK(cond, ...)                                                       \
    do {                                                                       \
        checks++;                                                              \
        if (!(cond)) {                                                         \
            failures++;                                                        \
            printf("  FAIL %s:%d  ", __func__, __LINE__);                      \
            printf(__VA_ARGS__);                                               \
            printf("\n");                                                      \
        }                                                                      \
    } while (0)

#endif  // DSTERM_TESTS_CHECK_H
