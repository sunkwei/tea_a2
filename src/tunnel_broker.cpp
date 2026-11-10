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
//   a2_broker --stun-port 3478 --signal-port 3479 \
//              --realm a2 --turn-user a2 --turn-pass <secret> --token <secret>
//
// The signalling protocol (one JSON object per line, both ways):
//
//   -> {"room":"plant-a","token":"...","role":"agent"}
//   <- {"type":"joined","peer":true|false}
//   -> {"type":"offer"|"answer"|"candidate","sdp":"..."}   (relayed to the peer)
//
#include <juice/juice.h>
#include <nlohmann/json.hpp>

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


#include "tunnel_net.h"

namespace {

// ---- one connected signalling client ---------------------------------------
struct Peer {
  tea_socket_t fd = kTeaInvalidSocket;
  std::string role;
  std::string room;
  std::mutex out;        // serialises writes to fd
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
  const int n = ::send(p->fd, line.data(), static_cast<int>(line.size()), kTeaNoSignal);
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
    ssize_t n = ::recv(p->fd, chunk, sizeof(chunk), 0);
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
  tea_net_close(p->fd);
}

// ---- the TURN/STUN server (libjuice) ---------------------------------------
bool start_stun_turn(uint16_t port, const std::string& realm, const std::string& user,
                      const std::string& pass, uint16_t relay_begin, uint16_t relay_end) {
  juice_server_credentials_t creds;
  creds.username = user.c_str();
  creds.password = pass.c_str();
  creds.allocations_quota = 64;

  juice_server_config_t config;
  config.credentials = &creds;
  config.credentials_count = 1;
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

}  // namespace

int main(int argc, char** argv) {
  uint16_t stun_port = 3478;
  uint16_t signal_port = 3479;   // internal; nginx fronts it on the public port
  std::string signal_bind = "127.0.0.1";
  uint16_t relay_begin = 49160;
  uint16_t relay_end = 49260;
  std::string realm = "a2";
  std::string turn_user = kDefaultTurnUser;
  std::string turn_pass = kDefaultTurnPass;
  std::string token;

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&]() -> std::string { return (i + 1 < argc) ? argv[++i] : std::string(); };
    if (a == "--stun-port") stun_port = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--signal-port") signal_port = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--signal-bind") signal_bind = next();
    else if (a == "--relay-begin") relay_begin = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--relay-end") relay_end = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--realm") realm = next();
    else if (a == "--turn-user") turn_user = next();
    else if (a == "--turn-pass") turn_pass = next();
    else if (a == "--token") token = next();
    else if (a == "--help" || a == "-h") {
      std::printf(
          "a2_broker --stun-port 3478 --signal-port 8382 --signal-bind 127.0.0.1 \\\n"
          "          --realm a2\n"
          "          (the TURN credentials are built in; the room token comes from\n"
          "           --token or from ~/.tea_agent/tunnel/token)\n");
      return 0;
    }
  }
  // The room token is optional on the broker: without one every room learns
  // its token from the side that registers it (the agent side generates a
  // fresh one on every start). With --token the broker pins one token for
  // every room.
  if (token.empty()) {
    std::string from_file;
    if (read_token_file(&from_file)) token = from_file;
  }
  if (turn_pass.empty()) {
    std::fprintf(stderr, "broker: no TURN password\n");
    return 2;
  }
  g_token = token;

  if (!start_stun_turn(stun_port, realm, turn_user, turn_pass, relay_begin, relay_end)) return 1;

  int lfd = listen_tcp(signal_port, signal_bind);
  if (lfd < 0) {
    std::fprintf(stderr, "broker: cannot listen on tcp/%u\n", signal_port);
    return 1;
  }
  std::printf("broker: signalling on tcp/%u (bound to %s)\n", signal_port, signal_bind.c_str());
  std::fflush(stdout);

  while (true) {
    tea_socket_t fd = ::accept(lfd, nullptr, nullptr);
    if (fd == kTeaInvalidSocket) continue;
    tea_net_set_nodelay(fd);
    auto p = std::make_shared<Peer>();
    p->fd = fd;
    std::thread(peer_loop, p).detach();
  }
}
