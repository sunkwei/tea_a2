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
//     --tls --ca-bundle <pem>      (the broker's certificate must be in it)
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
  const char* env = std::getenv("TEA_AGENT_HOME");
  std::string home;
  if (env && *env) {
    home = env;
  } else {
#ifdef _WIN32
    const char* up = std::getenv("USERPROFILE");
    home = (up && *up) ? up : ".";
#else
    const char* h = std::getenv("HOME");
    home = (h && *h) ? h : ".";
#endif
  }
  std::string path = home;
  if (!path.empty() && path.back() != '/' && path.back() != '\\') path += '/';
  path += ".tea_agent/tunnel/token";
  return path;
}

/// The conventional CA bundle: the same file the a2 trusts (it holds the
/// public roots plus the self-signed certificate of the broker).
std::string default_ca_bundle() {
  const char* env = std::getenv("TEA_AGENT_HOME");
  std::string home;
  if (env && *env) {
    home = env;
  } else {
#ifdef _WIN32
    const char* up = std::getenv("USERPROFILE");
    home = (up && *up) ? up : ".";
#else
    const char* h = std::getenv("HOME");
    home = (h && *h) ? h : ".";
#endif
  }
  std::string path = home;
  if (!path.empty() && path.back() != '/' && path.back() != '\\') path += '/';
  path += ".tea_agent/ca-bundle/cacert.pem";
  return path;
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
    if (use_net_) {
      mbedtls_net_free(&net_);
    } else if (fd_ != kTeaInvalidSocket) {
      tea_net_close(fd_);
    }
  }

  /// Plain TCP: the socket is already connected.
  void set_plain(tea_socket_t fd) { fd_ = fd; }

  /// TLS: mbedTLS does the connecting (it owns the socket afterwards).
  bool connect_tls(const std::string& host, uint16_t port, const std::string& ca_file,
                    std::string* err) {
    mbedtls_ssl_init(&ssl_);
    mbedtls_ssl_config_init(&conf_);
    mbedtls_x509_crt_init(&ca_);
    if (!ca_file.empty()) {
      if (mbedtls_x509_crt_parse_file(&ca_, ca_file.c_str()) != 0) {
        *err = "cannot read the CA bundle '" + ca_file + "'";
        return false;
      }
      mbedtls_ssl_conf_ca_chain(&conf_, &ca_, nullptr);
    }
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
    mbedtls_ssl_conf_authmode(&conf_, ca_file.empty() ? MBEDTLS_SSL_VERIFY_NONE
                                                       : MBEDTLS_SSL_VERIFY_REQUIRED);

    mbedtls_net_init(&net_);
    char port_str[16];
    std::snprintf(port_str, sizeof(port_str), "%u", static_cast<unsigned>(port));
    const int rc = mbedtls_net_connect(&net_, host.c_str(), port_str, IPPROTO_TCP);
    if (rc < 0) {
      char buf[160];
      mbedtls_strerror(rc, buf, sizeof(buf));
      *err = "cannot connect to " + host + ":" + port_str + ": " + buf;
      mbedtls_net_free(&net_);
      return false;
    }
    use_net_ = true;
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
    mbedtls_ssl_set_bio(&ssl_, &net_, mbedtls_net_send, mbedtls_net_recv, mbedtls_net_recv_timeout);

    const int hs = mbedtls_ssl_handshake(&ssl_);
    if (hs != 0) {
      char buf[160];
      mbedtls_strerror(hs, buf, sizeof(buf));
      *err = "the TLS handshake failed: " + std::string(buf);
      return false;
    }
    tls_ = true;
    return true;
  }

  ssize_t read(char* buf, size_t len) {
    if (tls_) return mbedtls_ssl_read(&ssl_, reinterpret_cast<unsigned char*>(buf), len);
    return ::recv(fd_, buf, static_cast<int>(len), 0);
  }

  ssize_t write(const char* buf, size_t len) {
    if (tls_) {
      const int rc = mbedtls_ssl_write(&ssl_, reinterpret_cast<const unsigned char*>(buf), len);
      return rc < 0 ? -1 : rc;
    }
    return ::send(fd_, buf, static_cast<int>(len), kTeaNoSignal);
  }

 private:
  tea_socket_t fd_ = kTeaInvalidSocket;
  bool tls_ = false;
  bool use_net_ = false;
  mbedtls_net_context net_{};
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
                   const std::string& ca_file, bool registering) {
    tea_net_init();
    tea_socket_t fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd == kTeaInvalidSocket) return false;
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
      log_line("sidecar: bad broker address %s\n", host.c_str());
      tea_net_close(fd);
      return false;
    }
    if (use_tls) {
      // mbedTLS does the connecting itself (it owns the socket afterwards).
      tea_net_close(fd);
      std::string err;
      if (!link_.connect_tls(host, port, ca_file, &err)) {
        log_line("sidecar: %s\n", err.c_str());
        return false;
      }
    } else {
      if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        log_line("sidecar: cannot reach the broker %s:%u\n", host.c_str(), port);
        tea_net_close(fd);
        return false;
      }
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

