// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_codec.h -- the small slice of MQTT 3.1.1 that both halves speak.
//
// The agent is the client (a2 --mqtt ...) and a2_broker is the server (--mqtt).
// They only use CONNECT/CONNACK, SUBSCRIBE/SUBACK, PUBLISH/PUBACK,
// PINGREQ/PINGRESP and DISCONNECT -- no QoS 2, no persistent sessions, no
// MQTT 5. Keeping the codec here (header only, no dependencies beyond the
// standard library) means the two binaries can never disagree about a frame.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace mqtt {

enum PacketType : uint8_t {
  CONNECT = 1,
  CONNACK = 2,
  PUBLISH = 3,
  PUBACK = 4,
  SUBSCRIBE = 8,
  SUBACK = 9,
  UNSUBSCRIBE = 10,
  UNSUBACK = 11,
  PINGREQ = 12,
  PINGRESP = 13,
  DISCONNECT = 14,
};

struct Packet {
  uint8_t type = 0;
  uint8_t flags = 0;
  std::string body;
};

enum class ParseStatus { ok, incomplete, error };

// ---- primitives ------------------------------------------------------------

inline void append_varint(uint32_t value, std::string* out) {
  do {
    uint8_t byte = static_cast<uint8_t>(value % 128);
    value /= 128;
    if (value) byte |= 0x80;
    out->push_back(static_cast<char>(byte));
  } while (value);
}

inline bool read_varint(const std::string& buffer, size_t* pos, uint32_t* value) {
  uint32_t multiplier = 1;
  uint32_t result = 0;
  size_t p = *pos;
  for (int i = 0; i < 4; ++i) {
    if (p >= buffer.size()) return false;
    const uint8_t byte = static_cast<uint8_t>(buffer[p++]);
    result += static_cast<uint32_t>(byte & 0x7f) * multiplier;
    if ((byte & 0x80) == 0) {
      *pos = p;
      *value = result;
      return true;
    }
    multiplier *= 128;
  }
  return false;
}

inline void append_string(std::string* out, const std::string& text) {
  out->push_back(static_cast<char>((text.size() >> 8) & 0xff));
  out->push_back(static_cast<char>(text.size() & 0xff));
  out->append(text);
}

inline bool read_string(const std::string& buffer, size_t* pos, std::string* out) {
  if (*pos + 2 > buffer.size()) return false;
  const size_t length = (static_cast<uint8_t>(buffer[*pos]) << 8) |
                        static_cast<uint8_t>(buffer[*pos + 1]);
  *pos += 2;
  if (*pos + length > buffer.size()) return false;
  out->assign(buffer, *pos, length);
  *pos += length;
  return true;
}

inline bool read_u16(const std::string& buffer, size_t* pos, uint16_t* out) {
  if (*pos + 2 > buffer.size()) return false;
  *out = static_cast<uint16_t>((static_cast<uint8_t>(buffer[*pos]) << 8) |
                               static_cast<uint8_t>(buffer[*pos + 1]));
  *pos += 2;
  return true;
}

inline std::string frame(uint8_t header, const std::string& body) {
  std::string out;
  out.reserve(body.size() + 5);
  out.push_back(static_cast<char>(header));
  append_varint(static_cast<uint32_t>(body.size()), &out);
  out += body;
  return out;
}

// ---- incremental packet reader ---------------------------------------------

/// Take one complete packet out of `buffer`. `consumed` says how many bytes to
/// drop. A frame larger than `max_size` is an error (do not grow the buffer
/// waiting for it: that is how a 2 GB PUBLISH turns into an OOM).
inline ParseStatus parse_packet(const std::string& buffer, size_t* consumed, Packet* out,
                                size_t max_size) {
  if (buffer.size() < 2) return ParseStatus::incomplete;
  size_t pos = 1;
  uint32_t length = 0;
  if (!read_varint(buffer, &pos, &length)) return ParseStatus::incomplete;
  if (max_size && length > max_size) return ParseStatus::error;
  if (buffer.size() < pos + length) return ParseStatus::incomplete;
  out->type = static_cast<uint8_t>(static_cast<uint8_t>(buffer[0]) >> 4);
  out->flags = static_cast<uint8_t>(static_cast<uint8_t>(buffer[0]) & 0x0f);
  out->body.assign(buffer, pos, length);
  *consumed = pos + length;
  return ParseStatus::ok;
}

// ---- CONNECT ---------------------------------------------------------------

struct ConnectInfo {
  std::string protocol;
  uint8_t level = 0;
  uint8_t flags = 0;
  uint16_t keepalive = 0;
  std::string client_id;
  std::string will_topic;
  std::string will_payload;
  std::string user;
  std::string password;
  bool clean_session() const { return (flags & 0x02) != 0; }
  bool has_will() const { return (flags & 0x04) != 0; }
  bool will_retain() const { return (flags & 0x20) != 0; }
};

