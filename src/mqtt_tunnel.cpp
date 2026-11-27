// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_tunnel.cpp -- spawn/stop the sidecars, validate commands, answer them.
//
// A device may run several tunnels at the same time, so everything here is a
// table of tunnels instead of a single one. The session code the sidecar
// prints is the handle of a tunnel: the operator uses it to stop one tunnel
// without touching the others.
#include "mqtt_tunnel.h"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstring>
#include <ctime>
#include <vector>

#include <nlohmann/json.hpp>

#include "log.h"
#include "mqtt_client.h"
#include "mqtt_codec.h"
#include "tunnel_idle.h"

#ifdef _WIN32
#include <windows.h>
#else
#include <signal.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>
#if defined(__linux__)
#include <sys/prctl.h>
#endif
#endif

namespace tea {
namespace {

const char* const kStarting = "starting";
const char* const kReady = "ready";
const char* const kStopping = "stopping";
const char* const kStopped = "stopped";
const char* const kFailed = "failed";

/// How many commands may run at the same time. Four is the number of tunnels
/// a device may hold, so every wake can be served while the others wait.
const int kCommandWorkers = 4;

/// The sidecar prints its client command; only the code is needed here.
bool extract_session(const std::string& text, std::string* code) {
  const std::string marker = "--session ";
  size_t at = text.find(marker);
  if (at == std::string::npos) return false;
  at += marker.size();
  size_t end = at;
  while (end < text.size() && std::isxdigit(static_cast<unsigned char>(text[end]))) ++end;
  if (end - at < 8) return false;
  *code = text.substr(at, end - at);
  return true;
}

#ifdef _WIN32
std::wstring widen(const std::string& text) {
  if (text.empty()) return std::wstring();
  const int needed =
      MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), nullptr, 0);
  std::wstring out(static_cast<size_t>(needed), L'\0');
  MultiByteToWideChar(CP_UTF8, 0, text.data(), static_cast<int>(text.size()), &out[0], needed);
  return out;
}

std::wstring quote_arg(const std::string& text) {
  const std::wstring wide = widen(text);
  if (wide.find(L' ') == std::wstring::npos && wide.find(L'\t') == std::wstring::npos) return wide;
  return L"\"" + wide + L"\"";
}
#endif

}  // namespace

// ---- TunnelController ------------------------------------------------------

struct TunnelController::Impl {
  /// One tunnel: the sidecar, its session code and how it ended.
  struct Entry {
    long long id = 0;
    std::string session;
    std::string state = kStarting;
    std::string broker;
    std::string error;
    int wait_timeout = 0;
    long long created_at = 0;
    long long expires_at = 0;
    long long finished_at = 0;
    bool stop_requested = false;
    std::string output;  // tail of the sidecar's stdout, for the session code
#ifdef _WIN32
    HANDLE process = nullptr;
    HANDLE job = nullptr;
    HANDLE output_pipe = nullptr;
#else
    long long child_pid = 0;
    int output_fd = -1;
#endif
  };

  mutable std::mutex mutex;
  std::condition_variable cv;
  std::string executable;  // path to a2_tunnel
  std::string target;      // what the sidecar proxies (this agent)
  int idle_timeout = 1800;
  long long next_id = 1;
  std::map<long long, std::shared_ptr<Entry>> entries;
  std::string last_exit_reason;
  std::vector<std::thread> readers;

  /// Is the tunnel still there? A finished one stays in the table for a
  /// while (so the answer of the command that started it can still be read)
  /// but it is not a tunnel any more.
  static bool live(const Entry& entry) {
    return entry.state == kStarting || entry.state == kReady || entry.state == kStopping;
  }

  Entry* find_by_session(const std::string& session) {
    for (std::map<long long, std::shared_ptr<Entry>>::iterator it = entries.begin();
         it != entries.end(); ++it) {
      if (live(*it->second) && it->second->session == session) return it->second.get();
    }
    return nullptr;
  }

  int live_count() const {
    int count = 0;
    for (std::map<long long, std::shared_ptr<Entry>>::const_iterator it = entries.begin();
         it != entries.end(); ++it) {
      if (live(*it->second)) ++count;
    }
    return count;
  }

