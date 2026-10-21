// http_socket.cpp -- portable plain-HTTP/1.1 backend (TCP sockets).
#include "http.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <mutex>

#include "log.h"
#include "tls.h"
#include "util.h"
#include "version.h"

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#else
#include <arpa/inet.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/types.h>
#include <unistd.h>
#endif

#include "net_compat.h"

namespace tea {
namespace {

#ifdef _WIN32
using tea_socket_t = SOCKET;
const tea_socket_t kInvalidSocket = INVALID_SOCKET;
void close_socket(tea_socket_t s) {
  if (s != INVALID_SOCKET) closesocket(s);
}
int last_socket_error() { return WSAGetLastError(); }
bool error_is_timeout(int e) { return e == WSAETIMEDOUT || e == WSAEWOULDBLOCK; }
std::string socket_error_text(int e) {
  char* buffer = nullptr;
  const DWORD n = FormatMessageA(
      FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS,
      nullptr, static_cast<DWORD>(e), MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT),
      reinterpret_cast<char*>(&buffer), 0, nullptr);
  std::string text = (n && buffer) ? std::string(buffer, n) : ("socket error " + std::to_string(e));
  if (buffer) LocalFree(buffer);
  return trim(text);
}
void ensure_winsock() {
  static std::once_flag once;
  std::call_once(once, [] {
    WSADATA data;
    WSAStartup(MAKEWORD(2, 2), &data);
  });
}
#else  // ---------------------------------------------------------------------
using tea_socket_t = int;
const tea_socket_t kInvalidSocket = -1;
void close_socket(tea_socket_t s) {
  if (s >= 0) ::close(s);
}
int last_socket_error() { return errno; }
bool error_is_timeout(int e) { return e == EAGAIN || e == EWOULDBLOCK || e == EINPROGRESS; }
std::string socket_error_text(int e) { return std::strerror(e); }
void ensure_winsock() {}
int select_nfds(tea_socket_t s) { return static_cast<int>(s) + 1; }
#endif

int wait_ready(tea_socket_t s, bool want_write, int timeout_ms) {
  fd_set fds;
  FD_ZERO(&fds);
  FD_SET(s, &fds);
  timeval tv;
  int ms = timeout_ms < 0 ? 0 : timeout_ms;
  tv.tv_sec = ms / 1000;
  tv.tv_usec = (ms % 1000) * 1000;
#ifdef _WIN32
  return ::select(0, want_write ? nullptr : &fds, want_write ? &fds : nullptr, nullptr, &tv);
#else
  return ::select(select_nfds(s), want_write ? nullptr : &fds, want_write ? &fds : nullptr, nullptr,
                  &tv);
#endif
}

void set_nonblocking(tea_socket_t s, bool nonblocking) {
#ifdef _WIN32
  u_long mode = nonblocking ? 1 : 0;
  ioctlsocket(s, FIONBIO, &mode);
#else
  int flags = fcntl(s, F_GETFL, 0);
  if (flags < 0) return;
  if (nonblocking) {
    fcntl(s, F_SETFL, flags | O_NONBLOCK);
  } else {
    fcntl(s, F_SETFL, flags & ~O_NONBLOCK);
  }
#endif
}

#if defined(TEA_STATIC_LIBC) && defined(__GLIBC__)
extern "C" int __nss_configure_lookup(const char* db, const char* configuration);
#endif

/// A statically linked glibc cannot load NSS modules safely: getaddrinfo
/// dlopen()s libnss_resolve.so.2 / libnss_mdns*.so.2 from /etc/nsswitch.conf,
/// and mixing a shared module into a static binary segfaults (verified with a
/// 20-line static repro on a systemd-resolved host). Pinning the "hosts"
/// database to the two implementations glibc compiles in keeps DNS working.
///
/// The trade-off, which only applies to static builds: systemd-resolved's
/// module and mDNS are bypassed. Plain DNS still works - on such hosts
/// /etc/resolv.conf points at the 127.0.0.53 stub, so names still resolve
/// through systemd-resolved, just without its NSS front end.
void configure_nss_for_static_build() {
#if defined(TEA_STATIC_LIBC) && defined(__GLIBC__)
  static std::once_flag once;
  std::call_once(once, [] {
    const int rc = __nss_configure_lookup("hosts", "files dns");
    if (rc != 0) {
      LOG_WARN("nss: cannot restrict the hosts database to 'files dns' (rc=", rc,
               "); name resolution may crash in this statically linked build");
    } else {
      LOG_DEBUG("nss: hosts lookups restricted to 'files dns' (static build)");
    }
  });
#endif
}

tea_socket_t tcp_connect(const std::string& host, int port, int timeout_ms, std::string* err) {
  ensure_winsock();
  configure_nss_for_static_build();
  addrinfo hints{};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  hints.ai_protocol = IPPROTO_TCP;
  addrinfo* list = nullptr;
  char port_text[16];
  std::snprintf(port_text, sizeof(port_text), "%d", port);
  const int rc = getaddrinfo(host.c_str(), port_text, &hints, &list);
  if (rc != 0 || !list) {
    *err = "cannot resolve host '" + host + "'";
    return kInvalidSocket;
  }

  tea_socket_t fd = kInvalidSocket;
  std::string last_error;
  for (addrinfo* ai = list; ai != nullptr; ai = ai->ai_next) {
    tea_socket_t s = ::socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
    if (s == kInvalidSocket) continue;

    set_nonblocking(s, true);
    const int cr = ::connect(s, ai->ai_addr, static_cast<int>(ai->ai_addrlen));
    bool connected = (cr == 0);
    if (!connected) {
      const int e = last_socket_error();
      if (error_is_timeout(e)) {
        if (wait_ready(s, true, timeout_ms) > 0) {
          int so_error = 0;
#ifdef _WIN32
          int len = sizeof(so_error);
#else
          socklen_t len = sizeof(so_error);
#endif
          if (getsockopt(s, SOL_SOCKET, SO_ERROR, reinterpret_cast<char*>(&so_error), &len) == 0 &&
              so_error == 0) {
            connected = true;
          } else {
            last_error = socket_error_text(so_error);
          }
        } else {
          last_error = "connection timed out";
        }
      } else {
        last_error = socket_error_text(e);
      }
    }
    if (!connected) {
      close_socket(s);
      continue;
    }

    set_nonblocking(s, false);
    tea_disable_sigpipe(static_cast<std::intptr_t>(s));
    int one = 1;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&one), sizeof(one));
#ifdef _WIN32
    DWORD rcv = static_cast<DWORD>(timeout_ms > 0 ? timeout_ms : 60000);
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&rcv), sizeof(rcv));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&rcv), sizeof(rcv));
#else
    timeval tv;
    tv.tv_sec = (timeout_ms > 0 ? timeout_ms : 60000) / 1000;
    tv.tv_usec = ((timeout_ms > 0 ? timeout_ms : 60000) % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
#endif
    fd = s;
    break;
  }
  freeaddrinfo(list);
  if (fd == kInvalidSocket) *err = last_error.empty() ? "connection failed" : last_error;
  return fd;
}

