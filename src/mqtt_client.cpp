// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_client.cpp -- one TLS connection, one reader/writer thread.
//
// The socket has exactly one writer (this thread): publish() only appends to
// an outbox. mbedTLS does not allow one ssl_context to be used from two
// threads at once, so the single-owner rule is not a style choice.
#include "mqtt_client.h"

#include <chrono>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <thread>
#include <utility>
#include <vector>

#include "log.h"
#include "mqtt_codec.h"
#include "tls.h"
#include "util.h"

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#include <iphlpapi.h>
#include <ipifcons.h>
#else
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <net/if.h>
#include <sys/ioctl.h>
#include <errno.h>
#include <fcntl.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <unistd.h>
#endif

namespace tea {
namespace {

#ifdef _WIN32
using mqtt_socket_t = SOCKET;
using mqtt_socklen_t = int;
constexpr mqtt_socket_t kBadMqttSocket = INVALID_SOCKET;
void close_mqtt_socket(mqtt_socket_t s) {
  if (s != kBadMqttSocket) ::closesocket(s);
}
#else
using mqtt_socket_t = int;
using mqtt_socklen_t = socklen_t;
constexpr mqtt_socket_t kBadMqttSocket = -1;
void close_mqtt_socket(mqtt_socket_t s) {
  if (s != kBadMqttSocket) ::close(s);
}
#endif

constexpr size_t kMaxPacketBytes = 300 * 1024;

// ---------------------------------------------------------------------------
// The heartbeat
// ---------------------------------------------------------------------------
// A PINGREQ goes out every **random** one to five minutes. Many devices sit
// behind the same broker; if they all pinged at the same second the broker
// would get a burst every minute, and a hundred devices would look like a
// thundering herd. The interval is drawn again after every ping, so the
// devices drift apart.
//
// The keepalive advertised in CONNECT has to cover the longest gap: the
// broker drops a session that stays silent for 1.5x keepalive
// (src/mqtt_server.cpp). So the advertised keepalive is the upper bound
// (five minutes) and a ping always arrives well before the limit.
constexpr int kPingMinSeconds = 60;
constexpr int kPingMaxSeconds = 300;

/// The window the next ping may fall into, clamped by the advertised
/// keepalive: a caller that lowers the keepalive cannot promise the broker
/// more silence than it tolerates.
std::pair<int, int> ping_window(int keepalive_s) {
  const int ceiling =
      (keepalive_s > 0 && keepalive_s < kPingMaxSeconds) ? keepalive_s : kPingMaxSeconds;
  const int floor = (kPingMinSeconds < ceiling) ? kPingMinSeconds : ceiling;
  return std::make_pair(floor, ceiling);
}

bool set_send_timeout(mqtt_socket_t s, int ms) {
#ifdef _WIN32
  DWORD tv = static_cast<DWORD>(ms);
  return ::setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&tv),
                      sizeof(tv)) == 0;
#else
  timeval tv;
  tv.tv_sec = ms / 1000;
  tv.tv_usec = (ms % 1000) * 1000;
  return ::setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv)) == 0;
#endif
}

mqtt_socket_t connect_tcp(const std::string& host, int port, int timeout_ms, std::string* err) {
  addrinfo hints;
  std::memset(&hints, 0, sizeof(hints));
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  addrinfo* list = nullptr;
  const std::string service = std::to_string(port);
  if (::getaddrinfo(host.c_str(), service.c_str(), &hints, &list) != 0 || !list) {
    *err = "cannot resolve " + host;
    return kBadMqttSocket;
  }
  mqtt_socket_t result = kBadMqttSocket;
  std::string last;
  for (addrinfo* it = list; it; it = it->ai_next) {
    mqtt_socket_t s = ::socket(it->ai_family, it->ai_socktype, it->ai_protocol);
    if (s == kBadMqttSocket) continue;
#ifdef _WIN32
    u_long nonblocking = 1;
    ::ioctlsocket(s, FIONBIO, &nonblocking);
#else
    const int flags = ::fcntl(s, F_GETFL, 0);
    ::fcntl(s, F_SETFL, flags | O_NONBLOCK);
#endif
    bool ok = ::connect(s, it->ai_addr, static_cast<mqtt_socklen_t>(it->ai_addrlen)) == 0;
    if (!ok) {
      fd_set write_set;
      FD_ZERO(&write_set);
      FD_SET(s, &write_set);
      timeval tv;
      tv.tv_sec = timeout_ms / 1000;
      tv.tv_usec = (timeout_ms % 1000) * 1000;
#ifdef _WIN32
      const int selected = ::select(0, nullptr, &write_set, nullptr, &tv);
#else
      const int selected = ::select(static_cast<int>(s) + 1, nullptr, &write_set, nullptr, &tv);
#endif
      if (selected > 0) {
        int so_error = 0;
        mqtt_socklen_t len = static_cast<mqtt_socklen_t>(sizeof(so_error));
        if (::getsockopt(s, SOL_SOCKET, SO_ERROR, reinterpret_cast<char*>(&so_error), &len) == 0 &&
            so_error == 0) {
          ok = true;
        } else {
          last = std::string("connect failed: ") + std::to_string(so_error);
        }
      } else {
        last = "connection timed out";
      }
    }
#ifdef _WIN32
    u_long blocking = 0;
    ::ioctlsocket(s, FIONBIO, &blocking);
#else
    ::fcntl(s, F_SETFL, flags);
#endif
    if (ok) {
      result = s;
      break;
    }
    close_mqtt_socket(s);
  }
  ::freeaddrinfo(list);
  if (result == kBadMqttSocket) {
    *err = last.empty() ? ("cannot connect to " + host) : last;
  }
  return result;
}