  /// Drops the tunnels that finished long ago.
  void prune() {
    const long long now = std::time(nullptr);
    for (std::map<long long, std::shared_ptr<Entry>>::iterator it = entries.begin();
         it != entries.end();) {
      const Entry& entry = *it->second;
      if (!live(entry) && entry.finished_at > 0 && now - entry.finished_at > 300) {
        it = entries.erase(it);
      } else {
        ++it;
      }
    }
  }

  bool spawn(const std::shared_ptr<Entry>& entry, const std::string& broker, int wait_timeout_s);
  void read_output(const std::shared_ptr<Entry>& entry);
  /// SIGTERM (or SIGKILL with `force`) to the sidecar and its group.
  void kill_entry(Entry* entry, bool force);
};

#ifndef _WIN32
bool TunnelController::Impl::spawn(const std::shared_ptr<Entry>& entry, const std::string& broker,
                                   int wait_timeout_s) {
  // Fail early and say which path: a missing sidecar would otherwise surface
  // only as "exited before printing a session code", which says nothing.
  if (::access(executable.c_str(), X_OK) != 0) {
    entry->error = "cannot run " + executable + ": " + std::strerror(errno);
    return false;
  }
  int fds[2];
  if (::pipe(fds) != 0) {
    entry->error = "pipe() failed";
    return false;
  }
  std::vector<std::string> args;
  args.push_back(executable);
  args.push_back("--role");
  args.push_back("agent");
  args.push_back("--broker");
  args.push_back(broker);
  args.push_back("--target");
  args.push_back(target);
  args.push_back("--wait-timeout");
  args.push_back(std::to_string(wait_timeout_s));
  args.push_back("--idle-timeout");
  args.push_back(std::to_string(idle_timeout));
  std::vector<char*> argv;
  for (size_t i = 0; i < args.size(); ++i) argv.push_back(const_cast<char*>(args[i].c_str()));
  argv.push_back(nullptr);

  const pid_t pid = ::fork();
  if (pid < 0) {
    ::close(fds[0]);
    ::close(fds[1]);
    entry->error = "fork() failed";
    return false;
  }
  if (pid == 0) {
    ::setsid();
#if defined(__linux__)
    ::prctl(PR_SET_PDEATHSIG, SIGTERM);
#endif
    ::dup2(fds[1], STDOUT_FILENO);
    ::dup2(fds[1], STDERR_FILENO);
    ::close(fds[0]);
    ::close(fds[1]);
    ::execv(executable.c_str(), argv.data());
    ::_exit(127);
  }
  ::close(fds[1]);
  entry->child_pid = pid;
  entry->output_fd = fds[0];
  readers.emplace_back([this, entry]() { read_output(entry); });
  return true;
}

void TunnelController::Impl::read_output(const std::shared_ptr<Entry>& entry) {
  char chunk[4096];
  for (;;) {
    const ssize_t n = ::read(entry->output_fd, chunk, sizeof(chunk));
    if (n <= 0) break;
    std::lock_guard<std::mutex> lock(mutex);
    entry->output.append(chunk, static_cast<size_t>(n));
    if (entry->output.size() > 8192) entry->output.erase(0, entry->output.size() - 8192);
    if (entry->state == kStarting) {
      std::string code;
      if (extract_session(entry->output, &code)) {
        entry->session = code;
        entry->state = kReady;
        cv.notify_all();
      }
    }
  }
  int exit_code = 0;
  if (entry->child_pid > 0) {
    int child_status = 0;
    if (::waitpid(static_cast<pid_t>(entry->child_pid), &child_status, 0) > 0 &&
        WIFEXITED(child_status)) {
      exit_code = WEXITSTATUS(child_status);
    }
  }
  std::lock_guard<std::mutex> lock(mutex);
  if (entry->state == kStarting) {
    entry->state = kFailed;
    entry->error = "the sidecar exited before printing a session code";
    last_exit_reason = "failed";
  } else if (entry->state == kReady || entry->state == kStopping) {
    entry->state = kStopped;
    if (entry->stop_requested) {
      last_exit_reason = "stopped";
    } else if (exit_code == tea_tunnel::kIdleExitCode) {
      last_exit_reason = "idle";
    } else {
      last_exit_reason = "exited";
    }
  }
  entry->finished_at = std::time(nullptr);
  cv.notify_all();
}

