// tunnel_net.h -- the few socket calls the tunnel needs, on both platforms.
//
// The tunnel binaries are built for Windows as well as for POSIX, so the
// socket code cannot just include <sys/socket.h>. This is the same idea as
// src/net_compat.h in the agent: one small header instead of #ifdefs all over
// the sources.
#pragma once

#ifdef _WIN32

#include <winsock2.h>
#include <ws2tcpip.h>

#include <cstring>

using tea_socket_t = SOCKET;
constexpr tea_socket_t kTeaInvalidSocket = INVALID_SOCKET;
// Windows has no MSG_NOSIGNAL (it has no SIGPIPE to block).
constexpr int kTeaNoSignal = 0;

/// Bring up Winsock. Must be called once before any socket call.
inline int tea_net_init() {
  WSADATA data;
  return WSAStartup(MAKEWORD(2, 2), &data) == 0 ? 0 : -1;
}

inline void tea_net_close(tea_socket_t s) { ::closesocket(s); }

inline int tea_net_errno() { return WSAGetLastError(); }

inline int tea_net_set_nodelay(tea_socket_t s) {
  int one = 1;
  return ::setsockopt(s, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&one),
                       sizeof(one));
}

#else

#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>

using tea_socket_t = int;
constexpr tea_socket_t kTeaInvalidSocket = -1;
constexpr int kTeaNoSignal = MSG_NOSIGNAL;

inline int tea_net_init() { return 0; }

inline void tea_net_close(tea_socket_t s) { ::close(s); }

inline int tea_net_errno() { return errno; }

inline int tea_net_set_nodelay(tea_socket_t s) {
  int one = 1;
  return ::setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
}

#endif

/// The last socket error, as a message.
inline std::string tea_net_error_text() {
  return std::to_string(tea_net_errno());
}
