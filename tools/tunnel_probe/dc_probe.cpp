// dc_probe -- the smallest thing that proves the idea:
//   two peer connections in one process, ICE, one data channel,
//   and an HTTP/1.1 request/response riding inside it.
//
// It is the prototype behind docs/tunnel.md: if this prints a 200 OK over a
// data channel, then "HTTP over a WebRTC data channel" is a fact, not a plan.
//
// Build it with tools/tunnel_probe/build_probe.sh (it pulls the dependencies
// and builds them statically; no system packages needed).

#include <rtc/rtc.hpp>

#include <chrono>
#include <iostream>
#include <string>
#include <thread>
#include <variant>

static std::string to_text(const std::variant<rtc::binary, std::string>& msg) {
  return std::visit(
      [](auto&& v) {
        if constexpr (std::is_same_v<std::decay_t<decltype(v)>, std::string>) return v;
        else return std::string("<binary>");
      },
      msg);
}

int main() {
  rtc::InitLogger(rtc::LogLevel::Warning);
  rtc::Configuration cfg;
  // A real deployment would put a STUN server here; the loopback demo only
  // needs host candidates to exercise ICE + DTLS + SCTP.
  // cfg.iceServers.emplace_back("stun:stun.l.google.com:19302");

  rtc::PeerConnection server(cfg);
  rtc::PeerConnection client(cfg);

  // Candidates can arrive before the remote description; hold them back until
  // it is in, otherwise libdatachannel drops them ("Got a remote candidate
  // without remote description").
  std::vector<rtc::Candidate> server_pending, client_pending;
  bool server_have_rd = false, client_have_rd = false;

  server.onLocalCandidate([&](rtc::Candidate c) {
    if (client_have_rd) {
      client.addRemoteCandidate(c);
    } else {
      server_pending.push_back(c);
    }
  });
  client.onLocalCandidate([&](rtc::Candidate c) {
    if (server_have_rd) {
      server.addRemoteCandidate(c);
    } else {
      client_pending.push_back(c);
    }
  });

  // The signalling, the way a rendezvous server would carry it. This has to
  // be registered BEFORE the data channel is created: creating a data channel
  // makes libdatachannel produce the offer right away, and a handler that is
  // registered afterwards never sees it.
  server.onLocalDescription([&](rtc::Description d) {
    std::cout << "[server] local: " << d.typeString() << "\n";
    if (d.type() == rtc::Description::Type::Answer) {
      client.setRemoteDescription(d);
      client_have_rd = true;
      for (auto& c : client_pending) client.addRemoteCandidate(c);
      client_pending.clear();
    }
  });
  client.onLocalDescription([&](rtc::Description d) {
    std::cout << "[client] local: " << d.typeString() << "\n";
    if (d.type() == rtc::Description::Type::Offer) {
      server.setRemoteDescription(d);
      server_have_rd = true;
      for (auto& c : server_pending) server.addRemoteCandidate(c);
      server_pending.clear();
      // The server side answers by itself once it has the offer; asking for
      // an answer when it is already stable is refused ("Unexpected local
      // description type answer in signaling state stable").
      if (server.signalingState() == rtc::PeerConnection::SignalingState::HaveRemoteOffer) {
        server.setLocalDescription(rtc::Description::Type::Answer);
      }
    }
  });

  // The server side of the tunnel: it accepts the data channel and answers
  // whatever HTTP request arrives inside it.
  server.onDataChannel([](std::shared_ptr<rtc::DataChannel> dc) {
    std::cout << "[server] data channel accepted: " << dc->label() << "\n";
    dc->onOpen([]() { std::cout << "[server] channel open\n"; });
    dc->onMessage([dc](std::variant<rtc::binary, std::string> msg) {
      std::string s = to_text(msg);
      std::cout << "[server] got " << s.size() << " bytes: " << s.substr(0, 60) << "\n";
      std::string resp =
          "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
          "Content-Length: 27\r\n\r\n{\"version\":\"1.1.0\",\"ok\":true}";
      dc->send(resp);
    });
  });

  // Creating the data channel is enough: libdatachannel produces the offer
  // by itself (negotiationneeded). Asking for a second one is refused
  // ("Unexpected local description in signaling state have-local-offer").
  auto dc = client.createDataChannel("a2");
  dc->onOpen([&]() {
    std::cout << "[client] data channel open\n";
    dc->send("GET /api/health HTTP/1.1\r\nHost: inner-a2\r\n\r\n");
  });
  dc->onMessage([](std::variant<rtc::binary, std::string> msg) {
    std::cout << "[client] response:\n" << to_text(msg) << "\n";
  });
  dc->onClosed([]() { std::cout << "[client] closed\n"; });

  std::this_thread::sleep_for(std::chrono::seconds(6));
  return 0;
}
