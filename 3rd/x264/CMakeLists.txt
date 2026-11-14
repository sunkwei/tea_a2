# x264 -- the H.264 encoder, built from the vendored source.
#
# x264's own build is a shell script (`configure`) that wants make and a
# POSIX shell. This project builds with CMake and a compiler only, so the
# few values configure would compute are written down in config.h.in and
# x264_config.h.
#
# What is left out on purpose: the assembler version (it needs nasm). x264
# then uses its C versions of the MMX/SSE2 functions -- a little slower, but
# the build needs nothing but a compiler.
#
# x264 is GPL-2.0-or-later; the project as a whole is GPL-3.0-or-later
# (see LICENSE at the root).
cmake_minimum_required(VERSION 3.20)
project(x264 C)

set(CMAKE_C_STANDARD 99)

# --- what the machine can do ------------------------------------------------
if(CMAKE_SIZEOF_VOID_P EQUAL 8 AND CMAKE_SYSTEM_PROCESSOR MATCHES "x86_64|AMD64|amd64")
  set(X264_ARCH_X86_64 1)
else()
  set(X264_ARCH_X86_64 0)
endif()

if(MSVC)
  # MSVC has no GNU inline assembly and no vector extensions.
  set(X264_VECTOREXT 0)
  set(X264_CLOCK_GETTIME 0)
else()
  set(X264_VECTOREXT 1)
  set(X264_CLOCK_GETTIME 1)
endif()

if(WIN32)
  set(X264_SYS_WINDOWS 1)
  set(X264_WIN32THREAD 1)
else()
  set(X264_SYS_WINDOWS 0)
  set(X264_WIN32THREAD 0)
endif()

# The x86 functions come in two flavours: an assembler version (nasm) and
# a C version. Without nasm the C version is the one to use, and the C code
# has to be told not to expect the assembler symbols.
# The assembler version of x264 needs nasm, and it wants a nasm of the 2.x
# days: nasm 3 turned "label changed during code generation" into an error
# and is stricter about macros. Until that is sorted out the C version is
# the one to build - it needs nothing but a compiler, and it is a little
# slower.
unset(X264_USE_ASM CACHE)
option(X264_USE_ASM "Build x264's assembler version (needs nasm)" ON)
# find_program remembers "not found" in the cache, so a later run would not
# look again (nasm may well have been installed since).
unset(X264_NASM CACHE)
find_program(X264_NASM nasm)
set(X264_ASM_OBJECTS "")
if(X264_USE_ASM AND X264_NASM AND X264_ARCH_X86_64 AND NOT MSVC)
  set(X264_MMX 1)
  set(X264_INLINE_ASM 1)
  message(STATUS "x264: nasm found, the assembler version will be built")
  # The assembler files. The names are x264's own: the ones without a bit
  # depth are compiled without the depth macros, the rest are the 8 bit
  # build (see x264's Makefile).
  set(X264_ASM_PLAIN common/x86/cpu-a.asm)
  set(X264_ASM_8BIT
    common/x86/bitstream-a.asm
    common/x86/const-a.asm
    common/x86/cabac-a.asm
    common/x86/dct-a.asm
    common/x86/deblock-a.asm
    common/x86/mc-a.asm
    common/x86/mc-a2.asm
    common/x86/pixel-a.asm
    common/x86/predict-a.asm
    common/x86/quant-a.asm
    common/x86/dct-64.asm
    common/x86/trellis-64.asm
    common/x86/sad-a.asm)
  # nasm 3 turned "label changed during code generation" into an error;
  # x264's assembler was written for the older nasm. nasm 2 does not know
  # this flag, so it is only added for nasm 3.
  set(X264_NASM_EXTRA "")
  execute_process(COMMAND ${X264_NASM} -v
    OUTPUT_VARIABLE X264_NASM_VERSION OUTPUT_STRIP_TRAILING_WHITESPACE)
  string(REGEX MATCH "([0-9]+)\\." X264_NASM_MAJOR "${X264_NASM_VERSION}")
  message(STATUS "x264: nasm says '${X264_NASM_VERSION}' (major '${X264_NASM_MAJOR}')")
  if(X264_NASM_MAJOR AND X264_NASM_MAJOR GREATER_EQUAL 3)
    set(X264_NASM_EXTRA "-w+label-redef-late")
    message(STATUS "x264: nasm 3 needs -w+label-redef-late")
  endif()
  set(X264_ASM_DIR ${CMAKE_CURRENT_BINARY_DIR}/asm)
  file(MAKE_DIRECTORY ${X264_ASM_DIR})
  # One helper for both kinds: the difference is the depth macros.
  # The extra flags have to be a list: a string with spaces would arrive as
  # one argument, and nasm would not see the macros at all.
  function(x264_add_asm out_var source)
    get_filename_component(stem ${source} NAME_WE)
    # The 8 bit build of sad-a.asm is called sad-a-8.o (that is how x264
    # names it, so the two do not overwrite each other).
    if(stem STREQUAL "sad-a")
      set(obj ${X264_ASM_DIR}/sad-a-8.o)
    else()
      set(obj ${X264_ASM_DIR}/${stem}.o)
    endif()
    add_custom_command(OUTPUT ${obj}
      COMMAND ${X264_NASM} -f win64
        -I${CMAKE_CURRENT_SOURCE_DIR}
        -I${CMAKE_CURRENT_SOURCE_DIR}/common
        -I${CMAKE_CURRENT_SOURCE_DIR}/common/x86
        -DARCH_X86_64=1
        -DSTACK_ALIGNMENT=16
        ${X264_NASM_EXTRA}
        ${ARGN}
        -o ${obj} ${CMAKE_CURRENT_SOURCE_DIR}/${source}
      DEPENDS ${CMAKE_CURRENT_SOURCE_DIR}/${source}
      COMMENT "nasm ${source}")
    set(${out_var} ${${out_var}} ${obj} PARENT_SCOPE)
  endfunction()
  x264_add_asm(X264_ASM_OBJECTS ${X264_ASM_PLAIN})
  foreach(asm ${X264_ASM_8BIT})
    x264_add_asm(X264_ASM_OBJECTS ${asm} -DBIT_DEPTH=8 -Dprivate_prefix=x264_8)
  endforeach()
