// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_proc_stat.h -- what this process uses right now: open files, memory.
//
// The broker prints one line every minute with these numbers, so that the
// operator can see how close it is to the limits (open files, memory). Both
// numbers are best effort: when the platform does not tell us, the answer is
// -1 and the caller prints "n/a".
//
// Linux reads /proc; Windows answers the handle count and says "n/a" for the
// memory (the broker only runs there for development).
#pragma once

#include <cstddef>
#include <cstdio>

#ifdef _WIN32
#include <windows.h>
#else
#include <dirent.h>
#include <unistd.h>
#endif

namespace tea_tunnel {

/// The number of open file descriptors (the sockets included), or -1 when the
/// platform does not say.
inline int process_fd_count() {
#ifdef _WIN32
  DWORD count = 0;
  if (!GetProcessHandleCount(GetCurrentProcess(), &count)) return -1;
  return static_cast<int>(count);
#else
  DIR* dir = ::opendir("/proc/self/fd");
  if (!dir) return -1;
  int count = 0;
  while (struct dirent* entry = ::readdir(dir)) {
    if (entry->d_name[0] == '.') continue;  // "." and ".."
    ++count;
  }
  ::closedir(dir);
  // The handle of the directory itself is in the list: do not count it.
  return count > 0 ? count - 1 : count;
#endif
}

/// The resident memory of this process in bytes, or -1 when the platform does
/// not say.
inline long long process_rss_bytes() {
#ifdef _WIN32
  return -1;  // the broker is not used on Windows in production
#else
  std::FILE* file = std::fopen("/proc/self/statm", "rb");
  if (!file) return -1;
  long long total = 0;
  long long resident = 0;
  const int read = std::fscanf(file, "%lld %lld", &total, &resident);
  std::fclose(file);
  if (read != 2) return -1;
  const long long page = ::sysconf(_SC_PAGESIZE);
  if (page <= 0) return -1;
  return resident * page;
#endif
}

}  // namespace tea_tunnel
