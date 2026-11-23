// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// mqtt_tunnel.cpp -- spawn/stop the sidecar, validate commands, answer them.
#include "mqtt_tunnel.h"

#include <chrono>
#include <cctype>
#include <cstring>
#include <ctime>
#include <vector>

#include <nlohmann/json.hpp>

#include "log.h"
#include "mqtt_client.h"
#include "mqtt_codec.h"

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
  mutable std::mutex mutex;
  std::condition_variable ready_cv;
  Status status;
  std::string executable;  // path to a2_tunnel
  std::string target;      // what the sidecar proxies (this agent)
  std::string output;      // tail of the sidecar's stdout, for the session code
  std::thread reader;
  std::atomic<bool> shutting_down{false};

#ifdef _WIN32
  HANDLE process = nullptr;
  HANDLE job = nullptr;
  HANDLE output_pipe = nullptr;
#else
  long long child_pid = 0;
  int output_fd = -1;
#endif

  ~Impl() {
    kill_now(true);
    if (reader.joinable()) reader.join();
  }

  void set_state(const std::string& state, const std::string& error = std::string()) {
    status.state = state;
    status.error = error;
  }

  bool spawn(const std::string& broker, int wait_timeout_s);
  /// SIGTERM (or SIGKILL with `force`). The reader thread notices the pipe end
  /// and exits; the caller joins it outside the lock.
  void kill_now(bool force = false);
};

