// ca_bundle.h -- the TLS roots compiled into the binary.
//
// For single-file deployment the trusted roots must not live in a sidecar file:
// copying one ELF to a device should be enough for https:// to work. The PEM is
// embedded at build time from 3rd/ca-bundle/cacert.pem by
// tools/embed_ca_bundle.cpp.
//
// A build without a vendored bundle still succeeds: these return nullptr/0, and
// the runtime falls back to looking for a file (see find_ca_bundle in main.cpp).
#pragma once

#include <cstddef>

namespace tea {

/// NUL-terminated PEM text, or nullptr when nothing was embedded.
const char* embedded_ca_bundle_pem();

/// Length in bytes of embedded_ca_bundle_pem() (0 when absent).
std::size_t embedded_ca_bundle_size();

}  // namespace tea
