#include "server.h"

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <fstream>
#include <map>
#include <memory>
#include <mutex>
#include <sstream>
#include <thread>

#ifndef _WIN32
#include <sys/stat.h>
#endif

#include "httplib.h"

#include "agent.h"
#include "archive.h"
#include "env.h"
#include "html_export.h"
#include "http.h"
#include "log.h"
#include "task.h"
#include "trajectory.h"
#include "usage.h"
#include "util.h"
#include "version.h"
#include "web_assets.h"

namespace tea {

std::vector<std::string> reachable_urls(const std::string& bind_host, int port) {
  std::vector<std::string> urls;
  const std::string port_text = std::to_string(port);
  const std::string host = bind_host.empty() ? std::string("127.0.0.1") : bind_host;
  const bool wildcard = host == "0.0.0.0" || host == "::" || host == "[::]" || host == "*";
  if (!wildcard) {
    // An explicit bind address is exactly what a browser has to type (an IPv6
    // literal needs brackets in a URL).
    const bool ipv6 = host.find(':') != std::string::npos;
    urls.push_back("http://" + (ipv6 ? "[" + host + "]" : host) + ":" + port_text);
    return urls;
  }
  // Wildcard: every interface is bound, so the useful answer is this machine's
  // address on the LAN, not the loopback the banner used to print. Loopback is
  // still listed last, because it is the one that always works.
  const std::vector<std::string> addresses = local_ipv4_addresses();
  for (std::size_t i = 0; i < addresses.size(); ++i) {
    urls.push_back("http://" + addresses[i] + ":" + port_text);
  }
  urls.push_back("http://127.0.0.1:" + port_text);
  return urls;
}

namespace {

constexpr size_t kFileReadLimit = 2u * 1024u * 1024u;

/// Replay buffer for one running turn. A client that disconnected comes back and
/// asks for everything after the last frame it saw, so the frames have to be
/// kept for as long as the turn runs; 8 MB is far more than a long agent turn
/// streams, and the oldest frames are dropped past it (the client is then told
/// to resync from the persisted session rather than shown a silent gap).
constexpr std::size_t kTurnLogMaxBytes = 8u * 1024u * 1024u;

/// Images and PDFs are fetched verbatim by the browser, so the raw route needs a
/// much higher ceiling than the JSON `/file` view. 16 MB is generous for a
/// screenshot yet small enough that one request cannot exhaust the device.
constexpr uint64_t kRawReadLimit = 16u * 1024u * 1024u;

/// A session trajectory file is already bounded by `trajectory_max_bytes`
/// (8 MB by default); this is only a guard against an operator raising that cap
/// on a device that cannot serve it.
constexpr size_t kTrajectoryReadLimit = 32u * 1024u * 1024u;

/// Content type for the raw route. A fixed allow-list instead of sniffing: an
/// unknown extension is served as opaque bytes, never as HTML, so a crafted file
/// cannot execute script in the UI's origin.
const char* raw_mime_for(const std::string& file_path) {
  const std::string ext = path_extension(file_path);  // lower-case, includes the dot
  if (ext == ".png") return "image/png";
  if (ext == ".jpg" || ext == ".jpeg") return "image/jpeg";
  if (ext == ".gif") return "image/gif";
  if (ext == ".webp") return "image/webp";
  if (ext == ".svg") return "image/svg+xml";
  if (ext == ".bmp") return "image/bmp";
  if (ext == ".ico") return "image/x-icon";
  if (ext == ".pdf") return "application/pdf";
  if (ext == ".txt") return "text/plain; charset=utf-8";
  if (ext == ".md" || ext == ".markdown") return "text/markdown; charset=utf-8";
  return "application/octet-stream";
}

/// Limits for pasted images. A screenshot is well under 1 MB; the caps exist so
/// one request cannot exhaust memory or fill the artifacts directory.
constexpr std::size_t kMaxImagesPerMessage = 4;
constexpr std::size_t kMaxImageBytes = 8u * 1024u * 1024u;

/// ---- how many clients the process is willing to carry ----------------------
///
/// Every attached SSE response owns one HTTP worker thread for as long as it
/// lives (cpp-httplib hands a streaming response the thread that started it), so
/// these numbers are not abstract: with the old fixed pool of 32 threads, the
/// 32nd open stream made GET /api/health time out, i.e. the UI could not even
/// ask "are you alive?" or press stop. The pool now grows on demand, and the
/// spectators of one turn are bounded so a forgotten tab that reconnects in a
/// loop cannot take the whole server down with it.
constexpr std::size_t kHttpThreadsBase = 16;
constexpr std::size_t kHttpThreadsMaxDefault = 128;
constexpr std::size_t kHttpQueuedRequests = 1024;

/// Positive integer from the environment, or `fallback`. A malformed or too
/// small value is reported once and ignored: a typo in an env var must not take
/// the server down.
int env_int_at_least(const char* name, int fallback, int minimum) {
  const char* raw = std::getenv(name);
  if (!raw || !*raw) return fallback;
  char* end = nullptr;
  const long parsed = std::strtol(raw, &end, 10);
  if (end == raw || parsed < minimum || parsed > 100000) {
    LOG_WARN("server: ignoring ", name, "=", raw, " (needs an integer >= ", minimum, ")");
    return fallback;
  }
  LOG_INFO("server: ", name, "=", parsed);
  return static_cast<int>(parsed);
}

/// How many browsers may follow one turn. 3 browsers is the intended setup; 8
/// leaves room for a reload race or two. Every follower pins an HTTP worker
/// thread, so this is the knob that protects the server from a tab that
/// reconnects in a loop.
int max_subscribers_per_turn() {
  static const int value = env_int_at_least("TEA_AGENT_MAX_SUBSCRIBERS", 8, 1);
  return value;
}

/// Turns are cheap to keep but every one of them streams into every subscriber;
/// this is the "one server, several topics" ceiling.
int max_active_turns() {
  static const int value = env_int_at_least("TEA_AGENT_MAX_ACTIVE_TURNS", 24, 1);
  return value;
}
/// How long an explicit `{"interrupt": true}` waits for the replaced worker to
/// finish writing. Long enough for a tool call that is just returning, short
/// enough that the browser is not left hanging.
constexpr int kInterruptWaitMs = 15000;

/// Split a `data:<mime>;base64,<payload>` URL. Also accepts a bare base64
/// payload (the UI always sends the full URL, but curl users often do not).
bool decode_image_payload(const std::string& raw, std::string* bytes, std::string* err) {
  std::string payload = trim(raw);
  const std::string marker = ";base64,";
  if (starts_with(payload, "data:")) {
    const std::size_t at = payload.find(marker);
    if (at == std::string::npos) {
      *err = "image data URL must be base64-encoded (expected ';base64,')";
      return false;
    }
    payload = payload.substr(at + marker.size());
  }
  if (payload.empty()) {
    *err = "image payload is empty";
    return false;
  }
  if (!base64_decode(payload, bytes)) {
    *err = "image payload is not valid base64";
    return false;
  }
  return true;
}

/// What the system prompt says about the task this turn works on. Without it
/// the model has no idea a task exists, so it never reports progress and never
/// asks the user for a confirmation - the task record then stays at 0% for the
/// whole run.
std::string task_context_for(const Workspace& ws, const std::string& task_id) {
  if (task_id.empty()) return std::string();
  Task t;
  std::string err;
  if (!TaskStore::load(ws, task_id, &t, &err)) return std::string();
  std::string out = t.id;
  if (!t.title.empty()) out += " (" + t.title + ")";
  if (!t.prompt.empty()) out += ": " + t.prompt;
  return out;
}

void send_json(httplib::Response& res, const nlohmann::json& body, int status = 200) {
  res.status = status;
  res.set_header("Access-Control-Allow-Origin", "*");
  res.set_header("Cache-Control", "no-store");
  res.set_content(body.dump(), "application/json; charset=utf-8");
}

void send_error(httplib::Response& res, int status, const std::string& code,
                const std::string& message) {
  nlohmann::json body = nlohmann::json::object();
  nlohmann::json error = nlohmann::json::object();
  error["code"] = code;
  error["message"] = message;
  body["error"] = error;
  send_json(res, body, status);
}

void send_ok(httplib::Response& res) {
  nlohmann::json body = nlohmann::json::object();
  body["ok"] = true;
  send_json(res, body);
}

bool parse_json_body(const httplib::Request& req, nlohmann::json* out, httplib::Response& res) {
  if (req.body.empty()) {
    *out = nlohmann::json::object();
    return true;
  }
  try {
    *out = nlohmann::json::parse(req.body);
  } catch (const std::exception& e) {
    send_error(res, 400, "bad_json", std::string("request body is not valid JSON: ") + e.what());
    return false;
  }
  if (!out->is_object()) {
    send_error(res, 400, "bad_json", "request body must be a JSON object");
    return false;
  }
  return true;
}

std::string json_string(const nlohmann::json& j, const char* key, const std::string& def = "") {
  if (!j.is_object()) return def;
  auto it = j.find(key);
  if (it == j.end() || !it->is_string()) return def;
  return it->get<std::string>();
}

// ---------------------------------------------------------------------------
// One streaming chat turn.
//
// The turn outlives the connection that started it. Every event is kept in a
// bounded log with a sequence number, so a browser that is closed, put to sleep
// or reopened on another machine can ask whether the session still has a turn
// running (GET /api/chat/turn) and re-attach (POST /api/chat/attach): it gets
// the frames it missed and then follows live. A disconnect therefore *detaches*
// one subscriber; only /api/chat/stop, a newer turn on the same session, or
// shutting the server down cancels the work.
// ---------------------------------------------------------------------------
struct ChatTurn {
  std::string session_id;
  std::string workspace_id;
  std::string started_at;
  Workspace workspace;
  Session session;

  mutable std::mutex mu;
  std::condition_variable cv;
  /// Every frame this turn produced, verbatim (`data: {...}\n\n`), oldest first.
  /// This is both the write buffer for attached clients and the replay log.
  std::deque<std::string> log;
  /// Sequence number of `log.front()`. Dropping frames from the front raises it.
  long long log_base = 1;
  std::size_t log_bytes = 0;
  /// Frames dropped from the front. A client that asks for one of them is told to
  /// resync from the persisted session instead of being shown a silent gap.
  long long log_dropped = 0;
  /// Sequence number the next event will carry.
  long long next_seq = 1;
  bool closed = false;
  /// False once the worker has finished the loop and is winding down: after that
  /// a late `enqueue_prompt` must be refused, otherwise it would sit in the inbox
  /// of a turn that will never read it again.
  bool accepting = true;
  std::atomic<bool> cancel{false};
  /// Prompts the user typed while this turn is running. Adopted one per agent
  /// iteration; see prompt_queue.h.
  PromptInbox inbox;
  std::thread worker;

  /// One attached HTTP response: which frame it wants next.
  struct Subscriber {
    long long cursor = 1;
  };

  /// Live attached responses. Bounded, because each one pins an HTTP worker
  /// thread for its whole life (see max_subscribers_per_turn()): a page that
  /// reconnects in a loop must be refused, not allowed to starve every other
  /// request of the server.
  int subscribers = 0;

  bool add_subscriber() {
    std::lock_guard<std::mutex> lock(mu);
    if (closed || subscribers >= max_subscribers_per_turn()) return false;
    ++subscribers;
    return true;
  }

  void remove_subscriber() {
    std::lock_guard<std::mutex> lock(mu);
    if (subscribers > 0) --subscribers;
  }

  int subscriber_count() const {
    std::lock_guard<std::mutex> lock(mu);
    return subscribers;
  }

  ~ChatTurn() {
    if (worker.joinable()) worker.join();
  }

  void emit_event(const nlohmann::json& event) {
    nlohmann::json stamped = event;
    std::string frame;
    {
      std::lock_guard<std::mutex> lock(mu);
      // The sequence number is what makes a reconnect lossless: the client tells
      // us the last one it saw and we replay everything after it.
      stamped["seq"] = next_seq++;
      frame = "data: " + stamped.dump() + "\n\n";
      log.push_back(frame);
      log_bytes += frame.size();
      // One turn cannot grow without bound: a long agent run streams a lot of
      // text. Drop the oldest frames once the cap is hit; `log_dropped` turns
      // that into an explicit resync for whoever asks for them.
      while (log_bytes > kTurnLogMaxBytes && log.size() > 1) {
        log_bytes -= log.front().size();
        log.pop_front();
        ++log_base;
        ++log_dropped;
      }
    }
    cv.notify_all();
  }

  void close() {
    {
      std::lock_guard<std::mutex> lock(mu);
      closed = true;
    }
    cv.notify_all();
  }

  bool finished() const {
    std::lock_guard<std::mutex> lock(mu);
    return closed;
  }

  /// What GET /api/chat/turn reports. `running` is the only field the UI needs in
  /// order to decide whether to re-attach; the rest explains the replay window.
  nlohmann::json status_json() const {
    std::lock_guard<std::mutex> lock(mu);
    nlohmann::json j = nlohmann::json::object();
    j["running"] = !closed;
    j["session_id"] = session_id;
    j["workspace_id"] = workspace_id;
    j["started_at"] = started_at;
    j["last_seq"] = next_seq - 1;
    j["oldest_seq"] = log_base;
    j["buffered"] = static_cast<long long>(log.size());
    j["dropped"] = log_dropped;
    // How many browsers are watching right now. The UI shows it so "someone
    // else is following this turn" is visible instead of guessed.
    j["subscribers"] = subscribers;
    return j;
  }

  /// Queue one prompt. Returns false when the turn has already stopped accepting
  /// them (finished, or the worker is winding down). The check and the push share
  /// `mu` with close()/begin_shutdown(), so a prompt cannot slip into a turn that
  /// nobody will ever drain.
  bool enqueue_prompt(const QueuedPrompt& prompt) {
    std::lock_guard<std::mutex> lock(mu);
    if (!accepting) return false;
    inbox.push(prompt);
    return true;
  }

  /// Called by the worker after run_turn returned, before the final drain: from
  /// here on new prompts are refused and the drain below is authoritative.
  void begin_shutdown() {
    std::lock_guard<std::mutex> lock(mu);
    accepting = false;
  }

