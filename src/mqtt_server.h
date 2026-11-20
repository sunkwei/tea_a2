// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_server.h -- the broker's MQTT side: registration and forwarding.
//
// It shares the signalling port (and its TLS certificate) with the tunnel:
// the accept loop looks at the first byte after the handshake and hands the
// connection here when it is an MQTT CONNECT. Everything it needs is in
// a2_broker.db, so accounts can be added while the broker runs.
#pragma once

#include <cstddef>
#include <memory>
#include <string>

namespace tea_tunnel {
struct Connection;
}

namespace mqtt {

class Server {
 public:
  struct Options {
    std::string db_path;                // a2_broker.db
    size_t max_payload_bytes = 256 * 1024;
    size_t max_clients = 200;
    int read_timeout_ms = 500;          // how often a session does its chores
  };

  Server();
  ~Server();

  Server(const Server&) = delete;
  Server& operator=(const Server&) = delete;

  /// Creates mqtt_accounts when it is missing (the rest of the database is
  /// left alone) and opens the handle used by every CONNECT.
  bool start(const Options& options, std::string* err);

  /// Takes over a connection whose first byte (0x10) was already read.
  /// Returns immediately; the session lives in its own thread.
  void serve(std::shared_ptr<tea_tunnel::Connection> connection, const std::string& initial);

  /// One JSON line for the capacity snapshot.
  std::string snapshot() const;

 private:
  struct Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace mqtt
