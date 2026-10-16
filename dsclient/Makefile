# SPDX-License-Identifier: CC0-1.0
#
# SPDX-FileContributor: Antonio Niño Díaz, 2023-2026

export BLOCKSDS			?= /opt/blocksds/core
export BLOCKSDSEXT		?= /opt/blocksds/external

export WONDERFUL_TOOLCHAIN	?= /opt/wonderful
ARM_NONE_EABI_PATH	?= $(WONDERFUL_TOOLCHAIN)/toolchain/gcc-arm-none-eabi/bin/

# User config
# ===========

NAME		:= dsterm

# These three are what the DS/TWiLightMenu launcher shows for the card, so they
# are the app's identity rather than build trivia.
GAME_TITLE	:= DS Claude Terminal
GAME_SUBTITLE	?= Claude Code on your DS
GAME_AUTHOR	?= made by LC
GAME_ICON	?= icon.gif

# A compile_commands.json file is created if this is set to 1
COMPDB		?= 0

# DLDI and internal SD slot of DSi
# --------------------------------

# Root folder of the SD image
SDROOT		?= sdroot
# Name of the generated image it "DSi-1.sd" for no$gba in DSi mode
SDIMAGE		?= image.bin

# Source code paths
# -----------------

SOURCEDIRS	?= source
INCLUDEDIRS	?=
# Art the extension compiles in, if any; the terminal itself has none.
GFXDIRS		?=
BINDIRS		?=
AUDIODIRS	?=
# List of folders to combine into the root of NitroFS:
NITROFSDIR	?=

# The extension
# -------------
#
# Exactly one ext_*.c is compiled: ext_$(EXT).c, from the first source
# directory unless EXTSRC says otherwise.  `none` is the terminal alone and
# is what this Makefile builds on its own; a checkout that carries an
# extension names it in local.mk, so a plain `make` there cannot forget it.
# BlocksDS 1.22.2 and 1.22.3 cannot open the SD card when the console starts
# dsterm with its system registers locked (a DSiWare exploit, some
# forwarders): the card halts at "cannot read the SD card".  1.23.0 fixed it.
# A warning rather than an error -- every other launcher works -- so an older
# SDK still builds, and says what it is missing.
BLOCKSDS_VERSION := $(strip $(shell cat $(BLOCKSDS)/version.txt 2>/dev/null))
ifneq ($(filter v1.22.2 v1.22.3,$(BLOCKSDS_VERSION)),)
$(warning BlocksDS $(BLOCKSDS_VERSION): SD access fails from locked-SCFG launchers; 1.23.0 or later is recommended for release builds)
endif

-include local.mk
EXT		?= none
EXTSRC		?= $(firstword $(SOURCEDIRS))/ext_$(EXT).c

# Defines passed to all files
# ---------------------------

# The grid geometry is a build-time contract with the server: the same two
# numbers the server reads from DSTERM_COLS/DSTERM_ROWS at import.  A mismatch
# is an out-of-bounds write on a device with no MMU.  The terminal font and
# renderer origins derive from these.
GRID_COLS	?= 42
GRID_ROWS	?= 24

DEFINES		?= -DPROTO_COLS=$(GRID_COLS) -DPROTO_ROWS=$(GRID_ROWS)

# Libraries
# ---------

# The ARM7 core is built here (arm7/source/main.c, Makefile.arm7) rather than
# taken from $(BLOCKSDS)/sys/arm7/main_core: it is that core with Wi-Fi
# updates guarded against nesting, which crashed the card at random on a DSi.
ARM7ELF		:= build/arm7.elf

LIBS		:= -ldswifi9 -lnds9
LIBDIRS		+= $(BLOCKSDS)/libs/libnds
LIBDIRS		+= $(BLOCKSDS)/libs/dswifi

# Build artifacts
# ---------------

# Geometry is part of the output identity as well as the compiler command.
# Without it, switching from 64x32 back to 42x24 could leave a newer 64x32
# dsterm.nds in place and make would correctly-but-dangerously say there was
# nothing to do.  The image is named by the exact client/server contract;
# `boot` then copies that proven image to the conventional boot filename.
BUILD_TAG	:= $(NAME)-$(GRID_COLS)x$(GRID_ROWS)
ifneq ($(EXT),none)
    BUILD_TAG	:= $(BUILD_TAG)-$(EXT)
endif
BUILDDIR	:= build/$(BUILD_TAG)
ELF		:= build/$(BUILD_TAG).elf
DUMP		:= build/$(BUILD_TAG).dump
MAP		:= build/$(BUILD_TAG).map
ROM		:= $(BUILD_TAG).nds
BOOT_ROM	:= $(NAME).nds

