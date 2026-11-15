// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_png.cpp -- read a PNG into pixels.
//
// The PNG format is a container: the picture is split into chunks, the
// ones that carry pixels are compressed with zlib, and every line of
// pixels is filtered (the difference from the line above it). Reading it
// is therefore three steps: find the chunks, unpack them, undo the
// filters.
//
// The colours come out as BGRA (blue first) - that is what the encoder
// and the browser want.
#include "tunnel_png.h"

#include <cstdio>
#include <cstring>

#include "miniz.h"

namespace tea_tunnel {

namespace {

uint32_t read_be32(const unsigned char* p) {
  return (static_cast<uint32_t>(p[0]) << 24) | (static_cast<uint32_t>(p[1]) << 16) |
         (static_cast<uint32_t>(p[2]) << 8) | static_cast<uint32_t>(p[3]);
}

/// The zlib stream inside the PNG: the chunks are zlib wrapped, so the
/// window is 15 bits (the usual) and not the raw deflate of gzip.
bool inflate_zlib(const std::string& in, std::string* out) {
  mz_stream stream{};
  if (mz_inflateInit2(&stream, 15) != MZ_OK) return false;
  stream.next_in = reinterpret_cast<const unsigned char*>(in.data());
  stream.avail_in = static_cast<unsigned int>(in.size());

  std::string result;
  char buffer[16384];
  int status = MZ_OK;
  do {
    stream.next_out = reinterpret_cast<unsigned char*>(buffer);
    stream.avail_out = static_cast<unsigned int>(sizeof(buffer));
    status = mz_inflate(&stream, MZ_NO_FLUSH);
    if (status != MZ_OK && status != MZ_BUF_ERROR && status != MZ_STREAM_END) {
      mz_inflateEnd(&stream);
      return false;
    }
    result.append(buffer, sizeof(buffer) - stream.avail_out);
    if (status == MZ_BUF_ERROR && stream.avail_in == 0) {
      mz_inflateEnd(&stream);
      return false;   // the data ends in the middle of the stream
    }
  } while (status != MZ_STREAM_END);
  mz_inflateEnd(&stream);
  out->swap(result);
  return true;
}

/// Undo the filter of one line of pixels. `filter` is the byte in front
/// of the line, `bpp` how many bytes a pixel has.
void unfilter_line(uint8_t filter, uint8_t* line, const uint8_t* previous, std::size_t length,
                   std::size_t bpp) {
  switch (filter) {
    case 0:   // none
      break;
    case 1:   // sub: the byte to the left
      for (std::size_t i = bpp; i < length; i++) {
        line[i] = static_cast<uint8_t>(line[i] + line[i - bpp]);
      }
      break;
    case 2:   // up: the byte above
      if (previous != nullptr) {
        for (std::size_t i = 0; i < length; i++) {
          line[i] = static_cast<uint8_t>(line[i] + previous[i]);
        }
      }
      break;
    case 3:   // average: half of the left and the one above
      for (std::size_t i = 0; i < length; i++) {
        const int left = i >= bpp ? line[i - bpp] : 0;
        const int up = previous != nullptr ? previous[i] : 0;
        line[i] = static_cast<uint8_t>(line[i] + ((left + up) / 2));
      }
      break;
    case 4: {  // paeth: the one that predicts best
      for (std::size_t i = 0; i < length; i++) {
        const int left = i >= bpp ? line[i - bpp] : 0;
        const int up = previous != nullptr ? previous[i] : 0;
        const int up_left = (previous != nullptr && i >= bpp) ? previous[i - bpp] : 0;
        const int p = left + up - up_left;
        const int pa = p > left ? (p > up ? p - up : p - left) : (left > up ? left - up : up - p);
        const int pred = (pa == std::abs(p - left)) ? left : (pa == std::abs(p - up) ? up : up_left);
        line[i] = static_cast<uint8_t>(line[i] + pred);
      }
      break;
    }
    default:
      break;
  }
}

}  // namespace

bool png_read_memory(const std::string& data, PngImage* out, std::string* err) {
  auto fail = [&](const char* why) {
    if (err) *err = why;
    return false;
  };
  if (out == nullptr) return fail("no place for the picture");
  out->bgra.clear();
  out->width = 0;
  out->height = 0;

  static const unsigned char kSignature[8] = {0x89, 'P', 'N', 'G', 0x0d, 0x0a, 0x1a, 0x0a};
  if (data.size() < 8 || std::memcmp(data.data(), kSignature, 8) != 0) {
    return fail("that is not a PNG");
  }

  int width = 0;
  int height = 0;
  int channels = 0;
  std::string compressed;
  std::size_t at = 8;
  bool seen_header = false;
  while (at + 8 <= data.size()) {
    const unsigned char* chunk = reinterpret_cast<const unsigned char*>(data.data()) + at;
    const uint32_t length = read_be32(chunk);
    const std::string type(reinterpret_cast<const char*>(chunk + 4), 4);
    at += 8;
    if (at + length + 4 > data.size()) return fail("the PNG is cut off");
    if (type == "IHDR") {
      if (length < 13) return fail("the PNG header is too short");
      const unsigned char* head = reinterpret_cast<const unsigned char*>(data.data()) + at;
      width = static_cast<int>(read_be32(head));
      height = static_cast<int>(read_be32(head + 4));
      const int depth = head[8];
      const int colour = head[9];
      const int interlace = head[12];
      if (depth != 8) return fail("the PNG is not 8 bits a channel");
      if (interlace != 0) return fail("the PNG is interlaced");
      switch (colour) {
        case 0: channels = 1; break;   // grey
        case 2: channels = 3; break;   // RGB
        case 4: channels = 2; break;   // grey + alpha
        case 6: channels = 4; break;   // RGBA
        default: return fail("the PNG has a colour type this does not read");
      }
      seen_header = true;
    } else if (type == "IDAT") {
      compressed.append(data, at, length);
    } else if (type == "IEND") {
      break;
    }
    at += length + 4;   // the payload and the CRC
  }
  if (!seen_header) return fail("the PNG has no header");
  if (width <= 0 || height <= 0) return fail("the PNG has no size");
  if (compressed.empty()) return fail("the PNG has no picture in it");

  std::string raw;
  if (!inflate_zlib(compressed, &raw)) return fail("the PNG's picture is broken");

  const std::size_t bpp = static_cast<std::size_t>(channels);
  const std::size_t stride = static_cast<std::size_t>(width) * bpp;
  if (raw.size() < (stride + 1) * static_cast<std::size_t>(height)) {
    return fail("the PNG's picture is shorter than it says");
  }

  // The lines are filtered one after the other: each one is a filter byte
  // followed by its pixels.
  std::vector<uint8_t> out_pixels(stride * static_cast<std::size_t>(height));
  const uint8_t* source = reinterpret_cast<const uint8_t*>(raw.data());
  for (int y = 0; y < height; y++) {
    const uint8_t filter = *source++;
    uint8_t* line = out_pixels.data() + stride * static_cast<std::size_t>(y);
    std::memcpy(line, source, stride);
    source += stride;
    const uint8_t* previous = y > 0 ? out_pixels.data() + stride * static_cast<std::size_t>(y - 1)
                                     : nullptr;
    unfilter_line(filter, line, previous, stride, bpp);
  }

  // Into BGRA: what the encoder and the browser want.
  out->bgra.resize(static_cast<std::size_t>(width) * static_cast<std::size_t>(height) * 4);
  uint8_t* target = out->bgra.data();
  const uint8_t* pixels = out_pixels.data();
  for (std::size_t i = 0, count = static_cast<std::size_t>(width) * static_cast<std::size_t>(height);
       i < count; i++) {
    const uint8_t* pixel = pixels + i * bpp;
    uint8_t* rgba = target + i * 4;
    switch (channels) {
      case 1:   // grey
        rgba[0] = rgba[1] = rgba[2] = pixel[0];
        rgba[3] = 255;
        break;
      case 2:   // grey + alpha
        rgba[0] = rgba[1] = rgba[2] = pixel[0];
        rgba[3] = pixel[1];
        break;
      case 3:   // RGB
        rgba[0] = pixel[2];
        rgba[1] = pixel[1];
        rgba[2] = pixel[0];
        rgba[3] = 255;
        break;
      default:  // RGBA
        rgba[0] = pixel[2];
        rgba[1] = pixel[1];
        rgba[2] = pixel[0];
        rgba[3] = pixel[3];
        break;
    }
  }
  out->width = width;
  out->height = height;
  return true;
}

bool png_read_file(const std::string& path, PngImage* out, std::string* err) {
  std::FILE* file = std::fopen(path.c_str(), "rb");
  if (file == nullptr) {
    if (err) *err = "cannot open " + path;
    return false;
  }
  std::string data;
  char buffer[65536];
  while (true) {
    const std::size_t got = std::fread(buffer, 1, sizeof(buffer), file);
    if (got == 0) break;
    data.append(buffer, got);
  }
  std::fclose(file);
  return png_read_memory(data, out, err);
}

}  // namespace tea_tunnel