else()
  set(X264_MMX 0)
  set(X264_INLINE_ASM 0)
  message(STATUS "x264: no nasm, the C version is used (a little slower)")
endif()

configure_file(${CMAKE_CURRENT_SOURCE_DIR}/config.h.in ${CMAKE_CURRENT_BINARY_DIR}/config.h @ONLY)

# --- the sources ------------------------------------------------------------
# The list is x264's own (see its Makefile): the common part, the encoder,
# and the x86 C files. The command line tool (x264.c) is not needed.
set(X264_SOURCES
  common/osdep.c
  common/base.c
  common/cpu.c
  common/tables.c
  common/mc.c
  common/predict.c
  common/pixel.c
  common/macroblock.c
  common/frame.c
  common/dct.c
  common/cabac.c
  common/common.c
  common/rectangle.c
  common/set.c
  common/quant.c
  common/deblock.c
  common/vlc.c
  common/mvpred.c
  common/bitstream.c
  encoder/api.c
  encoder/analyse.c
  encoder/me.c
  encoder/ratecontrol.c
  encoder/set.c
  encoder/macroblock.c
  encoder/cabac.c
  encoder/cavlc.c
  encoder/encoder.c
  encoder/lookahead.c)

# The thread pool (the generic layer) is always there; on Windows the
# system primitives come from win32thread.c.
list(APPEND X264_SOURCES common/threadpool.c)
if(WIN32)
  list(APPEND X264_SOURCES common/win32thread.c)
endif()

if(X264_MMX)
  list(APPEND X264_SOURCES common/x86/mc-c.c common/x86/predict-c.c)
endif()

add_library(x264 STATIC ${X264_SOURCES})
if(X264_ASM_OBJECTS)
  # The objects nasm produced: CMake must not try to compile them.
  set_source_files_properties(${X264_ASM_OBJECTS} PROPERTIES
    EXTERNAL_OBJECT TRUE GENERATED TRUE)
  target_sources(x264 PRIVATE ${X264_ASM_OBJECTS})
endif()
target_include_directories(x264
  # The generated config.h comes first: the source tree must not have one
  # that would shadow it (x264's own `configure` writes one ✗).
  PRIVATE ${CMAKE_CURRENT_BINARY_DIR}
  PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_definitions(x264 PRIVATE
  _WIN32_WINNT=0x0A00 WINVER=0x0A00
  NOMINMAX
  # The same two macros x264's own Makefile passes for the 8 bit build.
  HIGH_BIT_DEPTH=0 BIT_DEPTH=8)
if(MSVC)
  # x264 is C99; MSVC's C mode needed a while to catch up.
  target_compile_options(x264 PRIVATE /std:c11)
  target_compile_definitions(x264 PRIVATE _CRT_SECURE_NO_WARNINGS)
else()
  target_compile_options(x264 PRIVATE -Wno-unused-parameter -Wno-unused-function)
endif()

find_package(Threads REQUIRED)
target_link_libraries(x264 PUBLIC Threads::Threads)