int dial_tcp(const std::string& host, uint16_t port) {
  tea_net_init();
  tea_socket_t fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd == kTeaInvalidSocket) return -1;
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
    tea_net_close(fd);
    return -1;
  }
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
  // The default is the public broker: the sidecar should work with nothing
  // but --room and --token.
  std::string broker_host = "103.232.92.54";
  uint16_t broker_port = 8382;
  std::string room;
  std::string token;
  std::string session;
  std::string listen_addr = "127.0.0.1:9931";
  std::string target = "127.0.0.1:8383";
  // The STUN and the TURN server of the public broker. The customer's
  // network is usually a symmetric NAT, so the relay is the normal path -
  // ICE picks between direct and relayed on its own.
  std::string stun_host = "103.232.92.54";
  uint16_t stun_port = 3478;
  std::string turn_host = "103.232.92.54";
  uint16_t turn_port = 3478;
  std::string turn_user = kDefaultTurnUser;
  std::string turn_pass = kDefaultTurnPass;
  std::string ca_file;
  // The public broker port speaks TLS (the room token travels over it), so
  // TLS is on by default; --no-tls is for a plain internal broker.
  bool use_tls = true;
  bool verbose = false;
  // How long to wait for the peer before giving up. The sidecar is meant to be
  // started on demand (the field staff starts it when access is needed), so
  // the default is five minutes, not "forever".
  int wait_timeout = 300;  // seconds; 0 = wait forever

  // The role decides who does what: the agent side sits next to the a2 and
  // registers the room, the client side is the one that connects from the
  // outside.
  bool is_agent = (role == "agent");

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&]() -> std::string { return (i + 1 < argc) ? argv[++i] : std::string(); };
    if (a == "--role") {
      role = next();
      is_agent = (role == "agent");
    }
    else if (a == "--broker") {
      std::string hp = next();
      split_hostport(hp, broker_host, broker_port);
    } else if (a == "--room") room = next();
    else if (a == "--token") token = next();
    else if (a == "--session") session = next();
    else if (a == "--listen") listen_addr = next();
    else if (a == "--target") target = next();
    else if (a == "--stun") {
      std::string hp = next();
      split_hostport(hp, stun_host, stun_port);
    } else if (a == "--turn") {
      std::string hp = next();
      split_hostport(hp, turn_host, turn_port);
    } else if (a == "--turn-user") turn_user = next();
    else if (a == "--turn-pass") turn_pass = next();
    else if (a == "--ca-bundle") ca_file = next();
    else if (a == "--wait-timeout") wait_timeout = std::atoi(next().c_str());
    else if (a == "--tls") use_tls = true;
    else if (a == "--no-tls") use_tls = false;
    else if (a == "--verbose") verbose = true;
    else if (a == "--help" || a == "-h") {
      std::printf(
          "a2_tunnel --role agent|client [--session <code>]\n"
          "          [--broker <host:port>] [--room <name>] [--token <secret>]\n"
          "          [--listen 127.0.0.1:9931] [--target 127.0.0.1:8383]\n"
          "          [--stun <host:port>] [--turn <host:port> --turn-user u --turn-pass p]\n"
          "          [--tls|--no-tls] [--ca-bundle <pem>] [--wait-timeout <seconds>]\n"
          "\n"
          "  --wait-timeout  how long the agent side waits for the peer before giving\n"
          "                  up (default 300 seconds; 0 = wait forever)\n");
      return 0;
    }
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
  // The CA bundle: the conventional one when it is not given explicitly.
  if (use_tls && ca_file.empty()) {
    const std::string conventional = default_ca_bundle();
    std::FILE* probe = std::fopen(conventional.c_str(), "rb");
    if (probe) {
      std::fclose(probe);
      ca_file = conventional;
    } else {
      log_line("sidecar: no CA bundle at %s - the broker's certificate will NOT be "
               "verified. Give --ca-bundle <pem> for a proper check.\n",
               conventional.c_str());
    }
  }
  if (verbose) rtc::InitLogger(rtc::LogLevel::Debug);
  else rtc::InitLogger(rtc::LogLevel::Warning);

  rtc::Configuration cfg;
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
  };

  Signal signal;

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
    });
    channel->onClosed([&, gen] {
      if (gen != pc_gen) return;
      tunnel_ready = false;
      // The peer is gone: the countdown for giving up starts now.
      waiting_since = std::chrono::duration_cast<std::chrono::milliseconds>(
                         std::chrono::steady_clock::now().time_since_epoch())
                         .count();
      log_line("sidecar: the data channel closed (%s)\n", role.c_str());
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
        tea_net_close(it->second);
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
      // The peer wants a fresh offer. If we already offered for this arrival,
      // this is the same request arriving twice: ignore it.
      if (is_agent) {
        if (offered_epoch == peer_epoch) {
          log_line("sidecar: need-offer, but we already offered\n");
          return;
        }
        log_line("sidecar: offering again\n");
        offered_epoch = peer_epoch;
        setup_pc();
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
  std::string listen_host;
  uint16_t listen_port = 0;
  int lfd = -1;
  start_listener = [&]() {
    if (lfd >= 0) return;
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
                            is_agent)) {
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

  // The peer connection is built **after** the signalling link is up: the
  // offer is produced the moment the channel is created, and an offer
  // produced before the link exists is a message nobody receives.
  if (!signal.connect_to(broker_host, broker_port, room, token, role, use_tls, ca_file, is_agent))
    return 1;
  setup_pc();

  log_line("sidecar: %s side, room %s, broker %s:%u\n", role.c_str(), room.c_str(),
               broker_host.c_str(), broker_port);

  if (!is_agent) start_listener();  // the local proxy is up even before the tunnel

  // 「就绪」要说人话：部署的人要知道**外面怎么连进来**。
  // client 侧打印一段可以直接粘进对方 a2 的 settings.peers 的配置；
  // agent 侧打印房间和令牌（两边必须一致）。
  std::fprintf(stderr, "---------------------------------------------------------------\n");
  if (is_agent) {
    log_line("sidecar: ready. Waiting in room \"%s\" for the other side.\n",
                 room.c_str());
    std::fprintf(stderr, "  broker        : %s:%u\n", broker_host.c_str(), broker_port);
    std::fprintf(stderr, "  room token    : %s\n", token.c_str());
    std::fprintf(stderr, "  target        : %s\n", target.c_str());
    std::fprintf(stderr, "  give this to whoever connects from the outside:\n");
    std::fprintf(stderr, "      a2_tunnel --role client --session %s\n",
                 session.empty() ? token.c_str() : session.c_str());
    std::fprintf(stderr, "  (the broker, TLS, the CA bundle and the STUN/TURN servers are\n"
                         "   the defaults; add --ca-bundle <pem> if that machine has no\n"
                         "   ~/.tea_agent/ca-bundle/cacert.pem)\n");
    std::fprintf(stderr, "  (it will listen locally and be reachable through the tunnel)\n");
    if (wait_timeout > 0) {
      std::fprintf(stderr, "  giving up in %d seconds if nobody connects.\n", wait_timeout);
    }
  } else {
    log_line("sidecar: ready. To reach it from the outside:\n");
    std::fprintf(stderr, "  add this to the settings.peers of the a2 that drives it:\n");
    std::fprintf(stderr, "      peers:\n");
    std::fprintf(stderr, "        %s:\n", room.c_str());
    std::fprintf(stderr, "          url: http://%s:%u\n", listen_host.c_str(), listen_port);
    std::fprintf(stderr, "          token: %s\n", token.c_str());
    std::fprintf(stderr, "          description: %s, reached through the tunnel\n", room.c_str());
    std::fprintf(stderr, "  toolkit_peer can then talk to it (it answers with its own model).\n");
    std::fprintf(stderr, "  The browser on this machine can open http://%s:%u/ for its web UI.\n",
                 listen_host.c_str(), listen_port);
  }
  std::fprintf(stderr, "  ! the lines above contain the token - do not share this log.\n");
  std::fprintf(stderr, "---------------------------------------------------------------\n");

  while (true) std::this_thread::sleep_for(std::chrono::seconds(3600));
}
