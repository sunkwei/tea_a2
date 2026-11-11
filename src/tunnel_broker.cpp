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
#include <nlohmann/json.hpp>

#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/error.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/pk.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>

#include <atomic>
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
#include "tunnel_paths.h"
#include "tunnel_tls.h"
#include "tunnel_turn_db.h"

// ---- the built-in TURN credentials -----------------------------------------
// The TURN relay is the broker's bandwidth, not a secret worth hiding: the
// credentials only decide who may push traffic through it. They are built in
// so that no configuration file has to be copied to every machine (the sidecar
// next to the a2, the operator's machine, the broker) - the CLI flags still
// override them.
constexpr const char* kDefaultTurnUser = "1hblsqt.";
constexpr const char* kDefaultTurnPass = "2ghlmcl,";

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
// The accounts it accepts come from two places: the built-in pair (so that
// nothing has to be configured anywhere) and the account book (a sqlite file
// a person edits by hand). libjuice takes a list, so both are simply put in
// one list.
//
// The strings have to stay alive as long as the server runs, so they live in
// a vector that outlives it.
bool start_stun_turn(uint16_t port, const std::string& realm,
                      const std::vector<tea_tunnel::TurnAccount>& accounts,
                      uint16_t relay_begin, uint16_t relay_end) {
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
    entry.allocations_quota = 64;
    creds.push_back(entry);
  }

  juice_server_config_t config;
  config.credentials = creds.empty() ? nullptr : &creds[0];
  config.credentials_count = static_cast<int>(creds.size());
  config.max_allocations = 64;
  config.max_peers = 16;
  config.bind_address = nullptr;
  config.external_address = nullptr;
  config.port = port;
  config.relay_port_range_begin = relay_begin;
  config.relay_port_range_end = relay_end;
  config.realm = realm.c_str();

  juice_server_t* server = juice_server_create(&config);
  if (!server) {
    std::fprintf(stderr, "broker: cannot start the STUN/TURN server on port %u\n", port);
    return false;
  }
  std::printf("broker: STUN+TURN on udp/%u (realm %s, relay %u-%u)\n", port, realm.c_str(),
              relay_begin, relay_end);
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

