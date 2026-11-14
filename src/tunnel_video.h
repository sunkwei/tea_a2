// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_video.h -- the pictures that travel through the tunnel.
//
// The first version encodes the screen as JPEG: Windows has the encoder and
// the decoder in WIC, a screen at 2 frames a second is a few kilobytes a
// frame, and the browser shows a JPEG without any help. The H.264 path is
// here too (Media Foundation, the encoder and the decoder that ship with
// Windows): it is the one to use when the bitrate really matters.
//
// Everything is pixels in BGRA on the outside (that is what GDI hands out
// and what the browser wants) and I420 in the middle (that is what the H.264
// encoder takes).
#pragma once

#include <cstddef>
#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace tea_tunnel {

/// One picture in BGRA (four bytes a pixel, blue first).
struct BgraFrame {
  int width = 0;
  int height = 0;
  std::vector<uint8_t> data;
};

/// One picture in I420 (the Y plane, then U, then V).
struct YuvFrame {
  int width = 0;
  int height = 0;
  std::vector<uint8_t> data;   // width * height * 3 / 2
};

/// Encode a picture as JPEG. The bytes are what travels. `quality` is 1..100
/// (0 = the default of the codec).
bool jpeg_encode(const uint8_t* bgra, int width, int height, int stride, int quality,
                 std::string* out, std::string* err);

/// Decode a JPEG back into pixels, and say how big it was.
bool jpeg_decode(const uint8_t* data, std::size_t len, std::vector<uint8_t>* bgra, int* width,
                 int* height, std::string* err);

/// BGRA -> I420 (what the H.264 encoder takes).
void bgra_to_yuv(const uint8_t* bgra, int width, int height, int stride, YuvFrame* out);

/// I420 -> BGRA (what the H.264 decoder gives, and what the browser wants).
void yuv_to_bgra(const YuvFrame& in, BgraFrame* out);

/// The H.264 encoder (Media Foundation). The bitstream is Annex-B: the NAL
/// units carry their own start codes.
class H264Encoder {
 public:
  H264Encoder();
  ~H264Encoder();
  H264Encoder(const H264Encoder&) = delete;
  H264Encoder& operator=(const H264Encoder&) = delete;

  /// Get ready to encode pictures of this size. `bitrate` is in bits per
  /// second, `fps` the number of pictures a second.
  bool start(int width, int height, int fps, int bitrate, std::string* err);

  /// Encode one picture. The Annex-B bytes are appended to `out`.
  bool encode(const YuvFrame& frame, int64_t timestamp_ms, std::string* out, std::string* err);

  void stop();
  bool running() const { return running_; }

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  bool running_ = false;
};

/// The H.264 decoder (Media Foundation).
class H264Decoder {
 public:
  H264Decoder();
  ~H264Decoder();
  H264Decoder(const H264Decoder&) = delete;
  H264Decoder& operator=(const H264Decoder&) = delete;

  bool start(int width_hint, int height_hint, std::string* err);

  /// Decode the bytes of one picture. Every picture the decoder produces is
  /// appended to `out`.
  bool decode(const uint8_t* data, std::size_t len, std::vector<YuvFrame>* out, std::string* err);

  void stop();
  bool running() const { return running_; }

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
  bool running_ = false;
};

}  // namespace tea_tunnel
