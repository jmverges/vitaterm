// Just enough of libnds for render.c and subscreen.c to draw into memory on
// a computer, so tests/screens.c can photograph every screen of the card
// (tools/screenshots.sh).  The two "VRAM" backgrounds are plain arrays.
#ifndef DSTERM_SHOT_NDS_H
#define DSTERM_SHOT_NDS_H

#include <stdint.h>
#include <string.h>

typedef uint16_t u16;
typedef uint8_t u8;
typedef uint32_t u32;

#define ARGB16(a, r, g, b) ((u16)(((a) << 15) | ((b) << 10) | ((g) << 5) | (r)))

#define MODE_5_2D 0
#define VRAM_A_MAIN_BG 0
#define VRAM_C_SUB_BG 0
typedef enum { BgType_Bmp16 } BgType;
typedef enum { BgSize_B16_256x256 } BgSize;

extern u16 shot_main[256 * 256];
extern u16 shot_sub[256 * 256];

static inline void videoSetMode(int m) { (void)m; }
static inline void videoSetModeSub(int m) { (void)m; }
static inline void vramSetBankA(int b) { (void)b; }
static inline void vramSetBankC(int b) { (void)b; }
static inline int bgInit(int l, BgType t, BgSize s, int a, int b)
{ (void)l; (void)t; (void)s; (void)a; (void)b; return 0; }
static inline int bgInitSub(int l, BgType t, BgSize s, int a, int b)
{ (void)l; (void)t; (void)s; (void)a; (void)b; return 1; }
static inline u16 *bgGetGfxPtr(int id) { return id ? shot_sub : shot_main; }
static inline void bgSetScroll(int id, int x, int y) { (void)id; (void)x; (void)y; }
static inline void bgUpdate(void) {}
#define POWER_ALL_2D 0
static inline void powerOn(int x) { (void)x; }
static inline void lcdMainOnTop(void) {}
// The real one is a DMA; here a copy is a copy.
static inline void dmaCopy(const void *src, void *dst, size_t len)
{ memcpy(dst, src, len); }

#endif
