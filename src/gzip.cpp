#include "gzip.h"

#include <cstring>

#include "miniz.h"
#include "util.h"

namespace tea {
namespace {

/// The gzip container: 10-byte header, raw deflate, 8-byte trailer.
///   magic (1f 8b) | method (08 = deflate) | flags | mtime (4) | xfl | os
/// We write no name and no timestamp: the file is machine-written and the
/// mtime lives in the filesystem anyway.
const unsigned char kGzipHeader[10] = {0x1f, 0x8b, 0x08, 0x00, 0x00,
                                       0x00, 0x00, 0x00, 0x00, 0xff};

/// The smallest valid deflate stream: one final, empty fixed-Huffman block.
const unsigned char kEmptyDeflate[2] = {0x03, 0x00};

uint32_t read_le32(const std::string& data, std::size_t at) {
  return static_cast<uint32_t>(static_cast<unsigned char>(data[at])) |
         (static_cast<uint32_t>(static_cast<unsigned char>(data[at + 1])) << 8) |
         (static_cast<uint32_t>(static_cast<unsigned char>(data[at + 2])) << 16) |
         (static_cast<uint32_t>(static_cast<unsigned char>(data[at + 3])) << 24);
}

void append_le32(std::string* out, uint32_t v) {
  out->push_back(static_cast<char>(v & 0xff));
  out->push_back(static_cast<char>((v >> 8) & 0xff));
  out->push_back(static_cast<char>((v >> 16) & 0xff));
  out->push_back(static_cast<char>((v >> 24) & 0xff));
}

}  // namespace

bool path_is_gzip(const std::string& path) { return path_extension(path) == ".gz"; }

bool gzip_compress(const std::string& data, std::string* out, std::string* err) {
  out->clear();
  std::string body;
  if (data.empty()) {
    body.assign(reinterpret_cast<const char*>(kEmptyDeflate), sizeof(kEmptyDeflate));
  } else {
    std::size_t raw_len = 0;
    // window_bits < 0 => raw deflate, no zlib wrapper: exactly what gzip wants.
    const mz_uint flags =
        tdefl_create_comp_flags_from_zip_params(6, -15, MZ_DEFAULT_STRATEGY);
    void* raw = tdefl_compress_mem_to_heap(data.data(), data.size(), &raw_len, flags);
    if (!raw) {
      if (err) *err = "gzip: compression failed";
      return false;
    }
    body.assign(static_cast<const char*>(raw), raw_len);
    mz_free(raw);
  }

  out->append(reinterpret_cast<const char*>(kGzipHeader), sizeof(kGzipHeader));
  out->append(body);
  const mz_ulong crc = mz_crc32(MZ_CRC32_INIT, reinterpret_cast<const unsigned char*>(data.data()),
                                data.size());
  append_le32(out, static_cast<uint32_t>(crc));
  append_le32(out, static_cast<uint32_t>(data.size() & 0xffffffffu));
  return true;
}

bool gzip_decompress(const std::string& data, std::string* out, std::string* err) {
  out->clear();
  if (data.size() < sizeof(kGzipHeader) + 8) {
    if (err) *err = "gzip: the stream is too short to be a gzip file";
    return false;
  }
  if (static_cast<unsigned char>(data[0]) != 0x1f ||
      static_cast<unsigned char>(data[1]) != 0x8b) {
    if (err) *err = "gzip: not a gzip file";
    return false;
  }
  const unsigned char method = static_cast<unsigned char>(data[2]);
  if (method != 0x08) {
    if (err) *err = "gzip: unsupported compression method";
    return false;
  }
  const unsigned char flags = static_cast<unsigned char>(data[3]);
  std::size_t pos = sizeof(kGzipHeader);
  // FEXTRA: a 16-bit length followed by that many bytes.
  if (flags & 0x04) {
    if (pos + 2 > data.size()) {
      if (err) *err = "gzip: truncated header";
      return false;
    }
    const std::size_t xlen = static_cast<unsigned char>(data[pos]) |
                             (static_cast<std::size_t>(static_cast<unsigned char>(data[pos + 1])) << 8);
    pos += 2 + xlen;
  }
  // FNAME / FCOMMENT: zero-terminated strings.
  if (flags & 0x08) {
    while (pos < data.size() && data[pos] != '\0') ++pos;
    ++pos;
  }
  if (flags & 0x10) {
    while (pos < data.size() && data[pos] != '\0') ++pos;
    ++pos;
  }
  // FHCRC: a 16-bit header CRC we do not verify.
  if (flags & 0x02) pos += 2;
  if (pos + 8 > data.size()) {
    if (err) *err = "gzip: truncated stream";
    return false;
  }

  const std::size_t raw_len = data.size() - pos - 8;
  std::size_t out_len = 0;
  void* raw = tinfl_decompress_mem_to_heap(data.data() + pos, raw_len, &out_len, 0);
  if (!raw) {
    if (err) *err = "gzip: the stream is corrupt";
    return false;
  }
  out->assign(static_cast<const char*>(raw), out_len);
  mz_free(raw);

  const uint32_t want_crc = read_le32(data, data.size() - 8);
  const uint32_t want_size = read_le32(data, data.size() - 4);
  const mz_ulong crc = mz_crc32(MZ_CRC32_INIT,
                                reinterpret_cast<const unsigned char*>(out->data()), out->size());
  if (static_cast<uint32_t>(crc) != want_crc ||
      static_cast<uint32_t>(out->size() & 0xffffffffu) != want_size) {
    if (err) *err = "gzip: the checksum does not match";
    out->clear();
    return false;
  }
  return true;
}

bool read_file_maybe_gzip(const std::string& path, std::string* out, std::string* err,
                          std::size_t max_bytes) {
  // The cleaner may have compressed the file: the plain one is gone and the
  // ".gz" twin holds the same bytes.
  std::string real = path;
  if (!path_is_gzip(path) && !path_exists(path) && path_exists(path + ".gz")) {
    real = path + ".gz";
  }
  if (!path_is_gzip(real)) return read_file(real, out, max_bytes, nullptr);
  std::string raw;
  if (!read_file_binary(real, &raw, 0, nullptr)) {
    if (err) *err = "cannot read " + real;
    return false;
  }
  return gzip_decompress(raw, out, err);
}

bool gzip_file_in_place(const std::string& path, std::string* err, int64_t* saved_bytes) {
  if (saved_bytes) *saved_bytes = 0;
  if (path_is_gzip(path)) return true;
  std::string raw;
  if (!read_file_binary(path, &raw, 0, nullptr)) {
    if (err) *err = "cannot read " + path;
    return false;
  }
  std::string packed;
  std::string pack_err;
  if (!gzip_compress(raw, &packed, &pack_err)) {
    if (err) *err = pack_err;
    return false;
  }
  if (packed.size() >= raw.size()) {
    // Not worth it: keep the plain file (and say so) rather than trade a
    // readable file for a bigger one.
    if (err) *err = "gzip: the file does not compress";
    return false;
  }
  const std::string target = path + ".gz";
  if (!write_file_atomic(target, packed, err)) return false;
  if (!remove_path(path, false, nullptr)) {
    remove_path(target, false, nullptr);
    if (err) *err = "cannot remove " + path;
    return false;
  }
  if (saved_bytes) *saved_bytes = static_cast<int64_t>(raw.size()) - static_cast<int64_t>(packed.size());
  return true;
}

}  // namespace tea
