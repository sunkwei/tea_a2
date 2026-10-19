#include "archive.h"

#include <cstring>

// miniz.h defines its whole zlib-compatible API as static inline helpers, and this
// translation unit only uses the ZIP writer: GCC then reports every other one as
// "defined but not used". The vendored header is not ours to annotate, so the
// warning is silenced around the include instead.
#if defined(__GNUC__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-function"
#endif
#include "miniz.h"
#if defined(__GNUC__)
#pragma GCC diagnostic pop
#endif

#include "log.h"
#include "util.h"

namespace tea {
namespace {

/// ZIP stores names with forward slashes and has no concept of a drive letter, so
/// whatever the caller passed as a prefix is normalised here once. Only a leading
/// '/' or './' is dropped: the dot in `.tea_agent_run` is part of the real name
/// and must survive, or unpacking yields a directory that is not the one the
/// operator asked for (and a hidden one on POSIX).
std::string normalise_prefix(const std::string& prefix) {
  std::string out = path_generic(prefix);
  for (;;) {
    if (!out.empty() && out[0] == '/') {
      out.erase(out.begin());
      continue;
    }
    if (out.size() >= 2 && out[0] == '.' && out[1] == '/') {
      out.erase(out.begin(), out.begin() + 2);
      continue;
    }
    break;
  }
  while (!out.empty() && out.back() == '/') out.pop_back();
  if (out == ".") out.clear();
  return out;
}

/// Depth-first walk that feeds one open miniz writer. Kept as a struct so the
/// limits, the skip list and the running totals travel together.
struct ZipWalker {
  mz_zip_archive* zip;
  const ArchiveLimits* limits;
  ArchiveResult* out;
  bool entry_limit_hit = false;

  void skip(const std::string& name, const std::string& reason) {
    // The list is for the caller's log/toast; a directory with 100k unreadable
    // entries must not become a 100k-element JSON blob.
    if (out->skipped.size() < 64) out->skipped.push_back(ArchiveSkipped{name, reason});
    LOG_WARN("archive: skipping ", name, ": ", reason);
  }

  bool add_mem(const std::string& name, const std::string& bytes, mz_uint level) {
    if (!mz_zip_writer_add_mem(zip, name.c_str(), bytes.data(), bytes.size(), level)) {
      skip(name, std::string("cannot store it (") +
                     mz_zip_get_error_string(mz_zip_get_last_error(zip)) + ")");
      return false;
    }
    out->entries++;
    return true;
  }

  void walk(const std::string& abs_dir, const std::string& name) {
    std::vector<DirEntryInfo> entries;
    std::string err;
    if (!list_directory(abs_dir, &entries, &err)) {
      skip(name, "cannot list the directory: " + err);
      return;
    }
    for (std::size_t i = 0; i < entries.size(); ++i) {
      if (out->entries >= limits->max_entries) {
        if (!entry_limit_hit) {
          entry_limit_hit = true;
          skip(name, "the archive already holds " + std::to_string(limits->max_entries) +
                         " entries; the rest is left out");
        }
        return;
      }
      const DirEntryInfo& e = entries[i];
      const std::string child = name.empty() ? e.name : (name + "/" + e.name);
      if (e.is_dir) {
        // Store the directory itself: an empty one would otherwise vanish, and
        // some unpackers only create parents they see listed.
        add_mem(child + "/", std::string(), MZ_NO_COMPRESSION);
        walk(e.path, child);
        continue;
      }
      if (e.size > limits->max_file_bytes) {
        skip(child, "it is " + human_size(static_cast<std::size_t>(e.size)) + ", the limit is " +
                        human_size(limits->max_file_bytes));
        continue;
      }
      if (out->uncompressed + e.size > limits->max_total_bytes) {
        skip(child, "the archive would grow past " + human_size(
                        static_cast<std::size_t>(limits->max_total_bytes)));
        continue;
      }
      std::string bytes;
      if (!read_file(e.path, &bytes)) {
        skip(child, "it cannot be read");
        continue;
      }
      // Text (JSON, JSONL, logs, markdown) is what this archive is made of, so
      // compress it; level 6 is the point where more effort stops paying off.
      if (!add_mem(child, bytes, MZ_DEFAULT_COMPRESSION)) continue;
      out->uncompressed += bytes.size();
    }
  }
};

}  // namespace

ArchiveResult zip_directory(const std::string& root, const std::string& prefix,
                            const ArchiveLimits& limits) {
  ArchiveResult out;
  if (!path_is_dir(root)) {
    out.error = "not a directory: " + root;
    return out;
  }

  mz_zip_archive zip;
  std::memset(&zip, 0, sizeof(zip));
  // Written to a growable heap block, not to a temp file: the response is sent
  // from memory and nothing is left on disk behind the caller's back.
  if (!mz_zip_writer_init_heap(&zip, 0, 64 * 1024)) {
    out.error = std::string("cannot start the archive (") +
                mz_zip_get_error_string(mz_zip_get_last_error(&zip)) + ")";
    return out;
  }

  ZipWalker walker;
  walker.zip = &zip;
  walker.limits = &limits;
  walker.out = &out;
  walker.walk(root, normalise_prefix(prefix));

  void* buffer = nullptr;
  std::size_t size = 0;
  if (!mz_zip_writer_finalize_heap_archive(&zip, &buffer, &size)) {
    out.error = std::string("cannot finish the archive (") +
                mz_zip_get_error_string(mz_zip_get_last_error(&zip)) + ")";
    mz_zip_writer_end(&zip);
    return out;
  }
  if (buffer && size) out.bytes.assign(static_cast<const char*>(buffer), size);
  if (buffer) mz_free(buffer);
  mz_zip_writer_end(&zip);

  out.ok = true;
  LOG_INFO("archive: ", out.entries, " entr(ies), ", out.uncompressed, " B raw -> ",
           out.bytes.size(), " B zipped, ", out.skipped.size(), " skipped");
  return out;
}

}  // namespace tea
