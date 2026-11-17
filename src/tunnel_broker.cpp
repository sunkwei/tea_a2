// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_broker.cpp -- the one process in the middle: STUN + signalling + TURN.
//
// It is what both sides talk to:
//
//   * STUN  : libjuice's server mode answers "what do you see me as?" and
//             hands out the srflx candidates ICE needs for hole punching.
//   * TURN  : the same server, with credentials, relays the UDP packets when
//             the hole does not punch (symmetric NAT, UDP blocked...). The
//             relay only ever sees DTLS ciphertext.
//   * signalling: a tiny line protocol (newline-delimited JSON) that carries
//             the SDP and the ICE candidates between the two peers.
//
// Nothing about the session's content passes through here: the data channel
// is end-to-end DTLS.
//
//   a2_broker --stun-port 3478 --signal-port 8382 \
//              --realm a2 --turn-user a2 --turn-pass <secret> --token <secret>
//
// ## One file, no web server in front
//
// The signalling carries the room token, so the public port must be TLS.
// It used to be nginx that terminated the TLS and passed a plain stream to
// this process - that meant two configurations to keep in step (nginx's and
// this one's) and a certificate to install by hand. Now the TLS is **here**:
// the broker makes its own certificate on the first start and keeps it, and
// prints its fingerprint. The client can then pin that fingerprint (see
// a2_tunnel --tls-pin), which does not depend on the address the broker is
// reached at - handy when the address changes.
//
// The signalling protocol (one JSON object per line, both ways):
//
//   -> {"room":"plant-a","token":"...","role":"agent"}
//   <- {"type":"joined","peer":true|false}
//   -> {"type":"offer"|"answer"|"candidate","sdp":"..."}   (relayed to the peer)
//
#include <juice/juice.h>
#include <cstdlib>
#include <ctime>
#include <nlohmann/json.hpp>

#ifndef TEA_TUNNEL_VERSION
#define TEA_TUNNEL_VERSION "unknown"
#endif

#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/error.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/pk.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>

#include <atomic>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <string>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using json = nlohmann::json;

#include "tunnel_net.h"
#include "tunnel_proc_stat.h"
#include "tunnel_paths.h"
#include "tunnel_tls.h"
#include "tunnel_turn_db.h"
#include "tunnel_usage.h"

// ---- the built-in TURN credentials -----------------------------------------
// The TURN relay is the broker's bandwidth, not a secret worth hiding: the
// credentials only decide who may push traffic through it. They are built in
// so that no configuration file has to be copied to every machine (the sidecar
// next to the a2, the operator's machine, the broker) - the CLI flags still
// override them.
constexpr const char* kDefaultTurnUser = "1hblsqt.";
constexpr const char* kDefaultTurnPass = "2ghlmcl,";

// ---- the defaults of the broker -------------------------------------------
// The relay pool may only use the ports the system may use for its own
// connections: 49152-65535.
constexpr uint16_t kEphemeralBegin = 49152;
constexpr uint16_t kEphemeralEnd = 65535;

// How much the broker serves when nobody says otherwise: 256 peer pairs
// (512 relay allocations) in a pool of 1016 ports.
constexpr uint16_t kDefaultStunPort = 3478;
constexpr uint16_t kDefaultSignalPort = 8382;
constexpr uint16_t kDefaultRelayBegin = 49160;
constexpr uint16_t kDefaultRelayEnd = 50175;
constexpr int kDefaultMaxPeerPair = 256;

// When the operator sets no limit: an explicit high number. Zero would mean
// "the library's own default (1000)", which is a silent cap.
constexpr int kUnlimitedAllocations = 4096;

/// Where the room token lives when it is not given on the command line.
/// The room token is a real secret: it is the key to the room, so it is NOT
/// built in like the TURN credentials.
std::string token_file_path() {
  return tea_tunnel::tunnel_dir() + "/token";
}

/// Read the room token from the conventional file (first line, trimmed).
bool read_token_file(std::string* out) {
  std::FILE* f = std::fopen(token_file_path().c_str(), "rb");
  if (!f) return false;
  char buf[512] = {0};
  size_t n = std::fread(buf, 1, sizeof(buf) - 1, f);
  std::fclose(f);
  if (n == 0) return false;
  std::string s(buf, n);
  size_t nl = s.find('\n');
  if (nl != std::string::npos) s = s.substr(0, nl);
  while (!s.empty() && (s.back() == '\r' || s.back() == ' ' || s.back() == '\t')) s.pop_back();
  if (s.empty()) return false;
  *out = s;
  return true;
}

namespace {

// ---- one connected signalling client ---------------------------------------
//
// A connection is a socket, and sometimes a TLS session on top of it. The
// two live together in one object: mbedTLS' socket helpers want a pointer to
// the socket, so the socket has to outlive the TLS session, and the TLS
// session has to outlive the handshake.
struct Connection {
  tea_socket_t fd = kTeaInvalidSocket;
  mbedtls_ssl_context ssl{};
  bool tls = false;