# If NITROFSDIR is set, the soundbank created by mmutil will be saved to NitroFS
SOUNDBANKINFODIR	:= $(BUILDDIR)/maxmod
ifeq ($(strip $(NITROFSDIR)),)
    SOUNDBANKDIR	:= $(BUILDDIR)/maxmod
else
    SOUNDBANKDIR	:= $(BUILDDIR)/maxmod_nitrofs
endif

# Tools
# -----

PREFIX		:= $(ARM_NONE_EABI_PATH)arm-none-eabi-
CC		:= $(PREFIX)gcc
CXX		:= $(PREFIX)g++
LD		:= $(PREFIX)gcc
OBJDUMP		:= $(PREFIX)objdump
MKDIR		:= mkdir
RM		:= rm -rf

# Verbose flag
# ------------

ifeq ($(VERBOSE),1)
V		:=
else
V		:= @
endif

# Source files
# ------------

ifneq ($(BINDIRS),)
    SOURCES_BIN	:= $(shell find -L $(BINDIRS) -name "*.bin")
    INCLUDEDIRS	+= $(addprefix $(BUILDDIR)/,$(BINDIRS))
endif
ifneq ($(GFXDIRS),)
    SOURCES_PNG	:= $(shell find -L $(GFXDIRS) -name "*.png")
    INCLUDEDIRS	+= $(addprefix $(BUILDDIR)/,$(GFXDIRS))
endif
ifneq ($(AUDIODIRS),)
    SOURCES_AUDIO	:= $(shell find -L $(AUDIODIRS) -regex '.*\.\(it\|mod\|s3m\|wav\|xm\)')
    ifneq ($(SOURCES_AUDIO),)
        INCLUDEDIRS	+= $(SOUNDBANKINFODIR)
    endif
endif

SOURCES_S	+= $(shell find -L $(SOURCEDIRS) -name "*.s")
# Every ext_*.c is dropped and the chosen one put back, so two extensions
# can never be linked together by accident.
SOURCES_C	+= $(filter-out %/ext_none.c $(wildcard $(addsuffix /ext_*.c,$(SOURCEDIRS))), \
		     $(shell find -L $(SOURCEDIRS) -name "*.c")) $(EXTSRC)
SOURCES_CPP	+= $(shell find -L $(SOURCEDIRS) -name "*.cpp")

# Compiler and linker flags
# -------------------------

DEFINES		+= -D__NDS__ -D__BLOCKSDS__ -DARM9

ARCH		:= -mthumb -mcpu=arm946e-s+nofp

SPECS		?= $(BLOCKSDS)/sys/crts/ds_arm9.specs

WARNFLAGS	:= -Wall

ifeq ($(SOURCES_CPP),)
	LIBS	+= -lc
else
	LIBS	+= -lstdc++ -lc
endif

INCLUDEFLAGS	:= $(foreach path,$(INCLUDEDIRS),-I$(path)) \
		   $(foreach path,$(LIBDIRS),-I$(path)/include)

LIBDIRSFLAGS	:= $(foreach path,$(LIBDIRS),-L$(path)/lib)

ASFLAGS		:= -x assembler-with-cpp $(INCLUDEFLAGS) $(DEFINES) \
		   $(ARCH) -ffunction-sections -fdata-sections \
		   -specs=$(SPECS) $(ASFLAGS)

CFLAGS		:= $(WARNFLAGS) $(INCLUDEFLAGS) $(DEFINES) \
		   $(ARCH) -O2 -ffunction-sections -fdata-sections \
		   -specs=$(SPECS) $(CFLAGS)

CXXFLAGS	:= $(WARNFLAGS) $(INCLUDEFLAGS) $(DEFINES) \
		   $(ARCH) -O2 -ffunction-sections -fdata-sections \
		   -fno-exceptions -fno-rtti \
		   -specs=$(SPECS) $(CXXFLAGS)

LDFLAGS		:= $(ARCH) $(LIBDIRSFLAGS) -Wl,-Map,$(MAP) $(DEFINES) \
		   -Wl,--start-group $(LIBS) -Wl,--end-group -specs=$(SPECS) \
		   $(LDFLAGS)

# Intermediate build files
# ------------------------

OBJS_ASSETS	:= $(addsuffix .o,$(addprefix $(BUILDDIR)/,$(SOURCES_BIN))) \
		   $(addsuffix .o,$(addprefix $(BUILDDIR)/,$(SOURCES_PNG)))