inline bool parse_connect(const std::string& body, ConnectInfo* out) {
  size_t pos = 0;
  if (!read_string(body, &pos, &out->protocol)) return false;
  if (pos + 1 > body.size()) return false;
  out->level = static_cast<uint8_t>(body[pos++]);
  if (pos + 1 > body.size()) return false;
  out->flags = static_cast<uint8_t>(body[pos++]);
  if (!read_u16(body, &pos, &out->keepalive)) return false;
  if (!read_string(body, &pos, &out->client_id)) return false;
  if (out->has_will()) {
    if (!read_string(body, &pos, &out->will_topic)) return false;
    if (!read_string(body, &pos, &out->will_payload)) return false;
  }
  if (out->flags & 0x80) {
    if (!read_string(body, &pos, &out->user)) return false;
  }
  if (out->flags & 0x40) {
    if (!read_string(body, &pos, &out->password)) return false;
  }
  return true;
}

inline std::string make_connect(const std::string& client_id, const std::string& user,
                                const std::string& password, uint16_t keepalive_s,
                                const std::string& will_topic, const std::string& will_payload,
                                bool will_retain = true) {
  std::string body;
  append_string(&body, "MQTT");
  body.push_back(4);  // protocol level 3.1.1
  uint8_t flags = 0x02;  // clean session
  if (!will_topic.empty()) flags |= 0x04;
  if (!will_topic.empty() && will_retain) flags |= 0x20;
  if (!user.empty()) flags |= 0x80;
  if (!password.empty()) flags |= 0x40;
  body.push_back(static_cast<char>(flags));
  body.push_back(static_cast<char>((keepalive_s >> 8) & 0xff));
  body.push_back(static_cast<char>(keepalive_s & 0xff));
  append_string(&body, client_id);
  if (!will_topic.empty()) {
    append_string(&body, will_topic);
    append_string(&body, will_payload);
  }
  if (!user.empty()) append_string(&body, user);
  if (!password.empty()) append_string(&body, password);
  return frame(static_cast<uint8_t>(CONNECT << 4), body);
}

inline std::string make_connack(uint8_t return_code) {
  std::string body;
  body.push_back(0);  // session present = false
  body.push_back(static_cast<char>(return_code));
  return frame(static_cast<uint8_t>(CONNACK << 4), body);
}

inline bool parse_connack(const std::string& body, uint8_t* return_code) {
  if (body.size() < 2) return false;
  *return_code = static_cast<uint8_t>(body[1]);
  return true;
}

// ---- SUBSCRIBE -------------------------------------------------------------

inline std::string make_subscribe(uint16_t packet_id, const std::vector<std::string>& filters) {
  std::string body;
  body.push_back(static_cast<char>((packet_id >> 8) & 0xff));
  body.push_back(static_cast<char>(packet_id & 0xff));
  for (size_t i = 0; i < filters.size(); ++i) {
    append_string(&body, filters[i]);
    body.push_back(1);  // requested QoS 1
  }
  return frame(0x82, body);  // SUBSCRIBE, reserved bits 0010
}

inline std::string make_suback(uint16_t packet_id, bool granted) {
  std::string body;
  body.push_back(static_cast<char>((packet_id >> 8) & 0xff));
  body.push_back(static_cast<char>(packet_id & 0xff));
  body.push_back(granted ? 1 : char(0x80));
  return frame(0x90, body);
}

struct SubscribeRequest {
  uint16_t packet_id = 0;
  std::vector<std::string> filters;
};

inline bool parse_subscribe(const std::string& body, SubscribeRequest* out) {
  size_t pos = 0;
  if (!read_u16(body, &pos, &out->packet_id)) return false;
  while (pos < body.size()) {
    std::string filter;
    if (!read_string(body, &pos, &filter)) return false;
    if (pos >= body.size()) return false;
    ++pos;  // requested QoS
    out->filters.push_back(filter);
  }
  return !out->filters.empty();
}

// ---- PUBLISH ---------------------------------------------------------------

struct PublishInfo {
  std::string topic;
  std::string payload;
  uint16_t packet_id = 0;
  uint8_t qos = 0;
  bool retain = false;
  bool dup = false;
};

inline bool parse_publish(uint8_t flags, const std::string& body, PublishInfo* out) {
  out->retain = (flags & 0x01) != 0;
  out->qos = static_cast<uint8_t>((flags >> 1) & 0x03);
  out->dup = (flags & 0x08) != 0;
  if (out->qos > 1) return false;  // we do not speak QoS 2
  size_t pos = 0;
  if (!read_string(body, &pos, &out->topic)) return false;
  if (out->qos > 0) {
    if (!read_u16(body, &pos, &out->packet_id)) return false;
  }
  if (pos > body.size()) return false;
  out->payload.assign(body, pos, std::string::npos);
  return true;
}

