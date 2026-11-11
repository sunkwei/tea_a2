// tunnel_turn_db.h -- the TURN account book (a sqlite file a person edits).
//
// The broker's TURN relay accepts the accounts in this file. The point of
// keeping them in a database instead of in the source is that **a person can
// add, change or remove an account without rebuilding anything**: open the
// file with any sqlite tool and change a row.
//
// The file is plain on purpose: the user and the password are stored as they
// are, so that editing them by hand is obvious and cannot go wrong. It is
// created with owner-only permissions (0600).
//
// The file is **a2_broker.db in the current directory** - next to the broker
// it belongs to, where the person who starts it can find it. On the first
// start it is created and the default account is written into it, so the
// broker has something to accept right away.
//
// The table is:
//
//     CREATE TABLE turn_accounts (
//       user TEXT NOT NULL,
//       pass TEXT NOT NULL
//     );
//
// and a row is one account:
//
//     INSERT INTO turn_accounts (user, pass) VALUES ('alice', 'secret');
//
#pragma once

#include <sqlite3.h>

#include <cstdio>
#include <string>
#include <vector>

#ifndef _WIN32
#include <sys/stat.h>
#endif

#include "tunnel_paths.h"

namespace tea_tunnel {

/// One TURN account.
struct TurnAccount {
  std::string user;
  std::string pass;
};

/// The table the accounts live in.
inline const char* turn_table_name() { return "turn_accounts"; }

/// Where the account book lives: **the current directory**, so it is next to
/// the broker that uses it.
inline std::string turn_db_path() { return "a2_broker.db"; }

/// The directory part of a path ("" when there is none).
inline std::string parent_directory(const std::string& path) {
  const std::size_t slash = path.find_last_of("/\\");
  if (slash == std::string::npos) return std::string();
  return path.substr(0, slash);
}

/// Is there a file at this path?
inline bool file_exists(const std::string& path) {
  std::FILE* probe = std::fopen(path.c_str(), "rb");
  if (!probe) return false;
  std::fclose(probe);
  return true;
}

/// Read the rows of the account book. The file is only opened for reading:
/// a missing file is not an error, it just means "no accounts here".
inline bool read_turn_accounts(const std::string& path, std::vector<TurnAccount>* out,
                                std::string* err) {
  out->clear();
  if (!file_exists(path)) return true;

  sqlite3* db = nullptr;
  const int rc = sqlite3_open_v2(path.c_str(), &db, SQLITE_OPEN_READONLY, nullptr);
  if (rc != SQLITE_OK) {
    *err = std::string("cannot open the account book '") + path + "': " +
           (db ? sqlite3_errmsg(db) : sqlite3_errstr(rc));
    if (db) sqlite3_close(db);
    return false;
  }
  char* message = nullptr;
  const std::string query = std::string("SELECT user, pass FROM ") + turn_table_name() + ";";
  sqlite3_stmt* statement = nullptr;
  const int prepare_rc = sqlite3_prepare_v2(db, query.c_str(), -1, &statement, nullptr);
  if (prepare_rc != SQLITE_OK) {
    *err = std::string("cannot read the account book '") + path + "': " + sqlite3_errmsg(db) +
           " (the table should be " + turn_table_name() + " with the columns user and pass)";
    sqlite3_close(db);
    return false;
  }
  while (sqlite3_step(statement) == SQLITE_ROW) {
    const unsigned char* user = sqlite3_column_text(statement, 0);
    const unsigned char* pass = sqlite3_column_text(statement, 1);
    if (user == nullptr) continue;
    TurnAccount account;
    account.user = reinterpret_cast<const char*>(user);
    account.pass = pass ? reinterpret_cast<const char*>(pass) : "";
    out->push_back(account);
  }
  sqlite3_finalize(statement);
  sqlite3_close(db);
  return true;
}

/// Read the account book, creating it (with the default accounts in it) when
/// it is not there yet.
///
/// `created` says whether the file was made by this call.
inline bool load_turn_accounts(const std::string& path, const std::vector<TurnAccount>& seed,
                               std::vector<TurnAccount>* out, std::string* err, bool* created) {
  out->clear();
  if (created) *created = false;

  const bool was_there = file_exists(path);
  const std::string parent = parent_directory(path);
  if (!parent.empty()) ensure_directory(parent);

  sqlite3* db = nullptr;
  const int rc =
      sqlite3_open_v2(path.c_str(), &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE, nullptr);
  if (rc != SQLITE_OK) {
    *err = std::string("cannot open the account book '") + path + "': " +
           (db ? sqlite3_errmsg(db) : sqlite3_errstr(rc));
    if (db) sqlite3_close(db);
    return false;
  }
  if (!was_there) {
    // A database file that sqlite just created follows the umask, and the
    // accounts in it are secrets: only the owner may read it.
#ifndef _WIN32
    ::chmod(path.c_str(), 0600);
#endif
    if (created) *created = true;
  }

  // Was the table there before? A file without it is a book that was never
  // used - it gets the default account. A table that is there but empty is
  // the operator's own doing: it stays empty.
  bool had_table = false;
  {
    const std::string query = std::string("SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
                                          "AND name='") + turn_table_name() + "';";
    sqlite3_stmt* statement = nullptr;
    if (sqlite3_prepare_v2(db, query.c_str(), -1, &statement, nullptr) == SQLITE_OK) {
      if (sqlite3_step(statement) == SQLITE_ROW) {
        had_table = sqlite3_column_int(statement, 0) > 0;
      }
      sqlite3_finalize(statement);
    }
  }

  char* message = nullptr;
  const std::string create = std::string("CREATE TABLE IF NOT EXISTS ") + turn_table_name() +
                             " (user TEXT NOT NULL, pass TEXT NOT NULL);";
  int exec_rc = sqlite3_exec(db, create.c_str(), nullptr, nullptr, &message);
  if (exec_rc != SQLITE_OK) {
    *err = std::string("cannot prepare the account book '") + path + "': " +
           (message ? message : sqlite3_errmsg(db));
    if (message) sqlite3_free(message);
    sqlite3_close(db);
    return false;
  }
  if (message) sqlite3_free(message);

  // A brand new book (or one that never had the table) gets the default
  // accounts, so that the broker has something to accept right away.
  if ((!was_there || !had_table) && !seed.empty()) {
    for (const TurnAccount& account : seed) {
      sqlite3_stmt* insert = nullptr;
      const std::string sql = std::string("INSERT INTO ") + turn_table_name() +
                              " (user, pass) VALUES (?, ?);";
      if (sqlite3_prepare_v2(db, sql.c_str(), -1, &insert, nullptr) != SQLITE_OK) {
        *err = std::string("cannot write the default account into '") + path + "': " +
               sqlite3_errmsg(db);
        sqlite3_close(db);
        return false;
      }
      sqlite3_bind_text(insert, 1, account.user.c_str(), -1, SQLITE_TRANSIENT);
      sqlite3_bind_text(insert, 2, account.pass.c_str(), -1, SQLITE_TRANSIENT);
      const int step_rc = sqlite3_step(insert);
      sqlite3_finalize(insert);
      if (step_rc != SQLITE_DONE) {
        *err = std::string("cannot write the default account into '") + path + "': " +
               sqlite3_errmsg(db);
        sqlite3_close(db);
        return false;
      }
    }
  }

  const std::string query = std::string("SELECT user, pass FROM ") + turn_table_name() + ";";
  sqlite3_stmt* statement = nullptr;
  const int prepare_rc = sqlite3_prepare_v2(db, query.c_str(), -1, &statement, nullptr);
  if (prepare_rc != SQLITE_OK) {
    *err = std::string("cannot read the account book '") + path + "': " + sqlite3_errmsg(db) +
           " (the table should be " + turn_table_name() + " with the columns user and pass)";
    sqlite3_close(db);
    return false;
  }
  while (sqlite3_step(statement) == SQLITE_ROW) {
    const unsigned char* user = sqlite3_column_text(statement, 0);
    const unsigned char* pass = sqlite3_column_text(statement, 1);
    if (user == nullptr) continue;
    TurnAccount account;
    account.user = reinterpret_cast<const char*>(user);
    account.pass = pass ? reinterpret_cast<const char*>(pass) : "";
    out->push_back(account);
  }
  sqlite3_finalize(statement);
  sqlite3_close(db);
  return true;
}

}  // namespace tea_tunnel
