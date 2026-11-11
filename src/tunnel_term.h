// tunnel_term.h -- the terminal that the tunnel can carry.
//
// The sidecar is normally a pipe: it forwards raw bytes between the two ends.
// This file adds the other half -- a small HTTP service that hands out a real
// shell (a PTY) on the machine the sidecar runs on.
//
// Why it lives here and not in the agent:
//   * the tunnel's job is to reach a machine that has no route from the
//     outside. A terminal on that machine is exactly the thing an operator
//     needs, and the sidecar is already there;
//   * the agent is optional. A device may run the sidecar alone, and then the
//     terminal is still there;
//   * the wire protocol does not change. The sidecar still forwards bytes;
//     the terminal is just another HTTP service on the far end, and the
//     browser talks to it through the same port the tunnel already uses.
//
// The service speaks the same shape as the agent's /api/shell endpoints, so
// the browser side of the terminal is one page that works either way.
#pragma once

#include <cstdint>
#include <memory>
#include <string>

namespace tea_tunnel {

/// The HTTP terminal service.
///
/// It listens on 127.0.0.1 only: it is reached through the tunnel, never
/// directly. `start()` picks a free port and reports it, so two terminals on
/// one machine never collide.
class TerminalService {
 public:
  TerminalService();
  ~TerminalService();

  TerminalService(const TerminalService&) = delete;
  TerminalService& operator=(const TerminalService&) = delete;

  /// Start listening. Returns false and fills `err` when the port could not
  /// be taken or the shell is not available on this platform.
  bool start(std::string* err);

  /// Stop listening and end every shell.
  void stop();

  /// The port the service listens on (0 when it is not running).
  uint16_t port() const { return port_; }

  /// True when this machine can give out a shell at all.
  static bool available();

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  uint16_t port_ = 0;
};

}  // namespace tea_tunnel
