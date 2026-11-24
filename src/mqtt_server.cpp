// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_server.cpp -- MQTT 3.1.1 server side, one thread per session.
//
// The thread owns the TLS context: it reads, writes the outbox and does the
// keepalive check. Publishers (other sessions) only push frames into an
// outbox, so a slow subscriber can never block them -- it hits the queue cap
// and is dropped instead.
#include "mqtt_server.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <deque>
#include <map>
#include <mutex>
#include <thread>
#include <vector>

#include <nlohmann/json.hpp>
#include <sqlite3.h>

#ifdef _WIN32
#include <winsock2.h>
#else
#include <cerrno>
#include <sys/time.h>
#endif

#include "mqtt_codec.h"
#include "tunnel_conn.h"
#include "tunnel_net.h"

namespace mqtt {
namespace {

using Clock = std::chrono::steady_clock;

/// What an account is allowed to do. The device name is NOT part of it: one
/// account may be used by many devices (each with its own registration name),
/// and a device may only touch the topics of the client id it presented.
struct Account {
  std::string user;
  std::string role;
  bool enabled = false;
};

struct Session {
  std::shared_ptr<tea_tunnel::Connection> conn;
  std::string client_id;
  std::string user;
  std::string role;
  std::string will_topic;
  std::string will_payload;
  bool will_retain = false;
  uint16_t keepalive_s = 60;

  std::mutex mutex;  // guards filters, outbox and next_packet_id
  std::vector<std::string> filters;
  std::deque<std::string> outbox;
  uint16_t next_packet_id = 1;

  std::atomic<bool> closed{false};
  bool taken_over = false;
  bool clean_disconnect = false;
  Clock::time_point last_rx = Clock::now();

  Account account() const {
    Account a;
    a.user = user;
    a.role = role;
    a.enabled = true;
    return a;
  }
};

bool allowed_subscribe(const Account& account, const std::string& filter,
                       const std::string& client_id) {
  if (account.role == "operator") {
    return filter == "mt_register/#" || filter == "mt_control/+/resp" ||
           filter == "mt_control/+/cmd";
  }
  // A device may only listen to the name it registered with. Many devices can
  // share one account; the name they present is what keeps them apart.
  if (account.role == "device" && !client_id.empty()) {
    return filter == "mt_control/" + client_id + "/cmd";
  }
  return false;
}

bool allowed_publish(const Account& account, const std::string& topic,
                     const std::string& client_id) {
  if (account.role == "operator") return topic_matches("mt_control/+/cmd", topic);
  if (account.role == "device" && !client_id.empty()) {
    return topic == "mt_control/" + client_id + "/resp" ||
           topic == "mt_register/" + client_id;
  }
  return false;
}

/// The zone a device belongs to when it names no operator account (or names
/// one that does not exist): the account every deployment has.
const char* const kDefaultZone = "zonekey";

/// The device name inside the topics we know: "mt_register/<name>" and
/// "mt_control/<name>/resp" (also "/cmd"). Empty when the topic names none.
std::string device_of_topic(const std::string& topic) {
  const std::string kRegister = "mt_register/";
  const std::string kControl = "mt_control/";
  if (topic.compare(0, kRegister.size(), kRegister) == 0) {
    return topic.substr(kRegister.size());
  }
  if (topic.compare(0, kControl.size(), kControl) == 0) {
    const std::string rest = topic.substr(kControl.size());
    const std::string::size_type slash = rest.find('/');
    return slash == std::string::npos ? rest : rest.substr(0, slash);
  }
  return std::string();
}

/// Did the read fail because the socket timed out?
///
/// The session socket has a short recv timeout (the keepalive check runs on a
/// tick). On POSIX the timeout comes back as EAGAIN and mbedtls turns it into
/// WANT_READ - a tick. On Windows the same timeout is WSAETIMEDOUT, which
/// mbedtls reports as a hard network error: a healthy but quiet session would
/// be killed after one timeout.
bool read_timed_out(int error) {
  if (error != MBEDTLS_ERR_NET_RECV_FAILED) return false;
#ifdef _WIN32
  return WSAGetLastError() == WSAETIMEDOUT;
#else
  return errno == EAGAIN || errno == EWOULDBLOCK;
#endif
}

void set_send_timeout(tea_socket_t s, int ms) {
#ifdef _WIN32
  DWORD tv = static_cast<DWORD>(ms);
  ::setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&tv), sizeof(tv));
#else
  timeval tv;
  tv.tv_sec = ms / 1000;
  tv.tv_usec = (ms % 1000) * 1000;
  ::setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
#endif
}