/// A UDP socket that never sends a packet: connect() only picks the local
/// address the kernel would use for the default route.
std::string find_primary_ipv4() {
  mqtt_socket_t s = ::socket(AF_INET, SOCK_DGRAM, 0);
  if (s == kBadMqttSocket) return std::string();
  sockaddr_in probe;
  std::memset(&probe, 0, sizeof(probe));
  probe.sin_family = AF_INET;
  probe.sin_port = htons(53);
  probe.sin_addr.s_addr = htonl(0x08080808u);
  std::string result;
  if (::connect(s, reinterpret_cast<sockaddr*>(&probe), sizeof(probe)) == 0) {
    sockaddr_in local;
    mqtt_socklen_t length = static_cast<mqtt_socklen_t>(sizeof(local));
    if (::getsockname(s, reinterpret_cast<sockaddr*>(&local), &length) == 0) {
      char text[INET_ADDRSTRLEN] = {0};
      if (::inet_ntop(AF_INET, &local.sin_addr, text, sizeof(text))) result = text;
    }
  }
  close_mqtt_socket(s);
  if (result == "0.0.0.0") result.clear();
  return result;
}

}  // namespace

std::string primary_ipv4() { return find_primary_ipv4(); }

#ifdef _WIN32
std::string find_primary_mac() {
  ULONG size = 0;
  DWORD rc = GetAdaptersAddresses(AF_INET, GAA_FLAG_SKIP_ANYCAST | GAA_FLAG_SKIP_MULTICAST |
                                               GAA_FLAG_SKIP_DNS_SERVER,
                                  nullptr, nullptr, &size);
  if (rc != ERROR_BUFFER_OVERFLOW) return std::string();
  std::vector<unsigned char> buffer(size);
  IP_ADAPTER_ADDRESSES* addresses = reinterpret_cast<IP_ADAPTER_ADDRESSES*>(buffer.data());
  if (GetAdaptersAddresses(AF_INET, 0, nullptr, addresses, &size) != NO_ERROR) {
    return std::string();
  }
  const std::string wanted = find_primary_ipv4();
  for (IP_ADAPTER_ADDRESSES* adapter = addresses; adapter; adapter = adapter->Next) {
    if (adapter->IfType == IF_TYPE_SOFTWARE_LOOPBACK) continue;
    if (adapter->PhysicalAddressLength < 6) continue;
    bool match = wanted.empty();
    for (IP_ADAPTER_UNICAST_ADDRESS* unicast = adapter->FirstUnicastAddress; unicast;
         unicast = unicast->Next) {
      if (!unicast->Address.lpSockaddr) continue;
      if (unicast->Address.lpSockaddr->sa_family != AF_INET) continue;
      const sockaddr_in* ipv4 = reinterpret_cast<const sockaddr_in*>(unicast->Address.lpSockaddr);
      char text[INET_ADDRSTRLEN] = {0};
      if (!::inet_ntop(AF_INET, &ipv4->sin_addr, text, sizeof(text))) continue;
      if (text == wanted) {
        match = true;
        break;
      }
    }
    if (!match) continue;
    char hex[13];
    std::snprintf(hex, sizeof(hex), "%02x%02x%02x%02x%02x%02x", adapter->PhysicalAddress[0],
                  adapter->PhysicalAddress[1], adapter->PhysicalAddress[2],
                  adapter->PhysicalAddress[3], adapter->PhysicalAddress[4],
                  adapter->PhysicalAddress[5]);
    return hex;
  }
  return std::string();
}
#else
std::string find_primary_mac() {
  const std::string wanted = find_primary_ipv4();
  ifaddrs* list = nullptr;
  if (getifaddrs(&list) != 0) return std::string();
  std::string result;
  for (ifaddrs* it = list; it; it = it->ifa_next) {
    if (!it->ifa_addr || it->ifa_addr->sa_family != AF_INET) continue;
    if (!it->ifa_name) continue;
    const sockaddr_in* addr = reinterpret_cast<const sockaddr_in*>(it->ifa_addr);
    char text[INET_ADDRSTRLEN] = {0};
    if (!::inet_ntop(AF_INET, &addr->sin_addr, text, sizeof(text))) continue;
    if (!wanted.empty() && wanted != text) continue;
    const int fd = ::socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) break;
    ifreq request;
    std::memset(&request, 0, sizeof(request));
    std::strncpy(request.ifr_name, it->ifa_name, IFNAMSIZ - 1);
    if (::ioctl(fd, SIOCGIFHWADDR, &request) == 0) {
      const unsigned char* mac = reinterpret_cast<const unsigned char*>(request.ifr_hwaddr.sa_data);
      char hex[13];
      std::snprintf(hex, sizeof(hex), "%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2], mac[3],
                    mac[4], mac[5]);
      result = hex;
    }
    ::close(fd);
    if (!result.empty()) break;
  }
  ::freeifaddrs(list);
  if (result == "000000000000") result.clear();
  return result;
}
#endif