bool send_all_fd(tea_socket_t fd, const std::string& data, int timeout_ms, std::string* err) {
  size_t sent = 0;
  while (sent < data.size()) {
    const int n = ::send(fd, data.data() + sent, static_cast<int>(data.size() - sent),
                         TEA_SEND_FLAGS);
    if (n > 0) {
      sent += static_cast<size_t>(n);
      continue;
    }
    const int e = last_socket_error();
    if (error_is_timeout(e)) {
      if (wait_ready(fd, true, timeout_ms) > 0) continue;
      *err = "send timed out";
      return false;
    }
    *err = "send failed: " + socket_error_text(e);
    return false;
  }
  return true;
}

/// A connection that transparently adds TLS when the URL is https://.
struct Conn {
  tea_socket_t fd = kInvalidSocket;
  std::unique_ptr<TlsStream> tls;
  std::string buf;
  bool eof = false;
  std::string err;

  ~Conn() { close_socket(fd); }

  bool send_all(const std::string& data, int timeout_ms, std::string* out_err) {
    if (tls) return tls->write(data.data(), data.size(), out_err);
    return send_all_fd(fd, data, timeout_ms, out_err);
  }

  /// Pull more bytes into `buf`. Returns false on fatal error (err set).
  bool fill(int timeout_ms) {
    if (eof) return true;
    char tmp[16384];
    if (tls) {
      std::string tls_err;
      const int n = tls->read(tmp, sizeof(tmp), &tls_err);
      if (n > 0) {
        buf.append(tmp, static_cast<size_t>(n));
        return true;
      }
      if (n == 0) {
        eof = true;
        return true;
      }
      err = tls_err.empty() ? "TLS read failed" : tls_err;
      return false;
    }
    const int n = ::recv(fd, tmp, sizeof(tmp), 0);
    if (n > 0) {
      buf.append(tmp, static_cast<size_t>(n));
      return true;
    }
    if (n == 0) {
      eof = true;
      return true;
    }
    const int e = last_socket_error();
    if (error_is_timeout(e)) {
      err = "read timed out after " + std::to_string(timeout_ms) + "ms";
      return false;
    }
    err = "recv failed: " + socket_error_text(e);
    return false;
  }

