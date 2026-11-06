// gzip.h -- real gzip (RFC 1952) for the cold half of the history.
//
// Why this exists: the history of a workspace (session transcripts and turn
// trajectories) is plain text, and plain text compresses by ~75%. On a device
// with a small flash that difference is the difference between "the history
// fits" and "the history fills the disk".
//
// Why not just zip the whole run_dir: a zip is one blob you cannot append to
// and cannot read a single turn out of. The history stays one file per
// session; only *cold* files (untouched for a while) are compressed, and the
// read path transparently decompresses them again. `gunzip` reads the result -
// it is a normal gzip file, not a private format.
//
// The deflate stream itself comes from miniz (already vendored for the ZIP
// writer behind GET /api/workspaces/{id}/archive); this file only wraps it in
// the gzip container.
#pragma once

#include <cstddef>
#include <string>
#include <cstdint>

namespace tea {

/// True when the path ends in ".gz".
bool path_is_gzip(const std::string& path);

/// Compress `data` into a gzip stream. False when the data cannot be
/// compressed (err is set).
bool gzip_compress(const std::string& data, std::string* out, std::string* err);

/// Decompress a gzip stream. The CRC and the length are verified: a truncated
/// or corrupted file is an error, not a short read.
bool gzip_decompress(const std::string& data, std::string* out, std::string* err);

/// Read a file, transparently handling gzip: a path ending in ".gz" is
/// decompressed, anything else is read as is.
bool read_file_maybe_gzip(const std::string& path, std::string* out, std::string* err = nullptr,
                          std::size_t max_bytes = 0);

/// Compress `path` into "<path>.gz" and remove the original. False when the
/// file is not worth compressing (the result would not be smaller) or when
/// something went wrong - the original is left alone in both cases.
bool gzip_file_in_place(const std::string& path, std::string* err = nullptr,
                        int64_t* saved_bytes = nullptr);

}  // namespace tea