HEADERS_ASSETS	:= $(patsubst %.bin,%_bin.h,$(addprefix $(BUILDDIR)/,$(SOURCES_BIN))) \
		   $(patsubst %.png,%.h,$(addprefix $(BUILDDIR)/,$(SOURCES_PNG)))

ifneq ($(SOURCES_AUDIO),)
    ifeq ($(strip $(NITROFSDIR)),)
        OBJS_ASSETS		+= $(SOUNDBANKDIR)/soundbank.c.o
    endif
    HEADERS_ASSETS	+= $(SOUNDBANKINFODIR)/soundbank.h
endif

OBJS_SOURCES	:= $(addsuffix .o,$(addprefix $(BUILDDIR)/,$(SOURCES_S))) \
		   $(addsuffix .o,$(addprefix $(BUILDDIR)/,$(SOURCES_C))) \
		   $(addsuffix .o,$(addprefix $(BUILDDIR)/,$(SOURCES_CPP)))

OBJS		:= $(OBJS_ASSETS) $(OBJS_SOURCES)

DEPS		:= $(OBJS:.o=.d)

# Targets
# -------

.PHONY: all boot clean dump dldipatch sdimage

all: boot

# Always refresh the conventional boot image from the geometry-named artifact.
# `boot` is phony deliberately: it is the last safety rail before the card
# writer, and a stale generic filename is more dangerous than one extra local
# copy.
boot: $(ROM)
	@echo "  BOOT    $(BOOT_ROM) <= $(ROM)"
	$(V)cp $(ROM) $(BOOT_ROM)

ifneq ($(strip $(NITROFSDIR)),)
# Additional arguments for ndstool
NDSTOOL_ARGS	:= -d $(NITROFSDIR)

ifneq ($(SOURCES_AUDIO),)
    NDSTOOL_ARGS	+= -d $(SOUNDBANKDIR)
endif

# Make the NDS ROM depend on the filesystem only if it is needed
$(ROM): $(NITROFSDIR)
endif

# Combine the title strings
ifeq ($(strip $(GAME_SUBTITLE)),)
    GAME_FULL_TITLE := $(GAME_TITLE);$(GAME_AUTHOR)
else
    GAME_FULL_TITLE := $(GAME_TITLE);$(GAME_SUBTITLE);$(GAME_AUTHOR)
endif

$(ARM7ELF): $(shell find -L arm7/source -name "*.c") Makefile.arm7
	$(V)+$(MAKE) --no-print-directory -f Makefile.arm7

$(ROM): $(ELF) $(ARM7ELF)
	@echo "  NDSTOOL $@"
	$(V)$(BLOCKSDS)/tools/ndstool/ndstool -c $@ \
		-7 $(ARM7ELF) -9 $(ELF) \
		-b $(GAME_ICON) "$(GAME_FULL_TITLE)" \
		$(NDSTOOL_ARGS)

$(ELF): $(OBJS)
	@echo "  LD      $@"
	$(V)$(LD) -o $@ $(OBJS) $(LDFLAGS)

$(DUMP): $(ELF)
	@echo "  OBJDUMP   $@"
	$(V)$(OBJDUMP) -h -C -S $< > $@

dump: $(DUMP)

clean:
	@echo "  CLEAN"
	$(V)$(RM) $(NAME)-*.nds $(NAME).nds build $(SDIMAGE) compile_commands.json

sdimage:
	@echo "  MKFATIMG $(SDIMAGE) $(SDROOT)"
	$(V)$(BLOCKSDS)/tools/mkfatimg/mkfatimg -t $(SDROOT) $(SDIMAGE)

dldipatch: $(ROM)
	@echo "  DLDIPATCH $(ROM)"
	$(V)$(BLOCKSDS)/tools/dldipatch/dldipatch patch \
		$(BLOCKSDS)/sys/dldi_r4/r4tf.dldi $(ROM)

ifeq ($(COMPDB),1)
# Add an additional dependency to the "all" rule
all: compile_commands.json

compile_commands.json: $(OBJS)
	@echo "  MERGE   compile_commands.json"
	$(V)$(WONDERFUL_TOOLCHAIN)/bin/wf-compile-commands-merge $@ $(patsubst %.o,%.cc.json,$^)
endif

# Rules
# -----

ifeq ($(COMPDB),1)

$(BUILDDIR)/%.s.o : %.s
	@echo "  AS      $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CC) $(ASFLAGS) -MMD -MP -c -MJ $(patsubst %.o,%.cc.json,$@) -o $@ $<