int main(int argc, char** argv) {
  uint16_t stun_port = 3478;
  uint16_t signal_port = 8382;
  std::string signal_bind = "0.0.0.0";
  uint16_t relay_begin = 49160;
  uint16_t relay_end = 49260;
  std::string realm = "a2";
  std::string turn_user = kDefaultTurnUser;
  std::string turn_pass = kDefaultTurnPass;
  bool turn_user_given = false;
  bool turn_pass_given = false;
  // The account book: a sqlite file with the TURN accounts in it. It is
  // a2_broker.db in the current directory, created on the first start with
  // the default account in it.
  std::string turn_db = tea_tunnel::turn_db_path();
  std::string token;
  // The names in the broker's own certificate. A client that checks a name
  // needs one; a client that pins the fingerprint does not care.
  std::vector<std::string> cert_names;
  bool use_tls = true;

  auto usage = [&](const char* why) {
    if (why) std::fprintf(stderr, "broker: %s\n", why);
    std::fprintf(stderr,
          "a2_broker [--stun-port 3478] [--signal-port 8382] [--signal-bind 0.0.0.0]\n"
          "          [--realm a2] [--relay-begin 49160] [--relay-end 49260]\n"
          "          [--token <secret>] [--turn-user <user>] [--turn-pass <pass>]\n"
          "          [--turn-db <file>]\n"
          "          [--cert-name <name>]... [--no-tls]\n"
          "\n"
          "  One process, one file: STUN + TURN + the signalling (with TLS).\n"
          "  The signalling port speaks TLS by default; the certificate is made\n"
          "  on the first start and kept in %s.\n"
          "\n"
          "  The TURN accounts: the rows of the account book (a sqlite file,\n"
          "  %s by default; the table is %s with the columns user and pass).\n"
          "  The book is created on the first start, with the default account\n"
          "  in it. --turn-user/--turn-pass replace the book for this run.\n"
          "\n"
          "  (the room token comes from --token or from %s)\n",
          tea_tunnel::turn_db_path().c_str(), tea_tunnel::turn_table_name(),
          token_file_path().c_str());
    std::exit(2);
  };

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    // A missing value is an error, not an empty string.
    auto next = [&]() -> std::string {
      if (i + 1 >= argc) usage((a + " needs a value").c_str());
      return argv[++i];
    };
    if (a == "--stun-port") stun_port = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--signal-port") signal_port = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--signal-bind") signal_bind = next();
    else if (a == "--relay-begin") relay_begin = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--relay-end") relay_end = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--realm") realm = next();
    else if (a == "--turn-user") { turn_user = next(); turn_user_given = true; }
    else if (a == "--turn-pass") { turn_pass = next(); turn_pass_given = true; }
    else if (a == "--turn-db") turn_db = next();
    else if (a == "--token") token = next();
    else if (a == "--cert-name") cert_names.push_back(next());
    else if (a == "--no-tls") use_tls = false;
    else if (a == "--tls") use_tls = true;
    else if (a == "--help" || a == "-h") usage(nullptr);
    else usage(("unknown option '" + a + "'").c_str());
  }
  // The room token is optional on the broker: without one every room learns
  // its token from the side that registers it (the agent side generates a
  // fresh one on every start). With --token the broker pins one token for
  // every room.
  if (token.empty()) {
    std::string from_file;
    if (read_token_file(&from_file)) token = from_file;
  }
  g_token = token;

  // ---- the TURN accounts -------------------------------------------------
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
  if (turn_user_given || turn_pass_given) {
    if (turn_pass.empty()) {
      std::fprintf(stderr, "broker: no TURN password\n");
      return 2;
    }
    tea_tunnel::TurnAccount given;
    given.user = turn_user;
    given.pass = turn_pass;
    accounts.push_back(given);
    std::printf("broker: the TURN accounts come from the command line (%zu)\n", accounts.size());
  } else {
    // What a brand new book starts with.
    std::vector<tea_tunnel::TurnAccount> seed;
    tea_tunnel::TurnAccount builtin;
    builtin.user = turn_user;
    builtin.pass = turn_pass;
    seed.push_back(builtin);

    std::vector<tea_tunnel::TurnAccount> from_db;
    std::string db_error;
    bool created = false;
    if (!tea_tunnel::load_turn_accounts(turn_db, seed, &from_db, &db_error, &created)) {
      std::fprintf(stderr, "broker: %s\n", db_error.c_str());
      std::fprintf(stderr, "broker: the TURN accounts in it are ignored\n");
    } else {
      accounts = from_db;
      std::printf("broker: the TURN account book is %s (%zu account%s%s)\n", turn_db.c_str(),
                  from_db.size(), from_db.size() == 1 ? "" : "s",
                  created ? ", created now" : "");
      if (created) {
        std::printf("broker:   the default account is in it; change it with:\n"
                    "           sqlite3 %s \"UPDATE %s SET user='name', pass='secret';\"\n",
                    turn_db.c_str(), tea_tunnel::turn_table_name());
      } else if (from_db.empty()) {
        std::printf("broker:   add one with: INSERT INTO %s (user, pass) VALUES ('name', 'secret');\n",
                    tea_tunnel::turn_table_name());
      }
    }
  }

  if (accounts.empty()) {
    std::fprintf(stderr, "broker: no TURN account at all - nobody could use the relay\n");
    return 2;
  }
  std::printf("broker: TURN accounts accepted: %zu\n", accounts.size());
  std::fflush(stdout);

  if (!start_stun_turn(stun_port, realm, accounts, relay_begin, relay_end)) return 1;

  TlsMaterial material;
  std::shared_ptr<TlsServer> tls_server;
  if (use_tls) {
    if (!load_or_make_certificate(cert_names, &material)) return 1;
    std::string err;
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

  int lfd = listen_tcp(signal_port, signal_bind);
  if (lfd < 0) {
    std::fprintf(stderr, "broker: cannot listen on tcp/%u\n", signal_port);
    return 1;
  }
  if (use_tls) {
    std::printf("broker: signalling on tcp/%u (TLS, bound to %s)\n", signal_port,
                signal_bind.c_str());
    std::printf("broker: certificate fingerprint (sha256): %s\n", material.fingerprint.c_str());
    std::printf("broker: the client can pin it with --tls-pin %s\n",
                material.fingerprint.c_str());
    std::printf("broker: the certificate itself is in %s\n", material.cert_path.c_str());
  } else {
    std::printf("broker: signalling on tcp/%u (plain, bound to %s)\n", signal_port,
                signal_bind.c_str());
  }
  std::fflush(stdout);

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