  bool ensure(size_t need, int timeout_ms) {
    while (buf.size() < need && !eof) {
      if (!fill(timeout_ms)) return false;
    }
    return true;
  }

  /// Read one CRLF/LF terminated line (line returned without the terminator).
  bool read_line(std::string* line, int timeout_ms) {
    for (;;) {
      const size_t nl = buf.find('\n');
      if (nl != std::string::npos) {
        std::string out = buf.substr(0, nl);
        buf.erase(0, nl + 1);
        if (!out.empty() && out.back() == '\r') out.pop_back();
        *line = out;
        return true;
      }
      if (eof) {
        if (buf.empty()) return false;
        *line = buf;
        buf.clear();
        return true;
      }
      if (!fill(timeout_ms)) return false;
    }
  }
};

size_t find_header_end(const std::string& buf, size_t* sep_len) {
  const size_t crlf = buf.find("\r\n\r\n");
  const size_t lf = buf.find("\n\n");
  if (crlf == std::string::npos && lf == std::string::npos) return std::string::npos;
  if (crlf != std::string::npos && (lf == std::string::npos || crlf <= lf)) {
    *sep_len = 4;
    return crlf;
  }
  *sep_len = 2;
  return lf;
}

std::string resolve_redirect(const ParsedUrl& base, const std::string& location) {
  if (location.empty()) return std::string();
  if (location.find("://") != std::string::npos) return location;
  std::string origin = base.scheme + "://" + base.host_header();
  if (location.front() == '/') return origin + location;
  std::string dir = base.path;
  const size_t slash = dir.rfind('/');
  dir = (slash == std::string::npos) ? std::string("/") : dir.substr(0, slash + 1);
  return origin + dir + location;
}

struct BodySink {
  HttpResponse* res;
  const HttpChunkCallback* cb;
  size_t limit;
  bool stop = false;

  void push(const char* data, size_t len) {
    if (stop || len == 0) return;
    if (limit == 0 || res->body.size() < limit) {
      const size_t room = (limit == 0) ? len : (limit - res->body.size());
      res->body.append(data, len < room ? len : room);
    }
    if (*cb) {
      if (!(*cb)(data, len)) {
        stop = true;
        res->aborted = true;
      }
    }
  }
};