void TunnelController::Impl::kill_entry(Entry* entry, bool force) {
  if (entry->child_pid > 0) {
    const int signal_number = force ? SIGKILL : SIGTERM;
    ::kill(-static_cast<pid_t>(entry->child_pid), signal_number);
    ::kill(static_cast<pid_t>(entry->child_pid), signal_number);
  }
}
#else
bool TunnelController::Impl::spawn(const std::shared_ptr<Entry>& entry, const std::string& broker,
                                   int wait_timeout_s) {
  const DWORD attributes = GetFileAttributesW(widen(executable).c_str());
  if (attributes == INVALID_FILE_ATTRIBUTES || (attributes & FILE_ATTRIBUTE_DIRECTORY)) {
    entry->error = "cannot run " + executable + " (not found)";
    return false;
  }
  SECURITY_ATTRIBUTES sa;
  std::memset(&sa, 0, sizeof(sa));
  sa.nLength = sizeof(sa);
  sa.bInheritHandle = TRUE;
  HANDLE read_end = nullptr;
  HANDLE write_end = nullptr;
  if (!CreatePipe(&read_end, &write_end, &sa, 0)) {
    entry->error = "CreatePipe failed";
    return false;
  }
  SetHandleInformation(read_end, HANDLE_FLAG_INHERIT, 0);

  STARTUPINFOW si;
  std::memset(&si, 0, sizeof(si));
  si.cb = sizeof(si);
  si.dwFlags = STARTF_STD_HANDLES;
  si.hStdOutput = write_end;
  si.hStdError = write_end;
  si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);

  std::wstring command = quote_arg(executable) + L" --role agent --broker " + quote_arg(broker) +
                         L" --target " + quote_arg(target) + L" --wait-timeout " +
                         widen(std::to_string(wait_timeout_s)) + L" --idle-timeout " +
                         widen(std::to_string(idle_timeout));
  std::vector<wchar_t> mutable_command(command.begin(), command.end());
  mutable_command.push_back(L'\0');

  PROCESS_INFORMATION pi;
  std::memset(&pi, 0, sizeof(pi));
  const BOOL created = CreateProcessW(nullptr, mutable_command.data(), nullptr, nullptr, TRUE,
                                      CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, nullptr,
                                      nullptr, &si, &pi);
  CloseHandle(write_end);
  if (!created) {
    CloseHandle(read_end);
    entry->error = "CreateProcess failed (" + std::to_string(GetLastError()) + ")";
    return false;
  }
  CloseHandle(pi.hThread);
  entry->process = pi.hProcess;
  entry->job = CreateJobObjectW(nullptr, nullptr);
  if (entry->job) {
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    std::memset(&limits, 0, sizeof(limits));
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    SetInformationJobObject(entry->job, JobObjectExtendedLimitInformation, &limits, sizeof(limits));
    AssignProcessToJobObject(entry->job, entry->process);
  }
  entry->output_pipe = read_end;
  readers.emplace_back([this, entry]() { read_output(entry); });
  return true;
}

