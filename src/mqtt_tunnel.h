// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_tunnel.h -- turning a control message into running a2_tunnels.
//
// Two pieces:
//   TunnelController  owns the sidecar processes (spawn, read their banner,
//                     stop). Up to kMaxTunnels of them may live on one device
//                     at the same time; the session code the sidecar prints
//                     is the handle of a tunnel, so the operator can stop one
//                     of them without touching the others.
//   CommandHandler    validates a command, calls the controller, publishes the
//                     answer. It runs on a small pool of threads: a sidecar
//                     that takes 20 s to come up must not block the MQTT
//                     socket, nor the other commands.
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
#include <vector>

namespace tea {

class MqttClient;

class TunnelController {
 public:
  /// How many tunnels one device may run at the same time.
  static const int kMaxTunnels = 4;

  struct Status {
    std::string state;  // starting | ready | stopping | stopped | failed
    std::string session;
    std::string broker;
    std::string error;
  };

  /// One row of the tunnel table.
  struct TunnelInfo {
    std::string session;
    std::string state;
    std::string broker;
    int wait_timeout = 0;
    long long created_at = 0;  // unix seconds
    long long expires_at = 0;  // unix seconds
  };

  TunnelController();
  ~TunnelController();

  /// Sidecar binary and what it should proxy (usually 127.0.0.1:<a2 port>).
  void configure(const std::string& executable, const std::string& target);

  /// How long the sidecar may sit there without traffic before it gives the
  /// tunnel up (0 = never). Handed to the sidecar as --idle-timeout.
  void set_idle_timeout(int seconds);

  /// Starts a NEW tunnel: every call makes a new session. False when the
  /// device already runs kMaxTunnels tunnels (error "too_many_tunnels") or
  /// when the sidecar cannot be started at all.
  bool start(const std::string& broker, int wait_timeout_s, long long* id, std::string* error);

  /// Waits until the tunnel is ready (or has failed).
  Status wait_for(long long id, int timeout_s);

  /// Stops the tunnel with that session code.
  Status stop(const std::string& session);

  /// The state of one tunnel. False when the session is unknown.
  bool lookup(const std::string& session, Status* out) const;

  /// Every tunnel that is alive right now.
  std::vector<TunnelInfo> list() const;

  /// Why the last tunnel ended ("idle", "stopped", "failed", "exited").
  std::string last_exit_reason() const;

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
