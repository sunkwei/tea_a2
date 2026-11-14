// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// gzip_stream.h -- a gzip stream that can be fed in pieces.
//
// gzip_compress() (gzip.h) compresses a whole buffer in one go: that is what
// the history cleaner wants. A live stream wants the opposite - the bytes
// have to leave as they come, or the caller waits for the end of the answer
// to see the first frame of it.
//
// So this is the same gzip container, written in pieces: every write() hands
// back the compressed part of what was written, flushed, so a reader can
// take it and decode it right away.
#pragma once

#include <cstddef>
#include <memory>
#include <string>

namespace tea {

/// A gzip stream (RFC 1952) over miniz's deflate.
///
/// The bytes come out as a normal gzip file: `gunzip` reads it, and so does
/// every browser that is told `Content-Encoding: gzip`.
class GzipStream {
public:
  GzipStream();
  ~GzipStream();
  GzipStream(const GzipStream&) = delete;
  GzipStream& operator=(const GzipStream&) = delete;

  /// True when the stream works. A stream that failed writes nothing more.
  bool ok() const;

  /// Compress `len` bytes and append what came out to `out`.
  ///
  /// The result is flushed, so a reader gets everything that was written so
  /// far - not at the end of the stream.
  bool write(const char* data, std::size_t len, std::string* out);

  /// Write the last block and the gzip trailer, and append them to `out`.
  ///
  /// After this the stream is closed: writing again fails.
  bool finish(std::string* out);

private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace tea