void TunnelController::Impl::read_output(const std::shared_ptr<Entry>& entry) {
  char chunk[4096];
  for (;;) {
    DWORD got = 0;
    if (!ReadFile(entry->output_pipe, chunk, sizeof(chunk), &got, nullptr) || got == 0) break;
    std::lock_guard<std::mutex> lock(mutex);
    entry->output.append(chunk, got);
    if (entry->output.size() > 8192) entry->output.erase(0, entry->output.size() - 8192);
    if (entry->state == kStarting) {
      std::string code;
      if (extract_session(entry->output, &code)) {
        entry->session = code;
        entry->state = kReady;
        cv.notify_all();
      }
    }
  }
  int exit_code = 0;
  if (entry->process) {
    WaitForSingleObject(entry->process, 5000);
    DWORD code = 0;
    if (GetExitCodeProcess(entry->process, &code)) exit_code = static_cast<int>(code);
    CloseHandle(entry->process);
    entry->process = nullptr;
  }
  std::lock_guard<std::mutex> lock(mutex);
  if (entry->state == kStarting) {
    entry->state = kFailed;
    entry->error = "the sidecar exited before printing a session code";
    last_exit_reason = "failed";
  } else if (entry->state == kReady || entry->state == kStopping) {
    entry->state = kStopped;
    if (entry->stop_requested) {
      last_exit_reason = "stopped";
    } else if (exit_code == tea_tunnel::kIdleExitCode) {
      last_exit_reason = "idle";
    } else {
      last_exit_reason = "exited";
    }
  }
  entry->finished_at = std::time(nullptr);
  cv.notify_all();
}

void TunnelController::Impl::kill_entry(Entry* entry, bool force) {
  (void)force;  // TerminateJobObject has no gentle mode
  if (entry->job) {
    TerminateJobObject(entry->job, 1);
    CloseHandle(entry->job);
    entry->job = nullptr;
  }
}
#endif

const int TunnelController::kMaxTunnels;

TunnelController::TunnelController() : impl_(new Impl()) {}
TunnelController::~TunnelController() { shutdown(); }

void TunnelController::configure(const std::string& executable, const std::string& target) {
  std::lock_guard<std::mutex> lock(impl_->mutex);
  impl_->executable = executable;
  impl_->target = target;
}

void TunnelController::set_idle_timeout(int seconds) {
  std::lock_guard<std::mutex> lock(impl_->mutex);
  impl_->idle_timeout = seconds > 0 ? seconds : 0;
}

bool TunnelController::start(const std::string& broker, int wait_timeout_s, long long* id,
                             std::string* error) {
  std::shared_ptr<Impl::Entry> entry;
  std::string executable;
  std::string target;
  int idle_timeout = 0;
  {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    impl_->prune();
    if (impl_->live_count() >= TunnelController::kMaxTunnels) {
      if (error) *error = "too_many_tunnels";
      return false;
    }
    entry = std::make_shared<Impl::Entry>();
    entry->id = impl_->next_id++;
    entry->state = kStarting;
    entry->broker = broker;
    entry->wait_timeout = wait_timeout_s;
    entry->created_at = std::time(nullptr);
    entry->expires_at = entry->created_at + wait_timeout_s;
    impl_->entries[entry->id] = entry;
    executable = impl_->executable;
    target = impl_->target;
    idle_timeout = impl_->idle_timeout;
    if (!impl_->spawn(entry, broker, wait_timeout_s)) {
      impl_->entries.erase(entry->id);
      if (error) *error = entry->error;
      return false;
    }
    if (id) *id = entry->id;
  }
  LOG_INFO("mqtt: ", "tunnel.start: spawning " + executable + " (target " + target + ", wait " +
                       std::to_string(wait_timeout_s) + "s, idle " +
                       std::to_string(idle_timeout) + "s)");
  return true;
}

TunnelController::Status TunnelController::wait_for(long long id, int timeout_s) {
  std::unique_lock<std::mutex> lock(impl_->mutex);
  impl_->cv.wait_for(lock, std::chrono::seconds(timeout_s), [this, id]() {
    std::map<long long, std::shared_ptr<Impl::Entry>>::const_iterator it = impl_->entries.find(id);
    if (it == impl_->entries.end()) return true;
    return it->second->state == kReady || it->second->state == kFailed ||
           it->second->state == kStopped;
  });
  Status out;
  std::map<long long, std::shared_ptr<Impl::Entry>>::const_iterator it = impl_->entries.find(id);
  if (it == impl_->entries.end()) {
    out.state = kFailed;
    out.error = "the tunnel is gone";
    return out;
  }
  const Impl::Entry& entry = *it->second;
  out.state = entry.state;
  out.session = entry.session;
  out.broker = entry.broker;
  out.error = entry.error;
  return out;
}

