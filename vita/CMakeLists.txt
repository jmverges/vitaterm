cmake_minimum_required(VERSION 3.16)

if(NOT DEFINED CMAKE_TOOLCHAIN_FILE)
  if(DEFINED ENV{VITASDK})
    set(CMAKE_TOOLCHAIN_FILE "$ENV{VITASDK}/share/vita.toolchain.cmake" CACHE PATH "toolchain file")
  else()
    message(FATAL_ERROR "Please define VITASDK to point to your SDK path!")
  endif()
endif()

project(dsterm-vita C)
include("$ENV{VITASDK}/share/vita.cmake" REQUIRED)

set(VITA_APP_NAME "Claude Terminal")
set(VITA_TITLEID  "DSCT00001")
set(VITA_VERSION  "01.00")

# The grid this client draws; the computer runs  dsterm serve --grid 120x32.
set(GRID_COLS 120)
set(GRID_ROWS 32)

# The DS card's protocol half, compiled unchanged.
set(DS ${CMAKE_SOURCE_DIR}/../dsclient/source)
set(DS_SOURCES
  ${DS}/backoff.c ${DS}/camera.c ${DS}/chacha20.c ${DS}/chime.c ${DS}/config.c
  ${DS}/discover.c ${DS}/entropy.c ${DS}/ext_none.c ${DS}/log.c ${DS}/pair.c
  ${DS}/profiles.c ${DS}/proto.c ${DS}/quick.c ${DS}/remote.c ${DS}/seal.c
  ${DS}/sessions.c ${DS}/sfx_assets.c ${DS}/sha256.c ${DS}/snippets.c ${DS}/sound.c
  ${DS}/x25519.c ${DS}/clip.c ${DS}/composer.c ${DS}/privacy.c ${DS}/project.c)

file(GLOB VITA_SOURCES src/*.c)

add_executable(${PROJECT_NAME} ${VITA_SOURCES} ${DS_SOURCES})
target_include_directories(${PROJECT_NAME} PRIVATE src ${DS})
target_compile_definitions(${PROJECT_NAME} PRIVATE PROTO_COLS=${GRID_COLS} PROTO_ROWS=${GRID_ROWS})
target_compile_options(${PROJECT_NAME} PRIVATE -O2 -Wall -Wno-unused-function)

target_link_libraries(${PROJECT_NAME}
  vita2d SceLibKernel_stub SceDisplay_stub SceGxm_stub SceCtrl_stub SceTouch_stub
  SceAudio_stub SceCamera_stub SceSysmodule_stub SceCommonDialog_stub SceIme_stub
  SceAppUtil_stub SceAppMgr_stub ScePgf_stub SceRtc_stub ScePower_stub SceNet_stub
  SceNetCtl_stub SceIofilemgr_stub freetype png jpeg bz2 z m c)

vita_create_self(eboot.bin ${PROJECT_NAME})
vita_create_vpk(${PROJECT_NAME}.vpk ${VITA_TITLEID} eboot.bin
  VERSION ${VITA_VERSION}
  NAME ${VITA_APP_NAME}
  FILE assets/font.ttf font.ttf
  FILE sce_sys/icon0.png sce_sys/icon0.png
  FILE sce_sys/livearea/contents/bg.png sce_sys/livearea/contents/bg.png
  FILE sce_sys/livearea/contents/startup.png sce_sys/livearea/contents/startup.png
  FILE sce_sys/livearea/contents/template.xml sce_sys/livearea/contents/template.xml)