#ifndef _WIN32
bool TunnelController::Impl::spawn(const std::string& broker, int wait_timeout_s) {
  int fds[2];
  if (::pipe(fds) != 0) {
    set_state(kFailed, "pipe() failed");
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
  std::vector<char*> argv;
  for (size_t i = 0; i < args.size(); ++i) argv.push_back(const_cast<char*>(args[i].c_str()));
  argv.push_back(nullptr);

  const pid_t pid = ::fork();
  if (pid < 0) {
    ::close(fds[0]);
    ::close(fds[1]);
    set_state(kFailed, "fork() failed");
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
  child_pid = pid;
  output_fd = fds[0];
  reader = std::thread([this]() {
    char chunk[4096];
    for (;;) {
      const ssize_t n = ::read(output_fd, chunk, sizeof(chunk));
      if (n <= 0) break;
      std::lock_guard<std::mutex> lock(mutex);
      output.append(chunk, static_cast<size_t>(n));
      if (output.size() > 8192) output.erase(0, output.size() - 8192);
      if (status.state == kStarting) {
        std::string code;
        if (extract_session(output, &code)) {
          status.session = code;
          status.state = kReady;
          ready_cv.notify_all();
        }
      }
    }
    int child_status = 0;
    if (child_pid > 0) ::waitpid(static_cast<pid_t>(child_pid), &child_status, 0);
    {
      std::lock_guard<std::mutex> lock(mutex);
      if (status.state == kStarting) {
        status.state = kFailed;
        status.error = "the sidecar exited before printing a session code";
      } else if (status.state != kFailed) {
        status.state = kStopped;
      }
      status.session.clear();
      child_pid = 0;
      output_fd = -1;
      ready_cv.notify_all();
    }
  });
  return true;
}

void TunnelController::Impl::kill_now(bool force) {
  if (child_pid > 0) {
    const int signal_number = force ? SIGKILL : SIGTERM;
    ::kill(-static_cast<pid_t>(child_pid), signal_number);
    ::kill(static_cast<pid_t>(child_pid), signal_number);
  }
}
#else
bool TunnelController::Impl::spawn(const std::string& broker, int wait_timeout_s) {
  SECURITY_ATTRIBUTES sa;
  std::memset(&sa, 0, sizeof(sa));
  sa.nLength = sizeof(sa);
  sa.bInheritHandle = TRUE;
  HANDLE read_end = nullptr;
  HANDLE write_end = nullptr;
  if (!CreatePipe(&read_end, &write_end, &sa, 0)) {
    set_state(kFailed, "CreatePipe failed");
    return false;
  }
  SetHandleInformation(read_end, HANDLE_FLAG_INHERIT, 0);

  STARTUPINFOW si;
  std::memset(&si, 0, sizeof(si));
  si.cb = sizeof(si);
  si.dwFlags = STARTF_USESTDHANDLES;
  si.hStdOutput = write_end;
  si.hStdError = write_end;
  si.hStdInput = GetStdHandle(STD_INPUT_HANDLE);

  std::wstring command = quote_arg(executable) + L" --role agent --broker " + quote_arg(broker) +
                         L" --target " + quote_arg(target) + L" --wait-timeout " +
                         widen(std::to_string(wait_timeout_s));
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
    set_state(kFailed, "CreateProcess failed (" + std::to_string(GetLastError()) + ")");
    return false;
  }
  CloseHandle(pi.hThread);
  process = pi.hProcess;
  job = CreateJobObjectW(nullptr, nullptr);
  if (job) {
    JOBOBJECT_EXTENDED_LIMIT_INFORMATION limits;
    std::memset(&limits, 0, sizeof(limits));
    limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    SetInformationJobObject(job, JobObjectExtendedLimitInformation, &limits, sizeof(limits));
    AssignProcessToJobObject(job, process);
  }
  output_pipe = read_end;
  reader = std::thread([this]() {
    char chunk[4096];
    DWORD got = 0;
    for (;;) {
      if (!ReadFile(output_pipe, chunk, sizeof(chunk), &got, nullptr) || got == 0) break;
      std::lock_guard<std::mutex> lock(mutex);
      output.append(chunk, got);
      if (output.size() > 8192) output.erase(0, output.size() - 8192);
      if (status.state == kStarting) {
        std::string code;
        if (extract_session(output, &code)) {
          status.session = code;
          status.state = kReady;
          ready_cv.notify_all();
        }
      }
    }
    if (process) {
      WaitForSingleObject(process, 5000);
      CloseHandle(process);
      process = nullptr;
    }
    {
      std::lock_guard<std::mutex> lock(mutex);
      if (status.state == kStarting) {
        status.state = kFailed;
        status.error = "the sidecar exited before printing a session code";
      } else if (status.state != kFailed) {
        status.state = kStopped;
      }
      status.session.clear();
      ready_cv.notify_all();
    }
  });
  return true;
}

void TunnelController::Impl::kill_now(bool force) {
  (void)force;  // TerminateJobObject has no gentle mode
  if (job) {
    TerminateJobObject(job, 1);
    CloseHandle(job);
    job = nullptr;
  }
}
#endif

TunnelController::TunnelController() : impl_(new Impl()) {}
TunnelController::~TunnelController() { shutdown(); }

void TunnelController::configure(const std::string& executable, const std::string& target) {
  std::lock_guard<std::mutex> lock(impl_->mutex);
  impl_->executable = executable;
  impl_->target = target;
}

TunnelController::Status TunnelController::start(const std::string& broker, int wait_timeout_s) {
  std::unique_lock<std::mutex> lock(impl_->mutex);
  if (impl_->status.state == kReady || impl_->status.state == kStarting) return impl_->status;
  if (impl_->status.state == kStopping) {
    impl_->ready_cv.wait_for(lock, std::chrono::seconds(7),
                             [this]() { return impl_->status.state != kStopping; });
  }
  // The previous reader may still exist (a stop just happened). Assigning a
  // new std::thread over a joinable one calls std::terminate, so reap it
  // first - outside the lock, because the reader takes the same lock to
  // notice the pipe end and exit.
  const bool join_old = impl_->reader.joinable();
  lock.unlock();
  if (join_old) impl_->reader.join();
  lock.lock();
  impl_->status = Status();
  impl_->status.state = kStarting;
  impl_->status.broker = broker;
  impl_->output.clear();
  LOG_INFO("mqtt: ", "tunnel.start: spawning " + impl_->executable + " (target " + impl_->target +
                       ", wait " + std::to_string(wait_timeout_s) + "s)");
  if (!impl_->spawn(broker, wait_timeout_s)) {
    impl_->status.state = kFailed;
    LOG_INFO("mqtt: ", "tunnel.start: spawn failed: " + impl_->status.error);
    return impl_->status;
  }
  impl_->ready_cv.wait_for(lock, std::chrono::seconds(25), [this]() {
    return impl_->status.state == kReady || impl_->status.state == kFailed ||
           impl_->status.state == kStopped;
  });
  if (impl_->status.state == kStarting) {
    impl_->status.state = kFailed;
    impl_->status.error = "the sidecar did not print a session code";
    impl_->kill_now();
  }
  LOG_INFO("mqtt: ", "tunnel.start: state=" + impl_->status.state +
                       (impl_->status.error.empty() ? std::string()
                                                    : " error=" + impl_->status.error));
  return impl_->status;
}

TunnelController::Status TunnelController::stop() {
  std::unique_lock<std::mutex> lock(impl_->mutex);
  if (impl_->status.state == kStopped || impl_->status.state == kFailed) {
    impl_->status.state = kStopped;
    impl_->status.session.clear();
    return impl_->status;
  }
  impl_->status.state = kStopping;
  impl_->kill_now(false);
  const bool exited = impl_->ready_cv.wait_for(lock, std::chrono::seconds(5), [this]() {
    return impl_->status.state == kStopped || impl_->status.state == kFailed;
  });
  if (!exited) {
    // A sidecar that ignores SIGTERM would keep the reader blocked forever;
    // the whole agent has to be able to stop.
    impl_->kill_now(true);
    impl_->ready_cv.wait_for(lock, std::chrono::seconds(2), [this]() {
      return impl_->status.state == kStopped || impl_->status.state == kFailed;
    });
  }
  impl_->status.state = kStopped;
  impl_->status.session.clear();
  const bool join_reader = impl_->reader.joinable();
  lock.unlock();
  if (join_reader) impl_->reader.join();
  lock.lock();
  return impl_->status;
}

TunnelController::Status TunnelController::status() const {
  std::lock_guard<std::mutex> lock(impl_->mutex);
  return impl_->status;
}

void TunnelController::shutdown() {
  impl_->shutting_down.store(true);
  stop();
}

// ---- CommandHandler --------------------------------------------------------

struct CommandHandler::Impl {
  MqttClient* client = nullptr;
  TunnelController* tunnel = nullptr;
  Options options;
  std::thread worker;
  std::mutex mutex;
  std::condition_variable cv;
  std::deque<std::string> queue;
  std::atomic<bool> stopping{false};

  std::map<std::string, std::string> answers;
  std::deque<std::string> order;
  int last_wait_timeout = 0;

  std::string remember(const std::string& id, const std::string& record) {
    answers[id] = record;
    order.push_back(id);
    while (order.size() > 64) {
      answers.erase(order.front());
      order.pop_front();
    }
    return record;
  }

  std::string answer(const std::string& id, const std::string& state,
                     const std::string& error = std::string(),
                     const TunnelController::Status* tunnel_status = nullptr) {
    nlohmann::json out;
    out["id"] = id;
    out["state"] = state;
    if (tunnel_status) {
      if (!tunnel_status->session.empty()) out["session"] = tunnel_status->session;
      if (!tunnel_status->broker.empty()) out["broker"] = tunnel_status->broker;
      if (tunnel_status->listen.empty()) {
        if (state == kReady) out["listen"] = "127.0.0.1:9931";
      } else {
        out["listen"] = tunnel_status->listen;
      }
      if (last_wait_timeout > 0 && state == kReady) out["expires_in"] = last_wait_timeout;
    }
    if (!error.empty()) out["error"] = error;
    return out.dump();
  }

  std::string execute(const std::string& payload) {
    nlohmann::json request;
    try {
      request = nlohmann::json::parse(payload);
    } catch (...) {
      return std::string();
    }
    if (!request.is_object()) return std::string();
    const std::string id = request.value("id", std::string());
    if (id.empty()) return std::string();

    const std::map<std::string, std::string>::const_iterator seen = answers.find(id);
    if (seen != answers.end()) return seen->second;

    const std::string action = request.value("action", std::string());
    LOG_INFO("mqtt: ", "command " + id + " action=" + action);
    const long long ts = request.value("ts", 0LL);
    const int ttl = request.value("ttl", 0);
    const long long now = static_cast<long long>(std::time(nullptr));
    if (ts > 0 && ttl > 0 && now - ts > ttl) {
      return remember(id, answer(id, "rejected", "expired"));
    }
    if (action != "tunnel.start" && action != "tunnel.stop" && action != "status") {
      return remember(id, answer(id, "rejected", "unsupported_action"));
    }

    nlohmann::json args = nlohmann::json::object();
    if (request.contains("args") && request["args"].is_object()) args = request["args"];

    if (action == "status") {
      const TunnelController::Status current =
          tunnel ? tunnel->status() : TunnelController::Status();
      nlohmann::json out;
      out["id"] = id;
      out["state"] = "status";
      out["tunnel"] = current.state.empty() ? "stopped" : current.state;
      out["mqtt"] = (client && client->connected()) ? "connected" : "offline";
      if (!current.session.empty()) out["session"] = current.session;
      if (!current.error.empty()) out["error"] = current.error;
      return remember(id, out.dump());
    }

    if (!tunnel) return remember(id, answer(id, kFailed, "the tunnel controller is not running"));

    if (action == "tunnel.stop") {
      const TunnelController::Status stopped = tunnel->stop();
      return remember(id, answer(id, kStopped, stopped.error, &stopped));
    }

    // tunnel.start
    const std::string target = args.value("target", std::string());
    if (!target.empty() && target != options.default_target) {
      return remember(id, answer(id, "rejected", "not_allowed"));
    }
    int wait_timeout = options.default_wait_timeout;
    if (args.contains("wait_timeout") && args["wait_timeout"].is_number_integer()) {
      wait_timeout = args["wait_timeout"].get<int>();
    }
    if (wait_timeout < 30) wait_timeout = 30;
    if (wait_timeout > options.max_wait_timeout) wait_timeout = options.max_wait_timeout;
    last_wait_timeout = wait_timeout;

    if (client && client->connected()) {
      client->publish(options.resp_topic, answer(id, kStarting), 1);
    }
    const TunnelController::Status started = tunnel->start(options.tunnel_broker, wait_timeout);
    return remember(id, answer(id, started.state, started.error, &started));
  }

  void run() {
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
      const std::string record = execute(payload);
      if (record.empty()) continue;
      const bool online = client && client->connected();
      LOG_INFO("mqtt: ", "answer " + options.resp_topic + " (" +
                           (online ? "connected" : "offline") + "): " + record);
      if (online) client->publish(options.resp_topic, record, 1);
    }
  }
};

CommandHandler::CommandHandler() : impl_(new Impl()) {}
CommandHandler::~CommandHandler() { stop(); }

void CommandHandler::start(MqttClient* client, TunnelController* tunnel, const Options& options) {
  impl_->client = client;
  impl_->tunnel = tunnel;
  impl_->options = options;
  impl_->worker = std::thread([this]() { impl_->run(); });
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
  if (impl_->worker.joinable()) impl_->worker.join();
}

}  // namespace tea