  /// Called repeatedly by httplib for one attached response; returns false to end
  /// it. Frames are copied out under the lock and written outside it, so a slow
  /// client cannot stall the worker or the other subscribers.
  bool pump(httplib::DataSink& sink, const std::shared_ptr<Subscriber>& sub) {
    for (;;) {
      std::string frame;
      std::string resync_frame;
      bool done = false;
      {
        std::unique_lock<std::mutex> lock(mu);
        // Wait for the next frame, but never longer than the heartbeat interval:
        // writing a ping is also how a client that vanished without a FIN is
        // noticed. `closed` ends the wait for good.
        for (;;) {
          const long long end_seq = log_base + static_cast<long long>(log.size());
          if (closed || sub->cursor < end_seq) break;
          if (cv.wait_for(lock, std::chrono::seconds(15)) == std::cv_status::timeout) break;
        }
        if (sub->cursor < log_base) {
          // It missed frames that are already gone. Say so, then continue from
          // the oldest one still held: the client reloads the persisted session
          // and picks the live stream up from here.
          nlohmann::json ev = nlohmann::json::object();
          ev["type"] = "resync";
          ev["reason"] = "event_buffer_overflow";
          ev["dropped"] = log_dropped;
          ev["oldest_seq"] = log_base;
          resync_frame = "data: " + ev.dump() + "\n\n";
          sub->cursor = log_base;
        }
        const long long end_seq = log_base + static_cast<long long>(log.size());
        if (sub->cursor >= log_base && sub->cursor < end_seq) {
          frame = log[static_cast<std::size_t>(sub->cursor - log_base)];
          ++sub->cursor;
        }
        done = closed && sub->cursor >= end_seq;
      }
      if (!resync_frame.empty() && !sink.write(resync_frame.data(), resync_frame.size())) {
        return false;
      }
      if (!frame.empty()) {
        if (!sink.write(frame.data(), frame.size())) return false;  // this client is gone
        continue;
      }
      if (done) {
        // A chunked body is only terminated by done(): returning false here is
        // treated as a cancellation and aborts the transfer *without* the
        // terminating "0\r\n\r\n" chunk, which browsers report as "Error in
        // input stream" (curl silently tolerates it).
        sink.done();
        return true;
      }
      if (!sink.is_writable()) return false;
      char heartbeat[80];
      const int n = std::snprintf(heartbeat, sizeof(heartbeat),
                                  "data: {\"type\":\"ping\",\"t\":%lld}\n\n",
                                  static_cast<long long>(now_ms() / 1000));
      if (n <= 0 || !sink.write(heartbeat, static_cast<std::size_t>(n))) return false;
    }
  }
};

/// Reserves one subscriber slot on a turn and releases it when the owning HTTP
/// response is destroyed - which also happens when httplib gives up on a client
/// that vanished without a FIN.
struct SubscriberSlot {
  std::shared_ptr<ChatTurn> turn;
  explicit SubscriberSlot(std::shared_ptr<ChatTurn> t) : turn(std::move(t)) {}
  ~SubscriberSlot() {
    if (turn) turn->remove_subscriber();
  }
};

/// A download name that survives every browser, shell and filesystem: ASCII only,
/// no separators, and a UTC stamp so two downloads do not collide.
std::string ascii_archive_name(const std::string& workspace, const std::string& rel) {
  std::string out;
  for (std::size_t i = 0; i < workspace.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(workspace[i]);
    const bool plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '-' || c == '_';
    out += plain ? static_cast<char>(c) : '_';
  }
  if (out.empty()) out = "workspace";
  if (out.size() > 48) out.resize(48);

  std::string dir;
  for (std::size_t i = 0; i < rel.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(rel[i]);
    const bool plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '-' || c == '_';
    if (c == '.' && dir.empty()) continue;             // no leading dot
    dir += plain ? static_cast<char>(c) : '_';
  }
  if (dir.empty()) dir = "workspace";
  if (dir.size() > 48) dir.resize(48);

  // 2026-10-19T09:12:33Z -> 20261019-091233
  std::string digits;
  const std::string stamp = now_iso8601();
  for (std::size_t i = 0; i < stamp.size(); ++i) {
    if (stamp[i] >= '0' && stamp[i] <= '9') digits += stamp[i];
  }
  const std::string when =
      digits.size() >= 14 ? (digits.substr(0, 8) + "-" + digits.substr(8, 6)) : digits;
  return out + "-" + dir + (when.empty() ? "" : "-" + when) + ".zip";
}

/// ASCII-only fallback for a `Content-Disposition: attachment` name. The real
/// name travels in `filename*` (RFC 5987); this one exists for old clients and
/// must not contain quotes, separators or control bytes.
std::string ascii_download_name(const std::string& name) {
  std::string out;
  for (std::size_t i = 0; i < name.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(name[i]);
    const bool plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
    out += plain ? static_cast<char>(c) : '_';
  }
  if (out.empty() || out == "." || out == "..") out = "download";
  if (out.size() > 96) {
    // Keep the extension: it decides what the receiving OS does with the file.
    const std::string ext = path_extension(out);
    out = out.substr(0, 96 - ext.size()) + ext;
  }
  return out;
}

/// Percent-encode for `filename*=UTF-8''...`, so a non-ASCII workspace name still
/// arrives readable instead of as a row of underscores (RFC 5987/6266).
std::string rfc5987_name(const std::string& name) {
  static const char* kHex = "0123456789ABCDEF";
  std::string out;
  for (std::size_t i = 0; i < name.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(name[i]);
    const bool plain = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                       (c >= '0' && c <= '9') || c == '-' || c == '_' || c == '.';
    if (plain) {
      out += static_cast<char>(c);
    } else {
      out += '%';
      out += kHex[c >> 4];
      out += kHex[c & 0x0F];
    }
  }
  return out;
}

/// Quote one scalar for the YAML we generate. Single quotes are literal in YAML
/// (the only escape is '' for a quote), so a pasted API key cannot inject a new
/// mapping key or a new document node into the config.
std::string yaml_single_quote(const std::string& value) {
  std::string out = "'";
  for (std::size_t i = 0; i < value.size(); ++i) {
    const char c = value[i];
    if (c == '\'') {
      out += "''";
    } else {
      out += c;
    }
  }
  out += "'";
  return out;
}

/// The first-run "I only have an API key" config. One provider, one role: the
/// point is that a fresh install can start a conversation with nothing but a
/// key, and everything here stays visible and editable afterwards.
/// deepseek-flash is the default (the fast/cheap one); the two documented
/// alternatives are listed so the model picker has something to switch to
/// without editing the file by hand.
std::string deepseek_config_yaml(const std::string& api_key, const std::string& api_url) {
  std::ostringstream out;
  out << "# Generated by the tea_agent first-run quick setup.\n"
         "# Everything here can be edited: add providers, reorder the models list\n"
         "# enable the DeepSeek web tools, and so on.\n"
         "version: 1\n"
         "\n"
         "providers:\n"
         "  DeepSeek:\n"
         "    api_url: "
      << yaml_single_quote(api_url)
      << "\n"
         "    api_key: "
      << yaml_single_quote(api_key)
      << "\n"
         "    models:\n"
         "      deepseek-flash:\n"
         "        max_context_tokens: 128000\n"
         "        max_output_tokens: 8192\n"
         "        supports_tools: true\n"
         "      deepseek-chat:\n"
         "        max_context_tokens: 128000\n"
         "        max_output_tokens: 8192\n"
         "        supports_tools: true\n"
         "      deepseek-reasoner:\n"
         "        max_context_tokens: 128000\n"
         "        max_output_tokens: 65536\n"
         "        supports_tools: true\n"
         "        supports_reasoning: true\n"
         "\n"
         "# The default is the first provider here and the FIRST model in its models\n"
         "# list (deepseek-flash right now). Move something up to change it; the\n"
         "# order is the switch, there is no separate default to keep in sync.\n";
  return out.str();
}

/// The MiniMax flavour of the same one-key setup. The endpoint, the model and
/// the window sizes are the ones the project documents in provider.example.yaml:
/// api.minimax.cn/v1 (the global api.minimax.io/v1 works too - change api_url),
/// the M Plan subscription key (sk-cp-...) and MiniMax-M3.1-Flash with its 1M
/// window. The quota badge works out of the box because the M Plan key can be
/// used to ask the provider how much of the subscription is left.
std::string minimax_config_yaml(const std::string& api_key, const std::string& api_url) {
  std::ostringstream out;
  out << "# Generated by the tea_agent first-run quick setup.\n"
         "# Everything here can be edited: add providers, reorder the models list,\n"
         "# point api_url at another endpoint, and so on.\n"
         "version: 1\n"
         "\n"
         "providers:\n"
         "  minimax:\n"
         "    api_url: "
      << yaml_single_quote(api_url)
      << "\n"
         "    api_key: "
      << yaml_single_quote(api_key)
      << "\n"
         "    # M Plan subscription key: the remaining quota (a 5-hour and a weekly\n"
         "    # window) is queried with this same key, and the badge in the top bar\n"
         "    # shows it. The console link works even if the query cannot.\n"
         "    supports_usage: true\n"
         "    usage_type: minimax_token_plan\n"
         "    usage_console_url: https://platform.minimax.cn/console/usage\n"
         "    models:\n"
         "      MiniMax-M3.1-Flash:\n"
         "        max_context_tokens: 1000000\n"
         "        max_output_tokens: 128000\n"
         "        supports_tools: true\n"
         "        supports_reasoning: true\n"
         "        supports_vision: true\n"
         "        reasoning_effort: auto\n"
         "\n"
         "# The default is the first provider here and the FIRST model in its models\n"
         "# list (MiniMax-M3.1-Flash right now). Move something up to change it; the\n"
         "# order is the switch, there is no separate default to keep in sync.\n";
  return out.str();
}

/// provider.yaml holds an API key. New files are created with the process umask
/// (usually 0644, world-readable); tighten it to owner-only where the OS has
/// POSIX permissions. Best effort: a filesystem without chmod is not an error.
void restrict_file_permissions(const std::string& path) {
#ifndef _WIN32
  ::chmod(path.c_str(), 0600);
#else
  (void)path;
#endif
}

/// Guard for the wipe (POST /api/destroy): never hand a filesystem root, an
/// empty string or a relative path to a recursive delete. The list of targets is
/// built by us, so this is the last line of defence rather than a policy.
bool is_wipeable_path(const std::string& path) {
  const std::string p = trim(path);
  if (p.size() < 2) return false;                    // "", "/", "."
  if (p == "/" || p == "\\") return false;
#ifdef _WIN32
  // "C:\", "C:/", "C:" - a drive root, not a directory inside it.
  if (p.size() <= 3 && p[1] == ':') return false;
  if (p.compare(0, 2, "\\\\") == 0 && p.find('\\', 2) == std::string::npos) return false;
#else
  if (p[0] != '/') return false;                     // relative: resolved against cwd
#endif
  return true;
}

const WebAsset* find_embedded_asset(const std::string& path) {
  for (std::size_t i = 0; i < kWebAssetCount; ++i) {
    if (path == kWebAssets[i].path) return &kWebAssets[i];
  }
  return nullptr;
}

}  // namespace

// ---------------------------------------------------------------------------
// Impl
// ---------------------------------------------------------------------------
struct ApiServer::Impl {
  AppConfig* config = nullptr;
  ToolRegistry* tools = nullptr;
  WorkspaceRegistry* registry = nullptr;

  httplib::Server server;
  std::thread listen_thread;
  std::atomic<bool> listen_returned{false};
  bool listen_ok = false;
  int64_t started_ms = 0;
  std::string web_dir;

  std::mutex mu;  // guards registry access and `turns`
  std::map<std::string, std::shared_ptr<ChatTurn>> turns;
  /// Turns displaced by a newer turn on the same session. ~ChatTurn joins its
  /// worker, so these are kept alive here and only destroyed outside `mu`:
  /// joining under the lock would freeze every other request for as long as
  /// the old worker is still stuck (e.g. in a long tool call).
  std::vector<std::shared_ptr<ChatTurn>> retired;
  /// Start a turn without an SSE subscriber.
  ///
  /// The chat route streams the turn to a browser; a task does not need a
  /// stream - whoever queued it (the scheduler, a script) watches the task record
  /// instead. The turn is registered like any other, so /api/chat/turn can still
  /// attach to it and a browser can watch the work happen.
  ///
  /// `task_id` is empty for an ordinary turn. With one, the task record follows
  /// the turn: running while it works, done (or waiting for the user) when it
  /// ends.
  std::shared_ptr<ChatTurn> launch_turn(const std::string& workspace_id,
                                        const std::string& session_id,
                                        const std::string& message,
                                        const std::string& provider,
                                        const std::string& model,
                                        const std::string& task_id,
                                        std::string* err) {
    auto turn = std::make_shared<ChatTurn>();
    turn->started_at = now_iso8601();
    if (!open_workspace(workspace_id, &turn->workspace, err)) return nullptr;
    turn->workspace_id = turn->workspace.id();
    if (!session_id.empty()) {
      turn->workspace.load_session(session_id, &turn->session, nullptr);
    }
    if (turn->session.info.id.empty()) {
      Session created;
      if (!turn->workspace.create_session("", &created, err)) return nullptr;
      turn->session = created;
    }
    turn->session_id = turn->session.info.id;

    std::vector<std::shared_ptr<ChatTurn>> graveyard;
    {
      std::lock_guard<std::mutex> lock(mu);
      prune_turns_locked(&graveyard);
      auto it = turns.find(turn->session_id);
      if (it != turns.end() && it->second && !it->second->finished()) {
        if (err) *err = "a turn is already running in session " + turn->session_id;
        return nullptr;
      }
      turns[turn->session_id] = turn;
    }
    // Joining a finished worker is immediate; doing it here keeps `mu` free.
    graveyard.clear();

    AppConfig* cfg = config;
    ToolRegistry* reg = tools;
    // The bus is captured by value: the worker may publish the last event of
    // the turn after the server object is gone.
    std::shared_ptr<EventBus> bus_ref = bus;
    turn->worker = std::thread([turn, cfg, reg, message, provider, model, task_id, bus_ref]() {
      Agent agent(cfg, reg);
      TurnOptions options;
      options.provider = provider;
      options.model = model;
      options.stream = true;
      options.inbox = &turn->inbox;
      options.task_id = task_id;
      options.task_context = task_context_for(turn->workspace, task_id);
      EventEmitter emit = [turn, task_id, bus_ref](const nlohmann::json& event) {
        turn->emit_event(event);
        if (task_id.empty()) return;
        // The task record follows the turn: the progress the agent reports with
        // toolkit_task is the progress, and the end of the turn is the end of the
        // task - unless the agent asked the user something, in which case the
        // task waits for the answer instead of claiming to be done.
        const std::string type = event.value("type", std::string());
        if (type != "done" && type != "error" && type != "stopped") return;
        Task t;
        std::string err;
        if (!TaskStore::load(turn->workspace, task_id, &t, &err)) return;
        const std::string message_text = event.value("message", std::string());
        TaskStore::update(
            turn->workspace, task_id,
            [&](Task* task) {
              task->finished_at = now_iso8601();
              if (type == "error") {
                task->status = task_status::kFailed;
                task->error = message_text;
                return;
              }
              if (type == "stopped") {
                task->status = task_status::kCancelled;
                return;
              }
              if (task->needs_confirmation) {
                task->status = task_status::kWaitingConfirmation;
                return;
              }
              task->status = task_status::kDone;
              task->progress = 100;
            },
            &t, &err);
        // The event the watchers are waiting for. Without it a task that
        // finishes on its own is silent: the browser never learns that the
        // work is done, and a script that waits for "task_done" waits forever.
        const char* what = "task_done";
        if (type == "error") what = "task_failed";
        else if (type == "stopped") what = "task_cancelled";
        else if (t.status == task_status::kWaitingConfirmation) what = "task_waiting_confirmation";
        publish_task_to(bus_ref, t, what);
      };
      try {
        agent.run_turn(turn->workspace, &turn->session, message, {}, options, emit,
                       &turn->cancel);
      } catch (const std::exception& e) {
        nlohmann::json event = nlohmann::json::object();
        event["type"] = "error";
        event["message"] = std::string("agent crashed: ") + e.what();
        turn->emit_event(event);
      }
      turn->close();
    });
    return turn;
  }

  // ---- the event bus -------------------------------------------------------
  //
  // A policy written in a script needs to *react* to things, not to poll for
  // them: "notify the user when the task is done" should not mean "ask every two
  // seconds". The bus is the server-side half of that: anything that happens
  // (a task was created, started, finished, is waiting for a human) is published
  // here, and GET /api/events streams it to whoever subscribed.
  using EventListener = std::function<void(const nlohmann::json&)>;
  struct Subscriber {
    std::shared_ptr<std::atomic<bool>> alive = std::make_shared<std::atomic<bool>>(true);
    EventListener listener;
  };
  /// The bus lives in a shared_ptr on purpose: the last event of a turn is
  /// published from the worker thread, and that thread may still be running
  /// when the server object is destroyed. The bus has to outlive the server for
  /// that to be safe.
  struct EventBus {
    std::mutex mu;
    std::vector<std::shared_ptr<Subscriber>> subscribers;
  };
  std::shared_ptr<EventBus> bus = std::make_shared<EventBus>();

  void subscribe(const EventListener& listener) {
    auto sub = std::make_shared<Subscriber>();
    sub->listener = listener;
    std::lock_guard<std::mutex> lock(bus->mu);
    bus->subscribers.push_back(sub);
  }

  void unsubscribe(const std::shared_ptr<std::atomic<bool>>& alive) {
    alive->store(false);
    std::lock_guard<std::mutex> lock(bus->mu);
    for (std::size_t i = 0; i < bus->subscribers.size();) {
      if (!bus->subscribers[i]->alive->load()) {
        bus->subscribers.erase(bus->subscribers.begin() + static_cast<std::ptrdiff_t>(i));
      } else {
        ++i;
      }
    }
  }

  /// Publish an event to every live subscriber. A subscriber that throws (or is
  /// gone) must not take the publisher down with it: the events are best effort
  /// notifications, not the source of truth.
  static void publish_to(const std::shared_ptr<EventBus>& bus, const nlohmann::json& event) {
    if (!bus) return;
    std::vector<std::shared_ptr<Subscriber>> subs;
    {
      std::lock_guard<std::mutex> lock(bus->mu);
      for (std::size_t i = 0; i < bus->subscribers.size();) {
        if (!bus->subscribers[i]->alive->load()) {
          bus->subscribers.erase(bus->subscribers.begin() + static_cast<std::ptrdiff_t>(i));
        } else {
          subs.push_back(bus->subscribers[i]);
          ++i;
        }
      }
    }
    for (std::size_t i = 0; i < subs.size(); ++i) {
      try {
        subs[i]->listener(event);
      } catch (...) {
        subs[i]->alive->store(false);
      }
    }
  }

  void publish(const nlohmann::json& event) { publish_to(bus, event); }

  /// A task that was running when the agent died is not running any more. Left
  /// as "running" it would sit in the queue forever: the scheduler only starts
  /// queued tasks, and POST .../start refuses anything else. So the tasks that
  /// were in flight when this process started are marked failed - with a
  /// reason that says what happened - and can be queued again with one PATCH.
  ///
  /// Only tasks that have not been touched for a minute are recovered: another
  /// agent process working on the same workspace would otherwise have its task
  /// pulled out from under it.
  void recover_stale_tasks() {
    if (!registry) return;
    const int64_t now = static_cast<int64_t>(
        std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::system_clock::now().time_since_epoch()).count());
    const std::string cutoff = iso8601_from_unix(now - 60);
    const std::vector<WorkspaceEntry>& entries = registry->entries();
    for (std::size_t i = 0; i < entries.size(); ++i) {
      Workspace ws;
      std::string err;
      if (!open_workspace(entries[i].id, &ws, &err)) continue;
      std::vector<Task> tasks = TaskStore::list(ws);
      for (std::size_t k = 0; k < tasks.size(); ++k) {
        if (tasks[k].status != task_status::kRunning) continue;
        if (!tasks[k].updated_at.empty() && tasks[k].updated_at >= cutoff) continue;
        Task out;
        TaskStore::update(ws, tasks[k].id,
                          [&](Task* t) {
                            t->status = task_status::kFailed;
                            t->finished_at = now_iso8601();
                            t->error =
                                "the agent stopped while this task was running (the server was "
                                "restarted); queue it again if the work still has to be done";
                          },
                          &out, &err);
        if (err.empty()) {
          LOG_WARN("task: ", tasks[k].id,
                   " was running when the server started - marked failed (", out.error, ")");
          publish_task(out, "task_failed");
        }
      }
    }
  }

  /// A task event, published wherever a task changes state.
  static void publish_task_to(const std::shared_ptr<EventBus>& bus, const Task& task,
                              const char* what) {
    if (!bus) return;
    nlohmann::json e = nlohmann::json::object();
    e["type"] = what;
    e["task"] = task.to_json();
    publish_to(bus, e);
  }

  void publish_task(const Task& task, const char* what) { publish_task_to(bus, task, what); }

  // ---- helpers -----------------------------------------------------------
  void install_routes();

  /// The turn currently owning `session_id`, if any. Never prunes and never
  /// joins: callers must not hold `mu` while a ChatTurn dies (its destructor
  /// joins the worker, which may still be inside a long tool call).
  std::shared_ptr<ChatTurn> find_turn(const std::string& session_id) {
    std::lock_guard<std::mutex> lock(mu);
    auto it = turns.find(session_id);
    return it == turns.end() ? nullptr : it->second;
  }

  bool session_turn_running(const std::string& session_id) {
    std::shared_ptr<ChatTurn> turn = find_turn(session_id);
    return turn && !turn->finished();
  }

  /// Number of turns that are still working. Every one of them keeps a worker
  /// thread and a replay buffer alive, so the total is capped.
  int active_turn_count() {
    std::vector<std::shared_ptr<ChatTurn>> live;
    {
      std::lock_guard<std::mutex> lock(mu);
      for (auto it = turns.begin(); it != turns.end(); ++it) {
        if (it->second) live.push_back(it->second);
      }
    }
    int active = 0;
    for (std::size_t i = 0; i < live.size(); ++i) {
      if (!live[i]->finished()) ++active;
    }
    return active;
  }

  bool open_workspace(const std::string& id, Workspace* out, std::string* err) {
    WorkspaceEntry copy;
    {
      std::lock_guard<std::mutex> lock(mu);
      const WorkspaceEntry* entry = registry->find(id);
      if (!entry) {
        *err = "workspace not found: " + id;
        return false;
      }
      copy = *entry;
    }
    return out->open(copy, config->settings, err);
  }

  /// The persona a workspace falls back to when `system_prompt.md` is empty:
  /// its own settings.yaml, else the machine-level default (tea_agent.yaml or
  /// the built-in rules). Trimmed, so it compares and displays predictably.
  std::string default_persona(const Workspace& ws) const {
    const std::string& settings_prompt = ws.settings().system_prompt;
    return trim(settings_prompt.empty() ? config->default_system_prompt() : settings_prompt);
  }

