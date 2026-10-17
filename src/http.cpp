#include "http.h"

#include <algorithm>
#include <atomic>
#include <cstdlib>
#include <mutex>

#include "log.h"
#include "tls.h"
#include "util.h"

namespace tea {
namespace {

std::atomic<int> g_engine{0};  // 0 = auto, 1 = socket/tls, 2 = winhttp

std::mutex g_tls_mu;
TlsOptions g_tls_config;

int engine_from_name(const std::string& name) {
  const std::string v = to_lower(trim(name));
  if (v.empty() || v == "auto") return 0;
  if (v == "socket" || v == "tls" || v == "mbedtls") return 1;
  if (v == "winhttp") return 2;
  return -1;
}

}  // namespace

std::string ParsedUrl::host_header() const {
  const bool default_port = (scheme == "http" && port == 80) || (scheme == "https" && port == 443);
  return default_port ? host : (host + ":" + std::to_string(port));
}

std::string ParsedUrl::request_target() const {
  std::string target = path.empty() ? "/" : path;
  if (!query.empty()) target += "?" + query;
  return target;
}

std::string HttpResponse::header(const std::string& name) const {
  const std::string want = to_lower(name);
  for (const auto& h : headers) {
    if (to_lower(h.name) == want) return h.value;
  }
  return std::string();
}

std::string HttpResponse::header_all(const std::string& name) const {
  const std::string want = to_lower(name);
  std::string out;
  for (const auto& h : headers) {
    if (to_lower(h.name) == want) {
      if (!out.empty()) out += ", ";
      out += h.value;
    }
  }
  return out;
}

ParsedUrl parse_url(const std::string& url) {
  ParsedUrl out;
  const size_t scheme_end = url.find("://");
  if (scheme_end == std::string::npos) {
    out.error = "url has no scheme: " + url;
    return out;
  }
  out.scheme = to_lower(url.substr(0, scheme_end));
  if (out.scheme != "http" && out.scheme != "https") {
    out.error = "unsupported scheme: " + out.scheme;
    return out;
  }
  size_t pos = scheme_end + 3;
  const size_t path_start = url.find_first_of("/?#", pos);
  std::string authority =
      (path_start == std::string::npos) ? url.substr(pos) : url.substr(pos, path_start - pos);
  std::string rest = (path_start == std::string::npos) ? std::string() : url.substr(path_start);

  const size_t at = authority.rfind('@');
  if (at != std::string::npos) authority = authority.substr(at + 1);

  if (!authority.empty() && authority.front() == '[') {
    // IPv6 literal
    const size_t close = authority.find(']');
    if (close == std::string::npos) {
      out.error = "malformed IPv6 host";
      return out;
    }
    out.host = authority.substr(1, close - 1);
    if (close + 1 < authority.size() && authority[close + 1] == ':') {
      out.port = std::atoi(authority.c_str() + close + 2);
    }
  } else {
    const size_t colon = authority.rfind(':');
    if (colon != std::string::npos) {
      out.host = authority.substr(0, colon);
      out.port = std::atoi(authority.c_str() + colon + 1);
    } else {
      out.host = authority;
    }
  }
  if (out.host.empty()) {
    out.error = "url has no host: " + url;
    return out;
  }
  if (out.port <= 0 || out.port > 65535) out.port = (out.scheme == "https") ? 443 : 80;

  if (!rest.empty() && rest.front() == '?') {
    out.query = rest.substr(1);
    out.path = "/";
  } else {
    const size_t q = rest.find('?');
    if (q == std::string::npos) {
      out.path = rest.empty() ? "/" : rest;
    } else {
      out.path = rest.substr(0, q);
      out.query = rest.substr(q + 1);
    }
  }
  if (out.path.empty()) out.path = "/";
  out.valid = true;
  return out;
}

void http_set_engine(const std::string& name) {
  const int engine = engine_from_name(name);
  if (engine < 0) {
    LOG_WARN("http: unknown engine '", name, "' (try auto|socket|winhttp)");
    return;
  }
  g_engine.store(engine);
}

std::string http_engine() {
  switch (g_engine.load()) {
    case 1: return "socket";
    case 2: return "winhttp";
    default: return "auto";
  }
}

std::string http_available_engines() {
  std::string out = "socket";
  if (tls_available()) out += "(tls:" + tls_backend_name() + ")";
#ifdef _WIN32
  out += ",winhttp";
#endif
  return out;
}

void http_set_tls_config(const TlsOptions& config) {
  std::lock_guard<std::mutex> lock(g_tls_mu);
  g_tls_config = config;
}

TlsOptions http_tls_config() {
  std::lock_guard<std::mutex> lock(g_tls_mu);
  return g_tls_config;
}

HttpResponse http_perform(const HttpRequest& req, const HttpChunkCallback& on_chunk) {
  const ParsedUrl url = parse_url(req.url);
  if (!url.valid) {
    HttpResponse bad;
    bad.error = url.error;
    return bad;
  }
  const int forced = g_engine.load();

  HttpResponse res;
  if (forced == 1) {
    res = http_perform_socket(req, on_chunk);
  } else if (forced == 2) {
#ifdef _WIN32
    res = http_perform_winhttp(req, on_chunk);
#else
    res.error = "the winhttp engine is only available on Windows";
#endif
  } else if (!url.is_https()) {
    res = http_perform_socket(req, on_chunk);
  } else {
#ifdef _WIN32
    res = http_perform_winhttp(req, on_chunk);
    // WinHTTP relies on schannel, which is unavailable in some restricted
    // environments (sandboxes, service accounts). Fall back to the bundled TLS
    // stack when the request never even produced a status line.
    if (res.status == 0 && !res.aborted && tls_available()) {
      LOG_WARN("http: WinHTTP could not reach ", url.host, " (", res.error,
               "); retrying with the bundled TLS backend");
      const HttpResponse retry = http_perform_socket(req, on_chunk);
      if (retry.ok) res = retry;
    }
#else
    if (tls_available()) {
      res = http_perform_socket(req, on_chunk);
    } else {
      res.error =
          "https:// is not supported by this build: no TLS backend is compiled in (add "
          "3rd/mbedtls)";
    }
#endif
  }
  if (res.effective_url.empty()) res.effective_url = req.url;
  return res;
}

}  // namespace tea