std::string primary_mac() { return find_primary_mac(); }

struct MqttClient::Impl {
  Config config;
  MessageHandler handler;
  std::thread worker;
  std::atomic<bool> stopping{false};
  std::atomic<bool> connected{false};
  std::atomic<bool> started{false};
  uint16_t next_packet_id = 1;
  std::string connected_register_topic;

  mutable std::mutex mutex;
  std::vector<std::string> outbox;
  std::string error;

  void set_error(const std::string& text) {
    std::lock_guard<std::mutex> lock(mutex);
    error = text;
  }

  std::string get_error() const {
    std::lock_guard<std::mutex> lock(mutex);
    return error;
  }

  std::string take_registration() {
    if (!config.register_payload) return std::string();
    return config.register_payload();
  }

  uint16_t packet_id() {
    const uint16_t id = next_packet_id++;
    if (next_packet_id == 0) next_packet_id = 1;
    return id;
  }

  bool write_frame(TlsStream* stream, const std::string& frame, std::string* err) {
    return stream->write(frame.data(), frame.size(), err);
  }

  /// Read one packet, waiting at most `timeout_ms`. Returns false on a real
  /// connection error; `got` says whether a packet arrived before the timeout.
  bool pump(TlsStream* stream, std::string* buffer, int timeout_ms, mqtt::Packet* packet,
            bool* got, std::string* err) {
    *got = false;
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(timeout_ms);
    while (!stopping.load()) {
      if (!stream->has_pending()) {
        const int ready = stream->wait_readable(50);
        if (ready <= 0) {
          if (std::chrono::steady_clock::now() >= deadline) return true;
          continue;
        }
      }
      char chunk[8192];
      const int n = stream->read(chunk, sizeof(chunk), err);
      if (n <= 0) {
        if (err->empty()) *err = "closed by the broker";
        return false;
      }
      buffer->append(chunk, static_cast<size_t>(n));
      size_t consumed = 0;
      const mqtt::ParseStatus status =
          mqtt::parse_packet(*buffer, &consumed, packet, kMaxPacketBytes);
      if (status == mqtt::ParseStatus::error) {
        *err = "the broker sent a packet that is too large or malformed";
        return false;
      }
      if (status == mqtt::ParseStatus::incomplete) {
        if (buffer->size() > kMaxPacketBytes) {
          *err = "the broker sent a packet that is too large";
          return false;
        }
        if (std::chrono::steady_clock::now() >= deadline) return true;
        continue;
      }
      buffer->erase(0, consumed);
      *got = true;
      return true;
    }
    return false;
  }

  void flush_outbox(TlsStream* stream, std::string* err) {
    std::vector<std::string> pending;
    {
      std::lock_guard<std::mutex> lock(mutex);
      pending.swap(outbox);
    }
    for (size_t i = 0; i < pending.size(); ++i) {
      if (!write_frame(stream, pending[i], err)) return;
    }
  }