  ~Connection() {
    if (tls) {
      mbedtls_ssl_free(&ssl);
      tea_net_close(fd);
    } else if (fd != kTeaInvalidSocket) {
      tea_net_close(fd);
    }
  }
};

struct Peer {
  std::shared_ptr<Connection> conn;
  std::string role;
  std::string room;
  std::mutex out;        // serialises writes to the socket
  std::atomic<bool> alive{true};
};

std::mutex g_rooms_mu;
std::map<std::string, std::vector<std::shared_ptr<Peer>>> g_rooms;

std::string g_token;
// The token of each room. A room learns its token from the side that
// registers it (the agent side generates a fresh one on every start), so
// nothing has to be configured on the machines: the token travels with the
// connection details the agent side prints.
std::map<std::string, std::string> g_room_tokens;

void send_line(const std::shared_ptr<Peer>& p, const json& j) {
  if (!p || !p->alive) return;
  std::string line = j.dump() + "\n";
  std::lock_guard<std::mutex> lock(p->out);
  int n = 0;
  if (p->conn->tls) {
    n = mbedtls_ssl_write(&p->conn->ssl, reinterpret_cast<const unsigned char*>(line.data()),
                           line.size());
  } else {
    n = tea_net_send(p->conn->fd, line.data(), line.size());
  }
  if (n != static_cast<int>(line.size())) p->alive = false;
}

std::vector<std::shared_ptr<Peer>> room_peers(const std::string& room) {
  std::lock_guard<std::mutex> lock(g_rooms_mu);
  auto it = g_rooms.find(room);
  if (it == g_rooms.end()) return {};
  return it->second;
}

void drop_peer(const std::shared_ptr<Peer>& p) {
  p->alive = false;
  std::lock_guard<std::mutex> lock(g_rooms_mu);
  auto it = g_rooms.find(p->room);
  if (it == g_rooms.end()) return;
  auto& v = it->second;
  for (size_t i = 0; i < v.size(); ++i) {
    if (v[i] == p) {
      v.erase(v.begin() + static_cast<long>(i));
      break;
    }
  }
  if (v.empty()) g_rooms.erase(it);
}

void handle_line(const std::shared_ptr<Peer>& p, const json& j) {
  // The first line joins a room; everything after that is relayed.
  if (j.contains("room") && j.contains("token")) {
    const std::string token = j.value("token", "");
    const bool registering = j.value("register", false);
    p->room = j.value("room", "");
    {
      std::lock_guard<std::mutex> lock(g_rooms_mu);
      std::string& current = g_room_tokens[p->room];
      if (current.empty()) {
        // First one in the room decides the token.
        current = token;
      } else if (registering) {
        // The agent side registers on every start: it just generated a new
        // token, so the room's old one is replaced.
        if (current != token) {
          std::fprintf(stderr, "broker: the token of room %s was replaced\n", p->room.c_str());
        }
        current = token;
      }
      if (!g_token.empty() && token != g_token) {
        // A broker-wide token was configured: it wins over the room's own.
        send_line(p, {{"type", "error"}, {"message", "bad token"}});
        p->alive = false;
        return;
      }
      if (token != current) {
        send_line(p, {{"type", "error"}, {"message", "bad token"}});
        p->alive = false;
        return;
      }
    }
    p->role = j.value("role", "");
    bool peer_present = false;
    {
      std::lock_guard<std::mutex> lock(g_rooms_mu);
      auto& v = g_rooms[p->room];
      // One agent + one client per room; a second of the same role replaces
      // the old one (a restarted sidecar).
      for (auto it = v.begin(); it != v.end();) {
        if ((*it)->role == p->role) {
          (*it)->alive = false;
          it = v.erase(it);
        } else {
          ++it;
        }
      }
      v.push_back(p);
      peer_present = v.size() > 1;
    }
    send_line(p, {{"type", "joined"}, {"peer", peer_present}});
    if (peer_present) {
      for (auto& other : room_peers(p->room)) {
        if (other != p) send_line(other, {{"type", "peer_joined"}});
      }
    }
    return;
  }

  // Everything else goes to the other side of the room.
  for (auto& other : room_peers(p->room)) {
    if (other != p && other->alive) send_line(other, j);
  }
}

void peer_loop(std::shared_ptr<Peer> p) {
  std::string buf;
  char chunk[4096];
  while (p->alive) {
    ssize_t n = 0;
    if (p->conn->tls) {
      n = mbedtls_ssl_read(&p->conn->ssl, reinterpret_cast<unsigned char*>(chunk), sizeof(chunk));
    } else {
      n = tea_net_recv(p->conn->fd, chunk, sizeof(chunk));
    }
    if (n <= 0) break;
    buf.append(chunk, static_cast<size_t>(n));
    size_t pos;
    while ((pos = buf.find('\n')) != std::string::npos) {
      std::string line = buf.substr(0, pos);
      buf.erase(0, pos + 1);
      if (line.empty()) continue;
      try {
        handle_line(p, json::parse(line));
      } catch (const std::exception& e) {
        send_line(p, {{"type", "error"}, {"message", std::string("bad json: ") + e.what()}});
      }
    }
  }
  drop_peer(p);
  p->conn.reset();
}

// ---- the TURN/STUN server (libjuice) ---------------------------------------
//
// The recorder: libjuice tells it when a relay session starts and ends.
tea_tunnel::TurnUsageRecorder g_usage;

void on_turn_usage(const juice_turn_usage_t* usage, void* user_ptr) {
  tea_tunnel::TurnUsageRecorder* recorder =
      static_cast<tea_tunnel::TurnUsageRecorder*>(user_ptr);
  if (!recorder || !usage) return;
  tea_tunnel::UsageEvent event;
  event.released = usage->event == JUICE_TURN_USAGE_RELEASED;
  event.username = usage->username ? usage->username : "";
  event.client = usage->client ? usage->client : "";
  event.relayed = usage->relayed ? usage->relayed : "";
  event.bytes_in = usage->bytes_in;
  event.bytes_out = usage->bytes_out;
  event.packets_in = usage->packets_in;
  event.packets_out = usage->packets_out;
  event.at = static_cast<long long>(std::time(nullptr));
  recorder->on_event(event);
}


// The accounts it accepts come from two places: the built-in pair (so that
// nothing has to be configured anywhere) and the account book (a sqlite file
// a person edits by hand). libjuice takes a list, so both are simply put in
// one list.
//
// The strings have to stay alive as long as the server runs, so they live in
// a vector that outlives it.
// ---- the options of the broker ---------------------------------------------
//
// Everything comes from the command line. The options are parsed into this
// struct, checked (a configuration that cannot work is refused at startup,
// not at the first allocation), and then turned into a Capacity.
struct BrokerOptions {
  uint16_t stun_port = kDefaultStunPort;
  uint16_t signal_port = kDefaultSignalPort;
  std::string signal_bind = "0.0.0.0";
  uint16_t relay_begin = kDefaultRelayBegin;
  uint16_t relay_end = kDefaultRelayEnd;
  int max_peer_pair = kDefaultMaxPeerPair;
  std::string realm = "a2";
  std::string token;
  std::string turn_user = kDefaultTurnUser;
  std::string turn_pass = kDefaultTurnPass;
  std::string turn_db;
  bool turn_user_given = false;
  bool turn_pass_given = false;
  int usage_days = 15;
  bool usage_only = false;
  bool use_tls = true;
  std::vector<std::string> cert_names;
};

/// What the broker can serve. One peer pair is two sidecars, and every
/// sidecar takes one relay allocation and one port of the pool - so the
/// number of pairs is half the number of allocations, and half the number of
/// ports the pool needs.
struct Capacity {
  int peer_pairs = 0;         // 0 = no limit from the options
  int allocations = 0;        // -> the server's max_allocations (all accounts share it)
  int quota_per_account = 0;  // -> the quota of every TURN account
  uint16_t pool_begin = 0;
  uint16_t pool_end = 0;
  int pool_size = 0;
};

/// The number of signalling connections that are open right now.
size_t signalling_connections() {
  std::lock_guard<std::mutex> lock(g_rooms_mu);
  size_t total = 0;
  for (const auto& room : g_rooms) total += room.second.size();
  return total;
}

// ---- what the relay could not give ----------------------------------------
//
// libjuice tells us when a client could not get a relay. The three reasons
// need three different answers from the operator, so each one gets its own
// line. The callback comes from the server's own thread: it only counts and
// prints, and one line per reason every five minutes is enough.
struct RelayErrors {
  std::mutex mu;
  std::map<int, long long> counts;
  std::map<int, long long> last_printed;
  Capacity capacity;
  tea_tunnel::TurnUsageRecorder* recorder = nullptr;
};

RelayErrors g_relay_errors;

void on_turn_error(const juice_turn_error_t* error, void* user_ptr) {
  RelayErrors* errors = static_cast<RelayErrors*>(user_ptr);
  if (!errors || !error) return;
  const int reason = static_cast<int>(error->reason);
  const long long now = static_cast<long long>(std::time(nullptr));
  std::lock_guard<std::mutex> lock(errors->mu);
  const long long count = ++errors->counts[reason];
  const auto last = errors->last_printed.find(reason);
  if (last != errors->last_printed.end() && now - last->second < 300) return;
  errors->last_printed[reason] = now;
  const size_t in_use = errors->recorder ? errors->recorder->open_sessions() : 0;
  switch (error->reason) {
  case JUICE_TURN_ERROR_SERVER_QUOTA:
    std::fprintf(stderr,
                 "broker: the relay is full: %zu allocations are in use (the limit is %d, %lld refused)\n",
                 in_use, errors->capacity.allocations, count);
    break;
  case JUICE_TURN_ERROR_ACCOUNT_QUOTA:
    std::fprintf(stderr,
                 "broker: the relay is full for the account '%s': its quota of %d is used up (%lld refused)\n",
                 error->username ? error->username : "", errors->capacity.quota_per_account, count);
    break;
  case JUICE_TURN_ERROR_RELAY_PORT:
    std::fprintf(stderr,
                 "broker: no free relay port in %u-%u (the pool is exhausted, %lld refused)\n",
                 static_cast<unsigned>(errors->capacity.pool_begin),
                 static_cast<unsigned>(errors->capacity.pool_end), count);
    break;
  }
  std::fflush(stderr);
}

// ---- the capacity snapshot -------------------------------------------------
//
// One line every minute: how many allocations are in use, how many the pool
// can hold, how many signalling connections are open, and what the process
// uses. It is the early warning: the operator sees the pool filling up
// before the sessions start failing.
void print_capacity_snapshot(const Capacity& capacity,
                              tea_tunnel::TurnUsageRecorder* recorder) {
  const size_t in_use = recorder ? recorder->open_sessions() : 0;
  const size_t connections = signalling_connections();
  const int fds = tea_tunnel::process_fd_count();
  const long long rss = tea_tunnel::process_rss_bytes();
  const int percent =
      capacity.allocations > 0 ? static_cast<int>(in_use * 100 / capacity.allocations) : 0;
  std::printf("broker: capacity: allocations %zu/%d (%d%%), relay ports %zu/%d, signalling %zu, "
              "fds %s, rss %s\n",
              in_use, capacity.allocations, percent, in_use, capacity.pool_size, connections,
              fds >= 0 ? std::to_string(fds).c_str() : "n/a",
              rss >= 0 ? tea_tunnel::usage_bytes(static_cast<uint64_t>(rss)).c_str() : "n/a");
  std::fflush(stdout);
}

bool start_stun_turn(uint16_t port, const std::string& realm,
                      const std::vector<tea_tunnel::TurnAccount>& accounts,
                      uint16_t relay_begin, uint16_t relay_end,
                      const Capacity& capacity,
                      tea_tunnel::TurnUsageRecorder* recorder) {
  static std::vector<std::string> usernames;
  static std::vector<std::string> passwords;
  usernames.clear();
  passwords.clear();
  for (const tea_tunnel::TurnAccount& account : accounts) {
    usernames.push_back(account.user);
    passwords.push_back(account.pass);
  }
  std::vector<juice_server_credentials_t> creds;
  for (std::size_t i = 0; i < usernames.size(); ++i) {
    juice_server_credentials_t entry;
    entry.username = usernames[i].c_str();
    entry.password = passwords[i].c_str();
    entry.allocations_quota = capacity.quota_per_account;
    creds.push_back(entry);
  }

  juice_server_config_t config;
  config.credentials = creds.empty() ? nullptr : &creds[0];
  config.credentials_count = static_cast<int>(creds.size());
  config.max_allocations = capacity.allocations;
  config.max_peers = 16;
  config.bind_address = nullptr;
  config.external_address = nullptr;
  config.port = port;
  config.relay_port_range_begin = relay_begin;
  config.relay_port_range_end = relay_end;
  config.realm = realm.c_str();
  // tea_a2: the usage of the relay is written into the account book
  config.cb_turn_usage = on_turn_usage;
  config.turn_usage_user_ptr = recorder;
  // tea_a2: the owner wants to know why a relay was refused
  config.cb_turn_error = on_turn_error;
  config.turn_error_user_ptr = &g_relay_errors;

  juice_server_t* server = juice_server_create(&config);
  if (!server) {
    std::fprintf(stderr, "broker: cannot start the STUN/TURN server on port %u\n", port);
    return false;
  }
  std::printf("broker: STUN+TURN on udp/%u (realm %s, relay %u-%u, %d ports)\n", port,
              realm.c_str(), relay_begin, relay_end, capacity.pool_size);
  std::fflush(stdout);
  return true;
}

// ---- the TLS material ------------------------------------------------------
// The broker makes its own certificate and keeps it, so the fingerprint the
// clients pin survives a restart. It lives next to the room token.
struct TlsMaterial {
  std::string cert_pem;
  std::string key_pem;
  std::string fingerprint;   // sha256 of the certificate, lower case hex
  std::string cert_path;
};

std::string certificate_path() { return tea_tunnel::tunnel_dir() + "/broker.crt"; }
std::string key_path() { return tea_tunnel::tunnel_dir() + "/broker.key"; }

/// Load the certificate from disk, or make a new one and save it.
///
/// The names in the certificate are the ones the operator gave us (--cert-name).
/// They are a convenience: a client that checks a *name* has something to
/// check. A client that pins the fingerprint does not care about them at all,
/// which is the point - the address of the broker may change.
bool load_or_make_certificate(const std::vector<std::string>& names, TlsMaterial* out) {
  out->cert_path = certificate_path();
  std::string cert_pem;
  std::string key_pem;
  if (tea_tunnel::read_whole_file(out->cert_path, &cert_pem) && !cert_pem.empty()) {
    std::string on_disk;
    if (tea_tunnel::read_whole_file(key_path(), &on_disk) && !on_disk.empty()) {
      out->cert_pem = cert_pem;
      out->key_pem = on_disk;
      return true;
    }
  }

  const std::string common_name = names.empty() ? std::string("a2-broker") : names.front();
  std::string err;
  if (!tea_tunnel::make_self_signed(common_name, names, &out->cert_pem, &out->key_pem, &err)) {
    std::fprintf(stderr, "broker: cannot make a certificate: %s\n", err.c_str());
    return false;
  }
  tea_tunnel::ensure_directory(tea_tunnel::tunnel_dir());
  if (!tea_tunnel::write_private_file(out->cert_path, out->cert_pem) ||
      !tea_tunnel::write_private_file(key_path(), out->key_pem)) {
    std::fprintf(stderr, "broker: cannot save the certificate to %s\n", out->cert_path.c_str());
    return false;
  }
  return true;
}

// ---- the signalling server -------------------------------------------------
int listen_tcp(uint16_t port, const std::string& bind_addr) {
  tea_net_init();
  tea_socket_t fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd == kTeaInvalidSocket) return -1;
  int one = 1;
  ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&one), sizeof(one));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  if (bind_addr.empty() || bind_addr == "0.0.0.0") {
    addr.sin_addr.s_addr = INADDR_ANY;
  } else if (::inet_pton(AF_INET, bind_addr.c_str(), &addr.sin_addr) != 1) {
    std::fprintf(stderr, "broker: bad bind address %s\n", bind_addr.c_str());
    tea_net_close(fd);
    return -1;
  }
  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    tea_net_close(fd);
    return -1;
  }
  if (::listen(fd, 16) != 0) {
    tea_net_close(fd);
    return -1;
  }
  return fd;
}