TunnelController::Status TunnelController::stop(const std::string& session) {
  std::shared_ptr<Impl::Entry> entry;
  {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    Impl::Entry* found = impl_->find_by_session(session);
    if (found == nullptr) {
      Status out;
      out.state = kFailed;
      out.error = "unknown_session";
      return out;
    }
    entry = impl_->entries[found->id];
    entry->stop_requested = true;
    entry->state = kStopping;
    impl_->kill_entry(entry.get(), false);
  }
  {
    std::unique_lock<std::mutex> lock(impl_->mutex);
    const bool gone = impl_->cv.wait_for(lock, std::chrono::seconds(5), [this, &entry]() {
      std::map<long long, std::shared_ptr<Impl::Entry>>::const_iterator it =
          impl_->entries.find(entry->id);
      if (it == impl_->entries.end()) return true;
      return it->second->state == kStopped || it->second->state == kFailed;
    });
    if (!gone) {
      // A sidecar that ignores SIGTERM would keep the reader blocked for
      // ever; the agent has to be able to stop.
      impl_->kill_entry(entry.get(), true);
      impl_->cv.wait_for(lock, std::chrono::seconds(2), [this, &entry]() {
        std::map<long long, std::shared_ptr<Impl::Entry>>::const_iterator it =
            impl_->entries.find(entry->id);
        if (it == impl_->entries.end()) return true;
        return it->second->state == kStopped || it->second->state == kFailed;
      });
    }
  }
  LOG_INFO("mqtt: ", "tunnel.stop: session " + session + " stopped");
  Status out;
  out.state = kStopped;
  out.session = session;
  return out;
}

bool TunnelController::lookup(const std::string& session, Status* out) const {
  std::lock_guard<std::mutex> lock(impl_->mutex);
  const Impl::Entry* found = impl_->find_by_session(session);
  if (found == nullptr) return false;
  if (out) {
    out->state = found->state;
    out->session = found->session;
    out->broker = found->broker;
    out->error = found->error;
  }
  return true;
}

std::vector<TunnelController::TunnelInfo> TunnelController::list() const {
  std::lock_guard<std::mutex> lock(impl_->mutex);
  std::vector<TunnelInfo> out;
  for (std::map<long long, std::shared_ptr<Impl::Entry>>::const_iterator it = impl_->entries.begin();
       it != impl_->entries.end(); ++it) {
    if (!Impl::live(*it->second)) continue;
    TunnelInfo info;
    info.session = it->second->session;
    info.state = it->second->state;
    info.broker = it->second->broker;
    info.wait_timeout = it->second->wait_timeout;
    info.created_at = it->second->created_at;
    info.expires_at = it->second->expires_at;
    out.push_back(info);
  }
  return out;
}

std::string TunnelController::last_exit_reason() const {
  std::lock_guard<std::mutex> lock(impl_->mutex);
  return impl_->last_exit_reason;
}

void TunnelController::shutdown() {
  {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    for (std::map<long long, std::shared_ptr<Impl::Entry>>::iterator it = impl_->entries.begin();
         it != impl_->entries.end(); ++it) {
      if (!Impl::live(*it->second)) continue;
      it->second->stop_requested = true;
      it->second->state = kStopping;
      impl_->kill_entry(it->second.get(), false);
    }
  }
  {
    std::unique_lock<std::mutex> lock(impl_->mutex);
    impl_->cv.wait_for(lock, std::chrono::seconds(3), [this]() {
      for (std::map<long long, std::shared_ptr<Impl::Entry>>::const_iterator it =
               impl_->entries.begin();
           it != impl_->entries.end(); ++it) {
        if (Impl::live(*it->second)) return false;
      }
      return true;
    });
    for (std::map<long long, std::shared_ptr<Impl::Entry>>::iterator it = impl_->entries.begin();
         it != impl_->entries.end(); ++it) {
      if (Impl::live(*it->second)) impl_->kill_entry(it->second.get(), true);
    }
  }
  // The readers take the lock themselves, so they are joined outside of it.
  std::vector<std::thread> readers;
  {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    readers.swap(impl_->readers);
  }
  for (std::vector<std::thread>::iterator it = readers.begin(); it != readers.end(); ++it) {
    if (it->joinable()) it->join();
  }
}