constexpr size_t kMaxOutbox = 64;

}  // namespace

struct Server::Impl {
  Options options;
  sqlite3* db = nullptr;
  std::mutex db_mutex;
  std::map<std::string, std::pair<int, long long>> failures;

  /// Which operator account owns which device (client_id -> account). The
  /// table survives a broker restart, the retained registrations do not.
  std::mutex zones_mutex;
  std::map<std::string, std::string> zones;

  std::mutex registry_mutex;
  std::map<std::string, std::shared_ptr<Session>> sessions;
  std::map<std::string, std::string> retained;
  std::atomic<long long> messages{0};
  std::atomic<long long> auth_failures{0};
  std::atomic<long long> rejected{0};

  ~Impl() {
    if (db) {
      sqlite3_close(db);
      db = nullptr;
    }
  }

  bool open_database(const std::string& path, std::string* err);
  bool authenticate(const std::string& user, const std::string& password, Account* out);
  /// Remember that this registration name is (or was) online, and who used it.
  void touch_device(const std::string& client_id, const std::string& account, bool online);
  /// Fill in ip/mac/version/port/zone from the retained registration message.
  void update_device_info(const std::string& client_id, const std::string& account,
                          const std::string& payload);
  /// The operator account a device asks for, resolved: the name must be an
  /// operator account in mqtt_accounts, anything else falls back to zonekey.
  std::string resolve_oper_name(const std::string& requested);
  /// The operator account that owns a device. A device we do not know yet
  /// belongs to the default zone.
  std::string zone_of(const std::string& client_id);
  /// May this account touch that topic? An operator only reaches the devices
  /// of its own zone.
  bool zone_allows(const Account& account, const std::string& topic);

  bool write_all(Session* session, const std::string& frame);
  bool flush(Session* session);
  void broadcast(const std::string& topic, const std::string& payload, bool retain);
  void forget(const std::shared_ptr<Session>& session);
  void session_loop(std::shared_ptr<Session> session, std::string buffer);
  bool handle(Session* session, const Packet& packet);
};

bool Server::Impl::open_database(const std::string& path, std::string* err) {
  if (sqlite3_open_v2(path.c_str(), &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr) !=
      SQLITE_OK) {
    *err = std::string("cannot open ") + path + ": " + (db ? sqlite3_errmsg(db) : "out of memory");
    return false;
  }
  sqlite3_busy_timeout(db, 1000);
  // The rest of the file (turn_accounts, the usage tables) is left alone.
  const char* ddl =
      "CREATE TABLE IF NOT EXISTS mqtt_accounts ("
      " id INTEGER PRIMARY KEY,"
      " user TEXT NOT NULL UNIQUE,"
      " pass TEXT NOT NULL,"
      " role TEXT NOT NULL,"
      " device_name TEXT,"
      " enabled INTEGER NOT NULL DEFAULT 1,"
      " note TEXT,"
      " created_at TEXT);"
      "CREATE INDEX IF NOT EXISTS idx_mqtt_accounts_user ON mqtt_accounts(user);"
      // One row per registration name. The account column says who is allowed
      // to use it; several devices may share one account. This is the part
      // that survives a broker restart (retained messages do not).
      "CREATE TABLE IF NOT EXISTS mqtt_devices ("
      " client_id TEXT PRIMARY KEY,"
      " account TEXT NOT NULL,"
      " ip TEXT, mac TEXT, version TEXT, port INTEGER,"
      " oper_name TEXT,"
      " online INTEGER NOT NULL DEFAULT 0,"
      " first_seen TEXT, last_seen TEXT, note TEXT);"
      "CREATE INDEX IF NOT EXISTS idx_mqtt_devices_account ON mqtt_devices(account);";
  char* message = nullptr;
  if (sqlite3_exec(db, ddl, nullptr, nullptr, &message) != SQLITE_OK) {
    *err = std::string("cannot create mqtt_accounts: ") + (message ? message : "unknown");
    if (message) sqlite3_free(message);
    return false;
  }
  // The zone column came later: a table that exists already gets it now.
  {
    bool has_zone = false;
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db, "PRAGMA table_info(mqtt_devices)", -1, &statement, nullptr) ==
        SQLITE_OK) {
      while (sqlite3_step(statement) == SQLITE_ROW) {
        const char* name = reinterpret_cast<const char*>(sqlite3_column_text(statement, 1));
        if (name && std::string(name) == "oper_name") {
          has_zone = true;
          break;
        }
      }
      sqlite3_finalize(statement);
    }
    if (!has_zone) {
      // A failure here is not fatal (the broker still runs), but the device
      // info would stop being stored - so say it out loud.
      char* why = nullptr;
      if (sqlite3_exec(db, "ALTER TABLE mqtt_devices ADD COLUMN oper_name TEXT", nullptr,
                       nullptr, &why) != SQLITE_OK) {
        std::printf("broker: cannot add the oper_name column to mqtt_devices: %s\n",
                    why ? why : "unknown");
        std::fflush(stdout);
      }
      if (why) sqlite3_free(why);
    }
  }
  return true;
}

