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
  virtual std::string description() const = 0;
};

struct TlsOptions {
  /// PEM file holding the trusted roots. Empty means "no roots loaded", which
  /// is only acceptable when verify_peer is false.
  std::string ca_bundle;
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