// ---- CommandHandler --------------------------------------------------------

struct CommandHandler::Impl {
  /// The answer of one command id. A command id is answered once: MQTT
  /// delivers at least once, and a redelivered "tunnel.start" must not start
  /// a second tunnel.
  struct Slot {
    bool done = false;
    std::string record;
  };

  struct Outcome {
    std::string record;
    bool duplicate = false;
  };

  MqttClient* client = nullptr;
  TunnelController* tunnel = nullptr;
  Options options;
  std::vector<std::thread> workers;
  std::mutex mutex;
  std::condition_variable cv;
  std::deque<std::string> queue;
  std::atomic<bool> stopping{false};

  std::map<std::string, std::shared_ptr<Slot>> answers;
  std::deque<std::string> order;

  /// The arguments come from the network: a wrong type must not throw its
  /// way out of the worker (an uncaught exception would take the agent
  /// down), it is simply "not given".
  static std::string string_arg(const nlohmann::json& args, const char* key) {
    if (!args.is_object() || !args.contains(key)) return std::string();
    const nlohmann::json& value = args[key];
    return value.is_string() ? value.get<std::string>() : std::string();
  }

  static bool bool_arg(const nlohmann::json& args, const char* key) {
    if (!args.is_object() || !args.contains(key)) return false;
    const nlohmann::json& value = args[key];
    return value.is_boolean() ? value.get<bool>() : false;
  }

  std::string answer(const std::string& id, const std::string& state,
                     const std::string& error = std::string()) {
    nlohmann::json out;
    out["id"] = id;
    out["state"] = state;
    if (!error.empty()) out["error"] = error;
    return out.dump();
  }

  nlohmann::json tunnel_list() const {
    nlohmann::json array = nlohmann::json::array();
    if (!tunnel) return array;
    const std::vector<TunnelController::TunnelInfo> tunnels = tunnel->list();
    const long long now = std::time(nullptr);
    for (std::vector<TunnelController::TunnelInfo>::const_iterator it = tunnels.begin();
         it != tunnels.end(); ++it) {
      nlohmann::json item;
      item["session"] = it->session;
      item["state"] = it->state;
      item["wait_timeout"] = it->wait_timeout;
      item["created_at"] = it->created_at;
      item["expires_in"] = it->expires_at > now ? it->expires_at - now : 0;
      array.push_back(item);
    }
    return array;
  }

  std::string do_status(const std::string& id, const nlohmann::json& args);
  std::string do_stop(const std::string& id, const nlohmann::json& args);
  std::string do_start(const std::string& id, const nlohmann::json& args);
  std::string run_command(const nlohmann::json& request, const std::string& id);
  Outcome execute(const std::string& payload);
  void run();
};

std::string CommandHandler::Impl::do_status(const std::string& id, const nlohmann::json& args) {
  const std::string session = string_arg(args, "session");
  nlohmann::json out;
  out["id"] = id;
  out["state"] = "status";
  out["mqtt"] = (client && client->connected()) ? "connected" : "offline";
  if (!session.empty()) {
    TunnelController::Status current;
    if (!tunnel || !tunnel->lookup(session, &current)) {
      out["tunnel"] = "unknown";
      out["error"] = "unknown_session";
      return out.dump();
    }
    out["tunnel"] = current.state;
    out["session"] = current.session;
    if (!current.error.empty()) out["error"] = current.error;
    return out.dump();
  }
  const nlohmann::json tunnels = tunnel_list();
  bool any_ready = false;
  for (nlohmann::json::const_iterator it = tunnels.begin(); it != tunnels.end(); ++it) {
    if (it->value("state", "") == kReady) any_ready = true;
  }
  out["tunnels"] = tunnels;
  // The old single field stays: it is the aggregate of the table.
  out["tunnel"] = any_ready ? std::string(kReady) : std::string(kStopped);
  return out.dump();
}

