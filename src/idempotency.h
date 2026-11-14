// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// idempotency.h -- "the same request, asked twice, is one request".
//
// A caller that retries (a queue, a watchdog, a phone with a flaky link)
// must not make the machine do the work twice: a second turn costs money and
// may run the same command again. So a write request may carry an
// **idempotency key**: the same key within the retention time gets the same
// answer back, and the work is done once.
//
// The answers are remembered in the tea home (next to workspaces.json), one
// file per key, so they survive a restart: a queue that comes back after a
// crash and retries the same request gets the turn it already started.
//
// The key itself is never used as a path - the file is named after its
// hash, so a key from outside cannot walk out of the folder.
#pragma once

#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

namespace tea {

/// How long an answer is remembered (24 hours).
constexpr int64_t kIdempotencyTtlMs = 24ll * 60ll * 60ll * 1000ll;

/// One remembered answer.
struct IdempotencyRecord {
  /// What was asked: a hash of the path and the body (without the key).
  std::string fingerprint;
  /// The status of the answer.
  int status = 200;
  /// The answer, verbatim (JSON).
  std::string body;
  /// The turn this request started, when the answer was a stream.
  std::string turn_id;
  /// When the answer was remembered (ms since the epoch).
  int64_t created_ms = 0;
};

/// The store of remembered answers.
///
/// One file per key under `<tea_home>/idempotency/`, named after the hash of
/// the key. The entries expire after a day; the expired ones are swept now
/// and then.
class IdempotencyStore {
 public:
  explicit IdempotencyStore(const std::string& tea_home);

  /// The answer remembered for this key. False when there is none (or when it
  /// has expired).
  bool load(const std::string& key, IdempotencyRecord* out);

  /// Remember an answer. An older answer for the same key is replaced.
  void save(const std::string& key, const IdempotencyRecord& record);

  /// Remove the answers that are older than the retention time.
  void sweep();

  /// The lock that guards a whole "look, do the work, remember" sequence.
  /// Two requests with the same key must not both start the work.
  std::mutex& mutex() { return mu_; }

  /// Where the answers live (for the log).
  const std::string& directory() const { return dir_; }

 private:
  /// The file this key lives in.
  std::string path_for(const std::string& key) const;

  std::string dir_;
  std::mutex mu_;
  int64_t last_sweep_ms_ = 0;
};

/// The idempotency key of a request: the `Idempotency-Key` header, or the
/// body's `idempotency_key` field (the task queue sends both). Empty when
/// the request carries none.
std::string idempotency_key_of(const std::string& header_value,
                                const std::string& body_value);

/// What a request asked for: a hash of the path and the body, without the
/// idempotency key itself (the same request may come with the key in the
/// header or in the body - that is still the same request).
std::string idempotency_fingerprint(const std::string& path, const std::string& body);

}  // namespace tea
