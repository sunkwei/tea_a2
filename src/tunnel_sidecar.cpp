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
// What it does:
//   * dials the broker over TCP and joins a room (the signalling);
//   * negotiates a WebRTC data channel through the broker's STUN/TURN
//     (ICE picks the direct path when it can punch, the relay otherwise);
//   * the client side listens on --listen and forwards every accepted TCP
//     connection into the tunnel; the agent side dials --target and pipes.
//
// The bytes inside the tunnel are raw TCP: the client side is a plain TCP
// proxy, so an existing `peers:` entry (url: http://127.0.0.1:9931) works
// against it without changing anything.
//
// Frame format on the data channel: [u8 type][u32 length][payload]
//   1 OPEN  payload: u32 connection id
//   2 DATA  payload: u32 connection id + bytes
//   3 CLOSE payload: u32 connection id
//
#include <arpa/inet.h>
#include <netinet/in.h>
#include <netinet/tcp.h>
#include <sys/socket.h>
#include <unistd.h>

#include <rtc/rtc.hpp>

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
class Signal {
 public:
  bool connect_to(const std::string& host, uint16_t port, const std::string& room,
                   const std::string& token, const std::string& role) {
    int fd = ::socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return false;
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
      std::fprintf(stderr, "sidecar: bad broker address %s\n", host.c_str());
      ::close(fd);
      return false;
    }
    if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
      std::fprintf(stderr, "sidecar: cannot reach the broker %s:%u\n", host.c_str(), port);
      ::close(fd);
      return false;
    }
    fd_ = fd;
    json join;
    join["room"] = room;
    join["token"] = token;
    join["role"] = role;
    send_line(join);
    std::thread([this] { reader(); }).detach();
    return true;
  }

  void send_line(const json& j) {
    std::string line = j.dump() + "\n";
    std::lock_guard<std::mutex> lock(out_);
    if (fd_ >= 0) ::send(fd_, line.data(), line.size(), MSG_NOSIGNAL);
  }

  void on_message(std::function<void(const json&)> cb) { on_message_ = std::move(cb); }

 private:
  void reader() {
    std::string buf;
    char chunk[4096];
    while (true) {
      ssize_t n = ::recv(fd_, chunk, sizeof(chunk), 0);
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
    std::fprintf(stderr, "sidecar: the signalling link closed\n");
  }

  int fd_ = -1;
  std::mutex out_;
  std::function<void(const json&)> on_message_;
};

int dial_tcp(const std::string& host, uint16_t port) {
  int fd = ::socket(AF_INET, SOCK_STREAM, 0);
  if (fd < 0) return -1;
  sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_port = htons(port);
  if (::inet_pton(AF_INET, host.c_str(), &addr.sin_addr) != 1) {
    ::close(fd);
    return -1;
  }
  if (::connect(fd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
    ::close(fd);
    return -1;
  }
  int one = 1;
  ::setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  return fd;
}

}  // namespace

