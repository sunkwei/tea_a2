// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// sha256.h -- a small SHA-256, so the code can hash without a dependency.
//
// The only thing the project needs a hash for is telling two requests apart
// ("is this the same request?"), and for naming a file after something that
// came from outside. Both want a hash that is stable and has no collisions
// in practice - not one that resists an attacker (the caller is the one who
// picks the key, and the answer it gets back is its own).
//
// This is the plain FIPS 180-4 algorithm, written out here because the
// project links no crypto library on every platform (mbedtls is there on
// some, WinHTTP on others) and a fingerprint has to be **the same** on every
// platform: a file written on one machine is read on another.
#pragma once

#include <string>

namespace tea {

/// The SHA-256 of `data`, as 64 lowercase hex characters.
std::string sha256_hex(const std::string& data);

}  // namespace tea
