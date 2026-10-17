// http_winhttp.cpp -- Windows backend built on WinHTTP (schannel).
//
// Used for every https:// request and, when forced with --http-engine winhttp,
// for plain http:// as well. Because the same code path serves both schemes,
// the TLS path can be exercised end to end against a local http:// server.
#include "http.h"

#ifdef _WIN32

#include <winsock2.h>
#include <windows.h>
#include <winhttp.h>

#include <cstdio>
#include <cstdlib>
#include <memory>

#include "log.h"
#include "util.h"
#include "version.h"

namespace tea {
namespace {

struct HandleCloser {
  void operator()(HINTERNET h) const {
    if (h) WinHttpCloseHandle(h);
  }
};
using WinHttpHandle = std::unique_ptr<void, HandleCloser>;

std::string last_error_text(const char* what) {
  const DWORD code = GetLastError();
  char buffer[512];
  std::snprintf(buffer, sizeof(buffer), "%s failed (WinHTTP error %lu)", what,
                static_cast<unsigned long>(code));
  return buffer;
}

}  // namespace

HttpResponse http_perform_winhttp(const HttpRequest& req, const HttpChunkCallback& on_chunk) {
  HttpResponse res;
  res.effective_url = req.url;
  const int64_t started = now_ms();

  const ParsedUrl url = parse_url(req.url);
  if (!url.valid) {
    res.error = url.error;
    return res;
  }

  std::string proxy;
  {
    const char* env = std::getenv(url.is_https() ? "HTTPS_PROXY" : "HTTP_PROXY");
    if (!env || !*env) env = std::getenv(url.is_https() ? "https_proxy" : "http_proxy");
    if (env && *env) proxy = env;
  }

  // A proxy value that still contains the scheme has to be split by hand. The
  // proxy port is not passed to WinHttpConnect: with a named proxy, WinHTTP
  // connects to the target host through the proxy itself.
  std::wstring proxy_host;
  if (!proxy.empty()) {
    std::string p = proxy;
    const std::size_t scheme = p.find("://");
    if (scheme != std::string::npos) p = p.substr(scheme + 3);
    const std::size_t at = p.find('@');
    if (at != std::string::npos) p = p.substr(at + 1);
    const std::size_t colon = p.rfind(':');
    proxy_host = utf8_to_wide(colon == std::string::npos ? p : p.substr(0, colon));
  }

  const std::wstring host = utf8_to_wide(url.host);
  const std::wstring path = utf8_to_wide(url.request_target());
  const std::wstring method = utf8_to_wide(req.method);

  const DWORD access_type =
      proxy_host.empty() ? WINHTTP_ACCESS_TYPE_NO_PROXY : WINHTTP_ACCESS_TYPE_NAMED_PROXY;
  LPCWSTR proxy_name = proxy_host.empty() ? nullptr : proxy_host.c_str();
  LPCWSTR proxy_bypass = nullptr;

  WinHttpHandle session(WinHttpOpen(L"tea_agent/" TEA_AGENT_VERSION_STRING, access_type,
                                    proxy_name, proxy_bypass, 0));
  if (!session) {
    res.error = last_error_text("WinHttpOpen");
    return res;
  }
  {
    const int connect_timeout = req.connect_timeout_ms > 0 ? req.connect_timeout_ms : 30000;
    const int receive_timeout = req.read_timeout_ms > 0 ? req.read_timeout_ms : 600000;
    WinHttpSetTimeouts(session.get(), connect_timeout, connect_timeout, connect_timeout,
                       receive_timeout);
  }

  WinHttpHandle connect(WinHttpConnect(session.get(), host.c_str(),
                                       static_cast<INTERNET_PORT>(url.port), 0));
  if (!connect) {
    res.error = last_error_text("WinHttpConnect");
    return res;
  }

  DWORD flags = url.is_https() ? WINHTTP_FLAG_SECURE : 0;
  WinHttpHandle request(WinHttpOpenRequest(connect.get(), method.c_str(), path.c_str(), nullptr,
                                           WINHTTP_NO_REFERER, WINHTTP_DEFAULT_ACCEPT_TYPES,
                                           flags));
  if (!request) {
    res.error = last_error_text("WinHttpOpenRequest");
    return res;
  }

  // Do not let WinHTTP negotiate gzip/deflate: we do not decompress. WinHTTP
  // leaves compression off by default, so this is a belt-and-braces extra that
  // is only available with the newer SDK headers.
#ifdef WINHTTP_DECOMPRESSION_FLAG_NONE
  {
    DWORD decompression = WINHTTP_DECOMPRESSION_FLAG_NONE;
    WinHttpSetOption(request.get(), WINHTTP_OPTION_DECOMPRESSION, &decompression,
                     sizeof(decompression));
  }
#endif

  std::wstring headers;
  bool has_content_type = false;
  for (std::size_t i = 0; i < req.headers.size(); ++i) {
    const std::string lname = to_lower(req.headers[i].name);
    if (lname == "content-length" || lname == "host" || lname == "connection") continue;
    if (lname == "content-type") has_content_type = true;
    headers += utf8_to_wide(req.headers[i].name + ": " + req.headers[i].value + "\r\n");
  }
  if (!req.body.empty() && !has_content_type) {
    headers += utf8_to_wide("Content-Type: " + req.content_type + "\r\n");
  }

  const DWORD body_length = static_cast<DWORD>(req.body.size());
  LPVOID body_ptr = req.body.empty() ? WINHTTP_NO_REQUEST_DATA
                                     : const_cast<char*>(req.body.data());

  if (!WinHttpSendRequest(request.get(),
                          headers.empty() ? WINHTTP_NO_ADDITIONAL_HEADERS : headers.c_str(),
                          headers.empty() ? 0 : static_cast<DWORD>(headers.size()), body_ptr,
                          body_length, body_length, 0)) {
    res.error = last_error_text("WinHttpSendRequest");
    return res;
  }
  if (!WinHttpReceiveResponse(request.get(), nullptr)) {
    res.error = last_error_text("WinHttpReceiveResponse");
    return res;
  }

  {
    DWORD status = 0;
    DWORD size = sizeof(status);
    if (WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_STATUS_CODE | WINHTTP_QUERY_FLAG_NUMBER,
                            WINHTTP_HEADER_NAME_BY_INDEX, &status, &size, WINHTTP_NO_HEADER_INDEX)) {
      res.status = static_cast<int>(status);
      res.ok = true;
    }
  }
  {
    DWORD size = 0;
    WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_RAW_HEADERS_CRLF,
                        WINHTTP_HEADER_NAME_BY_INDEX, nullptr, &size, WINHTTP_NO_HEADER_INDEX);
    if (size > 0) {
      std::wstring raw(size / sizeof(wchar_t), L'\0');
      if (WinHttpQueryHeaders(request.get(), WINHTTP_QUERY_RAW_HEADERS_CRLF,
                              WINHTTP_HEADER_NAME_BY_INDEX, &raw[0], &size,
                              WINHTTP_NO_HEADER_INDEX)) {
        while (!raw.empty() && raw.back() == L'\0') raw.pop_back();
        const std::vector<std::string> lines = split_lines(wide_to_utf8(raw));
        for (std::size_t i = 1; i < lines.size(); ++i) {
          const std::size_t colon = lines[i].find(':');
          if (colon == std::string::npos) continue;
          HttpHeader h;
          h.name = trim(lines[i].substr(0, colon));
          h.value = trim(lines[i].substr(colon + 1));
          if (!h.name.empty()) res.headers.push_back(h);
        }
      }
    }
  }

  bool aborted = false;
  for (;;) {
    DWORD available = 0;
    if (!WinHttpQueryDataAvailable(request.get(), &available)) {
      res.error = last_error_text("WinHttpQueryDataAvailable");
      break;
    }
    if (available == 0) break;

    std::string buffer;
    buffer.resize(available);
    DWORD read = 0;
    if (!WinHttpReadData(request.get(), &buffer[0], available, &read)) {
      res.error = last_error_text("WinHttpReadData");
      break;
    }
    if (read == 0) break;
    buffer.resize(read);

    if (req.body_limit == 0 || res.body.size() < req.body_limit) {
      const std::size_t room =
          req.body_limit == 0 ? read : (req.body_limit - res.body.size());
      res.body.append(buffer.data(), read < room ? read : room);
    }
    if (on_chunk && !on_chunk(buffer.data(), buffer.size())) {
      aborted = true;
      res.aborted = true;
      break;
    }
  }

  res.aborted = res.aborted || aborted;
  res.elapsed_ms = now_ms() - started;
  LOG_DEBUG("winhttp: ", req.method, " ", req.url, " -> ", res.status, " in ", res.elapsed_ms,
            "ms bytes=", res.body.size());
  return res;
}

}  // namespace tea

#endif  // _WIN32
