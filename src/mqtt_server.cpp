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
#include <sys/time.h>
#endif

#include "mqtt_codec.h"
#include "tunnel_conn.h"
#include "tunnel_net.h"

namespace mqtt {
namespace {

using Clock = std::chrono::steady_clock;

struct Account {
  std::string user;
  std::string role;
  std::string device_name;
  bool enabled = false;
};

struct Session {
  std::shared_ptr<tea_tunnel::Connection> conn;
  std::string client_id;
  std::string user;
  std::string role;
  std::string device_name;
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
    a.device_name = device_name;
    a.enabled = true;
    return a;
  }
};

bool allowed_subscribe(const Account& account, const std::string& filter) {
  if (account.role == "operator") {
    return filter == "mt_register/#" || filter == "mt_control/+/resp" ||
           filter == "mt_control/+/cmd";
  }
  if (account.role == "device" && !account.device_name.empty()) {
    return filter == "mt_control/" + account.device_name + "/cmd";
  }
  return false;
}

bool allowed_publish(const Account& account, const std::string& topic) {
  if (account.role == "operator") return topic_matches("mt_control/+/cmd", topic);
  if (account.role == "device" && !account.device_name.empty()) {
    return topic == "mt_control/" + account.device_name + "/resp" ||
           topic == "mt_register/" + account.device_name;
  }
  return false;
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
      "CREATE INDEX IF NOT EXISTS idx_mqtt_accounts_user ON mqtt_accounts(user);";
  char* message = nullptr;
  if (sqlite3_exec(db, ddl, nullptr, nullptr, &message) != SQLITE_OK) {
    *err = std::string("cannot create mqtt_accounts: ") + (message ? message : "unknown");
    if (message) sqlite3_free(message);
    return false;
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
  const char* sql =
      "SELECT pass, role, COALESCE(device_name, ''), enabled FROM mqtt_accounts WHERE user = ?";
  if (sqlite3_prepare_v2(db, sql, -1, &statement, nullptr) != SQLITE_OK) return false;
  sqlite3_bind_text(statement, 1, user.c_str(), -1, SQLITE_TRANSIENT);
  bool ok = false;
  if (sqlite3_step(statement) == SQLITE_ROW) {
    const char* stored = reinterpret_cast<const char*>(sqlite3_column_text(statement, 0));
    const char* role = reinterpret_cast<const char*>(sqlite3_column_text(statement, 1));
    const char* device_name = reinterpret_cast<const char*>(sqlite3_column_text(statement, 2));
    const int enabled = sqlite3_column_int(statement, 3);
    if (enabled != 0 && stored && password == stored) {
      out->user = user;
      out->role = role ? role : "";
      out->device_name = device_name ? device_name : "";
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
            !allowed_subscribe(account, request.filters[i])) {
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
      if (!allowed_publish(account, publish.topic)) {
        rejected.fetch_add(1);
        return false;  // the spec says: close the connection
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
  session->device_name = account.device_name;
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

  // ---- the session loop --------------------------------------------------
  while (!session->closed.load()) {
    if (!flush(session.get())) break;
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
    } else if (n == MBEDTLS_ERR_SSL_WANT_READ || n == MBEDTLS_ERR_SSL_TIMEOUT) {
      // a tick: the keepalive check below still runs
    } else {
      break;
    }
    if (session->keepalive_s > 0) {
      const std::chrono::seconds limit(session->keepalive_s + session->keepalive_s / 2);
      if (Clock::now() - session->last_rx > limit) break;
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
  session->conn.reset();
  if (send_will) broadcast(will_topic, will_payload, will_retain);
}

Server::Server() : impl_(new Impl()) {}
Server::~Server() = default;

bool Server::start(const Options& options, std::string* err) {
  impl_->options = options;
  if (!impl_->open_database(options.db_path, err)) return false;
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
