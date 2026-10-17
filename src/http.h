// http.h -- minimal HTTP/1.1 client.
//
// Two interchangeable backends:
//   * "socket"  : plain HTTP/1.1 over TCP, portable, used for http:// URLs.
//   * "winhttp" : WinHTTP (schannel), handles both http:// and https://.
//
// The engine can be forced globally with http_set_engine(), which is what the
// --http-engine command line flag does.
#pragma once

#include <cstddef>
#include <cstdint>
#include <functional>
#include <string>
#include <vector>

#include "tls.h"

namespace tea {

struct HttpHeader {
  std::string name;
  std::string value;
};

struct ParsedUrl {
  std::string scheme;
  std::string host;
  std::string path;   // includes the leading '/'
  std::string query;  // without the leading '?'
  int port = 0;
  bool valid = false;
  std::string error;
  bool is_https() const { return scheme == "https"; }
  std::string host_header() const;
  std::string request_target() const;
};

struct HttpRequest {
  std::string method = "POST";
  std::string url;
  std::vector<HttpHeader> headers;
  std::string body;
  std::string content_type = "application/json";
  int connect_timeout_ms = 30000;
  int read_timeout_ms = 600000;
  /// Stop appending to HttpResponse::body after this many bytes (0 = no limit).
  /// Streaming callers use this so a long SSE response does not grow unbounded.
  size_t body_limit = 0;
  bool follow_redirects = true;
};

struct HttpResponse {
  bool ok = false;  // got an HTTP status line
  bool aborted = false;
  int status = 0;
  std::string body;
  std::vector<HttpHeader> headers;
  std::string error;
  std::string effective_url;
  long long elapsed_ms = 0;
  std::string header(const std::string& name) const;  // case-insensitive, first match
  std::string header_all(const std::string& name) const;
};

/// Return false to abort the transfer early.
using HttpChunkCallback = std::function<bool(const char* data, std::size_t len)>;

ParsedUrl parse_url(const std::string& url);

/// Force a backend name ("auto", "socket", "winhttp", "tls"). Unknown names are
/// ignored. "tls" is an alias for "socket" (which performs TLS for https://).
void http_set_engine(const std::string& name);
std::string http_engine();
/// Human readable list of backends compiled into this binary.
std::string http_available_engines();

/// TLS settings used by the socket backend for https:// URLs.
void http_set_tls_config(const TlsOptions& config);
TlsOptions http_tls_config();

HttpResponse http_perform(const HttpRequest& req, const HttpChunkCallback& on_chunk = nullptr);

// ---- backends (exported for direct use/testing) ----
HttpResponse http_perform_socket(const HttpRequest& req, const HttpChunkCallback& on_chunk);
#ifdef _WIN32
HttpResponse http_perform_winhttp(const HttpRequest& req, const HttpChunkCallback& on_chunk);
#endif

}  // namespace tea
