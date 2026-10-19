// archive.h -- build a ZIP archive in memory (miniz underneath).
//
// One purpose: hand a whole directory of this device to the browser as a single
// download. Everything is assembled in RAM because the response is sent as one
// body; the limits below are what keeps that safe.
#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace tea {

/// A file that was left out, and why. Skipping is not an error: one oversized log
/// must not make the whole download fail.
struct ArchiveSkipped {
  std::string name;
  std::string reason;
};

/// Bounds for one archive. `.tea_agent_run` is normally a few MB of JSON and logs;
/// these exist so a single request cannot exhaust the device.
struct ArchiveLimits {
  /// A file bigger than this is skipped (it would have to be read whole).
  std::size_t max_file_bytes = 32u * 1024u * 1024u;
  /// Sum of the uncompressed sizes; past this, remaining files are skipped.
  unsigned long long max_total_bytes = 256ull * 1024u * 1024u;
  /// Guard against a directory tree with a pathological number of entries.
  std::size_t max_entries = 20000u;
};

struct ArchiveResult {
  bool ok = false;
  std::string bytes;                       // the .zip, ready to send
  std::size_t entries = 0;                 // files + directories stored
  unsigned long long uncompressed = 0;     // sum of the stored file sizes
  std::vector<ArchiveSkipped> skipped;
  std::string error;                       // set when ok == false
};

/// Zip a directory tree recursively. Entry names are `<prefix>/<relative path>`
/// with forward slashes, so extracting yields one folder; empty directories are
/// stored as entries ending in '/' so they survive the round trip.
///
/// `root` must exist and be a directory. Unreadable entries are reported in
/// `skipped` rather than failing the archive.
ArchiveResult zip_directory(const std::string& root, const std::string& prefix,
                            const ArchiveLimits& limits = ArchiveLimits());

}  // namespace tea