  bool run_session() {
    std::string err;
    mqtt_socket_t fd = connect_tcp(config.host, config.port, config.connect_timeout_ms, &err);
    if (fd == kBadMqttSocket) {
      set_error(err);
      return false;
    }
    set_send_timeout(fd, 3000);

    TlsOptions options;
    // The broker's certificate is self-signed (its address is in the CN), so
    // there is no chain to walk: the connection is encrypted, not verified. The
    // password is the gate - the same rule the operator CLI follows.
    options.verify_peer = false;
    std::unique_ptr<TlsStream> stream =
        tls_wrap_socket(static_cast<std::intptr_t>(fd), options, &err);
    if (!stream) {
      close_mqtt_socket(fd);
      set_error(err);
      return false;
    }
    if (!stream->handshake(config.host, &err)) {
      set_error(err);
      close_mqtt_socket(fd);
      return false;
    }

    connected_register_topic = std::string("mt_register/") + config.client_id;
    const std::string connect = mqtt::make_connect(
        config.client_id, config.user, config.password, static_cast<uint16_t>(config.keepalive_s),
        config.will_topic, config.will_payload);
    if (!write_frame(stream.get(), connect, &err)) {
      set_error(err);
      close_mqtt_socket(fd);
      return false;
    }

    std::string buffer;
    mqtt::Packet packet;
    bool got = false;
    if (!pump(stream.get(), &buffer, config.connect_timeout_ms, &packet, &got, &err)) {
      set_error(err);
      close_mqtt_socket(fd);
      return false;
    }
    if (!got || packet.type != mqtt::CONNACK) {
      set_error("the broker did not answer CONNECT");
      close_mqtt_socket(fd);
      return false;
    }
    uint8_t code = 0;
    if (!mqtt::parse_connack(packet.body, &code)) {
      set_error("the broker sent a malformed CONNACK");
      close_mqtt_socket(fd);
      return false;
    }
    if (code != 0) {
      std::string why;
      if (code == 4) {
        why = "the broker rejected the credentials for user \"" + config.user +
              "\" (wrong user or password)";
      } else if (code == 5) {
        why = "the broker refused the connection (not authorized)";
      } else if (code == 1) {
        why = "the broker refused the protocol level";
      } else if (code == 2) {
        why = "the broker refused the client id";
      } else if (code == 3) {
        why = "the broker is not available";
      } else {
        why = "the broker refused the connection (CONNACK " + std::to_string(code) + ")";
      }
      set_error(why);
      // A credential problem is not a network blip: say it on the console
      // (the default console level is warn) instead of retrying in silence.
      LOG_WARN("mqtt: ", why);
      close_mqtt_socket(fd);
      return false;
    }

    if (!config.subscribe_topic.empty()) {
      const std::string subscribe =
          mqtt::make_subscribe(packet_id(), std::vector<std::string>{config.subscribe_topic});
      if (!write_frame(stream.get(), subscribe, &err)) {
        set_error(err);
        close_mqtt_socket(fd);
        return false;
      }
      got = false;
      if (!pump(stream.get(), &buffer, 5000, &packet, &got, &err)) {
        set_error(err);
        close_mqtt_socket(fd);
        return false;
      }
      if (!got || packet.type != mqtt::SUBACK) {
        set_error("the broker did not acknowledge the subscription");
        close_mqtt_socket(fd);
        return false;
      }
    }

    const std::string registration = take_registration();
    if (!registration.empty()) {
      const std::string frame =
          mqtt::make_publish(connected_register_topic, registration, 1, true, packet_id());
      if (!write_frame(stream.get(), frame, &err)) {
        set_error(err);
        close_mqtt_socket(fd);
        return false;
      }
    }

    connected.store(true);
    set_error(std::string());
    LOG_INFO("mqtt: ", "connected to " + config.host + ":" + std::to_string(config.port) + " as " +
                         config.client_id);

    auto last_rx = std::chrono::steady_clock::now();
    auto last_register = last_rx;
    // The keepalive we advertise and the silence we tolerate are the same
    // number: the broker drops a session after 1.5x keepalive, so we use
    // the same bound on both sides.
    const int keepalive_s = (config.keepalive_s > 0) ? config.keepalive_s : kPingMaxSeconds;
    const auto keepalive = std::chrono::seconds(keepalive_s);
    // The heartbeat: a random gap of 1..5 minutes, drawn again after every
    // ping. It is the first ping that waits a full gap - the broker is
    // fresh anyway right after the connect.
    const std::pair<int, int> window = ping_window(keepalive_s);
    auto next_ping = std::chrono::steady_clock::now() +
                     std::chrono::seconds(random_int(window.first, window.second));
    LOG_DEBUG("mqtt: ", "the heartbeat is a random " + std::to_string(window.first) + ".." +
                         std::to_string(window.second) + " seconds");

    while (!stopping.load()) {
      flush_outbox(stream.get(), &err);
      if (!err.empty()) break;

      bool packet_got = false;
      const bool alive = pump(stream.get(), &buffer, 200, &packet, &packet_got, &err);
      if (!alive) break;
      if (packet_got) {
        last_rx = std::chrono::steady_clock::now();
        if (packet.type == mqtt::PUBLISH) {
          mqtt::PublishInfo info;
          if (!mqtt::parse_publish(packet.flags, packet.body, &info)) {
            err = "the broker sent a malformed PUBLISH";
            break;
          }
          if (info.qos == 1) {
            const std::string ack = mqtt::make_puback(info.packet_id);
            if (!write_frame(stream.get(), ack, &err)) break;
          }
          if (handler) handler(info.topic, info.payload);
        }
      }

      const auto now = std::chrono::steady_clock::now();
      if (now >= next_ping) {
        const std::string ping = mqtt::make_pingreq();
        if (!write_frame(stream.get(), ping, &err)) break;
        // Draw the next gap: the devices behind one broker should not ping
        // in lockstep.
        const int gap = random_int(window.first, window.second);
        next_ping = std::chrono::steady_clock::now() + std::chrono::seconds(gap);
        LOG_DEBUG("mqtt: ", "ping sent, the next one in " + std::to_string(gap) + " seconds");
      }
      if (now - last_register >= std::chrono::seconds(60)) {
        const std::string registration_now = take_registration();
        if (!registration_now.empty()) {
          const std::string frame =
              mqtt::make_publish(connected_register_topic, registration_now, 1, true, packet_id());
          if (!write_frame(stream.get(), frame, &err)) break;
        }
        last_register = now;
      }
      if (now - last_rx > keepalive + keepalive / 2) {
        err = "the broker stopped answering";
        break;
      }
    }

    connected.store(false);
    if (!err.empty()) set_error(err);
    {
      const std::string bye = mqtt::make_disconnect();
      std::string ignored;
      stream->write(bye.data(), bye.size(), &ignored);
    }
    close_mqtt_socket(fd);
    return false;
  }

