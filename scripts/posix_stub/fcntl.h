// A syntax-check harness for src/shell_posix.cpp.
//
// The POSIX backend cannot be compiled on this Windows machine (no Linux
// toolchain, no WSL distro). These headers give the compiler just enough of
// the POSIX world to *parse* the file: the point is to catch typos, wrong
// argument counts and missing includes - not to run anything.
//
//   g++ -std=c++17 -fsyntax-only -I .tea_agent_run/tmp/posix_stub \
//       -I src -I 3rd/nlohmann_json src/shell_posix.cpp
#pragma once
#include_next <fcntl.h>

#ifndef O_NOCTTY
#define O_NOCTTY 0x100
#endif

#ifdef __cplusplus
extern "C" {
#endif

int posix_openpt(int flags);
int grantpt(int fd);
int unlockpt(int fd);

#ifdef __cplusplus
}
#endif