inline std::string make_publish(const std::string& topic, const std::string& payload, uint8_t qos,
                                bool retain, uint16_t packet_id) {
  std::string body;
  append_string(&body, topic);
  if (qos > 0) {
    body.push_back(static_cast<char>((packet_id >> 8) & 0xff));
    body.push_back(static_cast<char>(packet_id & 0xff));
  }
  body += payload;
  uint8_t header = static_cast<uint8_t>(PUBLISH << 4);
  if (retain) header |= 0x01;
  header |= static_cast<uint8_t>((qos & 0x03) << 1);
  return frame(header, body);
}

inline std::string make_puback(uint16_t packet_id) {
  std::string body;
  body.push_back(static_cast<char>((packet_id >> 8) & 0xff));
  body.push_back(static_cast<char>(packet_id & 0xff));
  return frame(static_cast<uint8_t>(PUBACK << 4), body);
}

inline bool parse_packet_id(const std::string& body, uint16_t* packet_id) {
  size_t pos = 0;
  return read_u16(body, &pos, packet_id);
}

inline std::string make_pingreq() { return frame(static_cast<uint8_t>(PINGREQ << 4), std::string()); }
inline std::string make_pingresp() {
  return frame(static_cast<uint8_t>(PINGRESP << 4), std::string());
}
inline std::string make_disconnect() {
  return frame(static_cast<uint8_t>(DISCONNECT << 4), std::string());
}

// ---- topics ----------------------------------------------------------------

/// MQTT wildcard matching: '+' is one level, '#' is the rest (last level only).
inline bool topic_matches(const std::string& filter, const std::string& topic) {
  size_t f = 0, t = 0;
  for (;;) {
    const size_t fend = filter.find('/', f);
    const std::string flevel =
        fend == std::string::npos ? filter.substr(f) : filter.substr(f, fend - f);
    if (flevel == "#") return true;
    if (t > topic.size()) return false;
    const size_t tend = topic.find('/', t);
    const std::string tlevel =
        tend == std::string::npos ? topic.substr(t) : topic.substr(t, tend - t);
    if (flevel != "+" && flevel != tlevel) return false;
    const bool fdone = fend == std::string::npos;
    const bool tdone = tend == std::string::npos;
    if (fdone || tdone) return fdone && tdone;
    f = fend + 1;
    t = tend + 1;
  }
}

/// Topic filters may contain '+' and '#'; a topic name may not.
inline bool valid_topic_filter(const std::string& filter) {
  if (filter.empty() || filter.size() > 65535) return false;
  size_t start = 0;
  while (start <= filter.size()) {
    const size_t end = filter.find('/', start);
    const std::string level =
        end == std::string::npos ? filter.substr(start) : filter.substr(start, end - start);
    if (level.find('#') != std::string::npos) {
      if (level != "#") return false;
      if (end != std::string::npos) return false;  // '#' must be the last level
      return true;
    }
    if (level.find('+') != std::string::npos && level != "+") return false;
    if (end == std::string::npos) break;
    start = end + 1;
  }
  return true;
}

/// A packet name (topic, client id) must be valid UTF-8 and must not contain NUL.
inline bool valid_utf8(const std::string& text) {
  size_t i = 0;
  while (i < text.size()) {
    const uint8_t c = static_cast<uint8_t>(text[i]);
    if (c == 0) return false;
    if (c < 0x80) {
      ++i;
      continue;
    }
    int extra = 0;
    uint32_t cp = 0;
    if ((c & 0xE0) == 0xC0) {
      extra = 1;
      cp = c & 0x1F;
    } else if ((c & 0xF0) == 0xE0) {
      extra = 2;
      cp = c & 0x0F;
    } else if ((c & 0xF8) == 0xF0) {
      extra = 3;
      cp = c & 0x07;
    } else {
      return false;
    }
    if (i + static_cast<size_t>(extra) >= text.size()) return false;
    for (int k = 1; k <= extra; ++k) {
      const uint8_t cc = static_cast<uint8_t>(text[i + static_cast<size_t>(k)]);
      if ((cc & 0xC0) != 0x80) return false;
      cp = (cp << 6) | (cc & 0x3F);
    }
    if ((extra == 1 && cp < 0x80) || (extra == 2 && cp < 0x800) ||
        (extra == 3 && cp < 0x10000) || cp > 0x10FFFF ||
        (cp >= 0xD800 && cp <= 0xDFFF)) {
      return false;
    }
    i += static_cast<size_t>(extra) + 1;
  }
  return true;
}

}  // namespace mqtt