  void loop() {
    int backoff_s = 1;
    while (!stopping.load()) {
      run_session();
      if (stopping.load()) break;
      const std::string why = get_error();
      if (!why.empty()) LOG_INFO("mqtt: ", "disconnected: " + why);
      // Back off 1, 2, 4 ... 60 seconds with a little jitter, so 100 devices
      // coming back after a broker restart do not arrive in lockstep.
      const int sleep_ms = backoff_s * 1000 + (static_cast<int>(std::rand()) % 500);
      for (int waited = 0; waited < sleep_ms && !stopping.load(); waited += 100) {
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
      }
      backoff_s = backoff_s < 60 ? backoff_s * 2 : 60;
    }
    connected.store(false);
  }
};

MqttClient::MqttClient() : impl_(new Impl()) {}
MqttClient::~MqttClient() { stop(); }

void MqttClient::start(const Config& config, MessageHandler on_message) {
  bool expected = false;
  if (!impl_->started.compare_exchange_strong(expected, true)) return;
  impl_->config = config;
  impl_->handler = on_message;
  impl_->worker = std::thread([this]() { impl_->loop(); });
}

void MqttClient::stop() {
  impl_->stopping.store(true);
  if (impl_->worker.joinable()) impl_->worker.join();
}

bool MqttClient::publish(const std::string& topic, const std::string& payload, int qos) {
  if (!impl_->connected.load()) return false;
  const uint16_t id = impl_->packet_id();
  const std::string frame = mqtt::make_publish(topic, payload, static_cast<uint8_t>(qos), false, id);
  std::lock_guard<std::mutex> lock(impl_->mutex);
  if (impl_->outbox.size() > 256) return false;
  impl_->outbox.push_back(frame);
  return true;
}

bool MqttClient::connected() const { return impl_->connected.load(); }
std::string MqttClient::last_error() const { return impl_->get_error(); }
std::string MqttClient::client_id() const { return impl_->config.client_id; }

}  // namespace tea
