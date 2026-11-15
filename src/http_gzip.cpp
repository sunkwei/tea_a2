// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
#include "http_gzip.h"

#include <cctype>
#include <cstdlib>

namespace tea {
namespace {

std::string trim(const std::string& s) {
  std::size_t begin = 0;
  std::size_t end = s.size();
  while (begin < end && std::isspace(static_cast<unsigned char>(s[begin]))) ++begin;
  while (end > begin && std::isspace(static_cast<unsigned char>(s[end - 1]))) --end;
  return s.substr(begin, end - begin);
}

std::string lower(std::string s) {
  for (char& c : s) c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
  return s;
}

/// The weight of one item of an Accept-Encoding list.
///
/// 1.0 when the item names no weight at all, 0.0 when the weight says "not
/// acceptable". An item whose weight cannot be read is dropped by the
/// caller: a broken weight is not a permission.
bool weight_of(const std::string& params, double* q) {
  *q = 1.0;
  std::size_t pos = 0;
  while (pos < params.size()) {
    std::size_t semi = params.find(';', pos);
    if (semi == std::string::npos) semi = params.size();
    const std::string item = trim(params.substr(pos, semi - pos));
    pos = semi + 1;
    if (item.empty()) continue;
    const std::size_t eq = item.find('=');
    if (eq == std::string::npos) continue;   // a flag, not a weight
    if (lower(trim(item.substr(0, eq))) != "q") continue;
    const std::string value = trim(item.substr(eq + 1));
    if (value.empty()) return false;
    char* end = nullptr;
    const double parsed = std::strtod(value.c_str(), &end);
    if (end == value.c_str() || *end != '\0') return false;
    if (parsed < 0.0 || parsed > 1.0) return false;
    *q = parsed;
  }
  return true;
}

}  // namespace

bool accepts_gzip(const std::string& accept_encoding) {
  double gzip_q = -1.0;   // -1: gzip was not named at all
  double star_q = -1.0;   // -1: no wildcard either
  std::size_t pos = 0;
  while (pos < accept_encoding.size()) {
    std::size_t comma = accept_encoding.find(',', pos);
    if (comma == std::string::npos) comma = accept_encoding.size();
    const std::string item = accept_encoding.substr(pos, comma - pos);
    pos = comma + 1;
    const std::size_t semi = item.find(';');
    const std::string name =
        lower(trim(semi == std::string::npos ? item : item.substr(0, semi)));
    const std::string params = semi == std::string::npos ? std::string() : item.substr(semi + 1);
    double q = 1.0;
    if (!weight_of(params, &q)) continue;   // a broken weight is not a permission
    if (name == "gzip" || name == "x-gzip") {
      gzip_q = q;
    } else if (name == "*") {
      star_q = q;
    }
  }
  if (gzip_q >= 0.0) return gzip_q > 0.0;   // the named coding wins
  if (star_q >= 0.0) return star_q > 0.0;
  return false;
}

bool content_type_compressible(const std::string& content_type) {
  const std::size_t semi = content_type.find(';');
  const std::string mime =
      lower(trim(semi == std::string::npos ? content_type : content_type.substr(0, semi)));
  if (mime.empty()) return false;
  // A live stream is compressed by the stream itself: the frames have to
  // leave as they come, and a body that only exists at the end would be
  // exactly the wrong thing here.
  if (mime == "text/event-stream") return false;
  if (mime.rfind("text/", 0) == 0) return true;
  static const char* kCompressible[] = {
      "application/json",      "application/ld+json",  "application/javascript",
      "application/x-javascript", "application/ecmascript", "application/xml",
      "application/xhtml+xml", "application/rss+xml", "application/atom+xml",
      "application/xslt+xml", "application/protobuf", "application/x-ndjson",
      "application/yaml",      "application/x-yaml",   "application/sql",
      "image/svg+xml"};
  for (const char* type : kCompressible) {
    if (mime == type) return true;
  }
  return false;
}

}  // namespace tea