bool Server::Impl::authenticate(const std::string& user, const std::string& password,
                                Account* out) {
  if (user.empty()) return false;
  std::lock_guard<std::mutex> lock(db_mutex);
  const long long now = static_cast<long long>(std::time(nullptr));
  const std::map<std::string, std::pair<int, long long>>::const_iterator known =
      failures.find(user);
  if (known != failures.end() && known->second.first >= 5 && now < known->second.second) {
    return false;
  }
  sqlite3_stmt* statement = nullptr;
  const char* sql = "SELECT pass, role, enabled FROM mqtt_accounts WHERE user = ?";
  if (sqlite3_prepare_v2(db, sql, -1, &statement, nullptr) != SQLITE_OK) return false;
  sqlite3_bind_text(statement, 1, user.c_str(), -1, SQLITE_TRANSIENT);
  bool ok = false;
  if (sqlite3_step(statement) == SQLITE_ROW) {
    const char* stored = reinterpret_cast<const char*>(sqlite3_column_text(statement, 0));
    const char* role = reinterpret_cast<const char*>(sqlite3_column_text(statement, 1));
    const int enabled = sqlite3_column_int(statement, 2);
    if (enabled != 0 && stored && password == stored) {
      out->user = user;
      out->role = role ? role : "";
      out->enabled = true;
      ok = true;
      failures.erase(user);
    }
  }
  sqlite3_finalize(statement);
  if (!ok) {
    std::pair<int, long long>& failure = failures[user];
    failure.first += 1;
    const int shift = failure.first < 6 ? failure.first : 6;
    failure.second = now + (1 << shift);
  }
  return ok;
}

namespace {
std::string iso_now() {
  const std::time_t now = std::time(nullptr);
  std::tm parts;
#ifdef _WIN32
  gmtime_s(&parts, &now);
#else
  gmtime_r(&now, &parts);
#endif
  char text[32] = {0};
  std::strftime(text, sizeof(text), "%Y-%m-%dT%H:%M:%SZ", &parts);
  return text;
}
}  // namespace