std::string CommandHandler::Impl::do_stop(const std::string& id, const nlohmann::json& args) {
  const std::string session = string_arg(args, "session");
  if (session.empty()) return answer(id, "rejected", "session_required");
  const TunnelController::Status stopped = tunnel->stop(session);
  if (stopped.error == "unknown_session") return answer(id, "rejected", "unknown_session");
  nlohmann::json out;
  out["id"] = id;
  out["state"] = kStopped;
  out["session"] = session;
  return out.dump();
}

std::string CommandHandler::Impl::do_start(const std::string& id, const nlohmann::json& args) {
  const std::string target = string_arg(args, "target");
  if (!target.empty() && target != options.default_target) {
    return answer(id, "rejected", "not_allowed");
  }
  int wait_timeout = options.default_wait_timeout;
  if (args.contains("wait_timeout") && args["wait_timeout"].is_number_integer()) {
    wait_timeout = args["wait_timeout"].get<int>();
  }
  if (wait_timeout < 30) wait_timeout = 30;
  if (wait_timeout > options.max_wait_timeout) wait_timeout = options.max_wait_timeout;

  // --reuse: hand out a tunnel that is already running instead of a new one.
  if (bool_arg(args, "reuse") && tunnel) {
    const std::vector<TunnelController::TunnelInfo> tunnels = tunnel->list();
    for (std::vector<TunnelController::TunnelInfo>::const_iterator it = tunnels.begin();
         it != tunnels.end(); ++it) {
      if (it->state != kReady || it->session.empty()) continue;
      nlohmann::json out;
      out["id"] = id;
      out["state"] = kReady;
      out["session"] = it->session;
      out["broker"] = it->broker;
      out["expires_in"] = it->wait_timeout;
      return out.dump();
    }
  }

  long long tunnel_id = 0;
  std::string error;
  if (!tunnel->start(options.tunnel_broker, wait_timeout, &tunnel_id, &error)) {
    if (error == "too_many_tunnels") {
      nlohmann::json out;
      out["id"] = id;
      out["state"] = "rejected";
      out["error"] = error;
      // Say what is running: the operator has to stop one of them first.
      out["tunnels"] = tunnel_list();
      return out.dump();
    }
    return answer(id, kFailed, error);
  }
  if (client && client->connected()) {
    client->publish(options.resp_topic, answer(id, kStarting), 1);
  }
  const TunnelController::Status started = tunnel->wait_for(tunnel_id, 25);
  LOG_INFO("mqtt: ", "tunnel.start: state=" + started.state +
                       (started.error.empty() ? std::string() : " error=" + started.error));
  if (started.state == kReady) {
    nlohmann::json out;
    out["id"] = id;
    out["state"] = kReady;
    out["session"] = started.session;
    out["broker"] = started.broker;
    out["expires_in"] = wait_timeout;
    return out.dump();
  }
  if (started.state == kFailed) return answer(id, kFailed, started.error);
  return answer(id, kStopped, started.error);
}

std::string CommandHandler::Impl::run_command(const nlohmann::json& request, const std::string& id) {
  const std::string action = string_arg(request, "action");
  LOG_INFO("mqtt: ", "command " + id + " action=" + action);
  const long long ts = request.contains("ts") && request["ts"].is_number_integer()
                           ? request["ts"].get<long long>()
                           : 0LL;
  const int ttl = request.contains("ttl") && request["ttl"].is_number_integer()
                      ? request["ttl"].get<int>()
                      : 0;
  const long long now = static_cast<long long>(std::time(nullptr));
  if (ts > 0 && ttl > 0 && now - ts > ttl) {
    return answer(id, "rejected", "expired");
  }
  if (action != "tunnel.start" && action != "tunnel.stop" && action != "status") {
    return answer(id, "rejected", "unsupported_action");
  }

  nlohmann::json args = nlohmann::json::object();
  if (request.contains("args") && request["args"].is_object()) args = request["args"];

  if (action == "status") return do_status(id, args);
  if (!tunnel) return answer(id, kFailed, "the tunnel controller is not running");
  if (action == "tunnel.stop") return do_stop(id, args);
  return do_start(id, args);
}

