// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tls.h -- minimal client-side TLS stream.
//
// A backend is compiled in only when a TLS library is available (currently
// mbedTLS, vendored under 3rd/mbedtls). On Windows the WinHTTP backend is an
// alternative that uses the OS certificate store instead of a CA bundle.
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>

namespace tea {

class TlsStream {
 public:
  virtual ~TlsStream() {}
  virtual bool handshake(const std::string& host, std::string* err) = 0;
  /// > 0 bytes read, 0 = clean end of stream, -1 = error (err is set).
  virtual int read(char* buffer, std::size_t capacity, std::string* err) = 0;
  virtual bool write(const char* data, std::size_t length, std::string* err) = 0;
  /// Wait until the stream has something to read, or the timeout passes.
  ///
  /// > 0 readable, 0 timed out, -1 not supported by this backend. read() on a
  /// blocking stream never returns on a keepalive tick, so a caller that has
  /// to do work on a timer (MQTT ping, heartbeat) uses this first.
  virtual int wait_readable(int timeout_ms) {
    (void)timeout_ms;
    return -1;
  }
  /// Bytes already buffered inside the TLS layer (read() would return without
  /// touching the socket). A caller that polls with wait_readable() has to ask
  /// this first, or it can sleep while a whole record is sitting in memory.
  virtual bool has_pending() const { return false; }
  virtual std::string description() const = 0;
};

struct TlsOptions {
  /// PEM file holding the trusted roots. Empty means "no roots loaded", which
  /// is only acceptable when verify_peer is false.
  std::string ca_bundle;
  /// PEM text already in memory (the bundle embedded at build time). Used when
  /// `ca_bundle` is empty; `ca_bundle` wins so an operator can still point at a
  /// newer file without rebuilding.
  std::string ca_pem;
  bool verify_peer = true;
};

/// True when this binary was built with a TLS backend.
bool tls_available();
std::string tls_backend_name();

/// Wrap an already-connected TCP socket. `socket_handle` is passed as an
/// integer so this header stays free of platform socket types.
std::unique_ptr<TlsStream> tls_wrap_socket(std::intptr_t socket_handle, const TlsOptions& options,
                                           std::string* err);

}  // namespace tea
