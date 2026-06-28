cmake_minimum_required(VERSION 3.20)

project(palpal C CXX)

find_package(Python3 REQUIRED COMPONENTS Interpreter)
find_package(PkgConfig REQUIRED)
find_package(Threads REQUIRED)
pkg_check_modules(SDL3 REQUIRED IMPORTED_TARGET sdl3)

set(SDLPAL_DIR "${CMAKE_SOURCE_DIR}/vendor/sdlpal")

set(PALPAL_RUNTIME_DIR "${CMAKE_SOURCE_DIR}/runtime" CACHE PATH "Directory for live state, screenshots, and actions")
set(PALPAL_ASSET_DIR "${CMAKE_SOURCE_DIR}/data/pal" CACHE PATH "Directory containing DOS game resources")
set(PALPAL_GAME_BIN "${SDLPAL_DIR}/unix/sdlpal" CACHE FILEPATH "Patched SDLPAL executable")
set(PALPAL_HOST "127.0.0.1" CACHE STRING "Debug server host")
set(PALPAL_PORT "8765" CACHE STRING "Debug server port")
set(PALPAL_SAVE_SLOT "0" CACHE STRING "Save slot loaded by SDLPAL agent mode")
set(PALPAL_CHECKPOINT_SLOT "99" CACHE STRING "Private save slot used by PALPAL checkpoints")
set(PALPAL_START_MODE "wait" CACHE STRING "Agent startup mode: wait for Web UI or load PALPAL_SAVE_SLOT")
option(PALPAL_STRICT_WARNINGS "Enable compiler warnings for vendored SDLPAL sources" OFF)
option(PALPAL_QUIET_VENDOR_WARNINGS "Suppress compiler warnings from vendored SDLPAL sources during normal builds" ON)

set(PALPAL_ENV
    "PYTHON=${Python3_EXECUTABLE}"
    "PALPAL_RUNTIME_DIR=${PALPAL_RUNTIME_DIR}"
    "PALPAL_ASSET_DIR=${PALPAL_ASSET_DIR}"
    "PALPAL_GAME_BIN=${PALPAL_GAME_BIN}"
    "PALPAL_HOST=${PALPAL_HOST}"
    "PALPAL_PORT=${PALPAL_PORT}"
    "PALPAL_SAVE_SLOT=${PALPAL_SAVE_SLOT}"
    "PALPAL_CHECKPOINT_SLOT=${PALPAL_CHECKPOINT_SLOT}"
    "PALPAL_START_MODE=${PALPAL_START_MODE}"
)

set(SDLPAL_SOURCE_DIRS
    "${SDLPAL_DIR}"
    "${SDLPAL_DIR}/sdl_compat"
    "${SDLPAL_DIR}/adplug"
    "${SDLPAL_DIR}/liboggvorbis/src"
    "${SDLPAL_DIR}/libopusfile/src"
    "${SDLPAL_DIR}/libopusfile/celt"
    "${SDLPAL_DIR}/libopusfile/silk"
    "${SDLPAL_DIR}/libopusfile/silk/float"
    "${SDLPAL_DIR}/libmad"
    "${SDLPAL_DIR}/timidity"
)

foreach(source_dir IN LISTS SDLPAL_SOURCE_DIRS)
    file(GLOB dir_c CONFIGURE_DEPENDS "${source_dir}/*.c")
    file(GLOB dir_cpp CONFIGURE_DEPENDS "${source_dir}/*.cpp")
    list(APPEND SDLPAL_SOURCES ${dir_c} ${dir_cpp})
endforeach()

list(APPEND SDLPAL_SOURCES "${SDLPAL_DIR}/unix/unix.cpp")

if(APPLE)
    list(FILTER SDLPAL_SOURCES EXCLUDE REGEX ".*/mini_glloader\\.c$")
endif()

find_library(ALSA_LIBRARY asound)
if(ALSA_LIBRARY AND NOT APPLE)
    file(GLOB native_midi_c CONFIGURE_DEPENDS "${SDLPAL_DIR}/native_midi/*.c")
    list(APPEND SDLPAL_SOURCES ${native_midi_c} "${SDLPAL_DIR}/unix/native_midi_alsa.c")
else()
    list(APPEND SDLPAL_SOURCES "${SDLPAL_DIR}/unix/native_midi.cpp")
endif()

add_custom_command(
    OUTPUT "${SDLPAL_DIR}/generated.h"
    COMMAND /bin/sh "${SDLPAL_DIR}/scripts/gengitrev"
    WORKING_DIRECTORY "${SDLPAL_DIR}/unix"
    VERBATIM
)

add_custom_target(sdlpal-generated DEPENDS "${SDLPAL_DIR}/generated.h")

add_executable(sdlpal ${SDLPAL_SOURCES})
add_dependencies(sdlpal sdlpal-generated)

