// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_tunnel.h -- turning a control message into a running a2_tunnel.
//
// Two pieces:
//   TunnelController  owns the sidecar process (spawn, read its banner, stop).
//   CommandHandler    validates a command, calls the controller, publishes the
//                     answer. It runs on its own thread so a 20 s sidecar start
//                     never blocks the MQTT socket.
#pragma once

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

namespace tea {

class MqttClient;

class TunnelController {
 public:
  struct Status {
    std::string state;  // stopped | starting | ready | stopping | failed
    std::string session;
    std::string broker;
    std::string listen;
    std::string error;
  };

  TunnelController();
  ~TunnelController();

  /// Sidecar binary and what it should proxy (usually 127.0.0.1:<a2 port>).
  void configure(const std::string& executable, const std::string& target);

  /// Starts the sidecar and waits (up to ~25 s) for its session code.
  /// A second call while one is running returns the existing session.
  Status start(const std::string& broker, int wait_timeout_s);

  Status stop();
  Status status() const;
  /// Called when the agent exits: never leaves a sidecar behind.
  void shutdown();

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

class CommandHandler {
 public:
  struct Options {
    std::string resp_topic;
    std::string default_target;  // 127.0.0.1:8383
    std::string sidecar_path;
    std::string tunnel_broker;
    int default_wait_timeout = 1800;
    int max_wait_timeout = 7200;
  };

  CommandHandler();
  ~CommandHandler();

  void start(MqttClient* client, TunnelController* tunnel, const Options& options);
  /// Called from the MQTT thread; only queues, never blocks.
  void on_message(const std::string& topic, const std::string& payload);
  void stop();

 private:
  void run();
  std::string execute(const std::string& payload);

  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace tea
