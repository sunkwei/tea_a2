// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_png.h -- read a PNG into pixels.
//
// The Linux desktop backend takes its pictures with the compositor's own
// screenshot program (spectacle, grim, ...), and those write a PNG. The
// picture has to be turned into pixels before it can be encoded, so the
// PNG is read here.
//
// Only what a screenshot program writes is supported: 8 bits a channel,
// no interlacing, the colour types grey, RGB, grey+alpha and RGBA. That
// covers spectacle, grim, gnome-screenshot and maim.
//
// The decoder needs no library of its own: the zlib inside the PNG is
// unpacked with miniz, which is already in the tree.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace tea_tunnel {

/// One picture in BGRA (four bytes a pixel, blue first).
struct PngImage {
  int width = 0;
  int height = 0;
  std::vector<uint8_t> bgra;   // width * height * 4
};

/// Read a PNG file into pixels. False when the file is not a PNG this
/// decoder understands (the reason says which part it was).
bool png_read_file(const std::string& path, PngImage* out, std::string* err);

/// Read a PNG that is already in memory. False when it is not a PNG this
/// decoder understands.
bool png_read_memory(const std::string& data, PngImage* out, std::string* err);

}  // namespace tea_tunnel
