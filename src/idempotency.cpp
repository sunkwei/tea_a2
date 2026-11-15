// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// idempotency.cpp -- the store of remembered answers.

#include "idempotency.h"

#include <algorithm>

#include <nlohmann/json.hpp>

#include "log.h"
#include "sha256.h"
#include "util.h"

namespace tea {

namespace {

/// The answers live next to workspaces.json in the tea home.
const char* const kIdempotencyDir = "idempotency";

}  // namespace

IdempotencyStore::IdempotencyStore(const std::string& tea_home) {
  dir_ = path_join(tea_home.empty() ? std::string(".") : tea_home, kIdempotencyDir);
  make_dirs(dir_);
}

std::string IdempotencyStore::path_for(const std::string& key) const {
  // The name is the hash of the key: a key from outside can never be a path,
  // and two different keys never share a file.
  return path_join(dir_, sha256_hex(key) + ".json");
}

bool IdempotencyStore::load(const std::string& key, IdempotencyRecord* out) {
  if (key.empty() || dir_.empty()) return false;
  const std::string path = path_for(key);
  std::string text;
  if (!read_file(path, &text, 0, nullptr)) return false;
  nlohmann::json j;
  try {
    j = nlohmann::json::parse(text);
  } catch (const std::exception&) {
    return false;   // a torn file: no answer (the caller may try again)
  }
  if (!j.is_object()) return false;
  const int64_t created = static_cast<int64_t>(j.value("created_ms", static_cast<int64_t>(0)));
  if (created <= 0 || now_ms() - created > kIdempotencyTtlMs) {
    remove_path(path, false, nullptr);   // expired
    return false;
  }
  out->fingerprint = j.value("fingerprint", std::string());
  out->status = static_cast<int>(j.value("status", 200));
  out->body = j.value("body", std::string());
  out->turn_id = j.value("turn_id", std::string());
  out->created_ms = created;
  return true;
}

void IdempotencyStore::save(const std::string& key, const IdempotencyRecord& record) {
  if (key.empty() || dir_.empty()) return;
  nlohmann::json j = nlohmann::json::object();
  j["key"] = key;
  j["fingerprint"] = record.fingerprint;
  j["status"] = record.status;
  j["body"] = record.body;
  if (!record.turn_id.empty()) j["turn_id"] = record.turn_id;
  j["created_ms"] = record.created_ms;
  const std::string path = path_for(key);
  std::string err;
  // Written atomically: a file that is half written would be read as "no
  // answer" later, and the caller would do the work twice.
  if (!write_file_atomic(path, dump_json(j), &err)) {
    LOG_WARN("idempotency: cannot remember the answer for a key: ", err);
  }
  // The expired answers are swept now and then, not on every request.
  const int64_t now = now_ms();
  if (now - last_sweep_ms_ > 60ll * 1000ll) {
    last_sweep_ms_ = now;
    sweep();
  }
}

void IdempotencyStore::sweep() {
  if (dir_.empty()) return;
  std::vector<DirEntryInfo> entries;
  std::string err;
  if (!list_directory(dir_, &entries, &err)) return;
  const int64_t now = now_ms();
  for (std::size_t i = 0; i < entries.size(); ++i) {
    if (entries[i].is_dir) continue;
    if (entries[i].name.size() < 6 ||
        entries[i].name.compare(entries[i].name.size() - 5, 5, ".json") != 0) {
      continue;
    }
    // The age is in the file itself (the moment the answer was remembered),
    // not in the file system's timestamp: a copy of the tea home keeps it.
    std::string text;
    if (!read_file(entries[i].path, &text, 0, nullptr)) continue;
    nlohmann::json j;
    try {
      j = nlohmann::json::parse(text);
    } catch (const std::exception&) {
      remove_path(entries[i].path, false, nullptr);   // torn: no answer
      continue;
    }
    const int64_t created = static_cast<int64_t>(j.value("created_ms", static_cast<int64_t>(0)));
    if (created <= 0 || now - created > kIdempotencyTtlMs) {
      remove_path(entries[i].path, false, nullptr);
    }
  }
}

std::string idempotency_key_of(const std::string& header_value,
                                const std::string& body_value) {
  std::string key = trim(header_value);
  if (key.empty()) key = trim(body_value);
  // A key that is too long or has odd characters is not a key: it would make
  // a strange file name and a strange log line. The hash would survive it,
  // but the point of a key is that a human can read it back.
  if (key.size() > 200) return std::string();
  for (std::size_t i = 0; i < key.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(key[i]);
    const bool ok = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                    (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.' ||
                    c == ':' || c == '@' || c == '+';
    if (!ok) return std::string();
  }
  return key;
}

std::string idempotency_fingerprint(const std::string& path, const std::string& body) {
  return sha256_hex(path + "\n" + body);
}

}  // namespace tea
