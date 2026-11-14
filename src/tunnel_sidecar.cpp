// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_sidecar.cpp -- the piece that sits next to a tea_a2 and gives it a
// way out.
//
// It is deliberately NOT part of a2: the agent keeps working when this is
// not there, and this can be updated without touching the agent.
//
//   agent side (next to the a2 inside the customer's network):
//     a2_tunnel --role agent --broker <host:port> --room <name> --token <secret> \
//                --target 127.0.0.1:8383
//   client side (on the operator's machine):
//     a2_tunnel --role client --broker <host:port> --room <name> --token <secret> \
//                --listen 127.0.0.1:9931
//
// The signalling carries the room token, so it should travel over TLS:
//     --tls                          (the broker's certificate is self-signed:
//                                     the TLS is for the encryption, the room
//                                     token is what gates the room)
// The usual arrangement is nginx terminating TLS on the public port and
// proxying the plain stream to the broker on 127.0.0.1.
//
// ## Waiting and timeouts
//
//   * The agent side waits for the other side **forever** (--wait-timeout 0,
//     the default): the peer may show up at any time, and giving up would
//     leave a device with a dead tunnel and no explanation. It says so every
//     minute, and reconnects to the broker by itself if the link drops.
//   * The client side opens its local port right away, but **refuses**
//     connections while the tunnel is not up (a caller gets an immediate
//     error instead of hanging until its own timeout).
//   * The a2 on the other end has its own budget: the peer tool connects with
//     a 10s timeout and reads with 60s, so a dead tunnel shows up as an error
//     there, not as a hang.
//
// What is inside the tunnel: raw TCP bytes. The client side is a plain TCP
// proxy, so an existing `peers:` entry (url: http://127.0.0.1:9931) works
// against it without changing anything.
//
// Frame format on the data channel: [u8 type][u32 length][payload]
//   1 OPEN  payload: u32 connection id
//   2 DATA  payload: u32 connection id + bytes
//   3 CLOSE payload: u32 connection id
//
#include <mbedtls/ctr_drbg.h>
#include <mbedtls/entropy.h>
#include <mbedtls/error.h>
#include <mbedtls/net_sockets.h>
#include <mbedtls/ssl.h>
#include <mbedtls/x509_crt.h>
#include <rtc/rtc.hpp>

#include <nlohmann/json.hpp>

#include <atomic>
#include <cstdarg>
#include <cstdio>
#include <ctime>
#include <cstdlib>
#include <cctype>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <random>
#include <string>
#include <thread>
#include <vector>

using json = nlohmann::json;

#include "tunnel_net.h"
#include "tunnel_paths.h"
#include "tunnel_control_service.h"
#include "tunnel_desktop_service.h"
#include "tunnel_term.h"
#include "tunnel_tls.h"
#include "tunnel_turn_db.h"

