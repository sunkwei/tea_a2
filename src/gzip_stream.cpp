// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
#include "gzip_stream.h"

#include <cstring>

#include "miniz.h"

namespace tea {
namespace {

/// The gzip container: 10-byte header, raw deflate, 8-byte trailer.
///   magic (1f 8b) | method (08 = deflate) | flags | mtime (4) | xfl | os
/// The same header gzip.cpp writes: no name, no timestamp.
const unsigned char kGzipHeader[10] = {0x1f, 0x8b, 0x08, 0x00, 0x00,
                                       0x00, 0x00, 0x00, 0x00, 0xff};

void append_le32(std::string* out, uint32_t v) {
  out->push_back(static_cast<char>(v & 0xff));
  out->push_back(static_cast<char>((v >> 8) & 0xff));
  out->push_back(static_cast<char>((v >> 16) & 0xff));
  out->push_back(static_cast<char>((v >> 24) & 0xff));
}

}  // namespace

struct GzipStream::Impl {
  tdefl_compressor comp{};
  std::string buffer;      // what miniz produced since the last take
  uint32_t crc = MZ_CRC32_INIT;
  uint64_t total = 0;      // the uncompressed size, for the trailer
  bool started = false;
  bool finished = false;
  bool failed = false;

  /// miniz hands the compressed bytes to this: they go into the buffer and
  /// are picked up by the write() that caused them.
  static mz_bool put_buf(const void* buf, int len, void* user) {
    Impl* self = static_cast<Impl*>(user);
    if (len > 0) {
      self->buffer.append(static_cast<const char*>(buf), static_cast<std::size_t>(len));
    }
    return MZ_TRUE;
  }
};

GzipStream::GzipStream() : impl_(new Impl()) {
  // window_bits < 0 => raw deflate, no zlib wrapper: exactly what gzip wants.
  const mz_uint flags =
      tdefl_create_comp_flags_from_zip_params(6, -15, MZ_DEFAULT_STRATEGY);
  if (tdefl_init(&impl_->comp, &Impl::put_buf, impl_.get(), flags) != TDEFL_STATUS_OKAY) {
    impl_->failed = true;
  }
}

GzipStream::~GzipStream() = default;

bool GzipStream::ok() const { return impl_ && !impl_->failed; }

bool GzipStream::write(const char* data, std::size_t len, std::string* out) {
  if (!ok() || impl_->finished) return false;
  if (!impl_->started) {
    out->append(reinterpret_cast<const char*>(kGzipHeader), sizeof(kGzipHeader));
    impl_->started = true;
  }
  if (len > 0) {
    impl_->crc = mz_crc32(impl_->crc, reinterpret_cast<const unsigned char*>(data), len);
    impl_->total += len;
    impl_->buffer.clear();
    // TDEFL_SYNC_FLUSH: everything written so far comes out now, so a reader
    // does not wait for the end of the stream to see it.
    const tdefl_status status = tdefl_compress_buffer(&impl_->comp, data, len, TDEFL_SYNC_FLUSH);
    if (status != TDEFL_STATUS_OKAY && status != TDEFL_STATUS_DONE) {
      impl_->failed = true;
      return false;
    }
    out->append(impl_->buffer);
    impl_->buffer.clear();
  }
  return true;
}

bool GzipStream::finish(std::string* out) {
  if (!ok()) return false;
  if (impl_->finished) return true;
  if (!impl_->started) {
    out->append(reinterpret_cast<const char*>(kGzipHeader), sizeof(kGzipHeader));
    impl_->started = true;
  }
  impl_->buffer.clear();
  const tdefl_status status = tdefl_compress_buffer(&impl_->comp, nullptr, 0, TDEFL_FINISH);
  if (status != TDEFL_STATUS_DONE && status != TDEFL_STATUS_OKAY) {
    impl_->failed = true;
    return false;
  }
  out->append(impl_->buffer);
  impl_->buffer.clear();
  append_le32(out, impl_->crc);
  append_le32(out, static_cast<uint32_t>(impl_->total & 0xffffffffu));
  impl_->finished = true;
  return true;
}

}  // namespace tea
