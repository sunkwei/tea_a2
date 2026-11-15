// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// tunnel_usage.h -- who used the TURN relay, and how much.
//
// libjuice tells us when a relay session starts and ends, and how many bytes
// went through the relay (see the tea_a2 patch in
// 3rd/libdatachannel/deps/libjuice/src/server.c). The recorder writes that
// into the account book's sqlite file - **a2_broker.db**, the same file the
// accounts are in - so that a person can look at it with any sqlite tool:
//
//     sqlite3 a2_broker.db "SELECT * FROM turn_usage ORDER BY started_at DESC LIMIT 10;"
//
// Two tables:
//
//   turn_usage        one row per relay session. The details. They are kept
//                     for a while (15 days by default) and then pruned.
//   turn_usage_daily  the totals per day and per account. They are tiny and
//                     are kept for good.
//
// The callback comes from libjuice's own thread, so it must not block: it
// puts the event on a queue and a writer thread does the sqlite work.
#pragma once

#include <sqlite3.h>

#include <atomic>
#include <condition_variable>
#include <cstdio>
#include <ctime>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "tunnel_turn_db.h"

namespace tea_tunnel {

/// One relay session, as the recorder sees it.
struct UsageEvent {
  bool released = false;
  std::string username;
  std::string client;
  std::string relayed;
  uint64_t bytes_in = 0;
  uint64_t bytes_out = 0;
  uint64_t packets_in = 0;
  uint64_t packets_out = 0;
  long long at = 0;  // when it happened (unix seconds)
};

/// The tables the recorder needs.
inline const char* usage_table() { return "turn_usage"; }
inline const char* usage_daily_table() { return "turn_usage_daily"; }

/// "2026-11-11" for a unix timestamp (UTC).
inline std::string usage_day(long long when) {
  const std::time_t stamp = static_cast<std::time_t>(when);
  struct tm parts;
#if defined(_WIN32)
  if (gmtime_s(&parts, &stamp) != 0) return "";
#else
  if (gmtime_r(&stamp, &parts) == nullptr) return "";
#endif
  char text[16];
  std::snprintf(text, sizeof(text), "%04d-%02d-%02d", parts.tm_year + 1900, parts.tm_mon + 1,
                parts.tm_mday);
  return text;
}

/// "12.3 MB" - the way a person reads a number.
inline std::string usage_bytes(uint64_t bytes) {
  char text[32];
  if (bytes >= 1024ull * 1024 * 1024) {
    std::snprintf(text, sizeof(text), "%.1f GB",
                  static_cast<double>(bytes) / (1024.0 * 1024 * 1024));
  } else if (bytes >= 1024ull * 1024) {
    std::snprintf(text, sizeof(text), "%.1f MB", static_cast<double>(bytes) / (1024.0 * 1024));
  } else if (bytes >= 1024ull) {
    std::snprintf(text, sizeof(text), "%.1f KB", static_cast<double>(bytes) / 1024.0);
  } else {
    std::snprintf(text, sizeof(text), "%llu B", static_cast<unsigned long long>(bytes));
  }
  return text;
}

/// "12m30s" - the way a person reads a duration.
inline std::string usage_duration(long long seconds) {
  if (seconds < 0) seconds = 0;
  char text[32];
  if (seconds >= 3600) {
    std::snprintf(text, sizeof(text), "%lldh%02lldm", seconds / 3600, (seconds % 3600) / 60);
  } else if (seconds >= 60) {
    std::snprintf(text, sizeof(text), "%lldm%02llds", seconds / 60, seconds % 60);
  } else {
    std::snprintf(text, sizeof(text), "%llds", seconds);
  }
  return text;
}

/// The recorder: a queue in front, a writer thread behind.
class TurnUsageRecorder {
 public:
  ~TurnUsageRecorder() { stop(); }

  /// Open the database and start the writer thread.
  bool start(const std::string& db_path, int retention_days, std::string* err) {
    db_path_ = db_path;
    retention_days_ = retention_days;
    if (!prepare_database(err)) return false;
    running_ = true;
    writer_ = std::thread([this]() { writer_loop(); });
    return true;
  }

