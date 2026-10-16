// SPDX-License-Identifier: Zlib
// SPDX-FileNotice: Modified from the default ARM7 core of the BlocksDS project
// (sys/arm7/main_core): Wi-Fi updates are guarded against nesting.
//
// Copyright (C) 2005 Michael Noland (joat)
// Copyright (C) 2005 Jason Rogers (Dovoto)
// Copyright (C) 2005-2015 Dave Murphy (WinterMute)
// Copyright (C) 2023 Antonio Niño Díaz

// The ARM7 core: BlocksDS's default one with dswifi and Maxmod, and one
// change.
//
// Wifi_Update() is called from two interrupts: the FIFO message the ARM9
// sends when it queues a packet, and the vertical blank.  If interrupts are
// enabled inside it, one call can be interrupted by the other, and dswifi's
// author found that the nested call can overflow the ARM7's small stack
// (dswifi commit 59d2636, 2026-10-04, after the last BlocksDS release).  On
// the ARM7 that corrupts memory the ARM9 relies on, so the failure shows up
// on the ARM9, at random and far from its cause.  On a DSi it showed up as
// lwIP's timer list, corrupted, jumping into a table of timers: a data abort
// "out of nowhere", more often the more packets the card sent.
//
// Until a BlocksDS release carries that fix, this core makes both callers go
// through one guard, which is the same fix from outside.

#include <nds.h>
#include <dswifi7.h>
#include <maxmod7.h>

// dswifi's FIFO commands (source/common/wifi_shared.h, DSWifi_IpcCommands).
// Not in its public headers; the values have not changed since they were
// introduced.
#define DSTERM_WIFI_SYNC 0
#define DSTERM_WIFI_DEINIT 1

// Wifi_Deinit() is exported by libdswifi7 but declared only in its sources.
void Wifi_Deinit(void);

static volatile bool wifi_update_active = false;

static void wifi_update_once(void)
{
    // Interrupts are disabled while the flag is tested and set, so two
    // callers cannot both see it clear.
    const int ime = enterCriticalSection();
    if (wifi_update_active)
    {
        leaveCriticalSection(ime);
        return;
    }
    wifi_update_active = true;
    leaveCriticalSection(ime);

    Wifi_Update();

    wifi_update_active = false;
}

static void wifi_value32_handler(u32 value, void *userdata)
{
    (void)userdata;

    switch (value)
    {
        case DSTERM_WIFI_SYNC:
            wifi_update_once();
            break;
        case DSTERM_WIFI_DEINIT:
            Wifi_Deinit();
            break;
        default:
            break;
    }
}

// The camera LED rides on the power manager's I2C.  Writing it here is safe
// for the same reason the rest of the core is: FIFO handlers run one at a
// time, so this cannot interleave with the camera driver's own I2C, which
// also runs inside a FIFO handler; dswifi touches I2C only at card init; and
// the Wifi_Update guard above is unchanged.
static void camled_handler(u32 value, void *userdata)
{
    (void)userdata;

    i2cWriteRegister(I2C_PM, I2CREGPM_CAMLED, value ? 1 : 0);
}

static volatile bool exit_loop = false;

static void power_button_callback(void)
{
    exit_loop = true;
}

static void vblank_handler(void)
{
    inputGetAndSend();
    wifi_update_once();
}

int main(void)
{
    // Required for some functions within libnds.
    enableSound();

    // User information from the firmware (name, birthday, ...).
    readUserSettings();

    ledBlink(0);

    // Touch calibration from the values readUserSettings() read.
    touchInit();

    irqInit();
    fifoInit();

    // dswifi's own handlers, then its value handler replaced by the guarded
    // one.  The address handler (Wifi_Init) stays dswifi's.
    installWifiFIFO();
    fifoSetValue32Handler(FIFO_DSWIFI, wifi_value32_handler, 0);

    installSoundFIFO();

    installSystemFIFO(); // Sleep mode, storage, firmware...
    if (isDSiMode())
    {
        installCameraFIFO();
        fifoSetValue32Handler(FIFO_USER_01, camled_handler, 0);
    }

    // Maxmod uses timer 0.
    mmInstall(FIFO_MAXMOD);

    // A DSi's power button: leave cleanly.  No effect on a DS.
    setPowerButtonCB(power_button_callback);

    // The RTC is read once and then advanced by an interrupt each second.
    initClockIRQTimer(LIBNDS_DEFAULT_TIMER_RTC);

    irqSet(IRQ_VBLANK, vblank_handler);
    irqEnable(IRQ_VBLANK);

    while (!exit_loop)
    {
        const uint16_t key_mask = KEY_SELECT | KEY_START | KEY_L | KEY_R;
        uint16_t keys_pressed = ~REG_KEYINPUT;

        if ((keys_pressed & key_mask) == key_mask)
            exit_loop = true;

        swiWaitForVBlank();
    }

    return 0;
}
