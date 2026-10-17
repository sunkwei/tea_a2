// net_compat.h -- socket compatibility shims that must be included *after* the
// platform socket headers.
//
// SIGPIPE handling differs per platform:
//   Linux   MSG_NOSIGNAL flag on send()
//   Windows no SIGPIPE at all
//   macOS / BSD  the SO_NOSIGPIPE socket option (MSG_NOSIGNAL is undefined)
//
// Without this, a peer closing the connection mid-stream would kill the
// process with SIGPIPE on macOS.
#pragma once

#include <cstdint>

#ifdef _WIN32
#  define TEA_SEND_FLAGS 0
#else
#  if defined(MSG_NOSIGNAL)
#    define TEA_SEND_FLAGS MSG_NOSIGNAL
#  else
#    define TEA_SEND_FLAGS 0
#  endif
#endif

/// Suppress SIGPIPE for this socket where the platform needs a socket option.
/// No-op on Windows (nothing to suppress) and on Linux (send flags handle it).
inline void tea_disable_sigpipe(std::intptr_t fd) {
#if defined(SO_NOSIGPIPE)
  int one = 1;
  ::setsockopt(static_cast<int>(fd), SOL_SOCKET, SO_NOSIGPIPE, &one,
               static_cast<socklen_t>(sizeof(one)));
#else
  (void)fd;
#endif
}