  /// Everything the settings page needs to show and edit the workspace's
  /// system prompt: the override on disk (if any), the persona actually in
  /// effect (`current_prompt` - the editor shows that, not an empty box), what
  /// it falls back to, and the size/token cost of the prompt the next turn
  /// will really send.
  nlohmann::json system_prompt_info(const Workspace& ws) {
    bool custom = false;
    const std::string content = ws.load_system_prompt(&custom);
    const std::string fallback = default_persona(ws);
    const std::string current = custom ? trim(content) : fallback;

    Agent agent(config, tools);
    Session session;
    session.info.created_at = now_iso8601();
    const std::string effective = agent.build_system_prompt(ws, session);

    nlohmann::json j = nlohmann::json::object();
    j["content"] = content;              // raw override on disk ("" = none)
    j["custom"] = custom;
    j["current_prompt"] = current;       // what the model starts every turn with
    j["source"] = custom ? "workspace"
                         : (ws.settings().system_prompt.empty() ? "builtin" : "settings.yaml");
    j["path"] = ws.system_prompt_file();
    j["relative_path"] = ws.display_path(ws.system_prompt_file());
    j["default_prompt"] = fallback;
    j["default_source"] = ws.settings().system_prompt.empty() ? "builtin" : "settings.yaml";
    j["bytes"] = static_cast<long long>(content.size());
    j["max_bytes"] = static_cast<long long>(ws.system_prompt_max_bytes());
    j["effective_bytes"] = static_cast<long long>(effective.size());
    j["estimated_tokens"] = Agent::estimate_tokens(effective);
    return j;
  }

  /// Write `yaml_text` as provider.yaml and reload it in place. Validation
  /// happens against a scratch file, so a bad config never replaces a working
  /// one (and never leaves the server without a config). On failure `res` is
  /// filled with the error and false is returned. `origin` only labels the log
  /// line; the YAML itself is never echoed back to the client.
  bool apply_config_yaml(const std::string& yaml_text, const std::string& origin,
                         httplib::Response& res) {
    if (trim(yaml_text).empty()) {
      send_error(res, 400, "bad_request", "the request body is empty; send provider.yaml");
      return false;
    }
    if (yaml_text.size() > 4u * 1024u * 1024u) {
      send_error(res, 413, "too_large", "provider.yaml must be smaller than 4 MB");
      return false;
    }
    // Reject the upload while a turn is running: reloading the config in place
    // would pull the provider out from under the agent thread.
    {
      std::lock_guard<std::mutex> lock(mu);
      for (auto it = turns.begin(); it != turns.end(); ++it) {
        if (it->second && !it->second->finished()) {
          send_error(res, 409, "turn_running",
                     "a chat turn is still running; stop it before replacing the config");
          return false;
        }
      }
    }

    const std::string dir = path_parent(config->path);
    if (!dir.empty() && !make_dirs(dir)) {
      send_error(res, 500, "io_error", "cannot create " + dir);
      return false;
    }
    const std::string probe_path = config->path + ".upload.tmp";
    std::string io_err;
    if (!write_file(probe_path, yaml_text, false, &io_err)) {
      send_error(res, 500, "io_error", "cannot write " + probe_path + ": " + io_err);
      return false;
    }
    AppConfig probe;
    if (!probe.load(probe_path, config->tea_home)) {
      remove_path(probe_path, false, nullptr);
      send_error(res, 400, "invalid_config", probe.error);
      return false;
    }
    if (probe.providers.empty()) {
      remove_path(probe_path, false, nullptr);
      send_error(res, 400, "invalid_config",
                 "the file parsed, but declares no usable provider (needs a `providers:` "
                 "mapping with at least one api_url)");
      return false;
    }
    if (!move_path(probe_path, config->path, &io_err)) {
      remove_path(probe_path, false, nullptr);
      send_error(res, 500, "io_error", "cannot replace " + config->path + ": " + io_err);
      return false;
    }
    restrict_file_permissions(config->path);
    if (!config->load(config->path, config->tea_home)) {
      send_error(res, 500, "reload_failed", config->error);
      return false;
    }
    LOG_INFO("config: provider.yaml replaced (", origin, "; ", config->providers.size(),
             " provider(s), active=", config->active_provider, "/", config->active_model, ")");
    return true;
  }

  /// What a wipe would delete, in the order it deletes them: the tea home
  /// (provider.yaml, tea_agent.yaml, workspaces.json, the global log) and each
  /// registered workspace's `.tea_agent_run` (sessions, trajectories, memory,
  /// agent.md, per-workspace log). Project directories are never touched.
  nlohmann::json destroy_targets() {
    nlohmann::json list = nlohmann::json::array();
    list.push_back({{"kind", "tea_home"},
                    {"path", config->tea_home},
                    {"exists", path_is_dir(config->tea_home)}});
    {
      std::lock_guard<std::mutex> lock(mu);
      for (std::size_t i = 0; i < registry->entries().size(); ++i) {
        const WorkspaceEntry& e = registry->entries()[i];
        const std::string run_dir = path_join(e.path, ".tea_agent_run");
        list.push_back({{"kind", "workspace"},
                        {"workspace_id", e.id},
                        {"workspace", e.name},
                        {"path", run_dir},
                        {"exists", path_is_dir(run_dir)}});
      }
    }
    nlohmann::json j = nlohmann::json::object();
    j["tea_home"] = config->tea_home;
    j["targets"] = list;
    return j;
  }

  /// Delete one target, recording the outcome instead of stopping at the first
  /// failure: a half-wiped tree is worse than a report that names what survived.
  static void wipe_one(const std::string& path, nlohmann::json* removed,
                       nlohmann::json* skipped, nlohmann::json* failed) {
    if (!is_wipeable_path(path)) {
      failed->push_back({{"path", path}, {"error", "refusing to delete this path"}});
      return;
    }
    if (!path_exists(path)) {
      skipped->push_back(path);
      return;
    }
    std::string err;
    if (remove_path(path, true, &err)) {
      removed->push_back(path);
    } else {
      failed->push_back({{"path", path}, {"error", err}});
    }
  }

  /// Irreversible: remove every trace this agent left on the machine and drop
  /// the in-memory state, so the process behaves like a freshly started one
  /// without a provider.yaml (the UI then shows its first-run setup gate).
  nlohmann::json destroy_all() {
    nlohmann::json removed = nlohmann::json::array();
    nlohmann::json skipped = nlohmann::json::array();
    nlohmann::json failed = nlohmann::json::array();

    // Log files live inside the very directories being deleted. Close them
    // first: Windows refuses to unlink an open file, and on POSIX the space
    // would only come back when the process exits.
    Logger::instance().close_files();

    {
      std::lock_guard<std::mutex> lock(mu);
      for (std::size_t i = 0; i < registry->entries().size(); ++i) {
        wipe_one(path_join(registry->entries()[i].path, ".tea_agent_run"), &removed, &skipped,
                 &failed);
      }
      // The index itself is gone with the tea home; keep the in-memory list in
      // sync so the next request does not resurrect it by saving an entry.
      registry->mutable_entries().clear();
    }
    wipe_one(config->tea_home, &removed, &skipped, &failed);

    // Finished turns still replay from memory; there is nothing left to replay.
    std::vector<std::shared_ptr<ChatTurn>> graveyard;
    {
      std::lock_guard<std::mutex> lock(mu);
      for (auto it = turns.begin(); it != turns.end(); ++it) graveyard.push_back(it->second);
      turns.clear();
    }
    graveyard.clear();  // ~ChatTurn joins its worker; never do that under `mu`

    // Back to "no provider.yaml configured", exactly like a cold start.
    config->load(config->path, config->tea_home);
    config->active_provider.clear();
    config->active_model.clear();
    config->extra_path.clear();

    nlohmann::json j = nlohmann::json::object();
    j["ok"] = failed.empty();
    j["tea_home"] = config->tea_home;
    j["removed"] = removed;
    j["skipped"] = skipped;
    j["failed"] = failed;
    return j;
  }

  bool find_session_workspace(const std::string& session_id, Workspace* out, std::string* err) {
    WorkspaceEntry copy;
    bool found = false;
    {
      std::lock_guard<std::mutex> lock(mu);
      for (std::size_t i = 0; i < registry->entries().size(); ++i) {
        const WorkspaceEntry& e = registry->entries()[i];
        const std::string file = path_join(path_join(path_join(e.path, ".tea_agent_run"), "sessions"),
                                           session_id + ".json");
        if (path_exists(file)) {
          copy = e;
          found = true;
          break;
        }
      }
    }
    if (!found) {
      *err = "session not found: " + session_id;
      return false;
    }
    return out->open(copy, config->settings, err);
  }

  /// With `mu` held: move finished turns into `graveyard`. The caller destroys
  /// them after unlocking, because the destructor joins the worker thread.
  void prune_turns_locked(std::vector<std::shared_ptr<ChatTurn>>* graveyard) {
    for (auto it = turns.begin(); it != turns.end();) {
      if (it->second && it->second->finished()) {
        graveyard->push_back(it->second);
        it = turns.erase(it);
      } else {
        ++it;
      }
    }
    for (auto it = retired.begin(); it != retired.end();) {
      if (!*it || (*it)->finished()) {
        if (*it) graveyard->push_back(*it);
        it = retired.erase(it);
      } else {
        ++it;
      }
    }
  }

  bool serve_asset(const std::string& relative, httplib::Response& res) {
    // 1. explicit --web-dir override (handy while developing the UI)
    if (!web_dir.empty()) {
      const std::string file = path_join(web_dir, relative);
      if (path_is_file(file)) {
        std::string content;
        bool truncated = false;
        if (read_file(file, &content, 0, &truncated)) {
          const WebAsset* asset = find_embedded_asset(relative);
          res.set_content(content, asset ? asset->mime : "application/octet-stream");
          res.set_header("Cache-Control", "no-store");
          return true;
        }
      }
    }
    // 2. assets embedded at build time
    const WebAsset* asset = find_embedded_asset(relative);
    if (!asset) return false;
    // kAssetData_N is NUL-terminated, so never trust a size that runs past the
    // terminator: a build where they disagree used to append the next asset to
    // app.js, which is a JS syntax error and left the UI stuck on 连接中.
    std::size_t n = asset->size;
    const std::size_t term = std::strlen(asset->data);
    if (n > term) n = term;
    res.set_content(std::string(asset->data, n), asset->mime);
    res.set_header("Cache-Control", "no-store");
    return true;
  }
};