void Server::Impl::touch_device(const std::string& client_id, const std::string& account,
                                bool online) {
  if (client_id.empty()) return;
  std::lock_guard<std::mutex> lock(db_mutex);
  const char* sql =
      "INSERT INTO mqtt_devices(client_id, account, online, first_seen, last_seen) "
      "VALUES(?,?,?,?,?) "
      "ON CONFLICT(client_id) DO UPDATE SET account=excluded.account, "
      "online=excluded.online, last_seen=excluded.last_seen";
  sqlite3_stmt* statement = nullptr;
  if (sqlite3_prepare_v2(db, sql, -1, &statement, nullptr) != SQLITE_OK) return;
  const std::string now = iso_now();
  sqlite3_bind_text(statement, 1, client_id.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(statement, 2, account.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_int(statement, 3, online ? 1 : 0);
  sqlite3_bind_text(statement, 4, now.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_bind_text(statement, 5, now.c_str(), -1, SQLITE_TRANSIENT);
  sqlite3_step(statement);
  sqlite3_finalize(statement);
}

std::string Server::Impl::resolve_oper_name(const std::string& requested) {
  std::string user = requested.empty() ? std::string(kDefaultZone) : requested;
  bool found = false;
  {
    std::lock_guard<std::mutex> lock(db_mutex);
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db,
                           "SELECT 1 FROM mqtt_accounts WHERE user = ? AND role = 'operator' "
                           "AND enabled = 1",
                           -1, &statement, nullptr) == SQLITE_OK) {
      sqlite3_bind_text(statement, 1, user.c_str(), -1, SQLITE_TRANSIENT);
      found = (sqlite3_step(statement) == SQLITE_ROW);
      sqlite3_finalize(statement);
    }
  }
  if (!found) {
    if (user != kDefaultZone) {
      std::printf("broker: the operator account \"%s\" is not an operator here - the device "
                  "joins the zone \"%s\"\n",
                  user.c_str(), kDefaultZone);
      std::fflush(stdout);
    }
    user = kDefaultZone;
  }
  return user;
}

std::string Server::Impl::zone_of(const std::string& client_id) {
  // The map is filled when a device registers and when the broker starts (the
  // table survives a restart, the retained registrations do not), so there is
  // no database round trip here: this runs on every message.
  std::lock_guard<std::mutex> lock(zones_mutex);
  std::map<std::string, std::string>::const_iterator it = zones.find(client_id);
  if (it != zones.end()) return it->second;
  return kDefaultZone;  // a device we do not know yet belongs to the default zone
}

bool Server::Impl::zone_allows(const Account& account, const std::string& topic) {
  // A device stays in its own topics (the filters already limit it).
  if (account.role != "operator") return true;
  const std::string device = device_of_topic(topic);
  if (device.empty()) return true;  // not a device topic
  return zone_of(device) == account.user;
}

void Server::Impl::update_device_info(const std::string& client_id, const std::string& account,
                                      const std::string& payload) {
  nlohmann::json data;
  try {
    data = nlohmann::json::parse(payload);
  } catch (...) {
    return;
  }
  // The operator the device names: it must be an operator account here,
  // otherwise the device falls back to the default zone.
  const std::string zone = resolve_oper_name(data.value("oper_name", std::string()));
  const std::string ip = data.value("ip", std::string());
  const std::string mac = data.value("mac", std::string());
  const std::string version = data.value("version", std::string());
  const int port = data.value("port", 0);
  const std::string now = iso_now();
  {
    std::lock_guard<std::mutex> lock(db_mutex);
    const char* sql =
        "INSERT INTO mqtt_devices(client_id, account, ip, mac, version, port, oper_name, "
        "first_seen, last_seen) VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(client_id) DO UPDATE SET account=excluded.account, ip=excluded.ip, "
        "mac=excluded.mac, version=excluded.version, port=excluded.port, "
        "oper_name=excluded.oper_name, last_seen=excluded.last_seen";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db, sql, -1, &statement, nullptr) != SQLITE_OK) return;
    sqlite3_bind_text(statement, 1, client_id.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 2, account.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 3, ip.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 4, mac.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 5, version.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_int(statement, 6, port);
    sqlite3_bind_text(statement, 7, zone.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 8, now.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_bind_text(statement, 9, now.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_step(statement);
    sqlite3_finalize(statement);
  }
  {
    std::lock_guard<std::mutex> lock(zones_mutex);
    zones[client_id] = zone;
  }
}

bool Server::Impl::write_all(Session* session, const std::string& frame) {
  size_t written = 0;
  while (written < frame.size()) {
    const int rc = mbedtls_ssl_write(&session->conn->ssl,
                                     reinterpret_cast<const unsigned char*>(frame.data() + written),
                                     frame.size() - written);
    if (rc > 0) {
      written += static_cast<size_t>(rc);
      continue;
    }
    if (rc == MBEDTLS_ERR_SSL_WANT_READ || rc == MBEDTLS_ERR_SSL_WANT_WRITE) continue;
    return false;
  }
  return true;
}

bool Server::Impl::flush(Session* session) {
  for (;;) {
    std::string frame;
    {
      std::lock_guard<std::mutex> lock(session->mutex);
      if (session->outbox.empty()) return true;
      frame.swap(session->outbox.front());
      session->outbox.pop_front();
    }
    if (!write_all(session, frame)) return false;
  }
}

void Server::Impl::broadcast(const std::string& topic, const std::string& payload, bool retain) {
  std::lock_guard<std::mutex> lock(registry_mutex);
  if (retain) {
    if (payload.empty()) {
      retained.erase(topic);
    } else {
      retained[topic] = payload;
    }
  }
  for (std::map<std::string, std::shared_ptr<Session>>::iterator it = sessions.begin();
       it != sessions.end(); ++it) {
    Session* session = it->second.get();
    if (session->closed.load()) continue;
    // An operator only receives what belongs to its own zone.
    if (!zone_allows(session->account(), topic)) continue;
    bool match = false;
    {
      std::lock_guard<std::mutex> session_lock(session->mutex);
      for (size_t i = 0; i < session->filters.size(); ++i) {
        if (topic_matches(session->filters[i], topic)) {
          match = true;
          break;
        }
      }
      if (!match) continue;
      if (session->outbox.size() >= kMaxOutbox) {
        session->closed.store(true);  // slow subscriber: disconnect, do not grow
        continue;
      }
      const uint16_t id = session->next_packet_id++;
      if (session->next_packet_id == 0) session->next_packet_id = 1;
      session->outbox.push_back(make_publish(topic, payload, 1, false, id));
    }
  }
  messages.fetch_add(1);
}

void Server::Impl::forget(const std::shared_ptr<Session>& session) {
  std::lock_guard<std::mutex> lock(registry_mutex);
  const std::map<std::string, std::shared_ptr<Session>>::iterator it =
      sessions.find(session->client_id);
  if (it != sessions.end() && it->second == session) sessions.erase(it);
}

bool Server::Impl::handle(Session* session, const Packet& packet) {
  switch (packet.type) {
    case PINGREQ:
      return write_all(session, make_pingresp());
    case DISCONNECT:
      session->clean_disconnect = true;
      return false;
    case SUBSCRIBE: {
      SubscribeRequest request;
      if (!parse_subscribe(packet.body, &request)) return false;
      const Account account = session->account();
      bool allowed = true;
      for (size_t i = 0; i < request.filters.size(); ++i) {
        if (!valid_topic_filter(request.filters[i]) ||
            !allowed_subscribe(account, request.filters[i], session->client_id)) {
          allowed = false;
          break;
        }
      }
      if (!write_all(session, make_suback(request.packet_id, allowed))) return false;
      if (!allowed) {
        rejected.fetch_add(1);
        return true;
      }
      {
        std::lock_guard<std::mutex> lock(session->mutex);
        for (size_t i = 0; i < request.filters.size(); ++i) {
          session->filters.push_back(request.filters[i]);
        }
      }
      // Retained messages for the new subscription.
      std::vector<std::pair<std::string, std::string>> pending;
      {
        std::lock_guard<std::mutex> lock(registry_mutex);
        for (std::map<std::string, std::string>::const_iterator it = retained.begin();
             it != retained.end(); ++it) {
          for (size_t i = 0; i < request.filters.size(); ++i) {
            if (topic_matches(request.filters[i], it->first)) {
              pending.push_back(*it);
              break;
            }
          }
        }
      }
      // An operator only sees the devices of its own zone.
      {
        std::vector<std::pair<std::string, std::string>> visible;
        for (size_t i = 0; i < pending.size(); ++i) {
          if (zone_allows(session->account(), pending[i].first)) visible.push_back(pending[i]);
        }
        pending.swap(visible);
      }
      for (size_t i = 0; i < pending.size(); ++i) {
        uint16_t id = 0;
        {
          std::lock_guard<std::mutex> lock(session->mutex);
          id = session->next_packet_id++;
          if (session->next_packet_id == 0) session->next_packet_id = 1;
        }
        if (!write_all(session, make_publish(pending[i].first, pending[i].second, 1, true, id))) {
          return false;
        }
      }
      return true;
    }
    case PUBLISH: {
      PublishInfo publish;
      if (!parse_publish(packet.flags, packet.body, &publish)) return false;
      const Account account = session->account();
      if (!allowed_publish(account, publish.topic, session->client_id) ||
          !zone_allows(account, publish.topic)) {
        rejected.fetch_add(1);
        return false;  // the spec says: close the connection
      }
      if (publish.topic == "mt_register/" + session->client_id) {
        update_device_info(session->client_id, session->user, publish.payload);
      }
      broadcast(publish.topic, publish.payload, publish.retain);
      if (publish.qos == 1) return write_all(session, make_puback(publish.packet_id));
      return true;
    }
    case PUBACK:
      return true;
    default:
      return true;
  }
}

void Server::Impl::session_loop(std::shared_ptr<Session> session, std::string buffer) {
  // ---- the first packet must be CONNECT ---------------------------------
  Packet packet;
  bool got_connect = false;
  const Clock::time_point deadline = Clock::now() + std::chrono::seconds(10);
  while (Clock::now() < deadline && !session->closed.load()) {
    size_t consumed = 0;
    const ParseStatus status = parse_packet(buffer, &consumed, &packet, options.max_payload_bytes);
    if (status == ParseStatus::error) return;
    if (status == ParseStatus::ok) {
      buffer.erase(0, consumed);
      got_connect = true;
      break;
    }
    char chunk[4096];
    const int n = mbedtls_ssl_read(&session->conn->ssl,
                                   reinterpret_cast<unsigned char*>(chunk), sizeof(chunk));
    if (n > 0) {
      buffer.append(chunk, static_cast<size_t>(n));
      continue;
    }
    if (n == MBEDTLS_ERR_SSL_WANT_READ || n == MBEDTLS_ERR_SSL_TIMEOUT) continue;
    return;
  }
  if (!got_connect || packet.type != CONNECT) return;

  ConnectInfo info;
  if (!parse_connect(packet.body, &info)) return;
  if (info.level != 4 || info.client_id.empty() || !valid_utf8(info.client_id)) {
    write_all(session.get(), make_connack(1));
    return;
  }
  Account account;
  if (!authenticate(info.user, info.password, &account)) {
    auth_failures.fetch_add(1);
    write_all(session.get(), make_connack(4));
    return;
  }

  session->client_id = info.client_id;
  session->user = account.user;
  session->role = account.role;
  session->keepalive_s = info.keepalive;
  session->will_topic = info.will_topic;
  session->will_payload = info.will_payload;
  session->will_retain = info.will_retain();
  session->last_rx = Clock::now();

  {
    std::lock_guard<std::mutex> lock(registry_mutex);
    const std::map<std::string, std::shared_ptr<Session>>::iterator existing =
        sessions.find(session->client_id);
    if (existing != sessions.end()) {
      existing->second->taken_over = true;
      existing->second->closed.store(true);
    }
    sessions[session->client_id] = session;
  }
  if (!write_all(session.get(), make_connack(0))) {
    forget(session);
    return;
  }
  if (session->role == "device") touch_device(session->client_id, session->user, true);

  // ---- the session loop --------------------------------------------------
  while (!session->closed.load()) {
    if (!flush(session.get())) {
      break;
    }
    char chunk[4096];
    const int n = mbedtls_ssl_read(&session->conn->ssl,
                                   reinterpret_cast<unsigned char*>(chunk), sizeof(chunk));
    if (n > 0) {
      buffer.append(chunk, static_cast<size_t>(n));
      session->last_rx = Clock::now();
      for (;;) {
        size_t consumed = 0;
        const ParseStatus status =
            parse_packet(buffer, &consumed, &packet, options.max_payload_bytes);
        if (status == ParseStatus::error) {
          session->closed.store(true);
          break;
        }
        if (status == ParseStatus::incomplete) break;
        buffer.erase(0, consumed);
        if (!handle(session.get(), packet)) {
          session->closed.store(true);
          break;
        }
      }
      if (session->closed.load()) break;
    } else if (n == MBEDTLS_ERR_SSL_WANT_READ || n == MBEDTLS_ERR_SSL_TIMEOUT ||
               read_timed_out(n)) {
      // a tick: the keepalive check below still runs
    } else {
      break;
    }
    if (session->keepalive_s > 0) {
      const std::chrono::seconds limit(session->keepalive_s + session->keepalive_s / 2);
      if (Clock::now() - session->last_rx > limit) {
        break;
      }
    }
  }

  // ---- teardown ----------------------------------------------------------
  session->closed.store(true);
  const bool send_will = !session->clean_disconnect && !session->taken_over &&
                         !session->will_topic.empty();
  const std::string will_topic = session->will_topic;
  const std::string will_payload = session->will_payload;
  const bool will_retain = session->will_retain;
  forget(session);
  const bool is_device = session->role == "device" && !session->client_id.empty();
  if (is_device) touch_device(session->client_id, session->user, false);
  session->conn.reset();
  if (send_will) {
    broadcast(will_topic, will_payload, will_retain);
  } else if (is_device) {
    // A clean DISCONNECT sends no will, so the retained registration would go
    // on saying "online" forever and the operator's list would keep a device
    // that is already gone. Publish the offline state ourselves.
    nlohmann::json offline;
    offline["name"] = session->client_id;
    offline["online"] = false;
    offline["ts"] = static_cast<long long>(std::time(nullptr));
    broadcast("mt_register/" + session->client_id, offline.dump(), true);
  }
}

Server::Server() : impl_(new Impl()) {}
Server::~Server() = default;

bool Server::start(const Options& options, std::string* err) {
  impl_->options = options;
  if (!impl_->open_database(options.db_path, err)) return false;
  // Nothing is connected yet: every row in the table is from the previous run.
  std::map<std::string, std::string> known_zones;
  {
    std::lock_guard<std::mutex> lock(impl_->db_mutex);
    sqlite3_exec(impl_->db, "UPDATE mqtt_devices SET online = 0", nullptr, nullptr, nullptr);
    // The zones of the devices we know: the table survives a broker restart,
    // the retained registrations do not.
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(impl_->db, "SELECT client_id, COALESCE(oper_name, '') FROM mqtt_devices",
                           -1, &statement, nullptr) == SQLITE_OK) {
      while (sqlite3_step(statement) == SQLITE_ROW) {
        const char* id = reinterpret_cast<const char*>(sqlite3_column_text(statement, 0));
        const char* zone = reinterpret_cast<const char*>(sqlite3_column_text(statement, 1));
        if (!id || !*id) continue;
        known_zones[id] = (zone && *zone) ? zone : kDefaultZone;
      }
      sqlite3_finalize(statement);
    }
  }
  {
    std::lock_guard<std::mutex> lock(impl_->zones_mutex);
    for (std::map<std::string, std::string>::const_iterator it = known_zones.begin();
         it != known_zones.end(); ++it) {
      impl_->zones[it->first] = it->second;
    }
  }
  return true;
}

void Server::serve(std::shared_ptr<tea_tunnel::Connection> connection,
                   const std::string& initial) {
  {
    std::lock_guard<std::mutex> lock(impl_->registry_mutex);
    if (impl_->sessions.size() >= impl_->options.max_clients) return;
  }
  std::shared_ptr<Session> session(new Session());
  session->conn = connection;
  tea_net_set_recv_timeout(connection->fd, impl_->options.read_timeout_ms);
  set_send_timeout(connection->fd, 3000);
  Impl* impl = impl_.get();
  std::thread([impl, session, initial]() { impl->session_loop(session, initial); }).detach();
}

std::string Server::snapshot() const {
  nlohmann::json j;
  {
    std::lock_guard<std::mutex> lock(impl_->registry_mutex);
    j["clients"] = static_cast<long long>(impl_->sessions.size());
    j["retained"] = static_cast<long long>(impl_->retained.size());
  }
  j["messages"] = impl_->messages.load();
  j["auth_failures"] = impl_->auth_failures.load();
  j["rejected"] = impl_->rejected.load();
  return j.dump();
}

}  // namespace mqtt