HttpResponse perform_once(const HttpRequest& req, const HttpChunkCallback& on_chunk) {
  HttpResponse res;
  res.effective_url = req.url;
  const ParsedUrl url = parse_url(req.url);
  if (!url.valid) {
    res.error = url.error;
    return res;
  }
  if (url.scheme != "http" && url.scheme != "https") {
    res.error = "socket engine only supports http:// and https:// URLs";
    return res;
  }

  std::string err;
  Conn conn;
  conn.fd = tcp_connect(url.host, url.port, req.connect_timeout_ms, &err);
  if (conn.fd == kInvalidSocket) {
    res.error = err;
    return res;
  }

  if (url.is_https()) {
    if (!tls_available()) {
      res.error =
          "https:// support is not compiled into this build (no TLS backend, and WinHTTP is not "
          "available)";
      return res;
    }
    std::string tls_err;
    conn.tls = tls_wrap_socket(static_cast<std::intptr_t>(conn.fd), http_tls_config(), &tls_err);
    if (!conn.tls) {
      res.error = tls_err;
      return res;
    }
    if (!conn.tls->handshake(url.host, &tls_err)) {
      res.error = tls_err;
      return res;
    }
    LOG_DEBUG("http: TLS established with ", url.host, " (", conn.tls->description(), ")");
  }

  std::string head;
  head.reserve(512);
  head += req.method;
  head += ' ';
  head += url.request_target();
  head += " HTTP/1.1\r\nHost: ";
  head += url.host_header();
  head += "\r\nUser-Agent: tea_agent/" + std::string(tea::version_string()) + "\r\nAccept: */*\r\nAccept-Encoding: identity\r\nConnection: close\r\n";
  bool has_content_type = false;
  bool has_content_length = false;
  for (const auto& h : req.headers) {
    const std::string lname = to_lower(h.name);
    if (lname == "content-type") has_content_type = true;
    if (lname == "content-length") has_content_length = true;
    head += h.name;
    head += ": ";
    head += h.value;
    head += "\r\n";
  }
  if (!req.body.empty()) {
    if (!has_content_type) {
      head += "Content-Type: ";
      head += req.content_type;
      head += "\r\n";
    }
    if (!has_content_length) {
      head += "Content-Length: ";
      head += std::to_string(req.body.size());
      head += "\r\n";
    }
  }
  head += "\r\n";

  if (!conn.send_all(head, req.connect_timeout_ms, &err) ||
      (!req.body.empty() && !conn.send_all(req.body, req.connect_timeout_ms, &err))) {
    res.error = err;
    return res;
  }

  // ---- status line + headers ----
  size_t sep_len = 0;
  while (find_header_end(conn.buf, &sep_len) == std::string::npos) {
    if (conn.buf.size() > 512 * 1024) {
      res.error = "response headers too large";
      return res;
    }
    if (!conn.fill(req.read_timeout_ms)) {
      res.error = conn.err.empty() ? "connection closed before response headers" : conn.err;
      return res;
    }
    if (conn.eof && find_header_end(conn.buf, &sep_len) == std::string::npos) {
      res.error = "connection closed before response headers";
      return res;
    }
  }
  const size_t header_end = find_header_end(conn.buf, &sep_len);
  const std::string header_block = conn.buf.substr(0, header_end);
  conn.buf.erase(0, header_end + sep_len);

  const std::vector<std::string> lines = split_lines(header_block);
  if (lines.empty()) {
    res.error = "empty response";
    return res;
  }
  {
    const std::vector<std::string> parts = split(lines[0], ' ');
    if (parts.size() < 2) {
      res.error = "malformed status line: " + lines[0];
      return res;
    }
    res.status = std::atoi(parts[1].c_str());
    res.ok = res.status > 0;
  }
  for (size_t i = 1; i < lines.size(); ++i) {
    const size_t colon = lines[i].find(':');
    if (colon == std::string::npos) continue;
    HttpHeader h;
    h.name = trim(lines[i].substr(0, colon));
    h.value = trim(lines[i].substr(colon + 1));
    res.headers.push_back(h);
  }

  // ---- body ----
  BodySink sink{&res, &on_chunk, req.body_limit, false};
  const std::string transfer_encoding = to_lower(res.header("transfer-encoding"));
  const bool chunked = transfer_encoding.find("chunked") != std::string::npos;
  long long content_length = -1;
  const std::string cl = res.header("content-length");
  if (!cl.empty()) content_length = std::atoll(cl.c_str());

  if (chunked) {
    for (;;) {
      if (sink.stop) break;
      std::string size_line;
      if (!conn.read_line(&size_line, req.read_timeout_ms)) {
        if (!conn.err.empty()) res.error = conn.err;
        break;
      }
      const size_t semi = size_line.find(';');
      if (semi != std::string::npos) size_line = size_line.substr(0, semi);
      const long long chunk_size = std::strtoll(trim(size_line).c_str(), nullptr, 16);
      if (chunk_size < 0) {
        res.error = "malformed chunk size";
        break;
      }
      if (chunk_size == 0) {
        // consume optional trailers
        std::string trailer;
        while (conn.read_line(&trailer, req.read_timeout_ms) && !trailer.empty()) {
        }
        break;
      }
      if (!conn.ensure(static_cast<size_t>(chunk_size) + 2, req.read_timeout_ms)) {
        if (!conn.err.empty()) res.error = conn.err;
        break;
      }
      sink.push(conn.buf.data(), static_cast<size_t>(chunk_size));
      conn.buf.erase(0, static_cast<size_t>(chunk_size) + 2);
    }
  } else if (content_length >= 0) {
    size_t remaining = static_cast<size_t>(content_length);
    if (!conn.buf.empty()) {
      const size_t take = conn.buf.size() < remaining ? conn.buf.size() : remaining;
      sink.push(conn.buf.data(), take);
      conn.buf.erase(0, take);
      remaining -= take;
    }
    while (remaining > 0 && !sink.stop) {
      if (!conn.fill(req.read_timeout_ms)) {
        if (!conn.err.empty()) res.error = conn.err;
        break;
      }
      if (conn.buf.empty()) {
        if (conn.eof) {
          res.error = "connection closed before the full body was received";
          break;
        }
        continue;
      }
      const size_t take = conn.buf.size() < remaining ? conn.buf.size() : remaining;
      sink.push(conn.buf.data(), take);
      conn.buf.erase(0, take);
      remaining -= take;
    }
  } else {
    if (!conn.buf.empty()) {
      sink.push(conn.buf.data(), conn.buf.size());
      conn.buf.clear();
    }
    while (!sink.stop && !conn.eof) {
      if (!conn.fill(req.read_timeout_ms)) {
        if (!conn.err.empty()) res.error = conn.err;
        break;
      }
      if (!conn.buf.empty()) {
        sink.push(conn.buf.data(), conn.buf.size());
        conn.buf.clear();
      }
    }
  }
  return res;
}

}  // namespace

HttpResponse http_perform_socket(const HttpRequest& req, const HttpChunkCallback& on_chunk) {
  HttpRequest current = req;
  const int64_t start = now_ms();
  HttpResponse res;
  for (int hop = 0; hop <= 5; ++hop) {
    res = perform_once(current, on_chunk);
    res.effective_url = current.url;
    if (!req.follow_redirects || !res.ok) break;
    if (res.status != 301 && res.status != 302 && res.status != 303 && res.status != 307 &&
        res.status != 308) {
      break;
    }
    const std::string location = res.header("location");
    if (location.empty()) break;
    const ParsedUrl base = parse_url(current.url);
    const std::string next = resolve_redirect(base, location);
    if (next.empty()) break;
    LOG_DEBUG("http: redirect ", res.status, " -> ", next);
    HttpResponse redirect_body;  // discard the redirect body
    current.url = next;
    if (res.status == 303 || ((res.status == 301 || res.status == 302) && current.method == "POST")) {
      current.method = "GET";
      current.body.clear();
    }
  }
  res.elapsed_ms = now_ms() - start;
  return res;
}

}  // namespace tea
