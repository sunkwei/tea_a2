// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_client.h -- the agent's side of the control channel.
//
// One long-lived TLS connection to a broker, one subscription to the device's
// own command topic, one heartbeat. Nothing here knows about tunnels: the
// caller gets (topic, payload) and decides what to do.
#pragma once

#include <atomic>
#include <functional>
#include <memory>
#include <string>

namespace tea {

/// The IPv4 address of the interface that carries the default route, used as
/// the suffix of the registration name. Empty when there is none (airplane
/// mode, no route): the caller then says "unknown" instead of failing.
std::string primary_ipv4();

/// The MAC address of the interface that carries the default route, as 12
/// lower-case hex digits (no separators). Empty when it cannot be read.
///
/// The registration name carries it because an IPv4 address is not unique
/// across customer networks: two sites may both have 192.168.1.5 and both
/// call the machine "a2". The MAC is.
std::string primary_mac();

class MqttClient {
 public:
  struct Config {
    std::string host;
    int port = 1883;
    std::string user;
    std::string password;
    std::string client_id;  // the registered name: "<name>@<ipv4>"
    std::string ca_bundle;
    bool verify_peer = true;
    /// The keepalive advertised in CONNECT. It must be at least as long as
    /// the longest gap between two pings: the broker drops a session that
    /// stays silent for 1.5x keepalive (src/mqtt_server.cpp). The heartbeat
    /// is a random 1..5 minutes, so the keepalive is the upper bound (5
    /// minutes) - the broker then tolerates 7.5 minutes of silence.
    int keepalive_s = 300;
    int connect_timeout_ms = 8000;
    std::string subscribe_topic;
    std::string will_topic;
    std::string will_payload;
    /// Called on every (re)connect and then once a minute: the registration
    /// payload carries a timestamp and the online flag, so it is built fresh.
    std::function<std::string()> register_payload;
  };

  using MessageHandler = std::function<void(const std::string& topic, const std::string& payload)>;

  MqttClient();
  ~MqttClient();

  MqttClient(const MqttClient&) = delete;
  MqttClient& operator=(const MqttClient&) = delete;

  /// Starts the worker thread. Safe to call once; later calls are ignored.
  void start(const Config& config, MessageHandler on_message);
  void stop();

  /// Queues a message for the worker thread (the socket has one writer).
  bool publish(const std::string& topic, const std::string& payload, int qos);

  bool connected() const;
  std::string last_error() const;
  std::string client_id() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace tea