  /// Stop the writer thread and write what is left.
  void stop() {
    if (!running_.exchange(false)) return;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      wake_.notify_all();
    }
    if (writer_.joinable()) writer_.join();
  }

  /// Called from libjuice's thread. It only puts the event on the queue.
  void on_event(const UsageEvent& event) {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      queue_.push_back(event);
    }
    wake_.notify_one();
  }

  /// How many sessions are running right now.
  size_t open_sessions() {
    std::lock_guard<std::mutex> lock(mutex_);
    return open_sessions_;
  }

  /// The totals of the last `days` days, per day and per account.
  struct DayTotal {
    std::string day;
    std::string account;
    long long sessions = 0;
    long long seconds = 0;
    uint64_t bytes_in = 0;
    uint64_t bytes_out = 0;
  };

  /// Read the report out of the database file. Does not need a running broker.
  static bool read_report(const std::string& db_path, int days, std::vector<DayTotal>* totals,
                           long long* sessions, long long* seconds, uint64_t* bytes_in,
                           uint64_t* bytes_out, size_t* open_now, std::string* err) {
    totals->clear();
    *sessions = 0;
    *seconds = 0;
    *bytes_in = 0;
    *bytes_out = 0;
    *open_now = 0;

    // No file yet: there is simply nothing to show.
    if (!file_exists(db_path)) return true;

    sqlite3* db = nullptr;
    if (sqlite3_open_v2(db_path.c_str(), &db, SQLITE_OPEN_READONLY, nullptr) != SQLITE_OK) {
      *err = std::string("cannot open the account book '") + db_path + "': " +
             (db ? sqlite3_errmsg(db) : "unknown error");
      if (db) sqlite3_close(db);
      return false;
    }

    const std::string query =
        std::string("SELECT day, account, sessions, seconds, bytes_in, bytes_out FROM ") +
        usage_daily_table() + " WHERE day >= date('now', ?) ORDER BY day DESC, account;";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db, query.c_str(), -1, &statement, nullptr) != SQLITE_OK) {
      // A book from before the usage tables existed: nothing to show.
      const std::string message = sqlite3_errmsg(db);
      if (message.find("no such table") != std::string::npos) {
        sqlite3_close(db);
        return true;
      }
      *err = std::string("cannot read the usage from '") + db_path + "': " + message;
      sqlite3_close(db);
      return false;
    }
    const std::string window = "-" + std::to_string(days) + " days";
    sqlite3_bind_text(statement, 1, window.c_str(), -1, SQLITE_TRANSIENT);
    while (sqlite3_step(statement) == SQLITE_ROW) {
      DayTotal total;
      const unsigned char* day = sqlite3_column_text(statement, 0);
      const unsigned char* account = sqlite3_column_text(statement, 1);
      total.day = day ? reinterpret_cast<const char*>(day) : "";
      total.account = account ? reinterpret_cast<const char*>(account) : "";
      total.sessions = sqlite3_column_int64(statement, 2);
      total.seconds = sqlite3_column_int64(statement, 3);
      total.bytes_in = static_cast<uint64_t>(sqlite3_column_int64(statement, 4));
      total.bytes_out = static_cast<uint64_t>(sqlite3_column_int64(statement, 5));
      *sessions += total.sessions;
      *seconds += total.seconds;
      *bytes_in += total.bytes_in;
      *bytes_out += total.bytes_out;
      totals->push_back(total);
    }
    sqlite3_finalize(statement);

    // The sessions that are still running are not in the daily totals yet.
    const std::string open_query =
        std::string("SELECT COUNT(*) FROM ") + usage_table() + " WHERE ended_at IS NULL;";
    sqlite3_stmt* open_statement = nullptr;
    if (sqlite3_prepare_v2(db, open_query.c_str(), -1, &open_statement, nullptr) == SQLITE_OK) {
      if (sqlite3_step(open_statement) == SQLITE_ROW) {
        *open_now = static_cast<size_t>(sqlite3_column_int64(open_statement, 0));
      }
      sqlite3_finalize(open_statement);
    }
    sqlite3_close(db);
    return true;
  }

 private:
  /// Create the tables if they are not there yet.
  bool prepare_database(std::string* err) {
    sqlite3* db = nullptr;
    if (sqlite3_open_v2(db_path_.c_str(), &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE,
                        nullptr) != SQLITE_OK) {
      *err = std::string("cannot open the account book '") + db_path_ + "': " +
             (db ? sqlite3_errmsg(db) : "unknown error");
      if (db) sqlite3_close(db);
      return false;
    }
    char* message = nullptr;
    const std::string create =
        std::string("CREATE TABLE IF NOT EXISTS ") + usage_table() +
        " (id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " account TEXT NOT NULL,"
        " client TEXT NOT NULL,"
        " relayed TEXT,"
        " started_at INTEGER NOT NULL,"
        " ended_at INTEGER,"
        " seconds INTEGER NOT NULL DEFAULT 0,"
        " bytes_in INTEGER NOT NULL DEFAULT 0,"
        " bytes_out INTEGER NOT NULL DEFAULT 0,"
        " packets_in INTEGER NOT NULL DEFAULT 0,"
        " packets_out INTEGER NOT NULL DEFAULT 0);"
        "CREATE INDEX IF NOT EXISTS turn_usage_started_at ON " + usage_table() + "(started_at);"
        "CREATE INDEX IF NOT EXISTS turn_usage_account ON " + usage_table() + "(account);"
        "CREATE TABLE IF NOT EXISTS " + usage_daily_table() +
        " (day TEXT NOT NULL,"
        " account TEXT NOT NULL,"
        " sessions INTEGER NOT NULL DEFAULT 0,"
        " seconds INTEGER NOT NULL DEFAULT 0,"
        " bytes_in INTEGER NOT NULL DEFAULT 0,"
        " bytes_out INTEGER NOT NULL DEFAULT 0,"
        " PRIMARY KEY (day, account));";
    if (sqlite3_exec(db, create.c_str(), nullptr, nullptr, &message) != SQLITE_OK) {
      *err = std::string("cannot prepare the usage tables in '") + db_path_ + "': " +
             (message ? message : sqlite3_errmsg(db));
      if (message) sqlite3_free(message);
      sqlite3_close(db);
      return false;
    }
    if (message) sqlite3_free(message);
    sqlite3_close(db);
    return true;
  }

  /// The writer thread: takes the events off the queue and writes them.
  void writer_loop() {
    sqlite3* db = nullptr;
    if (sqlite3_open_v2(db_path_.c_str(), &db, SQLITE_OPEN_READWRITE, nullptr) != SQLITE_OK) {
      if (db) sqlite3_close(db);
      return;
    }
    sqlite3_exec(db, "PRAGMA journal_mode=WAL;", nullptr, nullptr, nullptr);
    sqlite3_exec(db, "PRAGMA synchronous=NORMAL;", nullptr, nullptr, nullptr);

    // The sessions that were still open when the broker stopped: they are
    // over now (the broker was the one holding them). Their bytes are what
    // was last written down - a session that was cut off does not get a
    // final count.
    close_open_sessions(db);

    prune(db);

    long long last_prune = static_cast<long long>(std::time(nullptr));
    while (true) {
      UsageEvent event;
      {
        std::unique_lock<std::mutex> lock(mutex_);
        wake_.wait(lock, [this]() { return !queue_.empty() || !running_.load(); });
        if (queue_.empty()) {
          if (!running_.load()) break;
          continue;
        }
        event = queue_.front();
        queue_.pop_front();
      }
      write_event(db, event);

      const long long now = static_cast<long long>(std::time(nullptr));
      if (now - last_prune > 3600) {
        prune(db);
        last_prune = now;
      }
    }
    // Whatever is left goes in now.
    while (true) {
      UsageEvent pending;
      {
        std::lock_guard<std::mutex> lock(mutex_);
        if (queue_.empty()) break;
        pending = queue_.front();
        queue_.pop_front();
      }
      write_event(db, pending);
    }
    sqlite3_close(db);
  }

  /// Close the sessions that were open when the broker stopped.
  void close_open_sessions(sqlite3* db) {
    const long long now = static_cast<long long>(std::time(nullptr));
    const std::string roll_sql =
        std::string("INSERT INTO ") + usage_daily_table() +
        " (day, account, sessions, seconds, bytes_in, bytes_out)"
        " SELECT date(started_at, 'unixepoch'), account, 1, ? - started_at, bytes_in, bytes_out"
        " FROM " + usage_table() + " WHERE ended_at IS NULL"
        " ON CONFLICT(day, account) DO UPDATE SET"
        " sessions = sessions + 1, seconds = seconds + excluded.seconds,"
        " bytes_in = bytes_in + excluded.bytes_in, bytes_out = bytes_out + excluded.bytes_out;";
    sqlite3_stmt* roll = nullptr;
    if (sqlite3_prepare_v2(db, roll_sql.c_str(), -1, &roll, nullptr) == SQLITE_OK) {
      sqlite3_bind_int64(roll, 1, now);
      sqlite3_step(roll);
      sqlite3_finalize(roll);
    }
    const std::string close_sql =
        std::string("UPDATE ") + usage_table() +
        " SET ended_at = ?, seconds = ? - started_at WHERE ended_at IS NULL;";
    sqlite3_stmt* close_statement = nullptr;
    if (sqlite3_prepare_v2(db, close_sql.c_str(), -1, &close_statement, nullptr) == SQLITE_OK) {
      sqlite3_bind_int64(close_statement, 1, now);
      sqlite3_bind_int64(close_statement, 2, now);
      sqlite3_step(close_statement);
      sqlite3_finalize(close_statement);
    }
  }

  /// Write one event: a new row, or the end of an open one.
  void write_event(sqlite3* db, const UsageEvent& event) {
    if (!event.released) {
      const std::string sql = std::string("INSERT INTO ") + usage_table() +
                              " (account, client, relayed, started_at, bytes_in, bytes_out,"
                              " packets_in, packets_out) VALUES (?, ?, ?, ?, ?, ?, ?, ?);";
      sqlite3_stmt* statement = nullptr;
      if (sqlite3_prepare_v2(db, sql.c_str(), -1, &statement, nullptr) != SQLITE_OK) return;
      sqlite3_bind_text(statement, 1, event.username.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(statement, 2, event.client.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(statement, 3, event.relayed.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_int64(statement, 4, event.at);
      sqlite3_bind_int64(statement, 5, static_cast<sqlite3_int64>(event.bytes_in));
      sqlite3_bind_int64(statement, 6, static_cast<sqlite3_int64>(event.bytes_out));
      sqlite3_bind_int64(statement, 7, static_cast<sqlite3_int64>(event.packets_in));
      sqlite3_bind_int64(statement, 8, static_cast<sqlite3_int64>(event.packets_out));
      sqlite3_step(statement);
      const sqlite3_int64 id = sqlite3_last_insert_rowid(db);
      sqlite3_finalize(statement);
      {
        std::lock_guard<std::mutex> lock(mutex_);
        open_[event.client] = id;
        open_sessions_ = open_.size();
      }
      return;
    }

    // The end of a session: find the row that belongs to this client.
    sqlite3_int64 id = 0;
    {
      std::lock_guard<std::mutex> lock(mutex_);
      auto found = open_.find(event.client);
      if (found == open_.end()) return;
      id = found->second;
      open_.erase(found);
      open_sessions_ = open_.size();
    }

    // The duration comes from the row itself (the start is in the database).
    const std::string read_sql =
        std::string("SELECT started_at FROM ") + usage_table() + " WHERE id = ?;";
    sqlite3_stmt* read_statement = nullptr;
    long long started_at = event.at;
    if (sqlite3_prepare_v2(db, read_sql.c_str(), -1, &read_statement, nullptr) == SQLITE_OK) {
      sqlite3_bind_int64(read_statement, 1, id);
      if (sqlite3_step(read_statement) == SQLITE_ROW) {
        started_at = sqlite3_column_int64(read_statement, 0);
      }
      sqlite3_finalize(read_statement);
    }
    const long long seconds = event.at > started_at ? event.at - started_at : 0;

    const std::string sql = std::string("UPDATE ") + usage_table() +
                            " SET ended_at = ?, seconds = ?,"
                            " bytes_in = ?, bytes_out = ?, packets_in = ?, packets_out = ?"
                            " WHERE id = ?;";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db, sql.c_str(), -1, &statement, nullptr) != SQLITE_OK) return;
    sqlite3_bind_int64(statement, 1, event.at);
    sqlite3_bind_int64(statement, 2, seconds);
    sqlite3_bind_int64(statement, 3, static_cast<sqlite3_int64>(event.bytes_in));
    sqlite3_bind_int64(statement, 4, static_cast<sqlite3_int64>(event.bytes_out));
    sqlite3_bind_int64(statement, 5, static_cast<sqlite3_int64>(event.packets_in));
    sqlite3_bind_int64(statement, 6, static_cast<sqlite3_int64>(event.packets_out));
    sqlite3_bind_int64(statement, 7, id);
    sqlite3_step(statement);
    sqlite3_finalize(statement);

    // The daily totals.
    const std::string day = usage_day(started_at);
    const std::string roll_sql =
        std::string("INSERT INTO ") + usage_daily_table() +
        " (day, account, sessions, seconds, bytes_in, bytes_out) VALUES (?, ?, 1, ?, ?, ?)"
        " ON CONFLICT(day, account) DO UPDATE SET"
        " sessions = sessions + 1, seconds = seconds + excluded.seconds,"
        " bytes_in = bytes_in + excluded.bytes_in, bytes_out = bytes_out + excluded.bytes_out;";
    sqlite3_stmt* roll = nullptr;
    if (sqlite3_prepare_v2(db, roll_sql.c_str(), -1, &roll, nullptr) == SQLITE_OK) {
      sqlite3_bind_text(roll, 1, day.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(roll, 2, event.username.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_int64(roll, 3, seconds);
      sqlite3_bind_int64(roll, 4, static_cast<sqlite3_int64>(event.bytes_in));
      sqlite3_bind_int64(roll, 5, static_cast<sqlite3_int64>(event.bytes_out));
      sqlite3_step(roll);
      sqlite3_finalize(roll);
    }

    std::printf("broker: TURN session over: %s used %s in / %s out in %s (%s)\n",
                event.username.c_str(), usage_bytes(event.bytes_in).c_str(),
                usage_bytes(event.bytes_out).c_str(), usage_duration(seconds).c_str(),
                event.client.c_str());
    std::fflush(stdout);
  }

  /// Drop the details that are older than the retention window.
  void prune(sqlite3* db) {
    if (retention_days_ <= 0) return;
    const std::string sql =
        std::string("DELETE FROM ") + usage_table() + " WHERE started_at < strftime('%s','now',?);";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db, sql.c_str(), -1, &statement, nullptr) != SQLITE_OK) return;
    const std::string window = "-" + std::to_string(retention_days_) + " days";
    sqlite3_bind_text(statement, 1, window.c_str(), -1, SQLITE_TRANSIENT);
    sqlite3_step(statement);
    sqlite3_finalize(statement);
  }

  std::string db_path_;
  int retention_days_ = 15;
  std::atomic<bool> running_{false};
  std::thread writer_;
  std::mutex mutex_;
  std::condition_variable wake_;
  std::deque<UsageEvent> queue_;
  std::map<std::string, sqlite3_int64> open_;  // client address -> row id
  size_t open_sessions_ = 0;
};

}  // namespace tea_tunnel
