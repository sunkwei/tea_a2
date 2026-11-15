// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_screen.h -- the second data channel: the screen itself.
//
// The tunnel used to carry the screen as **HTTP**: the device side ran a
// small web service on the loopback and the picture was pulled out of it
// through the same channel that carried the a2's SSE. That had two costs:
// the picture had to be copied into HTTP and out again, and the two
// streams shared one channel - a slow picture held up the chat.
//
// So the tunnel has **two** data channels now:
//
//   "a2"     the a2's own bytes (the SSE stream). Unchanged: the frame
//            format is [u8 type][u32 length][payload] with OPEN=1,
//            DATA=2, CLOSE=3.
//   "screen" the picture. The device side pushes it, the control side
//            serves it to the browser.
//
// The label travels in the DCEP OPEN message, so the receiving side knows
// which channel is which (libdatachannel hands it over in the label).
//
// The screen channel carries its own small protocol. Every message is
// [u8 type][u32 length][payload] as well, so the two channels can share
// the helpers:
//
//   1 PICTURE       device -> control: the pictures
//   2 INFO         device -> control: the sizes of the picture and screen
//   3 INPUT        control -> device: the mouse and the keyboard
//   4 INPUT_RESULT device -> control: what happened to those events
//
// The picture is pushed, not pulled: the device side sends what it has,
// the control side keeps it and the browser asks for what it has not seen
// yet. That way a slow browser cannot make the device wait, and the
// device never has to keep a picture nobody asked for.
//
// The input goes the other way and has an answer: the browser asks for a
// click and wants to know whether it arrived, so the two sides agree on
// a number (the "id") that comes back with the answer.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace tea_tunnel {

/// The label of the channel that carries the a2's bytes.
constexpr const char* kTargetChannelLabel = "a2";

/// The label of the channel that carries the picture.
constexpr const char* kScreenChannelLabel = "screen";

/// The messages on the screen channel.
enum ScreenMessage : uint8_t {
  kScreenPicture = 1,      // device -> control: the pictures
  kScreenInfo = 2,         // device -> control: the sizes
  kScreenInput = 3,        // control -> device: the mouse and the keyboard
  kScreenInputResult = 4,  // device -> control: what happened to them
};

/// Put a 32 bit number into a string, big endian (the way the tunnel
/// writes all of its numbers).
void put_u32be(std::string* out, uint32_t value);

/// Read a 32 bit number from a string. False when there are not enough
/// bytes.
bool get_u32be(const std::string& body, std::size_t at, uint32_t* value);

/// One picture of the screen: its number, whether it is a key frame, and
/// the bytes.
struct ScreenPicture {
  uint32_t seq = 0;
  bool key = false;
  std::string bytes;
};

/// The sizes: how big the picture is, and how big the screen it shows is
/// (the mouse is placed in the screen's coordinates).
struct ScreenGeometry {
  int frame_width = 0;
  int frame_height = 0;
  int screen_width = 0;
  int screen_height = 0;
  int screen_x = 0;
  int screen_y = 0;

  /// The same, as the JSON that goes over the channel.
  std::string to_json() const;

  /// Read it back. False when the JSON does not have it.
  static bool from_json(const std::string& text, ScreenGeometry* out);
};

/// The pictures of the stream, packed into one body:
///
///   4 bytes   how many pictures
///   per picture: 4 bytes the number, 1 byte (1 = a key frame),
///                4 bytes the length, then the bytes
std::string pack_screen_pictures(const std::vector<ScreenPicture>& pictures);

/// Unpack what pack_screen_pictures wrote. False when the body is broken.
bool unpack_screen_pictures(const std::string& body, std::vector<ScreenPicture>* out);

/// The pictures and the sizes, in one body. The sizes travel with the
/// picture: a separate message could arrive later, and the browser needs
/// them before it can put the mouse where the user points.
std::string pack_screen_frame(const std::vector<ScreenPicture>& pictures,
                               const ScreenGeometry& geometry);

/// Unpack what pack_screen_frame wrote.
bool unpack_screen_frame(const std::string& body, std::vector<ScreenPicture>* out,
                         ScreenGeometry* geometry);

}  // namespace tea_tunnel