set_target_properties(sdlpal PROPERTIES
    C_STANDARD 99
    C_STANDARD_REQUIRED ON
    C_EXTENSIONS ON
    CXX_STANDARD 11
    CXX_STANDARD_REQUIRED ON
    RUNTIME_OUTPUT_DIRECTORY "${SDLPAL_DIR}/unix"
)

target_include_directories(sdlpal PRIVATE
    "${SDLPAL_DIR}/unix"
    "${SDLPAL_DIR}"
    "${SDLPAL_DIR}/sdl_compat"
    "${SDLPAL_DIR}/adplug"
    "${SDLPAL_DIR}/liboggvorbis/include"
    "${SDLPAL_DIR}/liboggvorbis/src"
    "${SDLPAL_DIR}/libopusfile/include"
    "${SDLPAL_DIR}/libopusfile/src"
    "${SDLPAL_DIR}/libopusfile/celt"
    "${SDLPAL_DIR}/libopusfile/silk"
    "${SDLPAL_DIR}/libopusfile/silk/float"
    "${SDLPAL_DIR}/timidity"
)

target_compile_definitions(sdlpal PRIVATE
    USE_SDL3=1
    HAVE_CONFIG_H
    PAL_HAS_GIT_REVISION
    PAL_HAS_PLATFORM_SPECIFIC_UTILS
    _GNU_SOURCE
)

target_compile_options(sdlpal PRIVATE
    -g
    -O2
    -fno-strict-aliasing
)

if(PALPAL_STRICT_WARNINGS)
    target_compile_options(sdlpal PRIVATE -Wall)
elseif(PALPAL_QUIET_VENDOR_WARNINGS)
    target_compile_options(sdlpal PRIVATE -w)
endif()

find_program(FLTK_CONFIG fltk-config)
if(FLTK_CONFIG)
    execute_process(COMMAND "${FLTK_CONFIG}" --cflags OUTPUT_VARIABLE FLTK_CFLAGS OUTPUT_STRIP_TRAILING_WHITESPACE)
    execute_process(COMMAND "${FLTK_CONFIG}" --cxxflags OUTPUT_VARIABLE FLTK_CXXFLAGS OUTPUT_STRIP_TRAILING_WHITESPACE)
    execute_process(COMMAND "${FLTK_CONFIG}" --ldflags OUTPUT_VARIABLE FLTK_LDFLAGS OUTPUT_STRIP_TRAILING_WHITESPACE)
    separate_arguments(FLTK_CFLAGS_LIST UNIX_COMMAND "${FLTK_CFLAGS}")
    separate_arguments(FLTK_CXXFLAGS_LIST UNIX_COMMAND "${FLTK_CXXFLAGS}")
    separate_arguments(FLTK_LDFLAGS_LIST UNIX_COMMAND "${FLTK_LDFLAGS}")
    target_compile_options(sdlpal PRIVATE ${FLTK_CFLAGS_LIST} ${FLTK_CXXFLAGS_LIST})
    target_link_libraries(sdlpal PRIVATE ${FLTK_LDFLAGS_LIST})
else()
    target_compile_definitions(sdlpal PRIVATE PAL_NO_LAUNCH_UI)
endif()

target_link_libraries(sdlpal PRIVATE
    PkgConfig::SDL3
    Threads::Threads
    m
)

if(ALSA_LIBRARY AND NOT APPLE)
    target_link_libraries(sdlpal PRIVATE "${ALSA_LIBRARY}")
endif()

if(APPLE)
    target_link_libraries(sdlpal PRIVATE "-framework OpenGL")
else()
    find_package(OpenGL REQUIRED)
    target_link_libraries(sdlpal PRIVATE OpenGL::GL)
endif()

add_custom_target(assets
    COMMAND ${CMAKE_COMMAND} -E env ${PALPAL_ENV} "${CMAKE_SOURCE_DIR}/run" assets
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    USES_TERMINAL
    COMMENT "Fetch gitignored DOS game resources"
)

add_custom_target(debug
    COMMAND ${CMAKE_COMMAND} -E env ${PALPAL_ENV} "${CMAKE_SOURCE_DIR}/run" debug
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    USES_TERMINAL
    COMMENT "Run browser debug server"
)

add_custom_target(game
    COMMAND ${CMAKE_COMMAND} -E env ${PALPAL_ENV} "${CMAKE_SOURCE_DIR}/run" game
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    USES_TERMINAL
    COMMENT "Run SDLPAL in agent mode"
)

add_custom_target(dev
    COMMAND ${CMAKE_COMMAND} -E env ${PALPAL_ENV} "${CMAKE_SOURCE_DIR}/run" dev
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    USES_TERMINAL
    COMMENT "Run debug server and SDLPAL"
)

add_custom_target(clean-runtime
    COMMAND ${CMAKE_COMMAND} -E env ${PALPAL_ENV} "${CMAKE_SOURCE_DIR}/run" clean
    WORKING_DIRECTORY "${CMAKE_SOURCE_DIR}"
    USES_TERMINAL
    COMMENT "Remove generated runtime files"
)
