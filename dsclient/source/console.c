#include "console.h"

console_family_t console_classify(uint8_t fw_type, uint8_t fw_board,
                                  int dsi_mode, int ram32)
{
    // The board is the most specific fact: W028 is only ever in a 3DS.
    if (fw_board == 0x03 || (dsi_mode && ram32)) return CONSOLE_3DS;
    if (fw_board == 0x01 || fw_board == 0x02 || fw_type == 0x57) {
        return CONSOLE_DSI;
    }
    if (fw_type == 0x20 || fw_type == 0x63) return CONSOLE_DS_LITE;
    if (fw_type == 0xFF || fw_type == 0x43) return CONSOLE_DS;
    // Only a DSi-family console can be in DSi mode at all.
    return dsi_mode ? CONSOLE_DSI : CONSOLE_UNKNOWN;
}

const char *console_name(const console_t *c)
{
    if (!c) return "unknown console";
    switch (c->family) {
    case CONSOLE_DS:      return "DS";
    case CONSOLE_DS_LITE: return "DS Lite";
    case CONSOLE_DSI:     return c->dsi_mode ? "DSi (DSi mode)" : "DSi (DS mode)";
    case CONSOLE_3DS:     return c->dsi_mode ? "3DS (DSi mode)" : "3DS (DS mode)";
    default:              return c->dsi_mode ? "DSi-mode console" : "DS-mode console";
    }
}

const char *console_wpa_advice(const console_t *c)
{
    const console_family_t f = c ? c->family : CONSOLE_UNKNOWN;
    if (f == CONSOLE_DS || f == CONSOLE_DS_LITE) {
        return "This DS can't join WPA or WPA2\n"
               "networks: its radio only does open\n"
               "ones. Use an open 2.4 GHz network that\n"
               "only you use -- a hotspot from your\n"
               "computer, or a spare router -- or a\n"
               "DSi or 3DS.";
    }
    if (f == CONSOLE_3DS) {
        return "This 3DS started dsterm in DS mode,\n"
               "where only open networks work.\n\n"
               "Start it from TWiLight Menu++ on the SD\n"
               "card with Run in: DSi mode (press Y on\n"
               "dsterm.nds). Flashcarts always run DS\n"
               "mode.";
    }
    return "This DSi started dsterm in DS mode (a\n"
           "flashcart, or TWiLight Menu++ set to DS\n"
           "mode), where only open networks work.\n\n"
           "Start dsterm.nds from the SD card with\n"
           "Unlaunch or TWiLight Menu++ (press Y:\n"
           "Run in DSi mode).";
}

const char *console_silent_advice(const console_t *c)
{
    const console_family_t f = c ? c->family : CONSOLE_UNKNOWN;
    if (f == CONSOLE_3DS) {
        return "No networks heard. Is wireless on?\n"
               "(The wireless switch, or the HOME Menu\n"
               "setting.) If you used a forwarder, start\n"
               "dsterm from TWiLight Menu++ instead.";
    }
    if (f == CONSOLE_DSI) {
        return "No networks heard. Is wireless turned\n"
               "on in System Settings? Only 2.4 GHz\n"
               "networks can be seen.";
    }
    return "No networks heard. Only 2.4 GHz\n"
           "networks that allow 802.11b can be seen\n"
           "by a DS. Move closer to the router and\n"
           "scan again.";
}