int main(int argc, char** argv) {
  std::string role = "client";
  std::string broker_host = "127.0.0.1";
  uint16_t broker_port = 3479;
  std::string room = "default";
  std::string token;
  std::string listen_addr = "127.0.0.1:9931";
  std::string target = "127.0.0.1:8383";
  std::string stun_host = "127.0.0.1";
  uint16_t stun_port = 3478;
  std::string turn_host;
  uint16_t turn_port = 3478;
  std::string turn_user = "a2";
  std::string turn_pass;
  bool verbose = false;

  for (int i = 1; i < argc; ++i) {
    std::string a = argv[i];
    auto next = [&]() -> std::string { return (i + 1 < argc) ? argv[++i] : std::string(); };
    if (a == "--role") role = next();
    else if (a == "--broker") {
      std::string hp = next();
      split_hostport(hp, broker_host, broker_port);
    } else if (a == "--room") room = next();
    else if (a == "--token") token = next();
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
    else if (a == "--verbose") verbose = true;
    else if (a == "--help" || a == "-h") {
      std::printf(
          "a2_tunnel --role agent|client --broker <host:port> --room <name> --token <secret>\n"
          "          [--listen 127.0.0.1:9931] [--target 127.0.0.1:8383]\n"
          "          [--stun <host:port>] [--turn <host:port> --turn-user u --turn-pass p]\n");
      return 0;
    }
  }
  if (token.empty()) {
    std::fprintf(stderr, "sidecar: --token is required\n");
    return 2;
  }
  if (verbose) rtc::InitLogger(rtc::LogLevel::Debug);
  else rtc::InitLogger(rtc::LogLevel::Warning);

  const bool is_agent = (role == "agent");

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
      std::fprintf(stderr, "sidecar: send failed: %s\n", e.what());
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

  auto attach_channel = [&](std::shared_ptr<rtc::DataChannel> channel) {
    dc = channel;
    channel->onOpen([&] {
      std::fprintf(stderr, "sidecar: the data channel is open (%s)\n", role.c_str());
      try {
        rtc::Candidate local, remote;
        if (pc && pc->getSelectedCandidatePair(&local, &remote)) {
          const std::string la = local.address().value_or("?");
          const std::string ra = remote.address().value_or("?");
          std::fprintf(stderr, "sidecar:   path: local %s:%d <-> remote %s:%d (%s)\n", la.c_str(),
                       local.port(), ra.c_str(), remote.port(),
                       remote.type() == rtc::Candidate::Type::Relayed
                           ? "RELAYED by the broker"
                           : "direct");
        }
      } catch (const std::exception& e) {
        std::fprintf(stderr, "sidecar:   (cannot read the selected pair: %s)\n", e.what());
      }
      if (!is_agent) start_listener();
    });
    channel->onMessage([&](std::variant<rtc::binary, std::string> msg) {
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
          std::fprintf(stderr, "sidecar: cannot reach the target %s\n", target.c_str());
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
        const ssize_t n = ::send(it->second, payload + 4, len - 4, MSG_NOSIGNAL);
        (void)n;
        return;
      }

      if (type == kClose) {
        if (len < 4) return;
        const unsigned int id = get_u32(payload);
        std::lock_guard<std::mutex> lock(mu);
        auto it = conns.find(id);
        if (it == conns.end()) return;
        ::close(it->second);
        conns.erase(it);
      }
    });
  };

  // Build (or rebuild) the peer connection. Rebuilding is what makes a
  // restarted peer survivable: the old ICE credentials are dead, so both sides
  // start a fresh negotiation instead of talking past each other.
  auto setup_pc = [&]() {
    remote_ready = false;
    pending_candidates.clear();
    local_candidates.clear();
    local_description.clear();
    pc = std::make_shared<rtc::PeerConnection>(cfg);

    pc->onLocalDescription([&](rtc::Description d) {
      const bool offer = (d.type() == rtc::Description::Type::Offer);
      local_description = std::string(d);
      local_description_type = offer ? "offer" : "answer";
      signal.send_line({{"type", local_description_type}, {"sdp", local_description}});
    });
    pc->onLocalCandidate([&](rtc::Candidate c) {
      local_candidates.push_back(std::string(c));
      signal.send_line({{"type", "candidate"}, {"sdp", std::string(c)}});
    });
    pc->onDataChannel([&](std::shared_ptr<rtc::DataChannel> channel) { attach_channel(channel); });

    if (is_agent) {
      // The agent creates the channel; creating it makes libdatachannel
      // produce the offer right away.
      attach_channel(pc->createDataChannel("a2"));
    }
  };

  signal.on_message([&](const json& j) {
    const std::string type = j.value("type", "");
    if (type == "offer" || type == "answer") {
      // One side offers, the other answers. Getting this backwards makes
      // libdatachannel refuse the description ("Incompatible roles"), so the
      // roles are checked here rather than hoped for.
      if (is_agent && type == "offer") {
        std::fprintf(stderr, "sidecar: the peer offered (we are the offerer), offering again\n");
        setup_pc();
        return;
      }
      if (!is_agent && type == "answer") {
        std::fprintf(stderr, "sidecar: an answer arrived (we are the answerer), waiting for an offer\n");
        return;
      }
      try {
        if (!pc) setup_pc();
        // A fresh offer from a peer that (re)started: throw the old
        // negotiation away and start over with it.
        if (type == "offer" && remote_ready) {
          std::fprintf(stderr, "sidecar: the peer offered again, renegotiating\n");
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
        std::fprintf(stderr, "sidecar: bad %s: %s\n", type.c_str(), e.what());
        // An answer we cannot use: the peer is talking about an offer we
        // already threw away. Offer again and let it answer the new one.
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
      std::fprintf(stderr, "sidecar: joined room %s (peer present: %s)\n", room.c_str(),
                   peer ? "yes" : "no");
      if (peer) {
        peer_epoch++;
        if (is_agent) {
          // We are the offerer and the peer is already here: offer now.
          std::fprintf(stderr, "sidecar: the peer is here, offering\n");
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
      std::fprintf(stderr, "sidecar: the peer joined, offering again\n");
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
          std::fprintf(stderr, "sidecar: need-offer, but we already offered\n");
          return;
        }
        std::fprintf(stderr, "sidecar: offering again\n");
        offered_epoch = peer_epoch;
        setup_pc();
      }
      return;
    }
    if (type == "error") {
      std::fprintf(stderr, "sidecar: broker says: %s\n", j.value("message", "").c_str());
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
    if (lfd < 0) return;
    int one = 1;
    ::setsockopt(lfd, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_port = htons(listen_port);
    ::inet_pton(AF_INET, listen_host.c_str(), &addr.sin_addr);
    if (::bind(lfd, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0 ||
        ::listen(lfd, 16) != 0) {
      std::fprintf(stderr, "sidecar: cannot listen on %s\n", listen_addr.c_str());
      ::close(lfd);
      lfd = -1;
      return;
    }
    std::fprintf(stderr, "sidecar: listening on %s\n", listen_addr.c_str());
    std::thread([&]() {
      while (true) {
        int fd = ::accept(lfd, nullptr, nullptr);
        if (fd < 0) continue;
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

  // The peer connection is built **after** the signalling link is up: the
  // offer is produced the moment the channel is created, and an offer
  // produced before the link exists is a message nobody receives.
  if (!signal.connect_to(broker_host, broker_port, room, token, role)) return 1;
  setup_pc();
  std::fprintf(stderr, "sidecar: %s side, room %s, broker %s:%u\n", role.c_str(), room.c_str(),
               broker_host.c_str(), broker_port);

  if (!is_agent) start_listener();  // the local proxy is up even before the tunnel

  while (true) std::this_thread::sleep_for(std::chrono::seconds(3600));
}