CommandHandler::Impl::Outcome CommandHandler::Impl::execute(const std::string& payload) {
  Outcome outcome;
  nlohmann::json request;
  try {
    request = nlohmann::json::parse(payload);
  } catch (...) {
    return outcome;
  }
  if (!request.is_object()) return outcome;
  const std::string id = string_arg(request, "id");
  if (id.empty()) return outcome;

  std::shared_ptr<Slot> slot;
  {
    std::unique_lock<std::mutex> lock(mutex);
    std::map<std::string, std::shared_ptr<Slot>>::iterator it = answers.find(id);
    if (it != answers.end()) {
      // The same command id: either it is answered already, or another
      // worker is running it right now. Either way it must not run twice.
      slot = it->second;
      if (!slot->done) {
        cv.wait_for(lock, std::chrono::seconds(30), [&slot]() { return slot->done; });
      }
      outcome.record = slot->record;
      outcome.duplicate = true;
      return outcome;
    }
    slot = std::make_shared<Slot>();
    answers[id] = slot;
    order.push_back(id);
  }

  std::string record;
  try {
    record = run_command(request, id);
  } catch (const std::exception& error) {
    // A broken command must not take the worker (and with it the agent)
    // down: the operator gets an answer and the tunnel table stays sane.
    record = answer(id, "rejected", std::string("bad_command: ") + error.what());
  }
  {
    std::lock_guard<std::mutex> lock(mutex);
    slot->record = record;
    slot->done = true;
    // Keep the table small, but never drop a command that is still running.
    while (order.size() > 64) {
      bool dropped = false;
      for (std::deque<std::string>::iterator it = order.begin(); it != order.end(); ++it) {
        std::map<std::string, std::shared_ptr<Slot>>::iterator found = answers.find(*it);
        if (found != answers.end() && found->second->done) {
          answers.erase(found);
          order.erase(it);
          dropped = true;
          break;
        }
      }
      if (!dropped) break;
    }
  }
  cv.notify_all();
  outcome.record = record;
  return outcome;
}

void CommandHandler::Impl::run() {
  while (!stopping.load()) {
    std::string payload;
    {
      std::unique_lock<std::mutex> lock(mutex);
      cv.wait_for(lock, std::chrono::milliseconds(500),
                  [this]() { return !queue.empty() || stopping.load(); });
      if (queue.empty()) continue;
      payload = queue.front();
      queue.pop_front();
    }
    const Outcome outcome = execute(payload);
    if (outcome.record.empty()) continue;
    const bool online = client && client->connected();
    LOG_INFO("mqtt: ", "answer " + options.resp_topic + " (" +
                         (online ? "connected" : "offline") + "): " + outcome.record);
    // A redelivered command is answered again: the operator may have missed
    // the first answer. What it must not do is run twice - and it does not:
    // the answer comes from the table, the tunnel is not started again.
    if (online) client->publish(options.resp_topic, outcome.record, 1);
  }
}

CommandHandler::CommandHandler() : impl_(new Impl()) {}
CommandHandler::~CommandHandler() { stop(); }

void CommandHandler::start(MqttClient* client, TunnelController* tunnel, const Options& options) {
  impl_->client = client;
  impl_->tunnel = tunnel;
  impl_->options = options;
  for (int i = 0; i < kCommandWorkers; ++i) {
    impl_->workers.emplace_back([this]() { impl_->run(); });
  }
}

void CommandHandler::on_message(const std::string& topic, const std::string& payload) {
  (void)topic;
  {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    if (impl_->queue.size() > 32) return;  // a flood is not a reason to grow memory
    impl_->queue.push_back(payload);
  }
  impl_->cv.notify_one();
}

void CommandHandler::stop() {
  impl_->stopping.store(true);
  impl_->cv.notify_all();
  for (std::vector<std::thread>::iterator it = impl_->workers.begin(); it != impl_->workers.end();
       ++it) {
    if (it->joinable()) it->join();
  }
  impl_->workers.clear();
}

}  // namespace tea