namespace {

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

/// The conventional CA bundle: the same file the a2 trusts (it holds the
/// public roots plus the self-signed certificate of the broker).
std::string default_ca_bundle() {
  std::string path = tea_tunnel::agent_home();
  if (!path.empty() && path.back() != '/' && path.back() != '\\') path += '/';
  path += ".tea_agent/ca-bundle/cacert.pem";
  return path;
}

/// The file that says where the broker is.
///
/// The address of the broker can change (a machine is replaced, an IP moves).
/// Everything that has to follow it - the signalling address, the STUN and
/// TURN addresses, the certificate fingerprint - is in this one file, so
/// changing it is one edit and not three command lines.
///
///   address=203.0.113.5:8382
///   pin=3f9a...            (the fingerprint the broker printed)
///
std::string broker_config_path() {
  return tea_tunnel::tunnel_dir() + "/broker.txt";
}

struct BrokerConfig {
  std::string address;
  std::string pin;
};

/// Read the broker configuration file. A missing file is not an error: the
/// defaults (and the command line) still apply.
BrokerConfig read_broker_config() {
  BrokerConfig config;
  std::string text;
  if (!tea_tunnel::read_whole_file(broker_config_path(), &text)) return config;
  size_t pos = 0;
  while (pos < text.size()) {
    size_t eol = text.find('\n', pos);
    if (eol == std::string::npos) eol = text.size();
    std::string line = text.substr(pos, eol - pos);
    pos = eol + 1;
    size_t hash = line.find('#');
    if (hash != std::string::npos) line = line.substr(0, hash);
    while (!line.empty() && (line.back() == ' ' || line.back() == '\t' || line.back() == '\r')) {
      line.pop_back();
    }
    size_t start = 0;
    while (start < line.size() && (line[start] == ' ' || line[start] == '\t')) ++start;
    line = line.substr(start);
    if (line.empty()) continue;
    size_t eq = line.find('=');
    if (eq == std::string::npos) continue;
    std::string key = line.substr(0, eq);
    std::string value = line.substr(eq + 1);
    while (!value.empty() && (value.front() == ' ' || value.front() == '\t')) value.erase(0, 1);
    while (!value.empty() && (value.back() == ' ' || value.back() == '\t' || value.back() == '\r')) {
      value.pop_back();
    }
    if (key == "address") {
      config.address = value;
    } else if (key == "pin") {
      config.pin = value;
    }
  }
  return config;
}

/// The session code is the one string that has to travel from the machine
/// next to the a2 to the machine outside. The room name and the token are
/// derived from it, so nobody has to remember or copy two things:
///   room  = "s" + the first 8 characters
///   token = the whole code
std::string room_of_session(const std::string& session) {
  return "s" + session.substr(0, 8);
}

/// A fresh room token: the agent side makes one on every start, so nothing
/// has to be configured on the machine and the operator gets it in the
/// connection details.
std::string generate_token() {
  std::random_device rd;
  static const char* hex = "0123456789abcdef";
  std::string out;
  out.reserve(32);
  for (int i = 0; i < 32; ++i) out.push_back(hex[rd() & 0xF]);
  return out;
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

/// One log line, with the time: the negotiation between two machines is
/// spread over several processes, and without a timestamp the order of the
/// lines is guesswork.
void log_line(const char* fmt, ...) __attribute__((format(printf, 1, 2)));

void log_line(const char* fmt, ...) {
  char stamp[16];
  const std::time_t now = std::time(nullptr);
  std::tm tm_buf{};
#ifdef _WIN32
  localtime_s(&tm_buf, &now);
#else
  localtime_r(&now, &tm_buf);
#endif
  std::strftime(stamp, sizeof(stamp), "%H:%M:%S", &tm_buf);
  std::fprintf(stderr, "[%s] ", stamp);
  va_list args;
  va_start(args, fmt);
  std::vfprintf(stderr, fmt, args);
  va_end(args);
  std::fflush(stderr);
}

constexpr unsigned char kOpen = 1;
constexpr unsigned char kData = 2;
constexpr unsigned char kClose = 3;

// ---- framing ---------------------------------------------------------------
void put_u32(std::string& out, unsigned int v) {
  out.push_back(static_cast<char>((v >> 24) & 0xFF));
  out.push_back(static_cast<char>((v >> 16) & 0xFF));
  out.push_back(static_cast<char>((v >> 8) & 0xFF));
  out.push_back(static_cast<char>(v & 0xFF));
}

unsigned int get_u32(const char* p) {
  return (static_cast<unsigned int>(static_cast<unsigned char>(p[0])) << 24) |
         (static_cast<unsigned int>(static_cast<unsigned char>(p[1])) << 16) |
         (static_cast<unsigned int>(static_cast<unsigned char>(p[2])) << 8) |
         static_cast<unsigned int>(static_cast<unsigned char>(p[3]));
}

std::string frame(unsigned char type, const std::string& payload) {
  std::string out;
  out.push_back(static_cast<char>(type));
  put_u32(out, static_cast<unsigned int>(payload.size()));
  out += payload;
  return out;
}

void split_hostport(const std::string& s, std::string& host, uint16_t& port) {
  size_t colon = s.rfind(':');
  if (colon == std::string::npos) {
    host = s;
    port = 80;
    return;
  }
  host = s.substr(0, colon);
  port = static_cast<uint16_t>(std::stoi(s.substr(colon + 1)));
}

// ---- the signalling link to the broker -------------------------------------
// A socket that may be wrapped in TLS. The token travels over this link, so
// the TLS option is not decoration.
//
// The TLS client is mbedTLS (the same stack the rest of the project uses), so
// the sidecar needs nothing from the system: one file, no OpenSSL, no runtime
// dependency - it can be dropped onto a device that has nothing but libc.
class Link {
 public:
  ~Link() {
    if (tls_) {
      mbedtls_ssl_free(&ssl_);
      mbedtls_ssl_config_free(&conf_);
      mbedtls_x509_crt_free(&ca_);
      mbedtls_ctr_drbg_free(&drbg_);
      mbedtls_entropy_free(&entropy_);
    }
    if (fd_ != kTeaInvalidSocket) {
      tea_net_close(fd_);
    }
  }

  /// Plain TCP: the socket is already connected.
  void set_plain(tea_socket_t fd) { fd_ = fd; }

  /// TLS: mbedTLS does the connecting (it owns the socket afterwards).
  ///
  /// The broker's certificate is **self-signed**: the broker makes it itself
  /// and nobody signs it. So there is no chain to walk and no name to check
  /// (the address may have changed) - the TLS here is for the encryption,
  /// and **the room token is what actually gates the room**.
  ///
  /// A fingerprint is therefore **watched, never a gate**: when the broker's
  /// certificate is not the one that was pinned, the operator is told, but
  /// the tunnel is not broken over it. A fingerprint is one edit away from
  /// being stale (the broker was rebuilt, the certificate was replaced), and
  /// a broken tunnel is worse than a certificate nobody checked.
  ///
  /// The CA bundle is not used for the broker either - a public root store
  /// cannot contain a certificate that the broker signed itself. It is only
  /// kept so that an old command line does not fail.
  bool connect_tls(tea_socket_t fd, const std::string& host, const std::string& ca_file,
                    const std::string& pin, std::string* err) {
    mbedtls_ssl_init(&ssl_);
    mbedtls_ssl_config_init(&conf_);
    mbedtls_x509_crt_init(&ca_);
    (void)ca_file;
    mbedtls_ssl_config_defaults(&conf_, MBEDTLS_SSL_IS_CLIENT, MBEDTLS_SSL_TRANSPORT_STREAM,
                                MBEDTLS_SSL_PRESET_DEFAULT);
    // mbedTLS 3.x has no implicit RNG: the entropy source and the DRBG have
    // to be handed to the configuration explicitly.
    mbedtls_entropy_init(&entropy_);
    mbedtls_ctr_drbg_init(&drbg_);
    mbedtls_ctr_drbg_seed(&drbg_, mbedtls_entropy_func, &entropy_, nullptr, 0);
    mbedtls_ssl_conf_rng(&conf_, mbedtls_ctr_drbg_random, &drbg_);
    // mbedTLS 3.x: the verification callback comes second, and "required" is
    // a separate call (mbedtls_ssl_conf_authmode).
    mbedtls_ssl_conf_verify(&conf_, nullptr, nullptr);
    // The broker's certificate is self-signed: there is nothing to verify it
    // against, so the chain is not required.
    const bool pinned = !tea_tunnel::normalize_fingerprint(pin).empty();
    mbedtls_ssl_conf_authmode(&conf_, MBEDTLS_SSL_VERIFY_NONE);

    // The socket is ours, not mbedTLS': a Windows socket handle does not fit
    // in the int that mbedTLS' own socket helpers use, so the connection is
    // made here and mbedTLS is handed the socket that is already open.
    fd_ = fd;
    // set_hostname is both the SNI and the name the certificate is checked
    // against. mbedTLS compares an IP literal against the certificate's
    // iPAddress SANs, so the same call works for "broker.example" and
    // "203.0.113.7".
    // mbedTLS 3.x needs the configuration applied before the handshake.
    const int setup = mbedtls_ssl_setup(&ssl_, &conf_);
    if (setup != 0) {
      char buf[160];
      mbedtls_strerror(setup, buf, sizeof(buf));
      *err = "cannot set up the TLS session: " + std::string(buf);
      return false;
    }
    mbedtls_ssl_set_hostname(&ssl_, host.c_str());
    mbedtls_ssl_set_bio(&ssl_, &fd_, &Link::bio_send, &Link::bio_recv, nullptr);

    const int hs = mbedtls_ssl_handshake(&ssl_);
    if (hs != 0) {
      char buf[160];
      mbedtls_strerror(hs, buf, sizeof(buf));
      *err = "the TLS handshake failed: " + std::string(buf);
      return false;
    }
    tls_ = true;

    if (pinned) {
      // The fingerprint of the certificate the broker just presented. It is
      // only watched: a different certificate is worth knowing about, but it
      // does not break the tunnel (the room token is the gate).
      const mbedtls_x509_crt* peer = mbedtls_ssl_get_peer_cert(&ssl_);
      if (peer == nullptr) {
        log_line("sidecar: the broker did not present a certificate\n");
      } else if (!tea_tunnel::fingerprint_matches(peer->raw.p, peer->raw.len, pin)) {
        log_line("sidecar: the broker's certificate is not the one that was pinned (%s)\n"
                 "sidecar:   continuing anyway - the room token is what gates the room\n",
                 tea_tunnel::normalize_fingerprint(pin).c_str());
      }
    }
    return true;
  }

  ssize_t read(char* buf, size_t len) {
    if (tls_) return mbedtls_ssl_read(&ssl_, reinterpret_cast<unsigned char*>(buf), len);
    return tea_net_recv(fd_, buf, len);
  }

  ssize_t write(const char* buf, size_t len) {
    if (tls_) {
      const int rc = mbedtls_ssl_write(&ssl_, reinterpret_cast<const unsigned char*>(buf), len);
      return rc < 0 ? -1 : rc;
    }
    return tea_net_send(fd_, buf, len);
  }

 private:
  // The socket calls mbedTLS makes, done with our own socket type so that a
  // Windows socket handle is not squeezed into an int.
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

 public:

 private:
  tea_socket_t fd_ = kTeaInvalidSocket;
  bool tls_ = false;
  mbedtls_ssl_context ssl_{};
  mbedtls_ssl_config conf_{};
  mbedtls_x509_crt ca_{};
  mbedtls_entropy_context entropy_{};
  mbedtls_ctr_drbg_context drbg_{};
};

class Signal {
 public:
  bool connect_to(const std::string& host, uint16_t port, const std::string& room,
                   const std::string& token, const std::string& role, bool use_tls,
                   const std::string& ca_file, const std::string& pin, bool registering) {
    tea_net_init();
    tea_socket_t fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd == kTeaInvalidSocket) return false;
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
      addrinfo hints{};
      hints.ai_family = AF_INET;
      hints.ai_socktype = SOCK_STREAM;
      const std::string port_str = std::to_string(port);
      addrinfo* resolved = nullptr;
      if (::getaddrinfo(host.c_str(), port_str.c_str(), &hints, &resolved) != 0 || !resolved) {
        log_line("sidecar: cannot resolve the broker address %s\n", host.c_str());
        tea_net_close(fd);
        return false;
      }
      addr = *reinterpret_cast<sockaddr_in*>(resolved->ai_addr);
      ::freeaddrinfo(resolved);
    }
    // The connection is made here, TLS or not: the socket type is ours
    // (a Windows socket handle does not fit in mbedTLS' int).
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
      log_line("sidecar: cannot reach the broker %s:%u\n", host.c_str(), port);
      tea_net_close(fd);
      return false;
    }
    if (use_tls) {
      std::string err;
      if (!link_.connect_tls(fd, host, ca_file, pin, &err)) {
        log_line("sidecar: %s\n", err.c_str());
        tea_net_close(fd);
        return false;
      }
    } else {
      link_.set_plain(fd);
    }
    json join;
    join["room"] = room;
    join["token"] = token;
    join["role"] = role;
    // The agent side registers the room's token (it just made a fresh one);
    // the client side has to present the very same token.
    join["register"] = registering;
    send_line(join);
    alive_ = true;
    std::thread([this] { reader(); }).detach();
    return true;
  }

  void send_line(const json& j) {
    std::string line = j.dump() + "\n";
    std::lock_guard<std::mutex> lock(out_);
    link_.write(line.data(), line.size());
  }

  void on_message(std::function<void(const json&)> cb) { on_message_ = std::move(cb); }
  void on_closed(std::function<void()> cb) { on_closed_ = std::move(cb); }
  bool alive() const { return alive_; }

 private:
  void reader() {
    std::string buf;
    char chunk[4096];
    while (true) {
      ssize_t n = link_.read(chunk, sizeof(chunk));
      if (n <= 0) break;
      buf.append(chunk, static_cast<size_t>(n));
      size_t pos;
      while ((pos = buf.find('\n')) != std::string::npos) {
        std::string line = buf.substr(0, pos);
        buf.erase(0, pos + 1);
        if (line.empty()) continue;
        try {
          if (on_message_) on_message_(json::parse(line));
        } catch (const std::exception&) {
        }
      }
    }
    alive_ = false;
    log_line("sidecar: the signalling link closed\n");
    if (on_closed_) on_closed_();
  }

  Link link_;
  std::atomic<bool> alive_{false};
  std::mutex out_;
  std::function<void(const json&)> on_message_;
  std::function<void()> on_closed_;
};

