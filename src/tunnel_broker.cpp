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
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <juice/juice.h>
#include <nlohmann/json.hpp>

#include <atomic>
#include <cstdio>
#include <cstring>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using json = nlohmann::json;

namespace {

// ---- one connected signalling client ---------------------------------------
struct Peer {
  int fd = -1;
  std::string role;
  std::string room;
  std::mutex out;        // serialises writes to fd
  std::atomic<bool> alive{true};
};

std::mutex g_rooms_mu;
std::map<std::string, std::vector<std::shared_ptr<Peer>>> g_rooms;

std::string g_token;

void send_line(const std::shared_ptr<Peer>& p, const json& j) {
  if (!p || !p->alive) return;
  std::string line = j.dump() + "\n";
  std::lock_guard<std::mutex> lock(p->out);
  ssize_t n = ::send(p->fd, line.data(), line.size(), MSG_NOSIGNAL);
  if (n != static_cast<ssize_t>(line.size())) p->alive = false;
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
    if (g_token.empty() || token != g_token) {
      send_line(p, {{"type", "error"}, {"message", "bad token"}});
      p->alive = false;
      return;
    }
    p->room = j.value("room", "");
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
  ::close(p->fd);
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
int listen_tcp(uint16_t port) {
  int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return -1;
  int one = 1;
  ::setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = INADDR_ANY;
  addr.sin_port = htons(port);
  if (::bind(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    ::close(fd);
    return -1;
  }
  if (::listen(fd, 16) != 0) {
    ::close(fd);
    return -1;
  }
  return fd;
}

}  // namespace

int main(int argc, char** argv) {
  uint16_t stun_port = 3478;
  uint16_t signal_port = 3479;
  uint16_t relay_begin = 49160;
  uint16_t relay_end = 49260;
  std::string realm = "a2";
  std::string turn_user = "a2";
  std::string turn_pass;
  std::string token;

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&]() -> std::string { return (i + 1 < argc) ? argv[++i] : std::string(); };
    if (a == "--stun-port") stun_port = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--signal-port") signal_port = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--relay-begin") relay_begin = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--relay-end") relay_end = static_cast<uint16_t>(std::stoi(next()));
    else if (a == "--realm") realm = next();
    else if (a == "--turn-user") turn_user = next();
    else if (a == "--turn-pass") turn_pass = next();
    else if (a == "--token") token = next();
    else if (a == "--help" || a == "-h") {
      std::printf(
          "a2_broker --stun-port 3478 --signal-port 3479 --realm a2 \\\n"
          "          --turn-user a2 --turn-pass <secret> --token <secret>\n");
      return 0;
    }
  }
  if (turn_pass.empty() || token.empty()) {
    std::fprintf(stderr, "broker: --turn-pass and --token are required\n");
    return 2;
  }
  g_token = token;

  if (!start_stun_turn(stun_port, realm, turn_user, turn_pass, relay_begin, relay_end)) return 1;

  int lfd = listen_tcp(signal_port);
  if (lfd < 0) {
    std::fprintf(stderr, "broker: cannot listen on tcp/%u\n", signal_port);
    return 1;
  }
  std::printf("broker: signalling on tcp/%u\n", signal_port);
  std::fflush(stdout);

  while (true) {
    int fd = ::accept(lfd, nullptr, nullptr);
    if (fd < 0) continue;
    int one = 1;
    ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    auto p = std::make_shared<Peer>();
    p->fd = fd;
    std::thread(peer_loop, p).detach();
  }
}
