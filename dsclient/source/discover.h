// Finding a server on the local network.
//
// Two ways, shown to the person as one list:
//
//  * Broadcast.  "DSTM probe 1 id=<hex>" to 255.255.255.255:8346, and every
//    server answers "DSTM here 1 name=<host> port=<tcp> pair=<0|1>" (see
//    dsterm/discovery.py, the other half).  Fast, and enough on any ordinary
//    LAN.
//  * A sweep of this /24: a TCP connect to the server port on every address,
//    a few in flight at once.  Slower, but it finds a server that broadcast
//    cannot reach -- WSL2 in NAT mode behind a Windows port proxy, which is
//    the case it exists for.  A completed connect that answers with a NONCE
//    is a server.
//
// This file is the pure half: the datagrams, the list, and which address the
// sweep tries next.  The sockets are in boot.c.
#ifndef DSTERM_DISCOVER_H
#define DSTERM_DISCOVER_H

#include <stddef.h>
#include <stdint.h>

#include "config.h"
#include "profiles.h"

#define DISCOVER_PORT 8346
#define DISCOVER_MAX 8
#define DISCOVER_DATAGRAM_MAX 96

typedef struct {
    char     name[PROFILE_NAME_MAX];
    char     host[CFG_ADDR_MAX];
    uint16_t port;
    int      pairing;     // the server has a pairing window open
    int      swept;       // found by the sweep, so no name or pairing state
} discover_entry_t;

typedef struct {
    unsigned count;
    discover_entry_t entries[DISCOVER_MAX];
} discover_list_t;

// The probe datagram, NUL-terminated; returns its length (without the NUL)
// or -1 if it does not fit.
int discover_probe(char *out, size_t cap, const char *device_id);

// Parse an answer.  Strict: anything but a well-formed "DSTM here" with a
// name and a port in range is rejected.  The name is reduced to the profile
// name alphabet before it is stored, so a hostile answer cannot put anything
// on screen the menu would not have written itself.
int discover_parse_answer(const uint8_t *data, size_t len, discover_entry_t *out);

void discover_list_init(discover_list_t *l);
// Add or update by host and port.  A broadcast answer replaces a swept entry
// for the same address (it knows the name); a swept entry never overwrites a
// broadcast one.  Returns 0 only when the list is full.
int discover_list_add(discover_list_t *l, const discover_entry_t *e);

// IPv4 in host byte order, for the sweep's arithmetic.
int ipv4_parse(const char *s, uint32_t *out);
void ipv4_format(uint32_t addr, char out[CFG_ADDR_MAX]);

// The sweep's addresses: every host of the /24 around `self` (a wider
// netmask is narrowed to /24 -- a /16 would be 65,534 connects), skipping
// the network address, the broadcast address and the console itself.
typedef struct {
    uint32_t next;
    uint32_t last;
    uint32_t self;
    unsigned total;
    unsigned done;
} discover_sweep_t;

int discover_sweep_init(discover_sweep_t *s, uint32_t self, uint32_t netmask);
// The next address to try, or 0 when the sweep is finished.
int discover_sweep_next(discover_sweep_t *s, uint32_t *addr);

#endif  // DSTERM_DISCOVER_H