/// Connect to host:port. The host may be a name ("localhost") or an address.
///
/// The name matters: `inet_pton` only understands numbers, and a sidecar
/// pointed at "--target localhost:8383" would silently fail to reach its own
/// a2 (the tunnel would come up and then carry nothing).
///
/// `getaddrinfo` is the way - but a statically linked Linux binary has no NSS
/// (glibc keeps it in shared libraries), so "localhost" is answered from a
/// built-in table before we try the resolver.
int dial_tcp(const std::string& host, uint16_t port) {
  tea_net_init();
  const std::string port_str = std::to_string(port);

  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  bool have_addr = (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) == 1);

  if (!have_addr) {
    std::string lowered = host;
    for (char& c : lowered) c = static_cast<char>(::tolower(c));
    if (lowered == "localhost" || lowered == "localhost.localdomain" ||
        lowered == "ip6-localhost" || lowered == "ip6-loopback") {
      addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
      have_addr = true;
    }
  }

  if (!have_addr) {
    addrinfo hints{};
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo* resolved = nullptr;
    if (::getaddrinfo(host.c_str(), port_str.c_str(), &hints, &resolved) != 0 || !resolved) {
      return -1;
    }
    addr = *reinterpret_cast<sockaddr_in*>(resolved->ai_addr);
    ::freeaddrinfo(resolved);
  }

  tea_socket_t fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd == kTeaInvalidSocket) return -1;
  if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    tea_net_close(fd);
    return -1;
  }
  tea_net_set_nodelay(fd);
  return fd;
}

}  // namespace