/// The TLS side of the signalling server: one configuration, many sessions.
///
/// The sessions are made on the accepted socket, one at a time, in the thread
/// that serves the peer - the handshake is quick and doing it there keeps the
/// accept loop free.
class TlsServer {
 public:
  bool start(const TlsMaterial& material, std::string* err) {
    mbedtls_ssl_config_init(&conf_);
    mbedtls_x509_crt_init(&cert_);
    mbedtls_pk_init(&key_);
    mbedtls_entropy_init(&entropy_);
    mbedtls_ctr_drbg_init(&drbg_);

    const int seeded = mbedtls_ctr_drbg_seed(&drbg_, mbedtls_entropy_func, &entropy_,
                                            reinterpret_cast<const unsigned char*>("a2-broker"),
                                            std::strlen("a2-broker"));
    if (seeded != 0) {
      *err = "cannot seed the random generator";
      return false;
    }
    int rc = mbedtls_ssl_config_defaults(&conf_, MBEDTLS_SSL_IS_SERVER,
                                         MBEDTLS_SSL_TRANSPORT_STREAM,
                                         MBEDTLS_SSL_PRESET_DEFAULT);
    if (rc != 0) {
      *err = "cannot set up the TLS configuration: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    mbedtls_ssl_conf_rng(&conf_, mbedtls_ctr_drbg_random, &drbg_);
    // The clients do not present a certificate: the room token is what says
    // who may join, and it never leaves the TLS session.
    mbedtls_ssl_conf_authmode(&conf_, MBEDTLS_SSL_VERIFY_NONE);

    rc = mbedtls_x509_crt_parse(&cert_, reinterpret_cast<const unsigned char*>(material.cert_pem.data()),
                                material.cert_pem.size() + 1);
    if (rc != 0) {
      *err = "cannot read the certificate: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    rc = mbedtls_pk_parse_key(&key_, reinterpret_cast<const unsigned char*>(material.key_pem.data()),
                              material.key_pem.size() + 1, nullptr, 0, mbedtls_ctr_drbg_random,
                              &drbg_);
    if (rc != 0) {
      *err = "cannot read the private key: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    rc = mbedtls_ssl_conf_own_cert(&conf_, &cert_, &key_);
    if (rc != 0) {
      *err = "cannot use the certificate: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    return true;
  }

  /// Wrap an accepted socket in TLS. Returns null when the handshake failed.
  ///
  /// The socket lives in the Connection object, and the BIO helpers are given
  /// its address: mbedTLS keeps that pointer for as long as the session
  /// lives, so it must not be a local variable.
  bool wrap(const std::shared_ptr<Connection>& conn, std::string* err) {
    mbedtls_ssl_init(&conn->ssl);
    int rc = mbedtls_ssl_setup(&conn->ssl, &conf_);
    if (rc != 0) {
      *err = "cannot set up the TLS session: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    mbedtls_ssl_set_bio(&conn->ssl, &conn->fd, &TlsServer::bio_send, &TlsServer::bio_recv,
                        nullptr);
    rc = mbedtls_ssl_handshake(&conn->ssl);
    if (rc != 0) {
      *err = "the TLS handshake failed: " + tea_tunnel::tls_error_text(rc);
      return false;
    }
    conn->tls = true;
    return true;
  }

 private:
  // mbedTLS' own socket helpers work on POSIX file descriptors; on Windows a
  // socket is not one, so the two calls are done here instead.
  static int bio_send(void* ctx, const unsigned char* buf, size_t len) {
    tea_socket_t fd = *static_cast<tea_socket_t*>(ctx);
    const int n = tea_net_send(fd, buf, len);
    if (n >= 0) return n;
    if (tea_net_would_block(tea_net_errno())) return MBEDTLS_ERR_SSL_WANT_WRITE;
    return MBEDTLS_ERR_NET_SEND_FAILED;
  }

  static int bio_recv(void* ctx, unsigned char* buf, size_t len) {
    tea_socket_t fd = *static_cast<tea_socket_t*>(ctx);
    const int n = tea_net_recv(fd, buf, len);
    if (n >= 0) return n;
    if (tea_net_would_block(tea_net_errno())) return MBEDTLS_ERR_SSL_WANT_READ;
    return MBEDTLS_ERR_NET_RECV_FAILED;
  }

  mbedtls_ssl_config conf_{};
  mbedtls_x509_crt cert_{};
  mbedtls_pk_context key_{};
  mbedtls_entropy_context entropy_{};
  mbedtls_ctr_drbg_context drbg_{};
};

}  // namespace

namespace {

// ---- the numbers of the command line ---------------------------------------

/// A number from the command line. No exceptions: a wrong value is an error
/// the operator sees, not a crash of the broker.
bool parse_number(const std::string& text, long* out) {
  if (text.empty()) return false;
  char* end = nullptr;
  errno = 0;
  const long value = std::strtol(text.c_str(), &end, 10);
  if (errno != 0 || end == text.c_str() || *end != '\0') return false;
  *out = value;
  return true;
}

/// The help text. It goes to stderr and the broker stops (exit code 2).
[[noreturn]] void print_usage() {
  std::fprintf(stderr,
        "a2_broker [--version] [--stun-port %u] [--signal-port %u]\n"
        "          [--signal-bind 0.0.0.0]\n"
        "          [--realm a2] [--relay-begin %u] [--relay-end %u]\n"
        "          [--max-peer-pair %d]\n"
        "          [--token <secret>] [--turn-user <user>] [--turn-pass <pass>]\n"
        "          [--turn-db <file>] [--usage] [--usage-days <n>]\n"
        "          [--cert-name <name>]... [--no-tls]\n"
        "\n"
        "  One process, one file: STUN + TURN + the signalling (with TLS).\n"
        "  The signalling port speaks TLS by default; the certificate is made\n"
        "  on the first start and kept in %s.\n"
        "\n"
        "  --max-peer-pair <n>  how many peer pairs the broker serves at the\n"
        "                   same time (one pair = two sidecars = two relay\n"
        "                   allocations). 0 means no limit from this flag.\n"
        "                   All TURN accounts share this limit.\n"
        "  --relay-begin/--relay-end  the pool of relay ports. It has to hold\n"
        "                   two ports per peer pair.\n"
        "\n"
        "  The TURN accounts: the rows of the account book (a sqlite file,\n"
        "  %s by default; the table is %s with the columns user and pass).\n"
        "  The book is created on the first start, with the default account\n"
        "  in it. --turn-user/--turn-pass replace the book for this run.\n"
        "\n"
        "  (the room token comes from --token or from %s)\n",
        static_cast<unsigned>(kDefaultStunPort), static_cast<unsigned>(kDefaultSignalPort),
        static_cast<unsigned>(kDefaultRelayBegin), static_cast<unsigned>(kDefaultRelayEnd),
        kDefaultMaxPeerPair, certificate_path().c_str(), tea_tunnel::turn_db_path().c_str(),
        tea_tunnel::turn_table_name(), token_file_path().c_str());
  std::exit(2);
}

/// A wrong command line: the reason first, then the help.
[[noreturn]] void fail_usage(const std::string& why) {
  std::fprintf(stderr, "broker: %s\n", why.c_str());
  print_usage();
}

/// Read the command line. Both "--flag value" and "--flag=value" work.
bool parse_options(int argc, char** argv, BrokerOptions* opts) {
  for (int i = 1; i < argc; ++i) {
    const std::string arg = argv[i];
    std::string name = arg;
    std::string value;
    bool has_value = false;
    const size_t equal = arg.find('=');
    if (equal != std::string::npos) {
      name = arg.substr(0, equal);
      value = arg.substr(equal + 1);
      has_value = true;
    }
    auto take_value = [&]() -> std::string {
      if (has_value) return value;
      if (i + 1 >= argc) fail_usage(name + " needs a value");
      return argv[++i];
    };
    auto take_number = [&](long low, long high) -> long {
      const std::string text = take_value();
      long parsed = 0;
      if (!parse_number(text, &parsed)) {
        fail_usage(name + " needs a number, got '" + text + "'");
      }
      if (parsed < low || parsed > high) {
        fail_usage(name + " must be between " + std::to_string(low) + " and " +
                   std::to_string(high));
      }
      return parsed;
    };
    auto take_flag = [&]() {
      if (has_value) fail_usage(name + " does not take a value");
    };

    if (name == "--stun-port") {
      opts->stun_port = static_cast<uint16_t>(take_number(1, 65535));
    } else if (name == "--signal-port") {
      opts->signal_port = static_cast<uint16_t>(take_number(1, 65535));
    } else if (name == "--signal-bind") {
      opts->signal_bind = take_value();
    } else if (name == "--relay-begin") {
      opts->relay_begin = static_cast<uint16_t>(take_number(0, 65535));
    } else if (name == "--relay-end") {
      opts->relay_end = static_cast<uint16_t>(take_number(0, 65535));
    } else if (name == "--max-peer-pair" || name == "--max_peer_pair") {
      opts->max_peer_pair = static_cast<int>(take_number(0, 1000000));
    } else if (name == "--realm") {
      opts->realm = take_value();
    } else if (name == "--turn-user") {
      opts->turn_user = take_value();
      opts->turn_user_given = true;
    } else if (name == "--turn-pass") {
      opts->turn_pass = take_value();
      opts->turn_pass_given = true;
    } else if (name == "--turn-db") {
      opts->turn_db = take_value();
    } else if (name == "--usage") {
      take_flag();
      opts->usage_only = true;
    } else if (name == "--usage-days") {
      opts->usage_days = static_cast<int>(take_number(0, 36500));
    } else if (name == "--token") {
      opts->token = take_value();
    } else if (name == "--cert-name") {
      opts->cert_names.push_back(take_value());
    } else if (name == "--no-tls") {
      take_flag();
      opts->use_tls = false;
    } else if (name == "--tls") {
      take_flag();
      opts->use_tls = true;
    } else if (name == "--version") {
      std::printf("a2_broker %s\n", TEA_TUNNEL_VERSION);
      std::fflush(stdout);
      std::exit(0);
    } else if (name == "--help" || name == "-h") {
      print_usage();
    } else {
      fail_usage("unknown option '" + name + "'");
    }
  }
  return true;
}

/// A configuration that cannot work is refused here, not at the first
/// allocation: the operator sees the reason at startup.
bool validate_options(const BrokerOptions& opts, std::string* err) {
  if (opts.relay_begin > opts.relay_end) {
    *err = "the relay port pool is empty: --relay-begin " + std::to_string(opts.relay_begin) +
           " is above --relay-end " + std::to_string(opts.relay_end);
    return false;
  }
  if (opts.relay_begin < kEphemeralBegin || opts.relay_end > kEphemeralEnd) {
    *err = "the relay port pool must be inside " + std::to_string(kEphemeralBegin) + "-" +
           std::to_string(kEphemeralEnd);
    return false;
  }
  if (opts.max_peer_pair < 0) {
    *err = "--max-peer-pair must not be negative";
    return false;
  }
  const long pool_size = static_cast<long>(opts.relay_end) - opts.relay_begin + 1;
  if (opts.max_peer_pair > 0) {
    const long need = 2L * opts.max_peer_pair;
    if (pool_size < need) {
      *err = "the relay port pool is too small: " + std::to_string(pool_size) +
             " ports for " + std::to_string(need) + " allocations (--max-peer-pair " +
             std::to_string(opts.max_peer_pair) + " needs " + std::to_string(need) +
             "); widen --relay-begin/--relay-end";
      return false;
    }
  }
  return true;
}

/// The capacity that goes with the options. Every peer pair takes two
/// allocations, and every allocation takes one port of the pool.
Capacity derive_capacity(const BrokerOptions& opts) {
  Capacity capacity;
  capacity.peer_pairs = opts.max_peer_pair;
  capacity.allocations =
      opts.max_peer_pair > 0 ? 2 * opts.max_peer_pair : kUnlimitedAllocations;
  capacity.quota_per_account = opts.max_peer_pair > 0 ? 2 * opts.max_peer_pair : 0;
  capacity.pool_begin = opts.relay_begin;
  capacity.pool_end = opts.relay_end;
  capacity.pool_size = opts.relay_end - opts.relay_begin + 1;
  return capacity;
}

}  // namespace

int main(int argc, char** argv) {
  BrokerOptions opts;
  parse_options(argc, argv, &opts);

  std::string validation_error;
  if (!validate_options(opts, &validation_error)) {
    std::fprintf(stderr, "broker: %s\n", validation_error.c_str());
    return 2;
  }
  const Capacity capacity = derive_capacity(opts);
  // A port of the pool that is already taken is not fatal: the pool skips
  // the ports it cannot bind. It is worth a word, though.
  if (opts.stun_port >= opts.relay_begin && opts.stun_port <= opts.relay_end) {
    std::fprintf(stderr,
                 "broker: the STUN port %u is inside the relay port pool %u-%u (that port "
                 "cannot be used for a relay)\n",
                 static_cast<unsigned>(opts.stun_port), static_cast<unsigned>(opts.relay_begin),
                 static_cast<unsigned>(opts.relay_end));
  }
  if (opts.signal_port >= opts.relay_begin && opts.signal_port <= opts.relay_end) {
    std::fprintf(stderr,
                 "broker: the signalling port %u is inside the relay port pool %u-%u (a "
                 "different protocol, but better move it)\n",
                 static_cast<unsigned>(opts.signal_port), static_cast<unsigned>(opts.relay_begin),
                 static_cast<unsigned>(opts.relay_end));
  }
  if (opts.turn_db.empty()) opts.turn_db = tea_tunnel::turn_db_path();

  // ---- the report ---------------------------------------------------------
  // --usage only reads the account book and prints what the relay was used
  // for. It does not start anything.
  if (opts.usage_only) {
    std::vector<tea_tunnel::TurnUsageRecorder::DayTotal> totals;
    long long sessions = 0;
    long long seconds = 0;
    uint64_t bytes_in = 0;
    uint64_t bytes_out = 0;
    size_t open_now = 0;
    std::map<std::string, std::string> meta;
    std::string error;
    if (!tea_tunnel::TurnUsageRecorder::read_report(opts.turn_db, opts.usage_days, &totals,
                                                    &sessions, &seconds, &bytes_in, &bytes_out,
                                                    &open_now, &meta, &error)) {
      std::fprintf(stderr, "broker: %s\n", error.c_str());
      return 2;
    }
    std::printf("TURN usage in %s (the last %d day%s)\n\n", opts.turn_db.c_str(), opts.usage_days,
                opts.usage_days == 1 ? "" : "s");
    if (totals.empty()) {
      std::printf("  nothing yet\n");
    } else {
      std::printf("%-12s %-16s %8s %10s %12s %12s\n", "day", "account", "sessions", "duration",
                  "bytes in", "bytes out");
      for (const tea_tunnel::TurnUsageRecorder::DayTotal& total : totals) {
        std::printf("%-12s %-16s %8lld %10s %12s %12s\n", total.day.c_str(),
                    total.account.c_str(), total.sessions,
                    tea_tunnel::usage_duration(total.seconds).c_str(),
                    tea_tunnel::usage_bytes(total.bytes_in).c_str(),
                    tea_tunnel::usage_bytes(total.bytes_out).c_str());
      }
      std::printf("\n%d day%s in total: %lld session%s, %s, %s in, %s out\n", opts.usage_days,
                  opts.usage_days == 1 ? "" : "s", sessions, sessions == 1 ? "" : "s",
                  tea_tunnel::usage_duration(seconds).c_str(),
                  tea_tunnel::usage_bytes(bytes_in).c_str(),
                  tea_tunnel::usage_bytes(bytes_out).c_str());
    }
    // The capacity the broker was started with. A book from before the
    // broker wrote it simply has no rows: that is not an error.
    const auto meta_value = [&meta](const char* key) -> std::string {
      const auto it = meta.find(key);
      return it == meta.end() ? std::string() : it->second;
    };
    const std::string pairs = meta_value("peer_pairs");
    if (pairs.empty()) {
      std::printf("\ncapacity: unknown (this book was written by an older broker)\n");
    } else {
      const std::string allocations = meta_value("max_allocations");
      const std::string pool_size = meta_value("pool_size");
      const long long limit = std::atoll(allocations.c_str());
      const long long used = static_cast<long long>(open_now);
      const int percent = limit > 0 ? static_cast<int>(used * 100 / limit) : 0;
      std::printf("\ncapacity: %s peer pairs (%s allocations max, shared by all accounts)\n",
                  pairs.c_str(), allocations.c_str());
      std::printf("          relay pool %s-%s (%s ports)\n", meta_value("relay_begin").c_str(),
                  meta_value("relay_end").c_str(), pool_size.c_str());
      std::printf(
          "now:      %lld allocations in use (%d%% of %lld), %lld of %lld relay ports in use\n",
          used, percent, limit, used, std::atoll(pool_size.c_str()));
    }
    if (open_now > 0) {
      std::printf("%zu session%s running right now\n", open_now, open_now == 1 ? "" : "s");
    }
    return 0;
  }

  // ---- the room token ----------------------------------------------------
  // The token is optional on the broker: without one every room learns its
  // token from the side that registers it (the agent side generates a fresh
  // one on every start). With --token the broker pins one token for every
  // room.
  std::string token = opts.token;
  if (token.empty()) {
    std::string from_file;
    if (read_token_file(&from_file)) token = from_file;
  }
  g_token = token;

  // ---- the TURN accounts --------------------------------------------------
  // They live in the account book: a sqlite file (a2_broker.db in the
  // current directory) whose rows are the accounts. On the first start the
  // file is created with the default account in it, so the broker has
  // something to accept right away - and from then on a person can add,
  // change or remove a row and restart.
  //
  // --turn-user/--turn-pass replace the book for this run (they are what a
  // brand new book starts with, and what a book that already exists is
  // ignored for).
  std::vector<tea_tunnel::TurnAccount> accounts;
  if (opts.turn_user_given || opts.turn_pass_given) {
    if (opts.turn_pass.empty()) {
      std::fprintf(stderr, "broker: no TURN password\n");
      return 2;
    }
    tea_tunnel::TurnAccount given;
    given.user = opts.turn_user;
    given.pass = opts.turn_pass;
    accounts.push_back(given);
    std::printf("broker: the TURN accounts come from the command line (%zu)\n", accounts.size());
  } else {
    // What a brand new book starts with.
    std::vector<tea_tunnel::TurnAccount> seed;
    tea_tunnel::TurnAccount builtin;
    builtin.user = opts.turn_user;
    builtin.pass = opts.turn_pass;
    seed.push_back(builtin);

    std::vector<tea_tunnel::TurnAccount> from_db;
    std::string db_error;
    bool created = false;
    if (!tea_tunnel::load_turn_accounts(opts.turn_db, seed, &from_db, &db_error, &created)) {
      std::fprintf(stderr, "broker: %s\n", db_error.c_str());
      std::fprintf(stderr, "broker: the TURN accounts in it are ignored\n");
    } else {
      accounts = from_db;
      std::printf("broker: the TURN account book is %s (%zu account%s%s)\n", opts.turn_db.c_str(),
                  from_db.size(), from_db.size() == 1 ? "" : "s",
                  created ? ", created now" : "");
      if (created) {
        std::printf("broker:   the default account is in it; change it with:\n"
                    "           sqlite3 %s \"UPDATE %s SET user='name', pass='secret';\"\n",
                    opts.turn_db.c_str(), tea_tunnel::turn_table_name());
      } else if (from_db.empty()) {
        std::printf("broker:   add one with: INSERT INTO %s (user, pass) VALUES ('name', 'secret');\n",
                    tea_tunnel::turn_table_name());
      }
    }
  }

  if (accounts.empty()) {
    std::fprintf(stderr, "broker: no TURN account at all - nobody could use the relay\n");
    std::fprintf(stderr, "broker:   add one with: INSERT INTO %s (user, pass) VALUES ('name', 'secret');\n",
                 tea_tunnel::turn_table_name());
    std::fprintf(stderr, "broker:   or give one on the command line: --turn-user <name> --turn-pass <secret>\n");
    return 2;
  }
  std::printf("broker: TURN accounts accepted: %zu\n", accounts.size());
  std::fflush(stdout);

  // ---- the usage recorder -------------------------------------------------
  // It writes into the same file as the accounts: a2_broker.db.
  std::string usage_error;
  if (!g_usage.start(opts.turn_db, opts.usage_days, &usage_error)) {
    std::fprintf(stderr, "broker: %s\n", usage_error.c_str());
    std::fprintf(stderr, "broker: the TURN usage is not recorded\n");
  } else {
    std::printf("broker: the TURN usage goes into %s (the details for %d day%s, "
                "the totals for good)\n",
                opts.turn_db.c_str(), opts.usage_days, opts.usage_days == 1 ? "" : "s");
    std::fflush(stdout);
  }

  // What this run is made of. It is written into the same book, so that
  // "a2_broker --usage" can tell how much the relay can take even when it
  // runs on another machine.
  g_usage.write_meta("peer_pairs", std::to_string(capacity.peer_pairs));
  g_usage.write_meta("max_allocations", std::to_string(capacity.allocations));
  g_usage.write_meta("quota_per_account", std::to_string(capacity.quota_per_account));
  g_usage.write_meta("relay_begin", std::to_string(capacity.pool_begin));
  g_usage.write_meta("relay_end", std::to_string(capacity.pool_end));
  g_usage.write_meta("pool_size", std::to_string(capacity.pool_size));
  g_usage.write_meta("broker_version", std::string("a2_broker ") + TEA_TUNNEL_VERSION);
  g_usage.write_meta("started_at", std::to_string(static_cast<long long>(std::time(nullptr))));

  g_relay_errors.capacity = capacity;
  g_relay_errors.recorder = &g_usage;

  if (!start_stun_turn(opts.stun_port, opts.realm, accounts, capacity.pool_begin,
                       capacity.pool_end, capacity, &g_usage)) {
    return 1;
  }

  TlsMaterial material;
  std::shared_ptr<TlsServer> tls_server;
  if (opts.use_tls) {
    std::string err;
    if (!load_or_make_certificate(opts.cert_names, &material)) return 1;
    if (!tea_tunnel::certificate_fingerprint(material.cert_pem, &material.fingerprint, &err)) {
      std::fprintf(stderr, "broker: %s\n", err.c_str());
      return 1;
    }
    tls_server = std::make_shared<TlsServer>();
    if (!tls_server->start(material, &err)) {
      std::fprintf(stderr, "broker: %s\n", err.c_str());
      return 1;
    }
  }

  int lfd = listen_tcp(opts.signal_port, opts.signal_bind);
  if (lfd < 0) {
    std::fprintf(stderr, "broker: cannot listen on tcp/%u\n", opts.signal_port);
    return 1;
  }

  // ---- what this broker can do --------------------------------------------
  std::printf("broker: capacity: %d peer pairs (%d allocations max, shared by all TURN accounts)\n",
              capacity.peer_pairs, capacity.allocations);
  if (opts.use_tls) {
    std::printf("broker: signalling on tcp/%u (TLS, bound to %s)\n", opts.signal_port,
                opts.signal_bind.c_str());
    std::printf("broker: certificate fingerprint (sha256): %s\n", material.fingerprint.c_str());
    std::printf("broker: the client can pin it with --tls-pin %s\n", material.fingerprint.c_str());
    std::printf("broker: the certificate itself is in %s\n", material.cert_path.c_str());
  } else {
    std::printf("broker: signalling on tcp/%u (plain, bound to %s)\n", opts.signal_port,
                opts.signal_bind.c_str());
  }
  std::fflush(stdout);

  // ---- the capacity snapshot ----------------------------------------------
  // One line every minute. It is the early warning: the operator sees the
  // pool filling up before the sessions start failing.
  std::thread([capacity]() {
    while (true) {
      std::this_thread::sleep_for(std::chrono::seconds(60));
      print_capacity_snapshot(capacity, &g_usage);
    }
  }).detach();

  while (true) {
    tea_socket_t fd = ::accept(lfd, nullptr, nullptr);
    if (fd == kTeaInvalidSocket) continue;
    tea_net_set_nodelay(fd);
    auto p = std::make_shared<Peer>();
    p->conn = std::make_shared<Connection>();
    p->conn->fd = fd;
    if (tls_server) {
      std::string err;
      if (!tls_server->wrap(p->conn, &err)) {
        std::fprintf(stderr, "broker: %s\n", err.c_str());
        continue;
      }
    }
    std::thread(peer_loop, p).detach();
  }
}
