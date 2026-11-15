// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// errors.h -- the API's error codes, in one place.
//
// A caller should not have to read an English sentence to find out whether it
// may try again. Every error the HTTP API answers with has a code, and every
// code says what it means, whether trying again makes sense, and what the
// caller can do about it. The catalog is served at `GET /api/errors`, so a
// client can look a code up instead of guessing from the message.
//
// The table is the truth about the codes: `tests/smoke_error_catalog.py`
// reads the source and checks that every code the server can send is in here
// with the same status - a code that is not in the catalog is a hole in the
// contract.
//
// A code may appear more than once (with different statuses and different
// meanings): `config_missing` is a 404 when there is no provider.yaml at all
// and a 409 when there is nothing to replace. So the catalog is a **list** of
// (code, status) pairs, not a map.
#pragma once

#include <string>
#include <vector>

namespace tea {

/// What one error code means.
struct ErrorInfo {
  /// The code the caller sees (`error.code`).
  std::string code;
  /// The status the code is sent with.
  int http = 500;
  /// May the caller try the same thing again right away?
  bool retryable = false;
  /// How long to wait before trying again, in seconds (0 = not known).
  int retry_after = 0;
  /// One line: what happened.
  const char* meaning = "";
  /// One line: what the caller can do about it.
  const char* hint = "";
};

/// Every error the HTTP API can answer with, sorted by status and then code.
const std::vector<ErrorInfo>& error_catalog();

/// What one (code, status) pair means. Null when the pair is not in the
/// catalog. The status matters: the same code can mean different things.
const ErrorInfo* error_info(const std::string& code, int http);

}  // namespace tea
