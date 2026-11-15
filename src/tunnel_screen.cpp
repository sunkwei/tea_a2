// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_screen.cpp -- the screen channel's little protocol.
//
// Both sides of the tunnel need to agree on it, so it lives in its own
// file (see tunnel_screen.h for what it is for).
#include "tunnel_screen.h"

#include <nlohmann/json.hpp>

namespace tea_tunnel {

namespace {
using json = nlohmann::json;
}  // namespace

void put_u32be(std::string* out, uint32_t value) {
  out->push_back(static_cast<char>((value >> 24) & 0xff));
  out->push_back(static_cast<char>((value >> 16) & 0xff));
  out->push_back(static_cast<char>((value >> 8) & 0xff));
  out->push_back(static_cast<char>(value & 0xff));
}

bool get_u32be(const std::string& body, std::size_t at, uint32_t* value) {
  if (at + 4 > body.size()) return false;
  *value = (static_cast<uint32_t>(static_cast<unsigned char>(body[at])) << 24) |
           (static_cast<uint32_t>(static_cast<unsigned char>(body[at + 1])) << 16) |
           (static_cast<uint32_t>(static_cast<unsigned char>(body[at + 2])) << 8) |
           static_cast<uint32_t>(static_cast<unsigned char>(body[at + 3]));
  return true;
}

std::string pack_screen_pictures(const std::vector<ScreenPicture>& pictures) {
  std::string out;
  put_u32be(&out, static_cast<uint32_t>(pictures.size()));
  for (const ScreenPicture& picture : pictures) {
    put_u32be(&out, picture.seq);
    out.push_back(picture.key ? 1 : 0);
    put_u32be(&out, static_cast<uint32_t>(picture.bytes.size()));
    out.append(picture.bytes);
  }
  return out;
}

bool unpack_screen_pictures(const std::string& body, std::vector<ScreenPicture>* out) {
  if (out == nullptr) return false;
  out->clear();
  uint32_t count = 0;
  if (!get_u32be(body, 0, &count)) return false;
  std::size_t at = 4;
  for (uint32_t i = 0; i < count; i++) {
    ScreenPicture picture;
    if (!get_u32be(body, at, &picture.seq)) return false;
    at += 4;
    if (at >= body.size()) return false;
    picture.key = body[at++] != 0;
    uint32_t length = 0;
    if (!get_u32be(body, at, &length)) return false;
    at += 4;
    if (at + length > body.size()) return false;
    picture.bytes = body.substr(at, length);
    at += length;
    out->push_back(picture);
  }
  return true;
}

std::string pack_screen_frame(const std::vector<ScreenPicture>& pictures,
                               const ScreenGeometry& geometry) {
  // The pictures, then the sizes as JSON, then how long the JSON is. The
  // length is the last four bytes, so the reader finds the sizes without
  // having to guess where the pictures stop.
  std::string out = pack_screen_pictures(pictures);
  const std::string sizes = geometry.to_json();
  out.append(sizes);
  put_u32be(&out, static_cast<uint32_t>(sizes.size()));
  return out;
}

bool unpack_screen_frame(const std::string& body, std::vector<ScreenPicture>* out,
                         ScreenGeometry* geometry) {
  if (out == nullptr) return false;
  if (body.size() < 4) return false;
  // The length of the sizes is the last four bytes.
  uint32_t length = 0;
  if (!get_u32be(body, body.size() - 4, &length)) return false;
  if (length > body.size() - 4) return false;
  const std::size_t pictures_length = body.size() - 4 - length;
  if (!unpack_screen_pictures(body.substr(0, pictures_length), out)) return false;
  if (geometry != nullptr) {
    if (!ScreenGeometry::from_json(body.substr(pictures_length, length), geometry)) return false;
  }
  return true;
}

std::string ScreenGeometry::to_json() const {
  json body = json::object();
  body["frame_width"] = frame_width;
  body["frame_height"] = frame_height;
  body["screen_width"] = screen_width;
  body["screen_height"] = screen_height;
  body["screen_x"] = screen_x;
  body["screen_y"] = screen_y;
  return body.dump();
}

bool ScreenGeometry::from_json(const std::string& text, ScreenGeometry* out) {
  if (out == nullptr) return false;
  json body;
  try {
    body = json::parse(text);
  } catch (const std::exception&) {
    return false;
  }
  if (!body.is_object()) return false;
  out->frame_width = body.value("frame_width", 0);
  out->frame_height = body.value("frame_height", 0);
  out->screen_width = body.value("screen_width", 0);
  out->screen_height = body.value("screen_height", 0);
  out->screen_x = body.value("screen_x", 0);
  out->screen_y = body.value("screen_y", 0);
  return true;
}

}  // namespace tea_tunnel