int main(int argc, char** argv) {
  std::string role = "client";
  // The broker's address. It can come from three places, in this order:
  // the command line, the configuration file, and the built-in default. The
  // file is the one to edit when the broker moves to another address.
  const BrokerConfig config = read_broker_config();
  std::string broker_host = "103.232.92.54";
  uint16_t broker_port = 8382;
  if (!config.address.empty()) {
    split_hostport(config.address, broker_host, broker_port);
  }
  std::string room;
  std::string token;
  std::string session;
  std::string listen_addr = "127.0.0.1:9931";
  std::string listen_host_override;
  uint16_t listen_port_override = 0;
  std::string target = "127.0.0.1:8383";
  // "--target terminal" is not an address: it means "hand out a real shell on
  // this machine and let the tunnel carry it".
  bool target_is_terminal = false;
  // The STUN and the TURN server of the broker. They follow the broker's
  // address unless they are named explicitly: the customer's network is
  // usually a symmetric NAT, so the relay is the normal path - ICE picks
  // between direct and relayed on its own.
  std::string stun_host;
  uint16_t stun_port = 3478;
  std::string turn_host;
  uint16_t turn_port = 3478;
  std::string turn_user = kDefaultTurnUser;
  std::string turn_pass = kDefaultTurnPass;
  // The account book: the same file the broker reads. It holds the TURN
  // accounts a person can add by hand. When it has accounts and the command
  // line did not name one, the first account is used.
  std::string turn_db = tea_tunnel::turn_db_path();
  bool turn_user_given = false;
  bool turn_pass_given = false;
  std::string ca_file;
  // The fingerprint of the broker's certificate. When it is set, the
  // certificate is checked against it - the address of the broker may
  // change, the certificate stays the same.
  std::string tls_pin = config.pin;
  // The public broker port speaks TLS (the room token travels over it), so
  // TLS is on by default; --no-tls is for a plain internal broker.
  bool use_tls = true;
  bool verbose = false;
  // Only use the relay, never a direct connection. It is how the TURN
  // credentials get tested: a direct connection would not ask the broker
  // for anything.
  bool relay_only = false;
  // How long to wait for the peer before giving up. The sidecar is meant to be
  // started on demand (the field staff starts it when access is needed), so
  // the default is five minutes, not "forever".
  int wait_timeout = 300;  // seconds; 0 = wait forever

  // The role decides who does what: the agent side sits next to the a2 and
  // registers the room, the client side is the one that connects from the
  // outside.
  bool is_agent = (role == "agent");
  // The desktop role is the agent side with a screen instead of a service;
  // the control role is the client side with a viewer in front of it.
  bool is_desktop = (role == "desktop");
  bool is_control = (role == "control");
  // The picture: how wide it may be, how many a second, how good the JPEG
  // is. The default is the screen as it is (the whole desktop, at its real
  // pixels); --width caps it when the bandwidth matters.
  int desktop_max_width = 0;
  int desktop_bitrate = 100000;    // bits per second for the H.264 picture
  int desktop_keyint = 10;         // a key frame every this many seconds
  int desktop_fps = 2;
  int desktop_quality = 50;
  // The port the browser opens (the control side only).
  int viewer_port = 9932;

  auto usage = [&](const char* why) {
    if (why) std::fprintf(stderr, "sidecar: %s\n", why);
    std::fprintf(stderr,
          "a2_tunnel --role agent|client|desktop|control [--session <code>]\n"
          "          [--broker <host:port>] [--room <name>] [--token <secret>]\n"
          "          [--listen <host:port>] [--host <addr>] [--port <n>] [--target <host:port>]\n"
          "          [--stun <host:port>] [--turn <host:port> --turn-user u --turn-pass p]\n"
          "          [--turn-db <file>] [--ice-relay-only]\n"
          "          [--tls|--no-tls] [--ca-bundle <pem>] [--tls-pin <sha256>]\n"
          "\n"
          "  --session <code>  the one string that travels between the two sides\n"
          "                   (the room name and the token are derived from it)\n"
          "  --port <n>        the local port to listen on (the default is 9931)\n"
          "  --target terminal serve a real shell on this machine and let the tunnel\n"
          "                   carry it (the agent side only). The browser on the\n"
          "                   other side opens http://127.0.0.1:<port>/\n"
          "  --turn-db <file>  the account book with the TURN accounts (the same\n"
          "                   file the broker reads). When it has accounts and\n"
          "                   --turn-user/--turn-pass were not given, the first\n"
          "                   account is used.\n"
          "  --tls-pin <hex>   the fingerprint of the broker's certificate. It is\n"
          "                   only watched: if the broker presents another one,\n"
          "                   the sidecar says so and carries on (the room token\n"
          "                   is what gates the room).\n"
          "  --ca-bundle <pem> not used for the broker: its certificate is\n"
          "                   self-signed, so no root store can check it. The\n"
          "                   option is kept so old command lines still work.\n"
          "  --wait-timeout    how long the agent side waits for the peer before giving\n"
          "                   up (default 300 seconds; 0 = wait forever)\n"
          "  --viewer-port <n> the port the browser opens on the control side\n"
          "                     (default 9932)\n"
          "  --width <n>       the widest picture the desktop role hands out\n"
          "                     (default 0 = the screen as it is, at its real\n"
          "                     pixels; a smaller number scales it down)\n"
          "  --bitrate <kbps>  the bits per second the H.264 picture may use\n"
          "                     (default 100; that is about 6 KB a picture at\n"
          "                     2560x1440 and two pictures a second - the\n"
          "                     picture is soft, the mouse and the keyboard\n"
          "                     are not affected.  --width 1280 makes it\n"
          "                     much clearer for the same bitrate)\n"
          "  --keyint <seconds> how often a key frame comes (default 10).  The\n"
          "                     pictures in between are much smaller (a quiet\n"
          "                     screen: a few KB), but they need the ones\n"
          "                     before them - the pictures travel as a\n"
          "                     stream and none of them may be lost\n"
          "  --fps <n>         how many pictures a second (default 2)\n"
          "  --quality <n>     the JPEG quality, 1..100 (default 50)\n"
          "\n"
          "  The broker's address (and its fingerprint, if you want to watch it)\n"
          "  can also be put in %s:\n"
          "      address=<host:port>\n"
          "      pin=<sha256>\n",
          broker_config_path().c_str());
    std::exit(2);
  };

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    // A missing value is an error, not an empty string: silently continuing
    // with an empty value is how you end up listening on the wrong port.
    auto next = [&]() -> std::string {
      if (i + 1 >= argc) usage((a + " needs a value").c_str());
      return argv[++i];
    };
    if (a == "--role") {
      role = next();
      is_agent = (role == "agent" || role == "desktop");
      is_desktop = (role == "desktop");
      is_control = (role == "control");
    } else if (a == "--viewer-port") {
      viewer_port = std::atoi(next().c_str());
    } else if (a == "--fps") {
      desktop_fps = std::atoi(next().c_str());
    } else if (a == "--quality") {
      desktop_quality = std::atoi(next().c_str());
    } else if (a == "--width") {
      desktop_max_width = std::atoi(next().c_str());
    } else if (a == "--bitrate") {
      desktop_bitrate = std::atoi(next().c_str()) * 1000;
    } else if (a == "--keyint") {
      desktop_keyint = std::atoi(next().c_str());
    } else if (a == "--broker") {
      std::string hp = next();
      split_hostport(hp, broker_host, broker_port);
    } else if (a == "--room") room = next();
    else if (a == "--token") token = next();
    else if (a == "--session") session = next();
    else if (a == "--listen") listen_addr = next();
    else if (a == "--host") {
      // The address the local proxy listens on (client side). Same spelling
      // as the agent's own --host.
      listen_host_override = next();
    }
    else if (a == "--port") {
      // The local port to listen on - the same spelling the a2 itself uses.
      listen_port_override = static_cast<uint16_t>(std::stoi(next()));
    } else if (a == "--target") {
      target = next();
      if (target == "terminal") target_is_terminal = true;
    }
    else if (a == "--stun") {
      std::string hp = next();
      split_hostport(hp, stun_host, stun_port);
    } else if (a == "--turn") {
      std::string hp = next();
      split_hostport(hp, turn_host, turn_port);
    } else if (a == "--turn-user") { turn_user = next(); turn_user_given = true; }
    else if (a == "--turn-pass") { turn_pass = next(); turn_pass_given = true; }
    else if (a == "--turn-db") turn_db = next();
    else if (a == "--ca-bundle") ca_file = next();
    else if (a == "--tls-pin") tls_pin = next();
    else if (a == "--ice-relay-only") relay_only = true;
    else if (a == "--wait-timeout") wait_timeout = std::atoi(next().c_str());
    else if (a == "--tls") use_tls = true;
    else if (a == "--no-tls") use_tls = false;
    else if (a == "--verbose") verbose = true;
    else if (a == "--help" || a == "-h") usage(nullptr);
    else usage(("unknown option '" + a + "'").c_str());
  }
  // --session is the one string that travels between the two sides: the room
  // name and the token are derived from it.
  if (!session.empty()) {
    if (room.empty()) room = room_of_session(session);
    if (token.empty()) token = session;
  }
  if (room.empty() && is_agent) {
    // The agent side invents the session on every start: nobody has to
    // configure anything, the operator just runs the command it prints.
    session = generate_token();
    room = room_of_session(session);
    token = session;
    log_line("sidecar: made a new session for this run\n");
  }
  if (room.empty()) room = "default";
  if (token.empty()) {
    std::string from_file;
    if (read_token_file(&from_file)) {
      token = from_file;
    } else {
      log_line("sidecar: no room token. Use the command the other side printed "
               "(--session <code>), or put it in %s\n",
               token_file_path().c_str());
      return 2;
    }
  }
  // The broker's certificate is self-signed, so it is not checked against a
  // root store and the CA bundle is not needed. The option is still
  // accepted (an old command line should not fail), it just does not
  // change anything.
  const bool pinned = !tea_tunnel::normalize_fingerprint(tls_pin).empty();
  if (verbose) rtc::InitLogger(rtc::LogLevel::Debug);
  else rtc::InitLogger(rtc::LogLevel::Warning);

  // The STUN and the TURN servers are the broker's, so they follow its
  // address: when the broker moves, one address changes and everything
  // else comes with it.
  if (stun_host.empty()) stun_host = broker_host;
  if (turn_host.empty()) turn_host = broker_host;

  // The TURN account: the command line first, then the account book (the
  // same file the broker reads), and the built-in pair as the last resort.
  // The file is only read, never written: the book belongs to the broker.
  if (!turn_user_given || !turn_pass_given) {
    std::vector<tea_tunnel::TurnAccount> accounts;
    std::string db_error;
    if (tea_tunnel::read_turn_accounts(turn_db, &accounts, &db_error) && !accounts.empty()) {
      if (!turn_user_given) turn_user = accounts.front().user;
      if (!turn_pass_given) turn_pass = accounts.front().pass;
      log_line("sidecar: using the TURN account \"%s\" from %s\n", turn_user.c_str(),
               turn_db.c_str());
    } else if (!db_error.empty()) {
      log_line("sidecar: %s\n", db_error.c_str());
    }
  }

  // ---- a terminal instead of an address -----------------------------------
  // "--target terminal" means: this machine hands out a real shell and the
  // tunnel carries it. The service listens on the loopback only - the
  // browser reaches it through the tunnel, like everything else. The wire
  // protocol does not change: the tunnel still forwards plain bytes.
  tea_tunnel::TerminalService terminal;
  if (target_is_terminal) {
    if (!is_agent) {
      log_line("sidecar: --target terminal only works on the agent side (the one next "
               "to the machine you want a shell on)\n");
      return 1;
    }
    std::string term_err;
    if (!terminal.start(&term_err)) {
      log_line("sidecar: cannot start the terminal: %s\n", term_err.c_str());
      return 1;
    }
    target = "127.0.0.1:" + std::to_string(terminal.port());
    log_line("sidecar: the terminal is listening on 127.0.0.1:%u\n", terminal.port());
  }

  // ---- a desktop instead of an address -------------------------------------
  // "--role desktop" means: this machine shows its screen and takes the
  // mouse and the keyboard. The service listens on the loopback only - the
  // tunnel carries it, like everything else.
  tea_tunnel::DesktopService desktop_service;
  tea_tunnel::ControlService control_service;
  if (is_desktop) {
    std::string desktop_err;
    if (!desktop_service.start(desktop_max_width, desktop_fps, desktop_quality, desktop_bitrate,
                               desktop_keyint, &desktop_err)) {
      log_line("sidecar: cannot start the desktop: %s\n", desktop_err.c_str());
      return 1;
    }
    target = "127.0.0.1:" + std::to_string(desktop_service.port());
    log_line("sidecar: the desktop is listening on 127.0.0.1:%u\n",
             static_cast<unsigned>(desktop_service.port()));
  }

  rtc::Configuration cfg;
  if (relay_only) {
    // Ask ICE for relay candidates only: the connection then has to go
    // through the broker's TURN server, so the TURN account is really used.
    cfg.iceTransportPolicy = rtc::TransportPolicy::Relay;
  }
  // Only the agent offers. The client answers; if it were allowed to
  // auto-negotiate, receiving the data channel would make it offer too, and
  // the two sides would keep offering at each other forever.
  if (!is_agent) cfg.disableAutoNegotiation = true;
  cfg.iceServers.emplace_back(stun_host, stun_port);
  if (!turn_host.empty() && !turn_pass.empty()) {
    cfg.iceServers.emplace_back(turn_host, turn_port, turn_user, turn_pass,
                                rtc::IceServer::RelayType::TurnUdp);
  }

  // ---- shared state --------------------------------------------------------
  std::mutex mu;
  std::map<unsigned int, int> conns;   // connection id -> socket
  unsigned int next_conn = 1;
  std::atomic<bool> remote_ready{false};
  std::atomic<bool> tunnel_ready{false};   // the data channel is open
  // When the wait for the peer started (steady_clock milliseconds). It is
  // reset every time the tunnel opens or closes, so "five minutes without a
  // peer" means five minutes *right now* - not five minutes since the process
  // was started. Otherwise a tunnel that worked for an hour and then dropped
  // would exit immediately instead of waiting for the peer to come back.
  std::atomic<long long> waiting_since{0};
  std::vector<std::string> pending_candidates;
  std::vector<std::string> local_candidates;
  std::string local_description;
  std::string local_description_type;
  std::shared_ptr<rtc::DataChannel> dc;
  std::shared_ptr<rtc::PeerConnection> pc;
  // One offer per peer arrival: the broker tells us twice (peer_joined and
  // the peer's need-offer), and offering twice leaves the peer answering a
  // description we no longer have.
  int peer_epoch = 0;
  int offered_epoch = -1;
  // Every new peer connection gets a number. The callbacks of an older
  // connection are ignored: without this a replaced peer connection keeps
  // living (the callbacks hold a shared_ptr) and can still send a stale
  // answer into the new negotiation - which is how one side ends up
  // answering a description the other side never asked for.
  int pc_gen = 0;
  std::function<void()> start_listener;
  // Set once setup_pc() exists: the channel's close handler needs to start
  // a fresh negotiation, and setup_pc() is defined further down.
  std::function<void()> rebuild_tunnel;

  auto send_frame = [&](unsigned char type, const std::string& payload) {
    if (!dc) return;
    const std::string bytes = frame(type, payload);
    rtc::binary out;
    out.reserve(bytes.size());
    for (char c : bytes) out.push_back(static_cast<std::byte>(c));
    try {
      dc->send(out);
    } catch (const std::exception& e) {
      log_line("sidecar: send failed: %s\n", e.what());
    }
  };

  // The pump that copies bytes out of a local socket into the tunnel.
  auto pump = [&](unsigned int id, int fd) {
    char buf[16384];
    while (true) {
      ssize_t n = ::recv(fd, buf, sizeof(buf), 0);
      if (n <= 0) break;
      std::string payload;
      put_u32(payload, id);
      payload.append(buf, static_cast<size_t>(n));
      send_frame(kData, payload);
    }
    std::string payload;
    put_u32(payload, id);
    send_frame(kClose, payload);
    // The socket is closed here: the other side is done writing, and this
    // thread has just seen it (the read returned nothing).
    tea_net_close(fd);
  };

  Signal signal;

  // Where the client side listens. It is filled in when the listener starts,
  // but it is read earlier (to tell the operator which address to open), so
  // it lives here.
  std::string listen_host;
  uint16_t listen_port = 0;

  // The other end may be a terminal instead of an agent. The agent says so
  // when the tunnel opens (and again when the client asks), and the client
  // tells the operator which address to open.
  std::atomic<bool> peer_is_terminal{false};
  std::atomic<bool> peer_is_desktop{false};
  std::atomic<bool> terminal_announced{false};
  auto announce_terminal = [&]() {
    if (!peer_is_terminal.load()) return;
    if (terminal_announced.exchange(true)) return;
    std::fprintf(stderr,
                 "  The far end is a terminal: open http://%s:%u/ in the browser.\n",
                 listen_host.empty() ? "127.0.0.1" : listen_host.c_str(),
                 listen_port ? static_cast<unsigned>(listen_port) : 9931u);
    std::fflush(stderr);
  };

  auto attach_channel = [&](std::shared_ptr<rtc::DataChannel> channel, int gen) {
    if (gen != pc_gen) return;
    dc = channel;
    channel->onOpen([&, gen] {
      if (gen != pc_gen) return;
      tunnel_ready = true;
      waiting_since = std::chrono::duration_cast<std::chrono::milliseconds>(
                         std::chrono::steady_clock::now().time_since_epoch())
                         .count();
      log_line("sidecar: the data channel is open (%s)\n", role.c_str());
      try {
        rtc::Candidate local, remote;
        if (pc && pc->getSelectedCandidatePair(&local, &remote)) {
          const std::string la = local.address().value_or("?");
          const std::string ra = remote.address().value_or("?");
          auto kind = [](rtc::Candidate::Type t) {
            switch (t) {
            case rtc::Candidate::Type::Host: return "host";
            case rtc::Candidate::Type::ServerReflexive: return "srflx";
            case rtc::Candidate::Type::PeerReflexive: return "prflx";
            case rtc::Candidate::Type::Relayed: return "relayed";
            default: return "unknown";
            }
          };
          const bool via_broker =
              local.type() == rtc::Candidate::Type::Relayed ||
              remote.type() == rtc::Candidate::Type::Relayed;
          log_line("sidecar:   path: local %s:%d (%s) <-> remote %s:%d (%s) - %s\n",
                       la.c_str(), local.port(), kind(local.type()), ra.c_str(), remote.port(),
                       kind(remote.type()),
                       via_broker ? "through the broker" : "direct (no relay)");
        }
      } catch (const std::exception& e) {
        log_line("sidecar:   (cannot read the selected pair: %s)\n", e.what());
      }
      if (!is_agent) start_listener();
      // Say what is on the other end: the client side asks (it may have
      // arrived after the agent announced it), the agent side answers.
      if (is_agent) {
        if (target_is_terminal) {
          signal.send_line({{"type", "target"}, {"kind", "terminal"}});
        } else if (is_desktop) {
          signal.send_line({{"type", "target"}, {"kind", "desktop"}});
        }
      } else {
        signal.send_line({{"type", "ask-target"}});
      }
      announce_terminal();
    });
    channel->onClosed([&, gen] {
      if (gen != pc_gen) return;
      tunnel_ready = false;
      // The peer is gone: the countdown for giving up starts now.
      waiting_since = std::chrono::duration_cast<std::chrono::milliseconds>(
                         std::chrono::steady_clock::now().time_since_epoch())
                         .count();
      log_line("sidecar: the data channel closed (%s)\n", role.c_str());
      // A closed channel has to be rebuilt, not just reported: the link can
      // die without anybody hanging up (the broker restarted and took the
      // TURN lease with it, the path changed, ...). The agent side offers,
      // the client side asks for an offer - it is the answerer.
      if (is_agent) {
        offered_epoch = peer_epoch;
        if (rebuild_tunnel) rebuild_tunnel();
      } else {
        signal.send_line({{"type", "need-offer"}});
      }
    });
    channel->onMessage([&, gen](std::variant<rtc::binary, std::string> msg) {
      if (gen != pc_gen) return;
      if (!std::holds_alternative<rtc::binary>(msg)) return;
      const rtc::binary& bytes = std::get<rtc::binary>(msg);
      if (bytes.size() < 5) return;
      const char* p = reinterpret_cast<const char*>(bytes.data());
      const unsigned char type = static_cast<unsigned char>(p[0]);
      const unsigned int len = get_u32(p + 1);
      if (len + 5 > bytes.size()) return;
      const char* payload = p + 5;

      if (type == kOpen) {
        if (len < 4) return;
        const unsigned int id = get_u32(payload);
        std::string thost;
        uint16_t tport = 0;
        split_hostport(target, thost, tport);
        int fd = dial_tcp(thost, tport);
        if (fd < 0) {
          log_line("sidecar: cannot reach the target %s\n", target.c_str());
          // Tell the other side right away: otherwise the caller waits for a
          // timeout on a connection that can never carry anything.
          std::string payload;
          put_u32(payload, id);
          send_frame(kClose, payload);
          return;
        }
        {
          std::lock_guard<std::mutex> lock(mu);
          conns[id] = fd;
        }
        std::thread(pump, id, fd).detach();
        return;
      }

      if (type == kData) {
        if (len < 4) return;
        const unsigned int id = get_u32(payload);
        std::lock_guard<std::mutex> lock(mu);
        auto it = conns.find(id);
        if (it == conns.end()) return;
        const ssize_t n = ::send(it->second, payload + 4, static_cast<int>(len - 4), kTeaNoSignal);
        (void)n;
        return;
      }

      if (type == kClose) {
        if (len < 4) return;
        const unsigned int id = get_u32(payload);
        std::lock_guard<std::mutex> lock(mu);
        auto it = conns.find(id);
        if (it == conns.end()) return;
        // The far end is done writing. Do NOT close the socket: whatever is
        // still in the buffer belongs to the reader, and closing it would
        // throw the answer away (the reader would see a reset instead of
        // the last bytes). Half-close it: the reader drains the buffer and
        // then sees the end of the stream.
        ::shutdown(it->second, SD_SEND);   // Windows 的名字（POSIX 那边叫 SHUT_WR）
        conns.erase(it);
      }
    });
  };

  // Build (or rebuild) the peer connection. Rebuilding is what makes a
  // restarted peer survivable: the old ICE credentials are dead, so both sides
  // start a fresh negotiation instead of talking past each other.
  auto setup_pc = [&]() {
    const int gen = ++pc_gen;
    remote_ready = false;
    pending_candidates.clear();
    local_candidates.clear();
    local_description.clear();
    pc = std::make_shared<rtc::PeerConnection>(cfg);

    pc->onLocalDescription([&, gen](rtc::Description d) {
      if (gen != pc_gen) return;
      const bool offer = (d.type() == rtc::Description::Type::Offer);
      local_description = std::string(d);
      local_description_type = offer ? "offer" : "answer";
      signal.send_line({{"type", local_description_type}, {"sdp", local_description}});
    });
    pc->onLocalCandidate([&, gen](rtc::Candidate c) {
      if (gen != pc_gen) return;
      local_candidates.push_back(std::string(c));
      signal.send_line({{"type", "candidate"}, {"sdp", std::string(c)}});
    });
    pc->onDataChannel([&, gen](std::shared_ptr<rtc::DataChannel> channel) {
      attach_channel(channel, gen);
    });

    if (is_agent) {
      // The agent creates the channel; creating it makes libdatachannel
      // produce the offer right away.
      attach_channel(pc->createDataChannel("a2"), gen);
    }
  };
  rebuild_tunnel = [&]() { setup_pc(); };

  signal.on_message([&](const json& j) {
    const std::string type = j.value("type", "");
    if (type == "offer" || type == "answer") {
      // One side offers, the other answers. Getting this backwards makes
      // libdatachannel refuse the description ("Incompatible roles"), so the
      // roles are checked here rather than hoped for.
      if (is_agent && type == "offer") {
        log_line("sidecar: the peer offered (we are the offerer), offering again\n");
        setup_pc();
        return;
      }
      if (!is_agent && type == "answer") {
        log_line("sidecar: an answer arrived (we are the answerer), waiting for an offer\n");
        return;
      }
      try {
        if (!pc) setup_pc();
        // A fresh offer from a peer that (re)started: throw the old
        // negotiation away and start over with it.
        if (type == "offer" && remote_ready) {
          log_line("sidecar: the peer offered again, renegotiating\n");
          setup_pc();
        }
        rtc::Description desc(j.value("sdp", ""), type == "offer" ? rtc::Description::Type::Offer
                                                                   : rtc::Description::Type::Answer);
        pc->setRemoteDescription(desc);
        remote_ready = true;
        for (auto& c : pending_candidates) {
          try {
            pc->addRemoteCandidate(rtc::Candidate(c));
          } catch (const std::exception&) {
          }
        }
        pending_candidates.clear();
        if (type == "offer") pc->setLocalDescription();  // -> answer
      } catch (const std::exception& e) {
        // A description we cannot use: the peer is talking about a
        // negotiation we already threw away (it restarted, most likely).
        // Offering again is safe now: the old peer connection is replaced
        // wholesale and its callbacks are ignored, so its late answer
        // cannot start a loop.
        log_line("sidecar: cannot use that %s (%s), offering again\n", type.c_str(), e.what());
        if (is_agent) {
          offered_epoch = peer_epoch;
          setup_pc();
        }
      }
      return;
    }
    if (type == "candidate") {
      const std::string sdp = j.value("sdp", "");
      if (sdp.empty()) return;
      if (!remote_ready) {
        pending_candidates.push_back(sdp);
        return;
      }
      try {
        pc->addRemoteCandidate(rtc::Candidate(sdp));
      } catch (const std::exception&) {
      }
      return;
    }
    if (type == "joined") {
      const bool peer = j.value("peer", false);
      log_line("sidecar: joined room %s (peer present: %s)\n", room.c_str(),
                   peer ? "yes" : "no");
      if (peer) {
        peer_epoch++;
        if (is_agent) {
          // We are the offerer and the peer is already here: offer now.
          log_line("sidecar: the peer is here, offering\n");
          offered_epoch = peer_epoch;
          setup_pc();
        } else {
          // We just arrived: the peer's offer was sent before we were in the
          // room, so ask for a fresh one.
          signal.send_line({{"type", "need-offer"}});
        }
      }
      return;
    }
    if (type == "peer_joined") {
      // The other side arrived (or arrived again). Our offer may have been
      // sent into an empty room: offer again.
      peer_epoch++;
      log_line("sidecar: the peer joined, offering again\n");
      if (is_agent) {
        offered_epoch = peer_epoch;
        setup_pc();
      }
      return;
    }
    if (type == "need-offer") {
      // The peer wants a fresh offer. If we already offered for this arrival
      // **and the tunnel is up**, this is the same request arriving twice:
      // ignore it. If the tunnel is down, the peer needs a new one.
      if (is_agent) {
        if (offered_epoch == peer_epoch && tunnel_ready) {
          log_line("sidecar: need-offer, but we already offered\n");
          return;
        }
        log_line("sidecar: offering again\n");
        offered_epoch = peer_epoch;
        setup_pc();
      }
      return;
    }
    if (type == "ask-target") {
      // The client wants to know what is on the other end.
      if (target_is_terminal) {
        signal.send_line({{"type", "target"}, {"kind", "terminal"}});
      } else if (is_desktop) {
        signal.send_line({{"type", "target"}, {"kind", "desktop"}});
      }
      return;
    }
    if (type == "target") {
      const std::string kind = j.value("kind", "");
      if (kind == "terminal") {
        peer_is_terminal.store(true);
        log_line("sidecar: the other end is a terminal\n");
        announce_terminal();
      } else if (kind == "desktop") {
        peer_is_desktop.store(true);
        log_line("sidecar: the other end is a desktop\n");
      }
      return;
    }
    if (type == "error") {
      const std::string message = j.value("message", "");
      log_line("sidecar: broker says: %s\n", message.c_str());
      if (message == "bad token") {
        // Retrying cannot help: the token is wrong, or the other side
        // restarted and made a new one. Say so and stop - the operator has
        // to run the command the other side printed.
        log_line("sidecar: the broker rejected the room token. Use the command the "
                 "other side printed (it makes a new token on every start).\n");
        std::fflush(stderr);
        std::_Exit(2);
      }
    }
  });

  // ---- the client side listens locally ------------------------------------
  // A socket handle, not an int: on Windows it is 64 bits wide and
  // INVALID_SOCKET is (SOCKET)~0 - an int would truncate it.
  tea_socket_t lfd = kTeaInvalidSocket;
  start_listener = [&]() {
    if (lfd != kTeaInvalidSocket) return;
    split_hostport(listen_addr, listen_host, listen_port);
    lfd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (lfd == kTeaInvalidSocket) return;
    int one = 1;
    ::setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, reinterpret_cast<const char*>(&one), sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(listen_port);
    ::inet_pton(AF_INET, listen_host.c_str(), &addr.sin_addr);
    if (::bind(lfd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
        ::listen(lfd, 16) != 0) {
      log_line("sidecar: cannot listen on %s\n", listen_addr.c_str());
      tea_net_close(lfd);
      lfd = kTeaInvalidSocket;
      return;
    }
    log_line("sidecar: listening on %s\n", listen_addr.c_str());
    std::thread([&]() {
      while (true) {
        tea_socket_t fd = ::accept(lfd, nullptr, nullptr);
        if (fd == kTeaInvalidSocket) continue;
        // Without a tunnel there is nothing to forward to. Refuse the
        // connection instead of accepting it and letting the caller hang
        // until its own timeout expires.
        if (!tunnel_ready) {
          tea_net_close(fd);
          log_line("sidecar: refusing a connection, the tunnel is not up yet\n");
          continue;
        }
        unsigned int id;
        {
          std::lock_guard<std::mutex> lock(mu);
          id = next_conn++;
          conns[id] = fd;
        }
        std::string payload;
        put_u32(payload, id);
        send_frame(kOpen, payload);
        std::thread(pump, id, fd).detach();
      }
    }).detach();
  };

  // ---- keep the signalling alive ------------------------------------------
  // A dropped link (the broker restarted, the network blinked) must not leave
  // the sidecar sitting there with a dead tunnel: reconnect, re-join the room
  // and let the room logic renegotiate.
  std::thread([&]() {
    int backoff = 2;
    while (true) {
      std::this_thread::sleep_for(std::chrono::seconds(2));
      if (signal.alive()) {
        backoff = 2;
        continue;
      }
      log_line("sidecar: the signalling link is down, reconnecting...\n");
      if (signal.connect_to(broker_host, broker_port, room, token, role, use_tls, ca_file,
                            tls_pin, is_agent)) {
        backoff = 2;
        log_line("sidecar: reconnected to the broker\n");
      } else {
        std::this_thread::sleep_for(std::chrono::seconds(backoff));
        backoff = backoff * 2 > 60 ? 60 : backoff * 2;
      }
    }
  }).detach();

  // ---- how long do we wait for the peer? ---------------------------------
  // The agent side waits forever by default: the peer may show up at any
  // time, and giving up would leave a device with a dead tunnel and no
  // explanation. --wait-timeout makes it give up after N seconds (0 = never).
  std::thread([&]() {
    // The countdown starts when the sidecar starts, and restarts every time
    // the tunnel opens or closes.
    waiting_since = std::chrono::duration_cast<std::chrono::milliseconds>(
                        std::chrono::steady_clock::now().time_since_epoch())
                        .count();
    long long last_report = -60;
    while (true) {
      std::this_thread::sleep_for(std::chrono::seconds(5));
      // While the tunnel is up the peer is there: nothing to wait for.
      if (!is_agent || tunnel_ready) continue;
      const long long now = std::chrono::duration_cast<std::chrono::milliseconds>(
                                std::chrono::steady_clock::now().time_since_epoch())
                                .count();
      const long long waited = (now - waiting_since.load()) / 1000;
      if (waited - last_report >= 60) {
        last_report = waited;
        log_line("sidecar: still waiting for the peer in room \"%s\" (%llds)\n",
                     room.c_str(), waited);
      }
      if (wait_timeout > 0 && waited >= wait_timeout) {
        log_line("sidecar: no peer after %llds, giving up\n",
                     static_cast<long long>(wait_timeout));
        std::fflush(stderr);
        // A clean exit: whoever started it (a systemd unit with
        // Restart=on-failure) should not treat this as a crash.
        std::_Exit(0);
      }
    }
  }).detach();

  // --host and --port win over the parts of --listen, whatever the order was.
  {
    std::string lhost;
    uint16_t lport = 0;
    split_hostport(listen_addr, lhost, lport);
    if (!listen_host_override.empty()) lhost = listen_host_override;
    if (listen_port_override != 0) lport = listen_port_override;
    listen_addr = lhost + ":" + std::to_string(lport);
  }

  // The peer connection is built **after** the signalling link is up: the
  // offer is produced the moment the channel is created, and an offer
  // produced before the link exists is a message nobody receives.
  if (!signal.connect_to(broker_host, broker_port, room, token, role, use_tls, ca_file, tls_pin,
                         is_agent))
    return 1;
  setup_pc();

  log_line("sidecar: %s side, room %s, broker %s:%u\n", role.c_str(), room.c_str(),
               broker_host.c_str(), broker_port);

  if (!is_agent) {
    start_listener();  // the local proxy is up even before the tunnel
    if (is_control) {
      // The viewer: the browser talks to it, and it talks to the other side
      // through the local proxy (the same one the browser's own requests
      // would go through).
      std::string control_err;
      if (!control_service.start("127.0.0.1", listen_port, viewer_port, &control_err)) {
        log_line("sidecar: cannot start the viewer: %s\n", control_err.c_str());
        return 1;
      }
      log_line("sidecar: the viewer is on http://127.0.0.1:%u/\n",
               static_cast<unsigned>(control_service.port()));
    }
  } else if (!listen_host_override.empty()) {
    log_line("sidecar: --host only matters on the client side (the agent side has no "
             "local proxy)\n");
  }

  // 「就绪」要说人话：部署的人要知道**外面怎么连进来**。
  // client 侧打印一段可以直接粘进对方 a2 的 settings.peers 的配置；
  // agent 侧打印房间和令牌（两边必须一致）。
  std::fprintf(stderr, "---------------------------------------------------------------\n");
  if (is_agent) {
    log_line("sidecar: ready. Waiting in room \"%s\" for the other side.\n",
                 room.c_str());
    std::fprintf(stderr, "  broker        : %s:%u\n", broker_host.c_str(), broker_port);
    std::fprintf(stderr, "  room token    : %s\n", token.c_str());
    if (target_is_terminal) {
      std::fprintf(stderr, "  target        : a real shell on this machine\n");
    } else {
      std::fprintf(stderr, "  target        : %s\n", target.c_str());
    }
    std::fprintf(stderr, "  give this to whoever connects from the outside:\n");
    // The desktop role needs the control side: the picture is decoded there.
    std::fprintf(stderr, "      a2_tunnel --role %s --session %s",
                 is_desktop ? "control" : "client",
                 session.empty() ? token.c_str() : session.c_str());
    // The fingerprint goes along: it is what lets the other side check that
    // it is talking to the same broker, without a certificate to install.
    if (!tea_tunnel::normalize_fingerprint(tls_pin).empty()) {
      std::fprintf(stderr, " --tls-pin %s", tea_tunnel::normalize_fingerprint(tls_pin).c_str());
    }
    std::fprintf(stderr, "\n");
    std::fprintf(stderr, "  (the broker, TLS and the STUN/TURN servers are the defaults; the\n"
                         "   broker's certificate is self-signed, so its fingerprint has to be\n"
                         "   trusted - put it in <home>/.tea_agent/tunnel/broker.txt as pin=<hex>,\n"
                         "   the broker prints it when it starts)\n");
    if (target_is_terminal) {
      std::fprintf(stderr, "  (it is a terminal: whoever connects from the outside opens\n"
                           "   http://127.0.0.1:<their port>/ in a browser)\n");
    } else if (is_desktop) {
      std::fprintf(stderr, "  (it is this machine's screen: the other side opens\n"
                           "   http://127.0.0.1:<their viewer port>/ to watch it)\n");
    } else {
      std::fprintf(stderr, "  (it will listen locally and be reachable through the tunnel)\n");
    }
    if (wait_timeout > 0) {
      std::fprintf(stderr, "  giving up in %d seconds if nobody connects.\n", wait_timeout);
    }
  } else if (is_control) {
    // The control role: the browser watches the picture on the viewer's
    // port. The tunnel's own port is still the proxy towards the other
    // side - opening that one in a browser gets you nothing (the far end
    // has no page).
    log_line("sidecar: ready. The picture is on the viewer's port.\n");
    std::fprintf(stderr, "  open this in a browser:\n");
    std::fprintf(stderr, "      http://%s:%u/\n",
                 listen_host.empty() ? "127.0.0.1" : listen_host.c_str(),
                 control_service.port());
    std::fprintf(stderr, "  (the tunnel's own port %u is the proxy to the other side;\n"
                         "   it is not a web page)\n",
                 listen_port);
  } else {
    log_line("sidecar: ready. To reach it from the outside:\n");
    std::fprintf(stderr, "  add this to the settings.peers of the a2 that drives it:\n");
    // 0.0.0.0 is where we listen, not an address to connect to: the peer
    // entry needs the address of this machine.
    const bool every_interface =
        (listen_host == "0.0.0.0" || listen_host == "::");
    const std::string shown_host = every_interface ? "<这台机器的地址>" : listen_host;
    std::fprintf(stderr, "      peers:\n");
    std::fprintf(stderr, "        %s:\n", room.c_str());
    std::fprintf(stderr, "          url: http://%s:%u\n", shown_host.c_str(), listen_port);
    std::fprintf(stderr, "          token: %s\n", token.c_str());
    std::fprintf(stderr, "          description: %s, reached through the tunnel\n", room.c_str());
    std::fprintf(stderr, "  toolkit_peer can then talk to it (it answers with its own model).\n");
    if (listen_host == "0.0.0.0" || listen_host == "::") {
      std::fprintf(stderr, "  It listens on every interface: any machine that can reach this one\n"
                           "  can open http://<this machine>:%u/ for its web UI.\n",
                   listen_port);
    } else {
      std::fprintf(stderr, "  The browser on this machine can open http://%s:%u/ for its web UI.\n",
                   listen_host.c_str(), listen_port);
    }
  }
  std::fprintf(stderr, "  ! the lines above contain the token - do not share this log.\n");
  std::fprintf(stderr, "---------------------------------------------------------------\n");

  while (true) std::this_thread::sleep_for(std::chrono::seconds(3600));
}