void ApiServer::Impl::install_routes() {
  httplib::Server& svr = server;

  svr.set_payload_max_length(64ull * 1024ull * 1024ull);
  svr.set_read_timeout(60, 0);
  // A streaming response owns this thread until it ends, so a dead-but-not-closed
  // socket must not be able to keep one for ten minutes. 20 minutes was the old
  // value and it is longer than any turn needs to produce a frame (the loop sends
  // a heartbeat every 15 s; a write that cannot complete in two minutes means the
  // peer is gone).
  svr.set_write_timeout(120, 0);
  svr.set_idle_interval(0, 200000);
  svr.set_keep_alive_max_count(1024);
  svr.set_tcp_nodelay(true);
  // Base threads stay around; extra ones are spawned on demand (an attached SSE
  // stream keeps one for its whole life) and retire after a few idle seconds, so
  // a handful of tabs costs a handful of threads. `TEA_AGENT_HTTP_THREADS_MAX`
  // lowers the ceiling on a memory-constrained board.
  svr.new_task_queue = [] {
    std::size_t max_threads = kHttpThreadsMaxDefault;
    const char* env = std::getenv("TEA_AGENT_HTTP_THREADS_MAX");
    if (env && *env) {
      const long long parsed = std::atoll(env);
      if (parsed >= static_cast<long long>(kHttpThreadsBase)) {
        max_threads = static_cast<std::size_t>(parsed);
      } else {
        LOG_WARN("server: ignoring TEA_AGENT_HTTP_THREADS_MAX=", env, " (needs to be >= ",
                 kHttpThreadsBase, ")");
      }
    }
    LOG_INFO("server: http pool base=", kHttpThreadsBase, " max=", max_threads);
    return new httplib::ThreadPool(kHttpThreadsBase, max_threads, kHttpQueuedRequests);
  };

  // ---- static ------------------------------------------------------------
  svr.Get("/", [this](const httplib::Request&, httplib::Response& res) {
    if (!serve_asset("index.html", res)) {
      send_error(res, 500, "no_assets", "the embedded web UI is missing");
    }
  });
  // NOTE: inside a bracket expression write the '-' LAST and unescaped:
  // libstdc++ up to GCC 6 throws std::regex_error("Invalid range in bracket
  // expression") for "\\-", which aborted the agent at startup on the
  // aarch64 target (Linaro GCC 6.5 runtime). The character set is identical.
  svr.Get(R"(/([A-Za-z0-9_.-]+))", [this](const httplib::Request& req, httplib::Response& res) {
    const std::string name = req.matches[1].str();
    if (name == "favicon.ico") {
      res.status = 204;
      return;
    }
    if (!serve_asset(name, res)) {
      send_error(res, 404, "not_found", "no such asset: " + name);
    }
  });

  // ---- CORS preflight ----------------------------------------------------
  svr.Options(R"(/.*)", [](const httplib::Request&, httplib::Response& res) {
    res.status = 204;
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS");
    res.set_header("Access-Control-Allow-Headers", "Content-Type, Authorization");
  });

  // ---- meta --------------------------------------------------------------
  svr.Get("/api/health", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["version"] = std::string(tea::version_string());
    // Which commit this build came from, when the build info file is next to the
    // binary. Empty means "no build info here" - a single-file copy.
    j["git_hash"] = tea::version_git_hash();
    j["uptime_s"] = static_cast<double>(now_ms() - started_ms) / 1000.0;
    j["provider"] = config->active_provider;
    j["model"] = config->active_model;
    j["http_engine"] = http_engine();
    // Hosts where WinHTTP was given up on and the bundled TLS backend took over
    // (Windows only, empty otherwise). Makes the fallback visible from outside
    // instead of only in the log.
    const std::vector<std::string> fallback = http_socket_fallback_hosts();
    if (!fallback.empty()) j["http_socket_fallback"] = fallback;
    // The UI blocks on this: without a config there is nothing to chat with.
    j["config_loaded"] = config->loaded;
    if (!config->loaded) j["config_error"] = config->error;
    // Where the mutable state lives; the settings page shows it next to
    // "销毁" so it is obvious what that button is about to delete.
    j["tea_home"] = config->tea_home;
    send_json(res, j);
  });

  // ---- first-run configuration -------------------------------------------
  // A missing provider.yaml is not fatal any more: the server comes up in setup
  // mode, the UI uploads the file, and the config is reloaded in place.
  svr.Get("/api/config/status", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json j = nlohmann::json::object();
    j["loaded"] = config->loaded;
    j["path"] = config->path;
    j["tea_home"] = config->tea_home;
    if (!config->loaded) {
      j["needs_setup"] = config->file_missing;
      j["error"] = config->error;
    }
    j["providers"] = config->provider_order;
    send_json(res, j);
  });

  svr.Post("/api/config", [this](const httplib::Request& req, httplib::Response& res) {
    // Body is the YAML text itself, or {"yaml": "..."} for JSON-only clients.
    // Decide by content rather than Content-Type: a browser File upload and a
    // curl --data-binary send the raw text, while the bundled UI wraps it.
    std::string yaml_text = req.body;
    if (!trim(req.body).empty() && trim(req.body)[0] == '{') {
      nlohmann::json body = nlohmann::json::parse(req.body, nullptr, false);
      if (body.is_object()) {
        const std::string wrapped = json_string(body, "yaml");
        if (!wrapped.empty()) yaml_text = wrapped;
        else {
          const std::string alt = json_string(body, "content");
          if (!alt.empty()) yaml_text = alt;
        }
      }
      // No wrapper key: the body is a YAML flow mapping, keep it verbatim.
    }
    if (!apply_config_yaml(yaml_text, "uploaded via the web UI", res)) return;
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["path"] = config->path;
    j["providers"] = config->provider_order;
    j["active"] = { {"provider", config->active_provider}, {"model", config->active_model} };
    send_json(res, j);
  });

  // ---- the primitives -----------------------------------------------------
  //
  // Everything above this line is a *product*: a chat that runs an agent loop, a
  // task queue with a fixed lifecycle, a UI. Everything below is a *primitive*:
  // one model call, one tool call, one record, one event. A policy - "check the
  // quota, then work the queue, then tell the user" - is a script that composes
  // them, not a mode the server has to grow.
  //
  //   POST /api/llm/chat    one model call: no agent loop, no tools executed
  //   PATCH /api/tasks/{id} write any field of a task record
  //   GET  /api/events      the lifecycle events, as they happen

  // One model call. The messages go in, the assistant message comes out; no
  // agent loop is run and no tool is executed - the caller decides what to do
  // with the answer, which is what makes it usable as a building block.
  svr.Post("/api/llm/chat", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    if (!config->loaded) {
      send_error(res, 409, "config_missing",
                 "no provider.yaml is configured yet; upload it in the web UI");
      return;
    }
    const std::string provider_name =
        trim(json_string(body, "provider", config->active_provider));
    const ProviderInfo* provider = config->provider(provider_name);
    if (!provider) {
      send_error(res, 400, "unknown_provider", "no provider named '" + provider_name + "'");
      return;
    }
    if (!provider->has_key()) {
      send_error(res, 409, "no_api_key",
                 "provider '" + provider->name + "' has no api_key configured");
      return;
    }
    // An unknown model name falls back to the provider's primary model, the same
    // way the agent does it, so a stale id is not a hard error.
    std::string model_name = trim(json_string(body, "model"));
    if (model_name.empty()) model_name = provider->primary_model();
    const ModelInfo* model_info = provider->find_model(model_name);
    if (!model_info && !provider->models.empty()) {
      model_name = provider->primary_model();
      model_info = provider->find_model(model_name);
    }

    // ---- messages --------------------------------------------------------
    std::vector<Message> messages;
    auto it = body.find("messages");
    if (it != body.end() && it->is_array()) {
      for (std::size_t i = 0; i < it->size(); ++i) {
        const nlohmann::json& m = (*it)[i];
        if (!m.is_object()) continue;
        Message msg;
        msg.role = json_string(m, "role");
        msg.content = json_string(m, "content");
        if (msg.role.empty()) continue;
        messages.push_back(msg);
      }
    }
    const std::string system = trim(json_string(body, "system"));
    if (!system.empty()) {
      Message sys = Message::make("system", system);
      messages.insert(messages.begin(), sys);
    }
    const std::string prompt = trim(json_string(body, "prompt"));
    if (!prompt.empty()) {
      messages.push_back(Message::make("user", prompt));
    }
    if (messages.empty()) {
      send_error(res, 400, "bad_request", "'messages' or 'prompt' is required");
      return;
    }

    // ---- request ---------------------------------------------------------
    ChatRequest request;
    request.provider = provider->name;
    request.model = model_name;
    request.api_url = provider->api_url;
    request.api_key = provider->api_key;
    request.headers = provider->headers;
    for (std::size_t i = 0; i < request.headers.size(); ++i) {
      request.headers[i].second =
          replace_all(request.headers[i].second, "${session_id}", "");
    }
    request.stream = body.value("stream", false);
    // The caller asked for the numbers: without usage there is no way to bill or
    // to budget what just happened.
    request.include_usage = body.value("include_usage", true);
    if (body.contains("temperature") && body["temperature"].is_number()) {
      request.has_temperature = true;
      request.temperature = body["temperature"].get<double>();
    }
    if (body.contains("top_p") && body["top_p"].is_number()) {
      request.has_top_p = true;
      request.top_p = body["top_p"].get<double>();
    }
    if (body.contains("max_tokens") && body["max_tokens"].is_number()) {
      request.max_tokens = body["max_tokens"].get<long long>();
    }
    if (request.max_tokens <= 0 && model_info && model_info->max_output_tokens > 0) {
      request.max_tokens = model_info->max_output_tokens;
    }
    // Tool schemas are passed through untouched: the caller owns the protocol,
    // the server only carries it. Nothing is executed here.
    auto tools_it = body.find("tools");
    if (tools_it != body.end() && tools_it->is_array()) request.tools = *tools_it;
    for (std::size_t i = 0; i < messages.size(); ++i) {
      request.messages.push_back(messages[i].to_wire(false));
    }
    request.connect_timeout_ms = config->settings.http_connect_timeout_ms;
    request.read_timeout_ms = config->settings.http_read_timeout_ms;

    // ---- the call --------------------------------------------------------
    if (!request.stream) {
      ChatResult result = llm_chat(request, DeltaCallback(), nullptr);
      if (!result.ok) {
        std::string message = result.error.empty() ? "the model call failed" : result.error;
        if (!result.detail.empty()) message += ": " + result.detail;
        send_error(res, 502, "upstream_error", message);
        return;
      }
      nlohmann::json j = nlohmann::json::object();
      j["ok"] = true;
      j["provider"] = request.provider;
      j["model"] = request.model;
      j["content"] = result.content;
      j["reasoning"] = result.reasoning;
      j["finish_reason"] = result.finish_reason;
      j["elapsed_ms"] = result.elapsed_ms;
      j["usage"] = result.usage.to_json();
      nlohmann::json calls = nlohmann::json::array();
      for (std::size_t i = 0; i < result.tool_calls.size(); ++i) {
        const ToolCall& call = result.tool_calls[i];
        nlohmann::json c = nlohmann::json::object();
        c["id"] = call.id;
        c["name"] = call.name;
        c["arguments"] = call.raw_arguments;
        calls.push_back(c);
      }
      j["tool_calls"] = calls;
      send_json(res, j);
      return;
    }

    // Streaming: the deltas as they come, then one done event with the whole
    // answer (content, tool calls, usage) - the same shape the chat route uses.
    res.set_header("Cache-Control", "no-cache");
    res.set_header("X-Accel-Buffering", "no");
    res.set_chunked_content_provider(
        "text/event-stream; charset=utf-8",
        [this, request](std::size_t, httplib::DataSink& sink) {
          ChatResult result;
          DeltaCallback on_delta = [&sink, &result](DeltaKind kind, const std::string& text) {
            nlohmann::json e = nlohmann::json::object();
            e["type"] = (kind == DeltaKind::Reasoning) ? "reasoning_delta" : "content_delta";
            e["text"] = text;
            const std::string frame = "data: " + e.dump() + "\n\n";
            if (!sink.write(frame.data(), frame.size())) return;
          };
          result = llm_chat(request, on_delta, nullptr);
          nlohmann::json done = nlohmann::json::object();
          done["type"] = "done";
          done["ok"] = result.ok;
          done["provider"] = request.provider;
          done["model"] = request.model;
          done["content"] = result.content;
          done["reasoning"] = result.reasoning;
          done["finish_reason"] = result.finish_reason;
          done["elapsed_ms"] = result.elapsed_ms;
          done["usage"] = result.usage.to_json();
          nlohmann::json calls = nlohmann::json::array();
          for (std::size_t i = 0; i < result.tool_calls.size(); ++i) {
            const ToolCall& call = result.tool_calls[i];
            nlohmann::json c = nlohmann::json::object();
            c["id"] = call.id;
            c["name"] = call.name;
            c["arguments"] = call.raw_arguments;
            calls.push_back(c);
          }
          done["tool_calls"] = calls;
          if (!result.ok) {
            done["error"] = result.error;
            done["detail"] = result.detail;
          }
          const std::string frame = "data: " + done.dump() + "\n\n";
          sink.write(frame.data(), frame.size());
          return true;
        });
  });

  // Write any field of a task. The lifecycle the server drives (start, done,
  // waiting for a human) is still the server's business; this is the escape
  // hatch for a script that keeps its own state machine on top of the same
  // records - it can set the status, the progress, the note, its own `meta`.
  svr.Patch(R"(/api/tasks/([^/]+))",
            [this](const httplib::Request& req, httplib::Response& res) {
              nlohmann::json body;
              if (!parse_json_body(req, &body, res)) return;
              const std::string id = req.matches[1].str();
              const std::string workspace_id =
                  req.has_param("workspace_id") ? req.get_param_value("workspace_id") : std::string();
              if (workspace_id.empty()) {
                send_error(res, 400, "bad_request", "'workspace_id' is required");
                return;
              }
              Workspace ws;
              std::string err;
              if (!open_workspace(workspace_id, &ws, &err)) {
                send_error(res, 404, "not_found", err);
                return;
              }
              Task task;
              if (!TaskStore::load(ws, id, &task, &err)) {
                send_error(res, 404, "not_found", err);
                return;
              }
              const std::string before = task.status;
              Task updated;
              const bool ok = TaskStore::update(
                  ws, id,
                  [&](Task* t) {
                    auto set_str = [&](const char* key, std::string* target) {
                      auto it = body.find(key);
                      if (it != body.end() && it->is_string()) *target = it->get<std::string>();
                    };
                    set_str("status", &t->status);
                    set_str("title", &t->title);
                    set_str("note", &t->note);
                    set_str("question", &t->question);
                    set_str("provider", &t->provider);
                    set_str("model", &t->model);
                    auto it = body.find("progress");
                    if (it != body.end() && it->is_number()) {
                      const int progress = it->get<int>();
                      if (progress < 0 || progress > 100) {
                        t->error = "progress must be between 0 and 100";
                        return;
                      }
                      t->progress = progress;
                    }
                    auto cit = body.find("needs_confirmation");
                    if (cit != body.end() && cit->is_boolean()) {
                      t->needs_confirmation = cit->get<bool>();
                    }
                    auto mit = body.find("meta");
                    if (mit != body.end() && mit->is_object()) t->meta = *mit;
                    // The intermediate-state history grows the same way the agent's
                    // own reports do, so both show up in one list.
                    auto sit = body.find("step");
                    if (sit != body.end() && sit->is_string()) {
                      nlohmann::json step = nlohmann::json::object();
                      step["at"] = now_iso8601();
                      step["note"] = sit->get<std::string>();
                      step["progress"] = t->progress;
                      t->steps.push_back(step);
                      while (t->steps.size() > 100) t->steps.erase(t->steps.begin());
                    }
                  },
                  &updated, &err);
              if (!ok) {
                send_error(res, 400, "bad_request", err);
                return;
              }
              publish_task(updated, "task_updated");
              nlohmann::json j = updated.to_json();
              j["ok"] = true;
              j["previous_status"] = before;
              send_json(res, j);
            });

  // The lifecycle events, as they happen. A script that has to *notify* someone
  // subscribes here instead of polling the task list every few seconds.
  svr.Get("/api/events", [this](const httplib::Request& req, httplib::Response& res) {
    auto queue = std::make_shared<std::deque<std::string>>();
    auto mutex = std::make_shared<std::mutex>();
    auto alive = std::make_shared<std::atomic<bool>>(true);
    const std::string workspace_filter =
        req.has_param("workspace_id") ? req.get_param_value("workspace_id") : std::string();

    subscribe([queue, mutex, alive, workspace_filter](const nlohmann::json& event) {
      if (!workspace_filter.empty()) {
        const nlohmann::json& task = event.contains("task") ? event["task"] : nlohmann::json();
        if (task.value("workspace_id", std::string()) != workspace_filter) return;
      }
      const std::string frame = "data: " + event.dump() + "\n\n";
      std::lock_guard<std::mutex> lock(*mutex);
      queue->push_back(frame);
    });

    res.set_header("Cache-Control", "no-cache");
    res.set_header("X-Accel-Buffering", "no");
    res.set_chunked_content_provider(
        "text/event-stream; charset=utf-8",
        [this, queue, mutex, alive](std::size_t, httplib::DataSink& sink) {
          for (;;) {
            std::deque<std::string> batch;
            {
              std::lock_guard<std::mutex> lock(*mutex);
              batch.swap(*queue);
            }
            for (std::size_t i = 0; i < batch.size(); ++i) {
              if (!sink.write(batch[i].data(), batch[i].size())) {
                alive->store(false);
                unsubscribe(alive);
                return true;
              }
            }
            if (batch.empty()) {
              if (!sink.is_writable()) {
                alive->store(false);
                unsubscribe(alive);
                return true;
              }
              std::this_thread::sleep_for(std::chrono::milliseconds(200));
            }
          }
          return true;
        },
        [this, alive](bool) {
          alive->store(false);
          unsubscribe(alive);
        });
  });

  // ---- tasks: the queue a scheduler works through --------------------------
  //
  // A task is "do this later": a prompt plus the state of that work. The
  // scheduler (sched/task_runner.py) lists the queued ones, starts them while the
  // provider still has quota and watches their progress; a human can do the same
  // over HTTP. The task record is one file per task under
  // <workspace>/.tea_agent_run/tasks/, so it survives a restart.
  svr.Post("/api/tasks", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string workspace_id = json_string(body, "workspace_id");
    if (workspace_id.empty()) {
      send_error(res, 400, "bad_request", "'workspace_id' is required");
      return;
    }
    Workspace ws;
    std::string err;
    if (!open_workspace(workspace_id, &ws, &err)) {
      send_error(res, 404, "not_found", err);
      return;
    }
    const std::string prompt = trim(json_string(body, "prompt"));
    if (prompt.empty()) {
      send_error(res, 400, "bad_request", "'prompt' is required");
      return;
    }
    Task task;
    task.workspace_id = ws.id();
    task.prompt = prompt;
    task.title = trim(json_string(body, "title"));
    if (task.title.empty()) task.title = Workspace::derive_title(prompt);
    task.session_id = trim(json_string(body, "session_id"));
    task.provider = trim(json_string(body, "provider"));
    task.model = trim(json_string(body, "model"));
    if (!TaskStore::create(ws, &task, &err)) {
      send_error(res, 500, "io_error", err);
      return;
    }
    LOG_INFO("task: created ", task.id, " (", task.title, ")");
    publish_task(task, "task_created");
    nlohmann::json j = task.to_json();
    j["ok"] = true;
    send_json(res, j);
  });

  svr.Get("/api/tasks", [this](const httplib::Request& req, httplib::Response& res) {
    const std::string workspace_id =
        req.has_param("workspace_id") ? req.get_param_value("workspace_id") : std::string();
    if (workspace_id.empty()) {
      send_error(res, 400, "bad_request", "'workspace_id' is required");
      return;
    }
    Workspace ws;
    std::string err;
    if (!open_workspace(workspace_id, &ws, &err)) {
      send_error(res, 404, "not_found", err);
      return;
    }
    const std::string status =
        req.has_param("status") ? trim(req.get_param_value("status")) : std::string();
    const std::vector<Task> tasks =
        status.empty() ? TaskStore::list(ws) : TaskStore::list_by_status(ws, status);
    nlohmann::json j = nlohmann::json::object();
    j["tasks"] = nlohmann::json::array();
    for (std::size_t i = 0; i < tasks.size(); ++i) j["tasks"].push_back(tasks[i].to_json());
    // The counts are what a scheduler polls for: how much is waiting, how much is
    // running, how much is blocked on a human.
    nlohmann::json counts = nlohmann::json::object();
    counts[task_status::kQueued] = 0;
    counts[task_status::kRunning] = 0;
    counts[task_status::kWaitingConfirmation] = 0;
    counts[task_status::kDone] = 0;
    counts[task_status::kFailed] = 0;
    counts[task_status::kCancelled] = 0;
    const std::vector<Task> all = TaskStore::list(ws);
    for (std::size_t i = 0; i < all.size(); ++i) {
      auto it = counts.find(all[i].status);
      if (it != counts.end()) it.value() = it.value().get<int>() + 1;
    }
    j["counts"] = counts;
    send_json(res, j);
  });

  svr.Get(R"(/api/tasks/([^/]+))",
           [this](const httplib::Request& req, httplib::Response& res) {
             const std::string id = req.matches[1].str();
             const std::string workspace_id =
                 req.has_param("workspace_id") ? req.get_param_value("workspace_id") : std::string();
             if (workspace_id.empty()) {
               send_error(res, 400, "bad_request", "'workspace_id' is required");
               return;
             }
             Workspace ws;
             std::string err;
             if (!open_workspace(workspace_id, &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             Task task;
             if (!TaskStore::load(ws, id, &task, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             send_json(res, task.to_json());
           });

  // Start a task now: the prompt goes to the model as a turn of its own.
  svr.Post(R"(/api/tasks/([^/]+)/start)",
           [this](const httplib::Request& req, httplib::Response& res) {
             const std::string id = req.matches[1].str();
             const std::string workspace_id =
                 req.has_param("workspace_id") ? req.get_param_value("workspace_id") : std::string();
             if (workspace_id.empty()) {
               send_error(res, 400, "bad_request", "'workspace_id' is required");
               return;
             }
             Workspace ws;
             std::string err;
             if (!open_workspace(workspace_id, &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             Task task;
             if (!TaskStore::load(ws, id, &task, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             if (task.status != task_status::kQueued && task.status != task_status::kWaitingConfirmation) {
               send_error(res, 409, "task_not_queued",
                          "task " + id + " is '" + task.status + "', not 'queued'");
               return;
             }
             if (!config->loaded) {
               send_error(res, 409, "config_missing",
                          "no provider.yaml is configured yet; upload it in the web UI");
               return;
             }
             // The body may name the provider: the scheduler gates the work on the
             // quota of one provider, so the work must run on that one.
             nlohmann::json body;
             if (!req.body.empty() && req.body != "{}") {
               if (!parse_json_body(req, &body, res)) return;
             }
             const std::string wanted_provider = trim(json_string(body, "provider"));
             const std::string wanted_model = trim(json_string(body, "model"));
             const std::string provider =
                 !task.provider.empty() ? task.provider
                                        : (!wanted_provider.empty() ? wanted_provider
                                                                    : config->active_provider);
             // The model has to belong to the provider that runs the work: the
             // active model belongs to the *active* provider, which is a
             // different one as soon as the scheduler gates on another
             // provider's quota.
             const ProviderInfo* pinfo = config->provider(provider);
             const std::string provider_model =
                 (pinfo && !pinfo->primary_model().empty()) ? pinfo->primary_model()
                                                            : config->active_model;
             const std::string model = !task.model.empty()
                                          ? task.model
                                          : (!wanted_model.empty() ? wanted_model : provider_model);
             std::shared_ptr<ChatTurn> turn =
                 launch_turn(ws.id(), task.session_id, task.prompt, provider, model, id, &err);
             if (!turn) {
               send_error(res, 409, "cannot_start", err);
               return;
             }
             Task started;
             TaskStore::update(ws, id,
                               [&](Task* t) {
                                 t->status = task_status::kRunning;
                                 t->session_id = turn->session_id;
                                 t->provider = provider;
                                 t->model = model;
                                 t->started_at = now_iso8601();
                                 t->finished_at.clear();
                                 t->error.clear();
                                 t->needs_confirmation = false;
                               },
                               &started, &err);
             LOG_INFO("task: started ", id, " in session ", turn->session_id);
             publish_task(started, "task_started");
             nlohmann::json j = started.to_json();
             j["ok"] = true;
             send_json(res, j);
           });

  svr.Post(R"(/api/tasks/([^/]+)/cancel)",
           [this](const httplib::Request& req, httplib::Response& res) {
             const std::string id = req.matches[1].str();
             const std::string workspace_id =
                 req.has_param("workspace_id") ? req.get_param_value("workspace_id") : std::string();
             Workspace ws;
             std::string err;
             if (!open_workspace(workspace_id, &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             Task task;
             if (!TaskStore::load(ws, id, &task, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             if (!task.session_id.empty()) {
               std::shared_ptr<ChatTurn> turn = find_turn(task.session_id);
               if (turn && !turn->finished()) {
                 turn->cancel.store(true);
                 LOG_INFO("task: cancel requested for ", id, " (session ", task.session_id, ")");
               }
             }
             Task cancelled;
             TaskStore::update(ws, id,
                               [&](Task* t) {
                                 if (t->status == task_status::kQueued ||
                                     t->status == task_status::kWaitingConfirmation) {
                                   t->status = task_status::kCancelled;
                                   t->finished_at = now_iso8601();
                                 }
                               },
                               &cancelled, &err);
             publish_task(cancelled, "task_cancelled");
             nlohmann::json j = cancelled.to_json();
             j["ok"] = true;
             send_json(res, j);
           });

  // The user answers a task that is waiting for a confirmation: the answer
  // becomes the next turn of the task's session.
  svr.Post(R"(/api/tasks/([^/]+)/confirm)",
           [this](const httplib::Request& req, httplib::Response& res) {
             nlohmann::json body;
             if (!parse_json_body(req, &body, res)) return;
             const std::string id = req.matches[1].str();
             const std::string workspace_id =
                 req.has_param("workspace_id") ? req.get_param_value("workspace_id") : std::string();
             if (workspace_id.empty()) {
               send_error(res, 400, "bad_request", "'workspace_id' is required");
               return;
             }
             Workspace ws;
             std::string err;
             if (!open_workspace(workspace_id, &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             Task task;
             if (!TaskStore::load(ws, id, &task, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             const std::string message = trim(json_string(body, "message"));
             if (message.empty()) {
               send_error(res, 400, "bad_request", "'message' is required");
               return;
             }
             const std::string provider =
                 task.provider.empty() ? config->active_provider : task.provider;
             const ProviderInfo* pinfo = config->provider(provider);
             const std::string provider_model =
                 (pinfo && !pinfo->primary_model().empty()) ? pinfo->primary_model()
                                                            : config->active_model;
             const std::string model = task.model.empty() ? provider_model : task.model;
             std::shared_ptr<ChatTurn> turn =
                 launch_turn(ws.id(), task.session_id, message, provider, model, id, &err);
             if (!turn) {
               send_error(res, 409, "cannot_start", err);
               return;
             }
             Task confirmed;
             TaskStore::update(ws, id,
                               [&](Task* t) {
                                 t->status = task_status::kRunning;
                                 t->session_id = turn->session_id;
                                 t->needs_confirmation = false;
                                 t->question.clear();
                                 t->finished_at.clear();
                                 t->note = "user confirmed: " + message;
                               },
                               &confirmed, &err);
             publish_task(confirmed, "task_confirmed");
             nlohmann::json j = confirmed.to_json();
             j["ok"] = true;
             send_json(res, j);
           });

  // ---- first-run quick setup: a key is the whole config -------------------
  // The upload gate normally asks for a complete provider.yaml, which is a lot
  // to type on a phone or a headless board. This route builds the file from an
  // API key alone - DeepSeek (default model deepseek-flash) or MiniMax (default
  // model MiniMax-M3.1-Flash) - and applies it with the same validation as an
  // upload. The key is written to provider.yaml and never echoed back: the
  // response describes the result, not the config.
  svr.Post("/api/config/quickstart", [this](const httplib::Request& req,
                                            httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string provider = to_lower(trim(json_string(body, "provider", "deepseek")));
    if (provider != "deepseek" && provider != "minimax") {
      send_error(res, 400, "unsupported_provider",
                 "quick setup supports: deepseek, minimax (got '" + provider + "')");
      return;
    }
    std::string api_key = trim(json_string(body, "api_key"));
    if (api_key.empty()) api_key = trim(json_string(body, "key"));
    if (api_key.empty()) {
      send_error(res, 400, "missing_key", "api_key is required");
      return;
    }
    if (api_key.size() > 4096) {
      send_error(res, 400, "bad_key", "api_key is implausibly long");
      return;
    }
    for (std::size_t i = 0; i < api_key.size(); ++i) {
      const unsigned char c = static_cast<unsigned char>(api_key[i]);
      if (c < 0x20 || c == 0x7f) {
        send_error(res, 400, "bad_key",
                   "api_key contains a control character (was it pasted with a newline?)");
        return;
      }
    }
    std::string api_url = trim(json_string(body, "api_url"));
    std::string yaml_text;
    if (provider == "minimax") {
      // api.minimax.cn is the endpoint the project documents; the global
      // api.minimax.io works the same way and can be passed as api_url.
      if (api_url.empty()) api_url = "https://api.minimax.cn/v1";
      yaml_text = minimax_config_yaml(api_key, api_url);
    } else {
      if (api_url.empty()) api_url = "https://api.deepseek.com";
      yaml_text = deepseek_config_yaml(api_key, api_url);
    }
    if (!apply_config_yaml(yaml_text, "generated by the first-run quick setup", res)) return;

    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["path"] = config->path;
    j["provider"] = config->active_provider;
    j["model"] = config->active_model;
    j["models"] = config->active_provider_info() ? config->active_provider_info()->models
                                                : std::vector<std::string>();
    send_json(res, j);
  });

  svr.Get("/api/providers", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json j = nlohmann::json::object();
    j["config_path"] = config->path;
    if (!config->extra_path.empty() && path_exists(config->extra_path)) {
      j["extra_config_path"] = config->extra_path;
    }
    nlohmann::json active = nlohmann::json::object();
    active["provider"] = config->active_provider;
    active["model"] = config->active_model;
    // The UI divides prompt_tokens by the window to show "上下文 12%", so the
    // window has to be reachable without the client guessing it.
    const ModelInfo* active_info = config->active_model_info();
    active["max_context_tokens"] = active_info ? active_info->max_context_tokens : 0;
    j["active"] = active;

    nlohmann::json list = nlohmann::json::array();
    for (std::size_t i = 0; i < config->provider_order.size(); ++i) {
      const ProviderInfo* p = config->provider(config->provider_order[i]);
      if (!p) continue;
      nlohmann::json pj = nlohmann::json::object();
      pj["name"] = p->name;
      pj["api_url"] = p->api_url;
      pj["default_model"] = p->default_model;
      pj["models"] = p->models;
      // `models` stays a plain array of names (existing clients read it that
      // way); per-model capabilities live in a parallel map keyed by model name.
      nlohmann::json model_info = nlohmann::json::object();
      for (std::size_t m = 0; m < p->models.size(); ++m) {
        const ModelInfo* info = p->find_model(p->models[m]);
        nlohmann::json mi = nlohmann::json::object();
        mi["max_context_tokens"] = info ? info->max_context_tokens : 0;
        if (info) {
          mi["max_output_tokens"] = info->max_output_tokens;
          mi["supports_tools"] = info->supports_tools;
          mi["supports_reasoning"] = info->supports_reasoning;
          mi["supports_vision"] = info->supports_vision;
        }
        model_info[p->models[m]] = mi;
      }
      pj["model_info"] = model_info;
      pj["has_key"] = p->has_key();  // never expose the key itself
      pj["supports_search"] = p->supports_search;
      pj["supports_fetch"] = p->supports_fetch;
      // A provider with a quota endpoint: the UI shows a badge for it, so it
      // needs to know *before* asking.
      pj["supports_usage"] = p->supports_usage;
      if (!p->usage_type.empty()) pj["usage_type"] = p->usage_type;
      // The page a human opens when the key alone cannot answer the question.
      if (!p->usage_console_url.empty()) pj["usage_console_url"] = p->usage_console_url;
      if (!p->description.empty()) pj["description"] = p->description;
      list.push_back(pj);
    }
    j["providers"] = list;

    nlohmann::json settings = nlohmann::json::object();
    settings["max_iterations"] = config->settings.max_iterations;
    settings["max_tool_output"] = config->settings.max_tool_output;
    settings["command_timeout_ms"] = config->settings.command_timeout_ms;
    settings["allow_outside_workspace"] = config->settings.allow_outside_workspace;
    j["settings"] = settings;
    j["warnings"] = config->warnings;
    send_json(res, j);
  });

  // How much of a provider's subscription is left. The endpoint is provider
  // specific (MiniMax M Plan: a 5-hour window and a weekly window), so the
  // server makes the call with the provider's own key - the browser never
  // sees it.
  svr.Get(R"(/api/providers/([^/]+)/usage)", [this](const httplib::Request& req, httplib::Response& res) {
    const std::string name = req.matches[1];
    const ProviderInfo* p = config->provider(name);
    if (!p) {
      send_error(res, 404, "not_found", "no provider named '" + name + "'");
      return;
    }
    if (!p->supports_usage) {
      send_error(res, 400, "usage_not_supported",
                 "provider '" + name +
                     "' does not declare supports_usage (set it in provider.yaml to query a quota)");
      return;
    }
    UsageQuota quota;
    query_usage(*p, &quota);
    if (!quota.ok) {
      send_error(res, 502, "usage_failed", quota.error);
      return;
    }
    send_json(res, quota.to_json());
  });

  svr.Get("/api/tools", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json list = nlohmann::json::array();
    const std::vector<ToolDef>& defs = tools->defs();
    for (std::size_t i = 0; i < defs.size(); ++i) {
      nlohmann::json t = nlohmann::json::object();
      t["name"] = defs[i].name;
      t["description"] = defs[i].description;
      t["parameters"] = defs[i].parameters;
      list.push_back(t);
    }
    nlohmann::json j = nlohmann::json::object();
    j["tools"] = list;
    send_json(res, j);
  });

  // ---- workspaces --------------------------------------------------------
  svr.Get("/api/workspaces", [this](const httplib::Request&, httplib::Response& res) {
    nlohmann::json list = nlohmann::json::array();
    {
      std::lock_guard<std::mutex> lock(mu);
      registry->refresh_flags();
      for (std::size_t i = 0; i < registry->entries().size(); ++i) {
        list.push_back(registry->entries()[i].to_json());
      }
    }
    nlohmann::json j = nlohmann::json::object();
    j["index_path"] = registry->index_path();
    j["tea_home"] = config->tea_home;
    j["workspaces"] = list;
    send_json(res, j);
  });

  svr.Post("/api/workspaces", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string path = json_string(body, "path");
    if (path.empty()) {
      send_error(res, 400, "bad_request", "'path' is required");
      return;
    }
    // The UI asks the user for a complete path. A relative one would silently
    // resolve against the server's cwd (and `create` would happily mkdir there),
    // which is not something the person typing it can see, so refuse it up front
    // with a message that shows the expected shape.
    if (!path_is_absolute(expand_user(trim(path)))) {
      send_error(res, 400, "bad_request",
                 "workspace path must be a full path, e.g. /home/me/project or "
                 "C:\\Users\\me\\project (got '" + trim(path) + "')");
      return;
    }
    // `create: true` lets the UI offer "the directory does not exist, create
    // it?" instead of dead-ending on a typo-free new project path.
    nlohmann::json::const_iterator create_it = body.find("create");
    if (create_it != body.end() && create_it->is_boolean() && create_it->get<bool>()) {
      const std::string expanded = path_absolute(expand_user(trim(path)));
      if (!path_exists(expanded)) {
        if (!make_dirs(expanded)) {
          send_error(res, 400, "workspace_error", "cannot create directory " + expanded);
          return;
        }
        LOG_INFO("workspace: created directory ", expanded);
      }
    }
    std::string err;
    WorkspaceEntry* entry = nullptr;
    {
      std::lock_guard<std::mutex> lock(mu);
      entry = registry->add(path, json_string(body, "name"), &err);
      if (!entry) {
        send_error(res, 400, "workspace_error", err);
        return;
      }
      registry->refresh_flags();
      // Materialise .tea_agent_run/ right away, like the CLI does.
      Workspace ws;
      std::string open_err;
      if (!ws.open(*entry, config->settings, &open_err)) {
        LOG_WARN("workspace: ", open_err);
      }
      send_json(res, entry->to_json());
    }
  });

  svr.Delete(R"(/api/workspaces/([^/]+))",
             [this](const httplib::Request& req, httplib::Response& res) {
               const std::string id = req.matches[1].str();
               std::string err;
               {
                 std::lock_guard<std::mutex> lock(mu);
                 if (!registry->remove(id, &err)) {
                   send_error(res, 404, "not_found", err);
                   return;
                 }
               }
               send_ok(res);
             });

  svr.Get(R"(/api/workspaces/([^/]+)/tree)",
          [this](const httplib::Request& req, httplib::Response& res) {
            const std::string id = req.matches[1].str();
            Workspace ws;
            std::string err;
            if (!open_workspace(id, &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string rel = req.has_param("path") ? req.get_param_value("path") : "";
            const std::string target = rel.empty() ? ws.root() : ws.resolve_path(rel, &err);
            if (target.empty()) {
              send_error(res, 400, "bad_path", err);
              return;
            }
            if (!path_is_dir(target)) {
              send_error(res, 400, "not_a_directory", "not a directory: " + rel);
              return;
            }
            std::vector<DirEntryInfo> entries;
            if (!list_directory(target, &entries, &err)) {
              send_error(res, 400, "list_failed", err);
              return;
            }
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < entries.size(); ++i) {
              nlohmann::json e = nlohmann::json::object();
              e["name"] = entries[i].name;
              e["path"] = ws.display_path(entries[i].path);
              e["type"] = entries[i].is_dir ? "dir" : "file";
              e["size"] = entries[i].size;
              e["mtime"] = entries[i].mtime;
              list.push_back(e);
            }
            nlohmann::json j = nlohmann::json::object();
            j["path"] = (target == ws.root()) ? std::string() : ws.display_path(target);
            j["root"] = ws.root();
            j["entries"] = list;
            send_json(res, j);
          });

  svr.Get(R"(/api/workspaces/([^/]+)/file)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string rel = req.has_param("path") ? req.get_param_value("path") : "";
            if (rel.empty()) {
              send_error(res, 400, "bad_request", "'path' is required");
              return;
            }
            const std::string target = ws.resolve_path(rel, &err);
            if (target.empty()) {
              send_error(res, 400, "bad_path", err);
              return;
            }
            if (!path_is_file(target)) {
              send_error(res, 404, "not_found",
                         path_exists(target) ? "not a regular file" : "file not found");
              return;
            }
            std::string content;
            bool truncated = false;
            if (!read_file(target, &content, kFileReadLimit, &truncated)) {
              send_error(res, 400, "read_failed", "cannot read " + rel);
              return;
            }
            nlohmann::json j = nlohmann::json::object();
            j["path"] = ws.display_path(target);
            j["size"] = file_size(target);
            j["truncated"] = truncated;
            j["content"] = content;
            send_json(res, j);
          });

  // ---- raw file bytes ----------------------------------------------------
  // Images/PDFs referenced from assistant markdown are loaded by the browser
  // directly, so they need a binary route; `/file` stays JSON-only.
  svr.Get(R"(/api/workspaces/([^/]+)/raw)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string rel = req.has_param("path") ? req.get_param_value("path") : "";
            if (rel.empty()) {
              send_error(res, 400, "bad_request", "'path' is required");
              return;
            }
            const std::string target = ws.resolve_path(rel, &err);
            if (target.empty()) {
              // resolve_path enforces the workspace boundary: ../.. is refused
              // here and never reaches the read below.
              send_error(res, 403, "forbidden", err);
              return;
            }
            if (!path_is_file(target)) {
              send_error(res, 404, "not_found",
                         path_exists(target) ? "not a regular file" : "file not found");
              return;
            }
            const uint64_t size = file_size(target);
            if (size > kRawReadLimit) {
              send_error(res, 413, "too_large",
                         "file is larger than 16 MB; use /file for a truncated view");
              return;
            }
            std::string bytes;
            if (!read_file(target, &bytes)) {
              send_error(res, 400, "read_failed", "cannot read " + rel);
              return;
            }
            res.set_header("Cache-Control", "no-store");
            res.set_header("Access-Control-Allow-Origin", "*");
            res.set_content(bytes, raw_mime_for(target));
          });

  // ---- download (attachment, streamed) -----------------------------------
  // /raw is for showing a file in the browser (images, PDFs) and reads it into
  // memory with a 16 MB cap. Artifacts can be a 200 MB video, so this route
  // hands the file to the client as an attachment and streams it from disk.
  svr.Get(R"(/api/workspaces/([^/]+)/download)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string rel = req.has_param("path") ? req.get_param_value("path") : "";
            if (rel.empty()) {
              send_error(res, 400, "bad_request", "'path' is required");
              return;
            }
            const std::string target = ws.resolve_path(rel, &err);
            if (target.empty()) {
              send_error(res, 403, "forbidden", err);
              return;
            }
            if (!path_is_file(target)) {
              send_error(res, 404, "not_found",
                         path_exists(target) ? "not a regular file" : "file not found");
              return;
            }
            const uint64_t size = file_size(target);
            // `inline=1` renders in place (a video, a PDF) instead of saving.
            const bool inline_file = req.has_param("inline") &&
                                     (req.get_param_value("inline") == "1" ||
                                      req.get_param_value("inline") == "true");
            const std::string name = path_filename(target);
            res.set_header("Content-Disposition",
                           std::string(inline_file ? "inline" : "attachment") +
                               "; filename=\"" + ascii_download_name(name) +
                               "\"; filename*=UTF-8''" + rfc5987_name(name));
            res.set_header("Cache-Control", "no-store");
            res.set_header("Access-Control-Allow-Origin", "*");
            res.set_header("Access-Control-Expose-Headers",
                           "Content-Disposition, Content-Length");
            const std::string mime = raw_mime_for(target);
            LOG_INFO("download: ", ws.display_path(target), " (", human_size(size), ", ",
                     mime, ")");
            if (size == 0) {
              res.set_content("", mime);
              return;
            }
            // Streamed: random access via fseek, so a 500 MB video never has to
            // sit in memory. (Range requests are not supported; a download does
            // not need them.)
            // MSVC needs the wide path (a non-ASCII name would otherwise fail);
            // POSIX takes the narrow one.
            auto file = std::make_shared<std::ifstream>();
#ifdef _WIN32
            file->open(utf8_to_wide(target).c_str(), std::ios::binary);
#else
            file->open(target.c_str(), std::ios::binary);
#endif
            if (!file->is_open()) {
              send_error(res, 500, "io_error", "cannot open " + rel);
              return;
            }
            res.set_content_provider(
                static_cast<size_t>(size), mime,
                [file](size_t offset, size_t length, httplib::DataSink& sink) {
                  file->clear();
                  file->seekg(static_cast<std::streamoff>(offset), std::ios::beg);
                  std::vector<char> buf(64 * 1024);
                  size_t remaining = length;
                  while (remaining > 0) {
                    const size_t chunk = remaining < buf.size() ? remaining : buf.size();
                    file->read(buf.data(), static_cast<std::streamsize>(chunk));
                    const std::streamsize got = file->gcount();
                    if (got <= 0) return false;
                    if (!sink.write(buf.data(), static_cast<size_t>(got))) return false;
                    remaining -= static_cast<size_t>(got);
                  }
                  return true;
                },
                nullptr);
          });

  svr.Put(R"(/api/workspaces/([^/]+)/file)",
          [this](const httplib::Request& req, httplib::Response& res) {
            nlohmann::json body;
            if (!parse_json_body(req, &body, res)) return;
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string rel = json_string(body, "path");
            if (rel.empty()) {
              send_error(res, 400, "bad_request", "'path' is required");
              return;
            }
            const std::string target = ws.resolve_path(rel, &err);
            if (target.empty()) {
              send_error(res, 400, "bad_path", err);
              return;
            }
            const std::string content = json_string(body, "content");
            if (!write_file(target, content, false, &err)) {
              send_error(res, 400, "write_failed", err);
              return;
            }
            nlohmann::json j = nlohmann::json::object();
            j["ok"] = true;
            j["bytes"] = static_cast<long long>(content.size());
            j["size"] = file_size(target);
            send_json(res, j);
          });

  // ---- destroy: delete every trace ---------------------------------------
  // Deletes the tea home (provider.yaml, tea_agent.yaml, workspaces.json, the
  // global log) and every indexed workspace's `.tea_agent_run`. Irreversible, so
  // GET only previews the list and POST requires an explicit confirmation in the
  // body - a stray POST with no payload must not be able to do this.
  svr.Get("/api/destroy", [this](const httplib::Request&, httplib::Response& res) {
    send_json(res, destroy_targets());
  });

  svr.Post("/api/destroy", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const bool confirmed = (body.contains("confirm") &&
                            ((body["confirm"].is_boolean() && body["confirm"].get<bool>()) ||
                             (body["confirm"].is_string() &&
                              body["confirm"].get<std::string>() == "destroy")));
    if (!confirmed) {
      send_error(res, 400, "not_confirmed",
                 "this deletes the tea home and every workspace's .tea_agent_run; "
                 "send {\"confirm\": \"destroy\"} to proceed");
      return;
    }
    // A running turn would keep writing sessions/trajectories into directories
    // that no longer exist (and could recreate them).
    {
      std::lock_guard<std::mutex> lock(mu);
      for (auto it = turns.begin(); it != turns.end(); ++it) {
        if (it->second && !it->second->finished()) {
          send_error(res, 409, "turn_running",
                     "a chat turn is still running; stop it before destroying the state");
          return;
        }
      }
    }
    nlohmann::json result = destroy_all();
    LOG_WARN("destroy: removed ", result["removed"].size(), " path(s), skipped ",
             result["skipped"].size(), ", failed ", result["failed"].size(),
             "; the agent is back to its unconfigured state");
    send_json(res, result);
  });

  // ---- system prompt (per workspace) -------------------------------------
  // The persona lives in `<run_dir>/system_prompt.md` and is re-read on every
  // request, so a save here reaches the next turn - no restart, no session
  // rewrite. An empty file means "use the default" (settings.yaml's
  // system_prompt, then the built-in rules).
  svr.Get(R"(/api/workspaces/([^/]+)/system-prompt)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            send_json(res, system_prompt_info(ws));
          });

  svr.Put(R"(/api/workspaces/([^/]+)/system-prompt)",
          [this](const httplib::Request& req, httplib::Response& res) {
            nlohmann::json body;
            if (!parse_json_body(req, &body, res)) return;
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            std::string content = json_string(body, "content");
            // The editor is seeded with the persona that is currently in effect,
            // so "saved without touching it" arrives as a copy of the default.
            // Storing that copy would silently pin the workspace to today's
            // default (a later change to the machine-level prompt would no
            // longer reach it), so it is normalised back to "no override".
            if (trim(content) == default_persona(ws)) content.clear();
            if (!ws.save_system_prompt(content, &err)) {
              send_error(res, 400, "bad_request", err);
              return;
            }
            LOG_INFO("system prompt: rewritten from the web UI (", content.size(),
                     " bytes; ", trim(content).empty() ? "using the default" : "workspace override",
                     ")");
            nlohmann::json j = system_prompt_info(ws);
            j["ok"] = true;
            send_json(res, j);
          });

  // ---- prompt preview ----------------------------------------------------
  // Shows exactly the system prompt this workspace produces, which is what
  // operators need in order to reason about device probing and cache stability.
  svr.Get(R"(/api/workspaces/([^/]+)/prompt)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            Agent agent(config, tools);
            Session session;
            session.info.created_at = now_iso8601();
            const std::string prompt = agent.build_system_prompt(ws, session);
            nlohmann::json j = nlohmann::json::object();
            j["system_prompt"] = prompt;
            j["bytes"] = static_cast<long long>(prompt.size());
            j["estimated_tokens"] = Agent::estimate_tokens(prompt);
            j["settings_file"] = ws.settings_file();
            j["instructions_file"] = ws.instructions_file();
            j["system_prompt_file"] = ws.system_prompt_file();
            j["has_instructions"] = !ws.instructions().empty();
            bool custom_prompt = false;
            ws.load_system_prompt(&custom_prompt);
            j["has_system_prompt"] = custom_prompt;
            send_json(res, j);
          });
  // ---- sessions ----------------------------------------------------------
  svr.Get(R"(/api/workspaces/([^/]+)/sessions)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::vector<SessionInfo> sessions = ws.list_sessions();
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < sessions.size(); ++i) list.push_back(sessions[i].to_json());
            nlohmann::json j = nlohmann::json::object();
            j["sessions"] = list;
            send_json(res, j);
          });

  svr.Post(R"(/api/workspaces/([^/]+)/sessions)",
           [this](const httplib::Request& req, httplib::Response& res) {
             nlohmann::json body;
             if (!parse_json_body(req, &body, res)) return;
             Workspace ws;
             std::string err;
             if (!open_workspace(req.matches[1].str(), &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             Session session;
             if (!ws.create_session(json_string(body, "title"), &session, &err)) {
               send_error(res, 400, "session_error", err);
               return;
             }
             send_json(res, session.info.to_json());
           });

  svr.Get(R"(/api/sessions/([^/]+))",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!find_session_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            Session session;
            if (!ws.load_session(req.matches[1].str(), &session, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            nlohmann::json messages = nlohmann::json::array();
            for (std::size_t i = 0; i < session.messages.size(); ++i) {
              messages.push_back(session.messages[i].to_store());
            }
            nlohmann::json todos = nlohmann::json::array();
            for (std::size_t i = 0; i < session.todos.size(); ++i) {
              todos.push_back(session.todos[i].to_json());
            }
            nlohmann::json artifacts = nlohmann::json::array();
            for (std::size_t i = 0; i < session.artifacts.size(); ++i) {
              artifacts.push_back(session.artifacts[i].to_json());
            }
            nlohmann::json j = nlohmann::json::object();
            j["session"] = session.info.to_json();
            j["workspace_id"] = ws.id();
            j["messages"] = messages;
            j["todos"] = todos;
            j["artifacts"] = artifacts;
            send_json(res, j);
          });

  // ---- export / import a session -----------------------------------------
  // Export is one self-contained HTML file: CSS inlined, images inlined as
  // data: URLs, no network, no sidecar directory. Double-click it on any
  // machine and the conversation is there. The same file carries the session as
  // JSON, so it can be imported back (here or on another device) instead of
  // only being read.
  svr.Get(R"(/api/sessions/([^/]+)/export)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!find_session_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            Session session;
            if (!ws.load_session(req.matches[1].str(), &session, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            std::map<std::string, std::string> image_data;
            const std::string html =
                export_session_html(ws, session, std::string(tea::version_string()), ExportLimits(),
                                    &image_data);
            const std::string name = export_file_name(session.info.title);
            res.set_header("Content-Disposition",
                           "attachment; filename=\"" + name + "\"; filename*=UTF-8''" +
                               rfc5987_name(name));
            res.set_header("Cache-Control", "no-store");
            res.set_header("Access-Control-Allow-Origin", "*");
            res.set_header("X-Session-Messages",
                           std::to_string(static_cast<long long>(session.messages.size())));
            res.set_header("X-Export-Images", std::to_string(image_data.size()));
            LOG_INFO("export: session ", session.info.id, " -> ", name, " (", html.size(),
                     " B, ", image_data.size(), " image(s))");
            res.set_content(html, "text/html; charset=utf-8");
          });

  // Import: the body may be an exported HTML file (raw, or {"html": "..."}) or
  // the session JSON on its own ({"messages": [...]}). A new session is always
  // created - importing twice must not overwrite the conversation it came from.
  svr.Post(R"(/api/workspaces/([^/]+)/sessions/import)",
           [this](const httplib::Request& req, httplib::Response& res) {
             Workspace ws;
             std::string err;
             if (!open_workspace(req.matches[1].str(), &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             if (trim(req.body).empty()) {
               send_error(res, 400, "bad_request",
                          "send the exported .html file, {\"html\": \"...\"} or the session "
                          "JSON");
               return;
             }
             if (req.body.size() > 64u * 1024u * 1024u) {
               send_error(res, 413, "too_large", "the export must be smaller than 64 MB");
               return;
             }
             std::string source = req.body;
             // A JSON wrapper ({"html": "..."} or the payload itself) is unwrapped
             // here; anything else is treated as the HTML file itself.
             if (trim(source)[0] == '{') {
               nlohmann::json body = nlohmann::json::parse(source, nullptr, false);
               if (body.is_object()) {
                 const std::string wrapped = json_string(body, "html");
                 if (!wrapped.empty()) {
                   source = wrapped;
                 } else if (body.contains("messages")) {
                   Session session;
                   if (!import_session_payload(ws, body, &session, &err)) {
                     send_error(res, 400, "bad_request", err);
                     return;
                   }
                   nlohmann::json j = session.info.to_json();
                   j["imported"] = true;
                   send_json(res, {{"ok", true}, {"session", j},
                                   {"messages", session.messages.size()}});
                   return;
                 }
               }
             }
             nlohmann::json payload;
             if (!extract_session_json_from_html(source, &payload, &err)) {
               send_error(res, 400, "bad_export", err);
               return;
             }
             Session session;
             if (!import_session_payload(ws, payload, &session, &err)) {
               send_error(res, 400, "bad_export", err);
               return;
             }
             nlohmann::json j = session.info.to_json();
             j["imported"] = true;
             LOG_INFO("import: created session ", session.info.id, " from an export (",
                      session.messages.size(), " messages)");
             send_json(res, {{"ok", true}, {"session", j},
                             {"messages", session.messages.size()}});
           });

  // The task list is writeable from outside as well: the UI ticks items off, and
  // a controlling agent can drive the plan without going through a chat turn.
  // Body: {"items": [...]} replaces the list, or {"index": n | "id": "t1",
  // "status": "...", "text": "..."} updates one entry.
  svr.Put(R"(/api/sessions/([^/]+)/todos)",
          [this](const httplib::Request& req, httplib::Response& res) {
            nlohmann::json body;
            if (!parse_json_body(req, &body, res)) return;
            Workspace ws;
            std::string err;
            if (!find_session_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            // A running turn owns its Session copy and writes it back wholesale,
            // so an edit made here would be undone by the worker's next save -
            // or, worse, the worker's save would be undone by this one. Refuse
            // and say why instead of silently losing the click.
            if (session_turn_running(req.matches[1].str())) {
              send_error(res, 409, "turn_running",
                         "this session's turn is still running and owns the task list; "
                         "tick items off after it finishes (the agent updates the list itself "
                         "while it works)");
              return;
            }
            Session session;
            if (!ws.load_session(req.matches[1].str(), &session, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            if (body.contains("items")) {
              if (!body["items"].is_array()) {
                send_error(res, 400, "bad_request", "'items' must be an array");
                return;
              }
              std::vector<TodoItem> next;
              for (auto it = body["items"].begin(); it != body["items"].end(); ++it) {
                TodoItem item = TodoItem::from_json(*it);
                if (trim(item.text).empty()) continue;
                if (item.id.empty()) item.id = "t" + std::to_string(next.size() + 1);
                next.push_back(item);
              }
              session.todos = next;
            } else {
              const std::string id = json_string(body, "id");
              const long long index = body.contains("index") && body["index"].is_number()
                                          ? body["index"].get<long long>()
                                          : 0;
              std::size_t at = session.todos.size();
              for (std::size_t i = 0; i < session.todos.size(); ++i) {
                if ((!id.empty() && session.todos[i].id == id) ||
                    (id.empty() && index >= 1 &&
                     static_cast<std::size_t>(index) == i + 1)) {
                  at = i;
                  break;
                }
              }
              if (at >= session.todos.size()) {
                send_error(res, 404, "not_found", "no such todo item");
                return;
              }
              const std::string status = to_lower(trim(json_string(body, "status")));
              const std::string text = json_string(body, "text");
              if (!status.empty()) {
                if (!TodoItem::valid_status(status)) {
                  send_error(res, 400, "bad_request", "status must be pending, active or done");
                  return;
                }
                if (status == "active") {
                  for (std::size_t i = 0; i < session.todos.size(); ++i) {
                    if (i != at && session.todos[i].status == "active") {
                      session.todos[i].status = "pending";
                    }
                  }
                }
                session.todos[at].status = status;
              }
              if (!trim(text).empty()) session.todos[at].text = trim(text);
            }
            session.info.updated_at = now_iso8601();
            if (!ws.save_session(session, &err)) {
              send_error(res, 500, "io_error", err);
              return;
            }
            int total = 0, done = 0;
            todo_counts(session.todos, &total, &done);
            nlohmann::json items = nlohmann::json::array();
            for (std::size_t i = 0; i < session.todos.size(); ++i) {
              items.push_back(session.todos[i].to_json());
            }
            send_json(res, {{"ok", true}, {"items", items}, {"total", total}, {"done", done}});
          });

  svr.Delete(R"(/api/sessions/([^/]+))",
             [this](const httplib::Request& req, httplib::Response& res) {
               Workspace ws;
               std::string err;
               if (!find_session_workspace(req.matches[1].str(), &ws, &err)) {
                 send_error(res, 404, "not_found", err);
                 return;
               }
               // The worker would keep saving the transcript it holds and
               // resurrect the file (with an index entry) after the delete.
               if (session_turn_running(req.matches[1].str())) {
                 send_error(res, 409, "turn_running",
                            "this session's turn is still running; stop it before deleting the "
                            "session, otherwise the worker would write it back");
                 return;
               }
               if (!ws.delete_session(req.matches[1].str(), &err)) {
                 send_error(res, 400, "session_error", err);
                 return;
               }
               send_ok(res);
             });

  // Renaming is what the sidebar's session index calls; the title is display-only.
  svr.Patch(R"(/api/sessions/([^/]+))",
            [this](const httplib::Request& req, httplib::Response& res) {
              nlohmann::json body;
              if (!parse_json_body(req, &body, res)) return;
              Workspace ws;
              std::string err;
              if (!find_session_workspace(req.matches[1].str(), &ws, &err)) {
                send_error(res, 404, "not_found", err);
                return;
              }
              const std::string title = json_string(body, "title");
              if (trim(title).empty()) {
                send_error(res, 400, "bad_request", "'title' is required");
                return;
              }
              // The running turn saves the whole Session, title included, so a
              // rename now would flicker back at its next iteration.
              if (session_turn_running(req.matches[1].str())) {
                send_error(res, 409, "turn_running",
                           "this session's turn is still running and will write the old title "
                           "back; rename it after the turn finishes");
                return;
              }
              if (!ws.rename_session(req.matches[1].str(), title, &err)) {
                send_error(res, 400, "session_error", err);
                return;
              }
              send_ok(res);
            });

  // ---- forking a session -------------------------------------------------
  // A branch starts as a copy of the parent's transcript through the end of the
  // selected turn; the next message sent to it becomes that branch's next turn
  // while the parent keeps going its own way. `message_index` addresses a user
  // message of the parent (the child also gets the assistant/tool messages that
  // answered it), omitted or negative forks at the end, and `title` overrides
  // the generated "分叉 <turn> · <parent title>".
  svr.Post(R"(/api/sessions/([^/]+)/fork)",
           [this](const httplib::Request& req, httplib::Response& res) {
             nlohmann::json body;
             if (!parse_json_body(req, &body, res)) return;
             const std::string session_id = req.matches[1].str();
             Workspace ws;
             std::string err;
             if (!find_session_workspace(session_id, &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             long long message_index = -1;
             auto idx_it = body.find("message_index");
             if (idx_it != body.end() && !idx_it->is_null()) {
               if (idx_it->is_number_unsigned()) {
                 const unsigned long long v = idx_it->get<unsigned long long>();
                 if (v > 1000000000ull) {
                   send_error(res, 400, "bad_request", "'message_index' is out of range");
                   return;
                 }
                 message_index = static_cast<long long>(v);
               } else if (idx_it->is_number_integer()) {
                 message_index = idx_it->get<long long>();
               } else {
                 send_error(res, 400, "bad_request", "'message_index' must be an integer");
                 return;
               }
             }
             // Copying a transcript that a running turn is still appending to
             // would freeze a half-finished answer into the branch. The UI does
             // not arm a fork point while streaming; this covers direct callers.
             {
               std::lock_guard<std::mutex> lock(mu);
               auto it = turns.find(session_id);
               if (it != turns.end() && it->second && !it->second->finished()) {
                 send_error(res, 409, "turn_running",
                            "this session has a turn in progress; it must finish before it "
                            "can be forked");
                 return;
               }
             }
             Session child;
             if (!ws.fork_session(session_id, message_index, json_string(body, "title"), &child,
                                  &err)) {
               send_error(res, 400, "fork_failed", err);
               return;
             }
             nlohmann::json messages = nlohmann::json::array();
             for (std::size_t i = 0; i < child.messages.size(); ++i) {
               messages.push_back(child.messages[i].to_store());
             }
             nlohmann::json j = nlohmann::json::object();
             j["ok"] = true;
             j["workspace_id"] = ws.id();
             j["session"] = child.info.to_json();
             j["messages"] = messages;
             send_json(res, j);
           });

  // ---- trajectories (the per-turn audit trail) ---------------------------
  // One JSONL file per session (`<run_dir>/trajectories/<session_id>.jsonl`,
  // one line per turn). The default view is the newest turn summaries first;
  // `?full=1` adds steps and file changes, `?format=jsonl` hands the raw bytes
  // to a controlling agent, and `/api/turns/<id>/trajectory` addresses exactly
  // the turn the SSE `done` event announced as `trajectory_id`.
  svr.Get(R"(/api/sessions/([^/]+)/trajectory)",
          [this](const httplib::Request& req, httplib::Response& res) {
            const std::string session_id = req.matches[1].str();
            // The id becomes a file name further down; refuse anything that is
            // not a plain id so a crafted path cannot escape the run directory.
            if (!trajectory_id_is_safe(session_id)) {
              send_error(res, 400, "bad_request", "invalid session id");
              return;
            }
            Workspace ws;
            std::string err;
            if (!find_session_workspace(session_id, &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string path = trajectory_file_path(ws.run_dir(), session_id);
            if (!path_is_file(path)) {
              send_error(res, 404, "not_found",
                         "no trajectory recorded for session " + session_id);
              return;
            }
            if (req.has_param("format") &&
                to_lower(req.get_param_value("format")) == "jsonl") {
              std::string bytes;
              if (!read_file(path, &bytes, kTrajectoryReadLimit)) {
                send_error(res, 400, "read_failed", "cannot read " + path);
                return;
              }
              res.status = 200;
              res.set_header("Access-Control-Allow-Origin", "*");
              res.set_header("Cache-Control", "no-store");
              res.set_content(bytes, "application/x-ndjson; charset=utf-8");
              return;
            }
            std::vector<nlohmann::json> turns;
            if (!load_trajectories(path, &turns, &err)) {
              send_error(res, 400, "trajectory_error", err);
              return;
            }
            const bool full = req.has_param("full") && req.get_param_value("full") != "0";
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < turns.size(); ++i) {
              list.push_back(full ? turns[i] : trajectory_summary(turns[i]));
            }
            send_json(res, list);
          });

  svr.Get(R"(/api/turns/([^/]+)/trajectory)",
          [this](const httplib::Request& req, httplib::Response& res) {
            const std::string turn_id = req.matches[1].str();
            if (!trajectory_id_is_safe(turn_id)) {
              send_error(res, 400, "bad_request", "invalid turn id");
              return;
            }
            // Turn ids are not indexed, so scan the registered workspaces'
            // session files and stop at the first hit (which is what a caller
            // polling right after `done` gets).
            std::vector<WorkspaceEntry> entries;
            {
              std::lock_guard<std::mutex> lock(mu);
              for (std::size_t i = 0; i < registry->entries().size(); ++i) {
                entries.push_back(registry->entries()[i]);
              }
            }
            for (std::size_t i = 0; i < entries.size(); ++i) {
              if (entries[i].path.empty() || !path_is_dir(entries[i].path)) continue;
              Workspace ws;
              std::string err;
              if (!ws.open(entries[i], config->settings, &err)) continue;
              const std::string dir = path_join(ws.run_dir(), "trajectories");
              if (!path_is_dir(dir)) continue;
              std::vector<DirEntryInfo> files;
              if (!list_directory(dir, &files, &err)) continue;
              for (std::size_t k = 0; k < files.size(); ++k) {
                if (files[k].is_dir) continue;
                if (path_extension(files[k].name) != ".jsonl") continue;
                std::vector<nlohmann::json> turns;
                if (!load_trajectories(files[k].path, &turns, &err)) continue;
                for (std::size_t t = 0; t < turns.size(); ++t) {
                  if (turns[t].value("turn_id", std::string()) != turn_id) continue;
                  send_json(res, turns[t]);
                  return;
                }
              }
            }
            send_error(res, 404, "not_found", "no trajectory recorded for turn " + turn_id);
          });

  // ---- memory ------------------------------------------------------------
  svr.Get(R"(/api/workspaces/([^/]+)/memory)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::vector<nlohmann::json> notes = ws.load_memory(&err);
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < notes.size(); ++i) list.push_back(notes[i]);
            nlohmann::json j = nlohmann::json::object();
            j["notes"] = list;
            j["path"] = ws.memory_file();
            send_json(res, j);
          });

  svr.Post(R"(/api/workspaces/([^/]+)/memory)",
           [this](const httplib::Request& req, httplib::Response& res) {
             nlohmann::json body;
             if (!parse_json_body(req, &body, res)) return;
             Workspace ws;
             std::string err;
             if (!open_workspace(req.matches[1].str(), &ws, &err)) {
               send_error(res, 404, "not_found", err);
               return;
             }
             const nlohmann::json note = ws.add_memory_note(json_string(body, "text"), &err);
             if (note.is_null()) {
               send_error(res, 400, "bad_request", err);
               return;
             }
             send_json(res, note);
           });

  svr.Delete(R"(/api/workspaces/([^/]+)/memory/([^/]+))",
             [this](const httplib::Request& req, httplib::Response& res) {
               Workspace ws;
               std::string err;
               if (!open_workspace(req.matches[1].str(), &ws, &err)) {
                 send_error(res, 404, "not_found", err);
                 return;
               }
               if (!ws.delete_memory_note(req.matches[2].str(), &err)) {
                 send_error(res, 404, "not_found", err);
                 return;
               }
               send_ok(res);
             });

  // ---- agent.md (workspace experience log) -------------------------------
  // The file is injected into the system prompt on every turn and maintained by
  // the agent through toolkit_agent_doc; these routes let a human read and fix it
  // from the web UI without shelling into the device.
  svr.Get(R"(/api/workspaces/([^/]+)/agent-doc)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            bool truncated = false;
            const std::string content = ws.load_agent_doc(&truncated);
            std::vector<DirEntryInfo> scripts;
            list_directory(ws.scripts_dir(), &scripts, nullptr);
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < scripts.size(); ++i) {
              if (scripts[i].is_dir) continue;
              list.push_back({{"name", scripts[i].name},
                              {"size", static_cast<long long>(scripts[i].size)},
                              {"mtime", scripts[i].mtime}});
            }
            nlohmann::json j = nlohmann::json::object();
            j["path"] = ws.agent_doc_file();
            j["relative_path"] = ws.display_path(ws.agent_doc_file());
            j["scripts_dir"] = ws.display_path(ws.scripts_dir());
            j["content"] = content;
            j["bytes"] = static_cast<long long>(content.size());
            j["truncated"] = truncated;
            j["scripts"] = list;
            send_json(res, j);
          });

  svr.Put(R"(/api/workspaces/([^/]+)/agent-doc)",
          [this](const httplib::Request& req, httplib::Response& res) {
            nlohmann::json body;
            if (!parse_json_body(req, &body, res)) return;
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            const std::string content = json_string(body, "content");
            if (!ws.save_agent_doc(content, &err)) {
              send_error(res, 400, "bad_request", err);
              return;
            }
            LOG_INFO("agent.md: rewritten from the web UI (", content.size(), " bytes)");
            nlohmann::json j = nlohmann::json::object();
            j["ok"] = true;
            j["path"] = ws.agent_doc_file();
            j["bytes"] = static_cast<long long>(file_size(ws.agent_doc_file()));
            send_json(res, j);
          });

  // ---- workspace access (the 受限工作区 / 全局 switch) ----------------------
  // `--allow-outside-workspace` used to be a start-up-only decision: changing your
  // mind meant restarting the process (and, if the agent runs headless on a
  // device, finding a shell). The switch below persists the same setting into the
  // workspace's own settings.yaml, which is exactly the file the layered config
  // already reads, so it survives a restart and is visible to whoever reads the
  // project next. Two rules keep it honest:
  //
  //   * the CLI flag is a floor - the UI may tighten, never loosen it;
  //   * the response always says what is in effect *now*, read back from the
  //     freshly updated Workspace rather than echoed from the request.
  svr.Get(R"(/api/workspaces/([^/]+)/settings)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            nlohmann::json j = nlohmann::json::object();
            j["allow_outside_workspace"] = ws.settings().allow_outside_workspace;
            j["fixed_by_cli"] = config->cli_allow_outside_workspace;
            j["workspace_root"] = ws.root();
            j["settings_file"] = ws.settings_file();
            j["relative_settings_file"] = ws.display_path(ws.settings_file());
            send_json(res, j);
          });

  svr.Put(R"(/api/workspaces/([^/]+)/settings)",
          [this](const httplib::Request& req, httplib::Response& res) {
            nlohmann::json body;
            if (!parse_json_body(req, &body, res)) return;
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }

            auto want = body.find("allow_outside_workspace");
            if (want == body.end() || !want->is_boolean()) {
              send_error(res, 400, "bad_request",
                         "'allow_outside_workspace' must be a boolean");
              return;
            }
            const bool value = want->get<bool>();

            if (!value && config->cli_allow_outside_workspace) {
              send_error(res, 409, "fixed_by_cli",
                          "this process was started with --allow-outside-workspace, which "
                          "cannot be switched off from the web UI; restart it without the "
                          "flag to restore the workspace boundary");
              return;
            }

            if (!ws.write_setting_bool("allow_outside_workspace", value, &err)) {
              send_error(res, 400, "settings_error", err);
              return;
            }
            if (value) {
              // Worth a WARN of its own: from now on the tools (and the file page)
              // reach the whole machine, not just this directory.
              LOG_WARN("workspace ", ws.id(), ": file access widened to the whole machine "
                                              "(allow_outside_workspace = true) via the web UI");
            } else {
              LOG_INFO("workspace ", ws.id(),
                       ": file access restricted to the workspace again (allow_outside_workspace "
                       "= false)");
            }

            nlohmann::json j = nlohmann::json::object();
            j["ok"] = true;
            j["allow_outside_workspace"] = ws.settings().allow_outside_workspace;
            j["fixed_by_cli"] = config->cli_allow_outside_workspace;
            j["workspace_root"] = ws.root();
            j["settings_file"] = ws.settings_file();
            j["relative_settings_file"] = ws.display_path(ws.settings_file());
            send_json(res, j);
          });

  // ---- logs --------------------------------------------------------------
  svr.Get(R"(/api/workspaces/([^/]+)/logs)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            std::size_t tail = 300;
            if (req.has_param("tail")) {
              try {
                const long long v = std::stoll(req.get_param_value("tail"));
                if (v > 0) tail = static_cast<std::size_t>(v > 5000 ? 5000 : v);
              } catch (const std::exception&) {
              }
            }
            const std::vector<std::string> lines = ws.tail_log(tail);
            nlohmann::json list = nlohmann::json::array();
            for (std::size_t i = 0; i < lines.size(); ++i) list.push_back(lines[i]);
            nlohmann::json j = nlohmann::json::object();
            j["path"] = ws.log_file();
            j["lines"] = list;
            send_json(res, j);
          });

  // ---- workspace archive download ----------------------------------------
  // One ZIP with a whole directory of *this device*, so an operator can take the
  // run data (sessions, trajectories, logs, memory, agent.md) with them without
  // shelling into the machine. Assembled in memory by src/archive.cpp; the limits
  // there are what keeps a single request from exhausting the device, and files
  // that do not fit are reported in X-Archive-Skipped instead of failing the
  // whole download.
  svr.Get(R"(/api/workspaces/([^/]+)/archive)",
          [this](const httplib::Request& req, httplib::Response& res) {
            Workspace ws;
            std::string err;
            if (!open_workspace(req.matches[1].str(), &ws, &err)) {
              send_error(res, 404, "not_found", err);
              return;
            }
            // The default is the run directory: that is what "download this
            // workspace's tea_agent data" means. Any other directory inside the
            // workspace works the same way - resolve_path is what refuses to
            // leave the sandbox, so `../..` never reaches the zipper.
            std::string rel = req.has_param("path") ? trim(req.get_param_value("path")) : "";
            if (rel.empty()) rel = ".tea_agent_run";
            const std::string target = ws.resolve_path(rel, &err);
            if (target.empty()) {
              send_error(res, 403, "forbidden", err);
              return;
            }
            if (!path_exists(target)) {
              send_error(res, 404, "not_found", "no such directory: " + rel);
              return;
            }
            if (!path_is_dir(target)) {
              send_error(res, 400, "not_a_directory",
                         "not a directory: " + rel + " (a single file is /raw)");
              return;
            }
            const ArchiveResult archive = zip_directory(target, rel);
            if (!archive.ok) {
              send_error(res, 500, "archive_failed", archive.error);
              return;
            }
            const std::string ascii = ascii_archive_name(path_filename(ws.root()), rel);
            res.set_header("Content-Disposition",
                           "attachment; filename=\"" + ascii + "\"; filename*=UTF-8''" +
                               rfc5987_name(ascii));
            res.set_header("Cache-Control", "no-store");
            res.set_header("Access-Control-Allow-Origin", "*");
            // Lets a caller say what it got without unpacking: how much went in
            // and how much the limits left out.
            res.set_header("Access-Control-Expose-Headers",
                           "Content-Disposition, X-Archive-Entries, X-Archive-Skipped, "
                           "X-Archive-Uncompressed");
            res.set_header("X-Archive-Entries", std::to_string(archive.entries));
            res.set_header("X-Archive-Skipped", std::to_string(archive.skipped.size()));
            res.set_header("X-Archive-Uncompressed", std::to_string(archive.uncompressed));
            LOG_INFO("archive: ", ws.display_path(target), " -> ", ascii, " (", archive.bytes.size(),
                     " B, ", archive.entries, " entries, ", archive.skipped.size(), " skipped)");
            res.set_content(archive.bytes, "application/zip");
          });

  // ---- chat (SSE) --------------------------------------------------------
  svr.Post("/api/chat", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;

    if (!config->loaded) {
      send_error(res, 409, "config_missing",
                 "no provider.yaml is configured yet; upload it in the web UI");
      return;
    }
    const std::string message = json_string(body, "message");
    // A message may be image-only ("look at this"), so emptiness is checked
    // after the attachments are parsed below.
    nlohmann::json::const_iterator images_it = body.find("images");
    const nlohmann::json images_json =
        images_it != body.end() ? *images_it : nlohmann::json();
    // An empty "images": [] must not turn into an empty turn.
    if (trim(message).empty() && (!images_json.is_array() || images_json.empty())) {
      send_error(res, 400, "bad_request", "'message' is required");
      return;
    }
    const std::string workspace_id = json_string(body, "workspace_id");
    if (workspace_id.empty()) {
      send_error(res, 400, "bad_request", "'workspace_id' is required");
      return;
    }
    const std::string requested_session = json_string(body, "session_id");
    bool interrupt = false;
    if (body.contains("interrupt")) {
      if (!body["interrupt"].is_boolean()) {
        send_error(res, 400, "bad_request", "'interrupt' must be true or false");
        return;
      }
      interrupt = body["interrupt"].get<bool>();
    }

    // ---- same-session policy: queue by default, replace only on request -----
    //
    // Two windows on one topic are a feature, so a second message must not
    // silently throw away what the other window (or the other machine) is
    // watching. Default: the message joins the running turn's inbox and the
    // agent adopts it at the next iteration - that is exactly what
    // POST /api/chat/queue does, so this route answers with the same shape and
    // the UI shows the queue strip.
    //
    // Replacing the turn needs `{"interrupt": true}`, and even then the old
    // worker is waited for: it owns its own Session copy and writes it wholesale,
    // so starting the replacement before it finished used to wipe the new
    // question and answer off disk (reproducible with
    // .tea_agent_run/scripts/same_session_race.py). Waiting also means the
    // replacement loads the session the old turn actually left behind.
    if (!requested_session.empty()) {
      std::shared_ptr<ChatTurn> running = find_turn(requested_session);
      if (running && !running->finished()) {
        if (!interrupt) {
          if (images_json.is_array() && !images_json.empty()) {
            send_error(res, 409, "images_not_queued",
                       "this session's turn is still running, and a queued prompt cannot carry "
                       "images; wait for it to finish, or send {\"interrupt\": true} to replace it");
            return;
          }
          QueuedPrompt queued;
          queued.id = make_id("q_");
          queued.text = trim(message);
          queued.created_at = now_iso8601();
          if (!running->enqueue_prompt(queued)) {
            // The turn closed between the check and the push: fall through and
            // start a fresh turn instead of dropping the message.
            LOG_INFO("chat: session ", requested_session,
                     " finished just as the prompt was queued; starting a new turn");
          } else {
            LOG_INFO("chat: queued prompt ", queued.id, " for the running turn of session ",
                     requested_session, " (", running->inbox.size(), " pending)");
            nlohmann::json j = nlohmann::json::object();
            j["ok"] = true;
            j["queued"] = {{"id", queued.id},
                           {"content", queued.text},
                           {"created_at", queued.created_at}};
            j["pending"] = static_cast<int>(running->inbox.size());
            j["session_id"] = requested_session;
            j["workspace_id"] = running->workspace_id;
            // The client uses this to say "another window is answering; your
            // message waits its turn" instead of pretending it started a turn.
            j["turn_running"] = true;
            send_json(res, j);
            return;
          }
        } else {
          running->cancel.store(true);
          LOG_INFO("chat: interrupt requested for the running turn of session ",
                   requested_session, "; waiting up to ", kInterruptWaitMs, " ms for it to stop");
          const int64_t deadline = now_ms() + kInterruptWaitMs;
          while (!running->finished() && now_ms() < deadline) {
            std::this_thread::sleep_for(std::chrono::milliseconds(25));
          }
          if (!running->finished()) {
            send_error(res, 409, "turn_still_running",
                       "the running turn did not stop within " +
                           std::to_string(kInterruptWaitMs / 1000) +
                           " s (it may be inside a long tool call); it was asked to cancel - "
                           "retry once it has stopped");
            return;
          }
          // The worker is done, so dropping the entry joins immediately. Do it
          // outside `mu`: the destructor joins, and joining under the lock would
          // freeze every other request.
          std::vector<std::shared_ptr<ChatTurn>> reaped;
          {
            std::lock_guard<std::mutex> lock(mu);
            auto it = turns.find(requested_session);
            if (it != turns.end() && it->second == running) {
              reaped.push_back(it->second);
              turns.erase(it);
            }
          }
          reaped.clear();
        }
      }
    }

    {
      const int active = active_turn_count();
      if (active >= max_active_turns()) {
        send_error(res, 429, "too_many_turns",
                   "this server is already running " + std::to_string(active) +
                       " turns (limit " + std::to_string(max_active_turns()) +
                       "); wait for one to finish or stop it first");
        return;
      }
    }

    auto turn = std::make_shared<ChatTurn>();
    turn->started_at = now_iso8601();
    std::string err;
    if (!open_workspace(workspace_id, &turn->workspace, &err)) {
      send_error(res, 404, "not_found", err);
      return;
    }
    turn->workspace_id = turn->workspace.id();

    if (!requested_session.empty()) {
      // Unknown session ids simply start a fresh conversation.
      turn->workspace.load_session(requested_session, &turn->session, nullptr);
    }
    if (turn->session.info.id.empty()) {
      Session created;
      if (!turn->workspace.create_session("", &created, &err)) {
        send_error(res, 500, "session_error", err);
        return;
      }
      turn->session = created;
    }
    turn->session_id = turn->session.info.id;

    const std::string provider = json_string(body, "provider");
    const std::string model = json_string(body, "model");
    // The agent loop as a primitive: a caller that keeps its own policy may hand
    // in the persona it wants and the tools it is willing to let the model use.
    const std::string system_prompt = trim(json_string(body, "system_prompt"));
    std::vector<std::string> allowed_tools;
    auto tools_it = body.find("tools");
    if (tools_it != body.end() && tools_it->is_array()) {
      for (std::size_t i = 0; i < tools_it->size(); ++i) {
        if ((*tools_it)[i].is_string()) allowed_tools.push_back((*tools_it)[i].get<std::string>());
      }
    }
    const std::string task_id = trim(json_string(body, "task_id"));

    // ---- image attachments ------------------------------------------------
    std::vector<ImageAttachment> images;
    if (images_json.is_array() && !images_json.empty()) {
      if (images_json.size() > kMaxImagesPerMessage) {
        send_error(res, 400, "too_many_images",
                   "at most " + std::to_string(kMaxImagesPerMessage) + " images per message");
        return;
      }
      // Reject before doing any work if the target model cannot see images:
      // otherwise the provider answers with a confusing 400, or silently
      // ignores them and the user believes the model looked at the picture.
      const ProviderInfo* pinfo =
          config->provider(provider.empty() ? config->active_provider : provider);
      std::string model_name = model.empty() ? (pinfo ? pinfo->primary_model() : std::string())
                                            : model;
      const ModelInfo* minfo = pinfo ? pinfo->find_model(model_name) : nullptr;
      // Mirror the agent's fallback: an unknown model name resolves to the
      // provider's default, so a stale id in the UI must not look like "this
      // model cannot see images".
      if (pinfo && !minfo && !pinfo->models.empty()) {
        model_name = pinfo->primary_model();
        minfo = pinfo->find_model(model_name);
      }
      if (!pinfo || !minfo || !minfo->supports_vision) {
        send_error(res, 400, "vision_unsupported",
                   "model '" + (pinfo ? pinfo->name : std::string("?")) + " / " + model_name +
                       "' does not declare supports_vision; set it in provider.yaml or pick a "
                       "vision model before attaching images");
        return;
      }
      const std::string artifacts = turn->workspace.artifacts_dir();
      if (!make_dirs(artifacts)) {
        send_error(res, 500, "io_error", "cannot create " + artifacts);
        return;
      }
      for (std::size_t i = 0; i < images_json.size(); ++i) {
        const nlohmann::json& item = images_json[i];
        std::string raw;
        if (item.is_string()) raw = item.get<std::string>();
        else if (item.is_object()) raw = json_string(item, "data");
        if (raw.empty()) {
          send_error(res, 400, "bad_request", "image " + std::to_string(i + 1) + " has no data");
          return;
        }
        std::string bytes;
        std::string decode_err;
        if (!decode_image_payload(raw, &bytes, &decode_err)) {
          send_error(res, 400, "bad_image", "image " + std::to_string(i + 1) + ": " + decode_err);
          return;
        }
        if (bytes.size() > kMaxImageBytes) {
          send_error(res, 413, "image_too_large",
                     "image " + std::to_string(i + 1) + " is " + human_size(bytes.size()) +
                         "; the limit is " + human_size(kMaxImageBytes));
          return;
        }
        const char* mime = sniff_image_mime(bytes);
        if (!mime) {
          send_error(res, 400, "bad_image",
                     "image " + std::to_string(i + 1) +
                         " is not a PNG, JPEG, GIF or WebP (identified by content, not by name)");
          return;
        }
        const std::string file = path_join(artifacts, "img_" + random_hex(8) + image_extension_for(mime));
        std::string io_err;
        if (!write_file(file, bytes, false, &io_err)) {
          send_error(res, 500, "io_error", "cannot store the image: " + io_err);
          return;
        }
        ImageAttachment att;
        att.mime = mime;
        // Workspace-relative, so the session file stays portable and the /raw
        // route can serve it back to the UI.
        att.path = turn->workspace.display_path(file);
        images.push_back(att);
      }
      LOG_INFO("chat: ", images.size(), " image attachment(s) stored under ", artifacts);
    }

    std::vector<std::shared_ptr<ChatTurn>> graveyard;
    {
      std::lock_guard<std::mutex> lock(mu);
      prune_turns_locked(&graveyard);
      auto it = turns.find(turn->session_id);
      if (it != turns.end() && it->second) {
        // Should be unreachable: a turn still running on this id was either
        // queued into (default) or interrupted and waited for above. Keep the
        // guard anyway - replacing a live worker without the wait is exactly the
        // race that silently dropped messages.
        LOG_WARN("chat: replacing a still-running turn on session ", turn->session_id,
                 " without an explicit interrupt");
        it->second->cancel.store(true);
        retired.push_back(it->second);  // destroyed after the lock is released
        turns.erase(it);
      }
      turns[turn->session_id] = turn;
    }
    // Joining a finished worker is immediate; doing it here keeps `mu` free.
    graveyard.clear();

    AppConfig* cfg = config;
    ToolRegistry* reg = tools;
    turn->worker = std::thread([turn, cfg, reg, message, images, provider, model, system_prompt,
                             allowed_tools, task_id]() {
      Agent agent(cfg, reg);
      TurnOptions options;
      options.provider = provider;
      options.model = model;
      options.stream = true;
      options.inbox = &turn->inbox;
      options.system_prompt = system_prompt;
      options.tools = allowed_tools;
      options.task_id = task_id;
      EventEmitter emit = [turn](const nlohmann::json& event) { turn->emit_event(event); };
      try {
        agent.run_turn(turn->workspace, &turn->session, message, images, options, emit,
                       &turn->cancel);
      } catch (const std::exception& e) {
        nlohmann::json event = nlohmann::json::object();
        event["type"] = "error";
        event["message"] = std::string("agent crashed: ") + e.what();
        turn->emit_event(event);
      }
      // run_turn drains the queue when it finishes, but a prompt can still land
      // in the microseconds between that drain and the worker stopping. Refuse
      // new ones first, then the drain below is final. Anything left is handed
      // back to the browser, which restores it to the composer; leaking it here
      // would make a queued message vanish without a trace.
      turn->begin_shutdown();
      std::vector<QueuedPrompt> leftover = turn->inbox.drain();
      if (!leftover.empty()) {
        nlohmann::json items = nlohmann::json::array();
        for (std::size_t i = 0; i < leftover.size(); ++i) {
          nlohmann::json item = nlohmann::json::object();
          item["id"] = leftover[i].id;
          item["content"] = leftover[i].text;
          item["created_at"] = leftover[i].created_at;
          items.push_back(item);
        }
        nlohmann::json event = nlohmann::json::object();
        event["type"] = "queue_unconsumed";
        event["items"] = items;
        turn->emit_event(event);
      }
      turn->close();
    });

    res.set_header("Cache-Control", "no-cache");
    res.set_header("X-Accel-Buffering", "no");
    res.set_header("Access-Control-Allow-Origin", "*");
    // No resource releaser here on purpose: the response ending (tab closed,
    // laptop asleep, network dropped) must not cancel the work. Only
    // /api/chat/stop, an explicit interrupt, or shutting the server down does
    // that. Whoever comes back re-attaches below.
    auto sub = std::make_shared<ChatTurn::Subscriber>();
    auto slot = std::make_shared<SubscriberSlot>(turn);
    if (!turn->add_subscriber()) {
      // Cannot happen for a turn created a few lines above, but do not write an
      // SSE response without a slot if the cap ever moves.
      send_error(res, 429, "too_many_subscribers",
                 "too many windows are already following this turn (limit " +
                     std::to_string(max_subscribers_per_turn()) + ")");
      return;
    }
    res.set_chunked_content_provider(
        "text/event-stream; charset=utf-8",
        [turn, sub, slot](std::size_t, httplib::DataSink& sink) {
          return turn->pump(sink, sub);
        });
  });

  // ---- re-attaching to a turn that is still running -----------------------
  // "Is this session's turn still running, and from which sequence number can I
  // replay it?" The UI asks when it opens a session - a reload, a second tab, a
  // laptop waking up - so a turn that outlived its connection is picked up
  // instead of being shown as a conversation that stopped mid-sentence.
  svr.Get("/api/chat/turn", [this](const httplib::Request& req, httplib::Response& res) {
    const std::string session_id =
        req.has_param("session_id") ? req.get_param_value("session_id") : std::string();
    if (session_id.empty()) {
      send_error(res, 400, "bad_request", "'session_id' is required");
      return;
    }
    std::shared_ptr<ChatTurn> turn;
    std::vector<std::shared_ptr<ChatTurn>> graveyard;
    {
      std::lock_guard<std::mutex> lock(mu);
      // Reap turns whose worker already finished. With no client left to notice
      // them they would otherwise hold their replay log until the next /api/chat.
      prune_turns_locked(&graveyard);
      auto it = turns.find(session_id);
      if (it != turns.end()) turn = it->second;
    }
    graveyard.clear();  // ~ChatTurn joins the worker: never under `mu`
    nlohmann::json j = turn ? turn->status_json() : nlohmann::json::object();
    if (turn) {
      j["pending"] = static_cast<long long>(turn->inbox.size());
    } else {
      j["running"] = false;
      j["session_id"] = session_id;
    }
    j["ok"] = true;
    send_json(res, j);
  });

  // Every turn that is still running, newest first. A browser that opens cold -
  // another machine, another profile, no localStorage - has no idea what it was
  // looking at, so this is how it finds work in progress instead of landing on
  // the first workspace in the index and showing an empty chat.
  svr.Get("/api/chat/turns", [this](const httplib::Request&, httplib::Response& res) {
    std::vector<std::shared_ptr<ChatTurn>> live;
    std::vector<std::shared_ptr<ChatTurn>> graveyard;
    {
      std::lock_guard<std::mutex> lock(mu);
      prune_turns_locked(&graveyard);
      for (auto it = turns.begin(); it != turns.end(); ++it) {
        if (it->second) live.push_back(it->second);
      }
    }
    graveyard.clear();  // ~ChatTurn joins the worker: never under `mu`
    nlohmann::json list = nlohmann::json::array();
    for (std::size_t i = 0; i < live.size(); ++i) {
      nlohmann::json t = live[i]->status_json();
      t["pending"] = static_cast<long long>(live[i]->inbox.size());
      list.push_back(t);
    }
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["turns"] = list;
    send_json(res, j);
  });

  // Re-attach: replay everything after `since` (0 or omitted = the whole buffer),
  // then follow live. This is what a reopened browser calls.
  svr.Post("/api/chat/attach", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string session_id = json_string(body, "session_id");
    if (session_id.empty()) {
      send_error(res, 400, "bad_request", "'session_id' is required");
      return;
    }
    std::shared_ptr<ChatTurn> turn;
    {
      std::lock_guard<std::mutex> lock(mu);
      auto it = turns.find(session_id);
      if (it != turns.end()) turn = it->second;
    }
    if (!turn) {
      send_error(res, 404, "no_active_turn", "this session has no turn to attach to");
      return;
    }
    if (turn->finished()) {
      // It ended while this request was in flight; the persisted session is the
      // final state, exactly like the 404 below.
      send_error(res, 404, "no_active_turn", "this session's turn has already finished");
      return;
    }
    if (!turn->add_subscriber()) {
      send_error(res, 429, "too_many_subscribers",
                 "already " + std::to_string(max_subscribers_per_turn()) +
                     " windows are following this turn; close one of them and reload");
      return;
    }
    auto sub = std::make_shared<ChatTurn::Subscriber>();
    auto slot = std::make_shared<SubscriberSlot>(turn);
    // `since` is the last seq the client saw. A seq older than the buffer (or no
    // seq at all) replays from the oldest frame still held; when frames were
    // dropped, pump() sends a `resync` event first so the client reloads the
    // persisted session instead of showing a hole.
    nlohmann::json::const_iterator since_it = body.find("since");
    if (since_it != body.end() && since_it->is_number_integer()) {
      const long long since = since_it->get<long long>();
      if (since > 0) sub->cursor = since + 1;
    }
    LOG_INFO("chat: re-attaching to the running turn of session ", session_id, " (replay from seq ",
             sub->cursor, ", ", turn->subscriber_count(), " subscriber(s))");
    res.set_header("Cache-Control", "no-cache");
    res.set_header("X-Accel-Buffering", "no");
    res.set_header("Access-Control-Allow-Origin", "*");
    res.set_chunked_content_provider(
        "text/event-stream; charset=utf-8",
        [turn, sub, slot](std::size_t, httplib::DataSink& sink) {
          return turn->pump(sink, sub);
        });
  });

  svr.Post("/api/chat/stop", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string session_id = json_string(body, "session_id");
    bool found = false;
    {
      std::lock_guard<std::mutex> lock(mu);
      auto it = turns.find(session_id);
      if (it != turns.end() && it->second) {
        it->second->cancel.store(true);
        found = true;
      }
    }
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["cancelled"] = found;
    send_json(res, j);
  });

  // ---- queued prompts ----------------------------------------------------
  // A prompt typed while a turn is already running does not start a second turn:
  // it is appended to the running conversation at the next agent iteration. The
  // queue belongs to that turn, so stopping or replacing the turn throws the
  // remaining prompts back to the caller (`queue_unconsumed` on the SSE stream)
  // instead of firing them later in an unrelated turn.
  svr.Post("/api/chat/queue", [this](const httplib::Request& req, httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string session_id = json_string(body, "session_id");
    const std::string message = json_string(body, "message");
    if (session_id.empty()) {
      send_error(res, 400, "bad_request", "'session_id' is required");
      return;
    }
    if (trim(message).empty()) {
      send_error(res, 400, "bad_request", "'message' is required");
      return;
    }
    // Queued prompts are text-only. An image must be decoded, size-checked and
    // written into the workspace before it can be referenced, which /api/chat
    // does for the request that starts a turn; silently dropping them here would
    // be worse than refusing.
    auto images_it = body.find("images");
    if (images_it != body.end() && images_it->is_array() && !images_it->empty()) {
      send_error(res, 400, "images_not_queued",
                 "a queued prompt cannot carry images; wait for the running turn to finish");
      return;
    }

    // No pruning here: this endpoint must stay fast, and destroying a retired
    // turn joins its worker (which can be stuck in a long tool call). A finished
    // turn refuses `enqueue_prompt` on its own, and the dead entry is reaped by
    // the next /api/chat for that session.
    std::shared_ptr<ChatTurn> turn;
    {
      std::lock_guard<std::mutex> lock(mu);
      auto it = turns.find(session_id);
      if (it != turns.end()) turn = it->second;
    }

    if (!turn) {
      send_error(res, 409, "no_active_turn",
                 "this session has no running turn; send the message directly");
      return;
    }

    QueuedPrompt prompt;
    prompt.id = make_id("q_");
    prompt.text = trim(message);
    prompt.created_at = now_iso8601();
    if (!turn->enqueue_prompt(prompt)) {
      send_error(res, 409, "no_active_turn",
                 "the running turn just finished; send the message directly");
      return;
    }

    LOG_INFO("chat: queued prompt ", prompt.id, " for session ", session_id, " (",
             turn->inbox.size(), " pending)");
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["queued"] = {{"id", prompt.id}, {"content", prompt.text}, {"created_at", prompt.created_at}};
    j["pending"] = static_cast<int>(turn->inbox.size());
    send_json(res, j);
  });

  svr.Post("/api/chat/queue/cancel", [this](const httplib::Request& req,
                                            httplib::Response& res) {
    nlohmann::json body;
    if (!parse_json_body(req, &body, res)) return;
    const std::string session_id = json_string(body, "session_id");
    const std::string id = json_string(body, "id");
    if (session_id.empty()) {
      send_error(res, 400, "bad_request", "'session_id' is required");
      return;
    }
    std::shared_ptr<ChatTurn> turn;
    {
      std::lock_guard<std::mutex> lock(mu);
      auto it = turns.find(session_id);
      if (it != turns.end()) turn = it->second;
    }
    int cancelled = 0;
    int pending = 0;
    if (turn) {
      if (id.empty()) {
        cancelled = static_cast<int>(turn->inbox.drain().size());
      } else {
        cancelled = turn->inbox.remove(id) ? 1 : 0;
      }
      pending = static_cast<int>(turn->inbox.size());
    }
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["cancelled"] = cancelled;
    j["pending"] = pending;
    send_json(res, j);
  });

  svr.Get("/api/chat/queue", [this](const httplib::Request& req, httplib::Response& res) {
    const std::string session_id =
        req.has_param("session_id") ? req.get_param_value("session_id") : std::string();
    if (session_id.empty()) {
      send_error(res, 400, "bad_request", "'session_id' is required");
      return;
    }
    std::shared_ptr<ChatTurn> turn;
    {
      std::lock_guard<std::mutex> lock(mu);
      auto it = turns.find(session_id);
      if (it != turns.end()) turn = it->second;
    }
    std::vector<QueuedPrompt> items;
    if (turn) items = turn->inbox.snapshot();
    nlohmann::json arr = nlohmann::json::array();
    for (std::size_t i = 0; i < items.size(); ++i) {
      arr.push_back({{"id", items[i].id},
                     {"content", items[i].text},
                     {"created_at", items[i].created_at}});
    }
    nlohmann::json j = nlohmann::json::object();
    j["ok"] = true;
    j["pending"] = static_cast<int>(items.size());
    j["items"] = arr;
    send_json(res, j);
  });

  // ---- fallback for unknown API routes ----------------------------------
  svr.Get(R"(/api/.*)", [](const httplib::Request& req, httplib::Response& res) {
    send_error(res, 404, "not_found", "no such endpoint: " + req.path);
  });
  svr.Post(R"(/api/.*)", [](const httplib::Request& req, httplib::Response& res) {
    send_error(res, 404, "not_found", "no such endpoint: " + req.path);
  });
  svr.Put(R"(/api/.*)", [](const httplib::Request& req, httplib::Response& res) {
    send_error(res, 404, "not_found", "no such endpoint: " + req.path);
  });
  svr.Delete(R"(/api/.*)", [](const httplib::Request& req, httplib::Response& res) {
    send_error(res, 404, "not_found", "no such endpoint: " + req.path);
  });

  svr.set_exception_handler([](const httplib::Request&, httplib::Response& res,
                               std::exception_ptr ep) {
    std::string what = "unknown error";
    try {
      if (ep) std::rethrow_exception(ep);
    } catch (const std::exception& e) {
      what = e.what();
    } catch (...) {
    }
    LOG_ERROR("server: unhandled exception: ", what);
    send_error(res, 500, "internal_error", what);
  });
}

// ---------------------------------------------------------------------------
// ApiServer
// ---------------------------------------------------------------------------
ApiServer::ApiServer(AppConfig* config, ToolRegistry* tools, WorkspaceRegistry* registry)
    : impl_(new Impl()) {
  impl_->config = config;
  impl_->tools = tools;
  impl_->registry = registry;
}

ApiServer::~ApiServer() { stop(); }

bool ApiServer::start(const ServerOptions& options, std::string* err) {
  options_ = options;
  impl_->web_dir = options.web_dir;
  impl_->started_ms = now_ms();
  // Route registration builds std::regex objects; on an old libstdc++ (GCC <= 6)
  // one of those used to throw and take the whole process down with no
  // diagnostic. Report it as a start error instead.
  try {
    impl_->install_routes();
  } catch (const std::exception& e) {
    if (err) *err = std::string("installing the routes threw: ") + e.what();
    return false;
  }

  // Tasks that were in flight when the agent died are not running any more.
  // Recover them before anything else: a task stuck in "running" is invisible
  // to the scheduler and can never be started again.
  try {
    impl_->recover_stale_tasks();
  } catch (const std::exception& e) {
    LOG_WARN("recovering stale tasks failed: ", e.what());
  }

  const std::string host = options.host.empty() ? "127.0.0.1" : options.host;
  impl_->listen_thread = std::thread([this, host]() {
    // An exception escaping this thread used to call std::terminate straight
    // away ("terminate called without an active exception"), leaving nothing
    // but a core dump to debug with. Turn it into a reported start failure.
    try {
      impl_->listen_ok = impl_->server.listen(host, options_.port);
    } catch (const std::exception& e) {
      LOG_ERROR("server: listen threw: ", e.what());
      impl_->listen_ok = false;
    } catch (...) {
      LOG_ERROR("server: listen threw an unknown exception");
      impl_->listen_ok = false;
    }
    impl_->listen_returned.store(true);
  });

  // Wait for the listener to come up (or to fail fast).
  for (int i = 0; i < 500; ++i) {
    if (impl_->server.is_running()) break;
    if (impl_->listen_returned.load()) break;
    std::this_thread::sleep_for(std::chrono::milliseconds(10));
  }
  if (!impl_->server.is_running()) {
    if (impl_->listen_thread.joinable()) impl_->listen_thread.join();
    if (err) {
      *err = "cannot listen on " + host + ":" + std::to_string(options_.port) +
             " (address already in use?)";
    }
    return false;
  }
  LOG_INFO("server: listening on http://", host, ":", options_.port);
  return true;
}

void ApiServer::stop() {
  if (!impl_) return;
  {
    std::lock_guard<std::mutex> lock(impl_->mu);
    for (auto it = impl_->turns.begin(); it != impl_->turns.end(); ++it) {
      if (it->second) it->second->cancel.store(true);
    }
  }
  impl_->server.stop();
  if (impl_->listen_thread.joinable()) impl_->listen_thread.join();
}

bool ApiServer::is_running() const { return impl_ && impl_->server.is_running(); }

int ApiServer::port() const { return options_.port; }

std::string ApiServer::url() const {
  return "http://127.0.0.1:" + std::to_string(options_.port);
}

std::vector<std::string> ApiServer::urls() const {
  return reachable_urls(options_.host, options_.port);
}

}  // namespace tea