$(BUILDDIR)/%.c.o : %.c
	@echo "  CC      $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CC) $(CFLAGS) -MMD -MP -c -MJ $(patsubst %.o,%.cc.json,$@) -o $@ $<

$(BUILDDIR)/%.arm.c.o : %.arm.c
	@echo "  CC      $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CC) $(CFLAGS) -MMD -MP -marm -mlong-calls -c -MJ $(patsubst %.o,%.cc.json,$@) -o $@ $<

$(BUILDDIR)/%.cpp.o : %.cpp
	@echo "  CXX     $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CXX) $(CXXFLAGS) -MMD -MP -c -MJ $(patsubst %.o,%.cc.json,$@) -o $@ $<

$(BUILDDIR)/%.arm.cpp.o : %.arm.cpp
	@echo "  CXX     $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CXX) $(CXXFLAGS) -MMD -MP -marm -mlong-calls -c -MJ $(patsubst %.o,%.cc.json,$@) -o $@ $<

else

$(BUILDDIR)/%.s.o : %.s
	@echo "  AS      $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CC) $(ASFLAGS) -MMD -MP -c -o $@ $<

$(BUILDDIR)/%.c.o : %.c
	@echo "  CC      $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CC) $(CFLAGS) -MMD -MP -c -o $@ $<

$(BUILDDIR)/%.arm.c.o : %.arm.c
	@echo "  CC      $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CC) $(CFLAGS) -MMD -MP -marm -mlong-calls -c -o $@ $<

$(BUILDDIR)/%.cpp.o : %.cpp
	@echo "  CXX     $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CXX) $(CXXFLAGS) -MMD -MP -c -o $@ $<

$(BUILDDIR)/%.arm.cpp.o : %.arm.cpp
	@echo "  CXX     $<"
	@$(MKDIR) -p $(@D)
	$(V)$(CXX) $(CXXFLAGS) -MMD -MP -marm -mlong-calls -c -o $@ $<

endif

$(BUILDDIR)/%.bin.o $(BUILDDIR)/%_bin.h : %.bin
	@echo "  BIN2C   $<"
	@$(MKDIR) -p $(@D)
	$(V)$(BLOCKSDS)/tools/bin2c/bin2c $< $(@D)
	$(V)$(CC) $(CFLAGS) -MMD -MP -c -o $(BUILDDIR)/$*.bin.o $(BUILDDIR)/$*_bin.c

$(BUILDDIR)/%.png.o $(BUILDDIR)/%.h : %.png %.grit
	@echo "  GRIT    $<"
	@$(MKDIR) -p $(@D)
	$(V)$(BLOCKSDS)/tools/grit/grit $< -ftc -W1 -o$(BUILDDIR)/$*
	$(V)$(CC) $(CFLAGS) -MMD -MP -c -o $(BUILDDIR)/$*.png.o $(BUILDDIR)/$*.c
	$(V)touch $(BUILDDIR)/$*.png.o $(BUILDDIR)/$*.h

ifneq ($(SOURCES_AUDIO),)

$(SOUNDBANKINFODIR)/soundbank.h: $(SOURCES_AUDIO)
	@echo "  MMUTIL  $^"
	@$(MKDIR) -p $(SOUNDBANKDIR)
	@$(MKDIR) -p $(SOUNDBANKINFODIR)
	$(V)$(BLOCKSDS)/tools/mmutil/mmutil $^ -d \
		-o$(SOUNDBANKDIR)/soundbank.bin -h$(SOUNDBANKINFODIR)/soundbank.h

ifeq ($(strip $(NITROFSDIR)),)
$(SOUNDBANKDIR)/soundbank.c.o: $(SOUNDBANKINFODIR)/soundbank.h
	@echo "  BIN2C   soundbank.bin"
	$(V)$(BLOCKSDS)/tools/bin2c/bin2c $(SOUNDBANKDIR)/soundbank.bin \
		$(SOUNDBANKDIR)
	@echo "  CC.9    soundbank_bin.c"
	$(V)$(CC) $(CFLAGS) -MMD -MP -c -o $(SOUNDBANKDIR)/soundbank.c.o \
		$(SOUNDBANKDIR)/soundbank_bin.c
endif

endif

# All assets must be built before the source code
# -----------------------------------------------

$(SOURCES_S) $(SOURCES_C) $(SOURCES_CPP): $(HEADERS_ASSETS)

# Include dependency files if they exist
# --------------------------------------

-include $(DEPS)
