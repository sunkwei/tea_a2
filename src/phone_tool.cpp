// phone_tool.cpp -- the phone bridge tool (Android builds only).
//
// The agent runs inside the APK (liba2.so). The Android app runs a small
// HTTP server on the loopback interface - the "phone bridge" - and this tool
// asks it for the things only the phone can do:
//
//   GET  /phone/device          model, Android version, battery, storage
//   GET  /phone/notifications   the notifications that came in
//   GET  /phone/location        where the phone is
//   GET  /phone/clipboard       what is on the clipboard
//   POST /phone/clipboard       put something on the clipboard
//   POST /phone/open            open a URL, an app, or a search
//   GET  /phone/screenshot      a PNG of the screen (needs the user's consent)
//   POST /phone/sms             send a text message
//   POST /phone/call            dial a number
//   GET  /phone/apps            the installed apps
//
// The bridge address comes from TEA_PHONE_BRIDGE ("127.0.0.1:8384" by
// default) so a desktop build can point at a bridge that runs somewhere else.
#include "phone_tool.h"

#include <cstdlib>
#include <string>

#include "http.h"
#include "log.h"
#include "util.h"

namespace tea {

namespace {

std::string bridge_base() {
  const char* env = std::getenv("TEA_PHONE_BRIDGE");
  std::string host = (env && *env) ? env : "127.0.0.1:8384";
  // A full URL is accepted as well ("http://127.0.0.1:8384").
  if (starts_with(host, "http://")) return host.substr(7);
  if (starts_with(host, "https://")) return host.substr(8);
  return host;
}

std::string arg_string(const nlohmann::json& args, const char* key) {
  if (!args.is_object()) return std::string();
  const auto it = args.find(key);
  if (it == args.end() || !it->is_string()) return std::string();
  return trim(it->get<std::string>());
}

ToolResult fail(const std::string& message) {
  ToolResult result;
  result.ok = false;
  result.output = "error: " + message;
  return result;
}

/// Call the bridge and hand the answer back as text. The bridge answers with
/// JSON; it is returned as it came, because the phone's own words ("no
/// notification access granted") are more useful than a re-worded error.
ToolResult call_bridge(const std::string& method, const std::string& path,
                       const std::string& body, int timeout_ms) {
  HttpRequest request;
  request.method = method;
  request.url = "http://" + bridge_base() + path;
  request.body = body;
  request.content_type = "application/json";
  request.connect_timeout_ms = 3000;
  request.read_timeout_ms = timeout_ms;

  const HttpResponse response = http_perform(request);
  if (!response.ok) {
    ToolResult result;
    result.ok = false;
    result.output = "error: the phone bridge at " + bridge_base() +
                    " did not answer (" + (response.error.empty() ? "no reply" : response.error) +
                    "). The Android app has to be running with the bridge enabled.";
    return result;
  }
  ToolResult result;
  result.ok = response.status >= 200 && response.status < 300;
  result.output = response.body;
  if (!result.ok) {
    result.output = "error: the phone bridge answered HTTP " +
                    std::to_string(response.status) + ": " + response.body;
  }
  return result;
}

std::string json_body(const nlohmann::json& payload) { return payload.dump(); }

}  // namespace

ToolResult tool_phone(const nlohmann::json& args, const ToolContext& ctx) {
  (void)ctx;
  const std::string action = arg_string(args, "action");
  if (action.empty()) return fail("'action' is required");

  if (action == "device") {
    return call_bridge("GET", "/phone/device", std::string(), 10000);
  }
  if (action == "notifications") {
    const long long limit = args.is_object() && args.contains("limit") &&
                                    args["limit"].is_number()
                                ? args["limit"].get<long long>()
                                : 20;
    return call_bridge("GET", "/phone/notifications?limit=" + std::to_string(limit),
                       std::string(), 10000);
  }
  if (action == "location") {
    // The fix can take a moment: the phone asks the GPS and waits.
    return call_bridge("GET", "/phone/location", std::string(), 30000);
  }
  if (action == "clipboard_get") {
    return call_bridge("GET", "/phone/clipboard", std::string(), 10000);
  }
  if (action == "clipboard_set") {
    const std::string text = arg_string(args, "text");
    if (text.empty()) return fail("'text' is required for clipboard_set");
    nlohmann::json payload = nlohmann::json::object();
    payload["text"] = text;
    return call_bridge("POST", "/phone/clipboard", json_body(payload), 10000);
  }
  if (action == "open") {
    nlohmann::json payload = nlohmann::json::object();
    const std::string url = arg_string(args, "url");
    const std::string package = arg_string(args, "package");
    const std::string query = arg_string(args, "query");
    if (!url.empty()) payload["url"] = url;
    if (!package.empty()) payload["package"] = package;
    if (!query.empty()) payload["query"] = query;
    if (payload.empty()) {
      return fail("one of 'url', 'package' or 'query' is required for open");
    }
    return call_bridge("POST", "/phone/open", json_body(payload), 10000);
  }
  if (action == "screenshot") {
    // The answer carries a PNG as base64; it is long, so it gets a bigger
    // deadline than the other calls.
    return call_bridge("GET", "/phone/screenshot", std::string(), 60000);
  }
  if (action == "sms") {
    const std::string to = arg_string(args, "to");
    const std::string text = arg_string(args, "text");
    if (to.empty() || text.empty()) return fail("'to' and 'text' are required for sms");
    nlohmann::json payload = nlohmann::json::object();
    payload["to"] = to;
    payload["text"] = text;
    return call_bridge("POST", "/phone/sms", json_body(payload), 20000);
  }
  if (action == "call") {
    const std::string number = arg_string(args, "number");
    if (number.empty()) return fail("'number' is required for call");
    nlohmann::json payload = nlohmann::json::object();
    payload["number"] = number;
    return call_bridge("POST", "/phone/call", json_body(payload), 10000);
  }
  if (action == "screen") {
    // What is on the screen, as text: the accessibility tree, or the UI dump.
    return call_bridge("GET", "/phone/screen", std::string(), 30000);
  }
  if (action == "click") {
    const std::string text = arg_string(args, "text");
    if (text.empty()) return fail("'text' is required for click");
    nlohmann::json payload = nlohmann::json::object();
    payload["text"] = text;
    return call_bridge("POST", "/phone/click", json_body(payload), 30000);
  }
  if (action == "global") {
    const std::string what = arg_string(args, "global_action");
    if (what.empty()) return fail("'global_action' is required for global");
    nlohmann::json payload = nlohmann::json::object();
    payload["action"] = what;
    return call_bridge("POST", "/phone/global", json_body(payload), 20000);
  }
  if (action == "shell") {
    const std::string command = arg_string(args, "command");
    if (command.empty()) return fail("'command' is required for shell");
    nlohmann::json payload = nlohmann::json::object();
    payload["command"] = command;
    return call_bridge("POST", "/phone/shell", json_body(payload), 30000);
  }
  if (action == "tap" || action == "swipe" || action == "type" || action == "paste") {
    nlohmann::json payload = nlohmann::json::object();
    const std::string text = arg_string(args, "text");
    if (!text.empty()) payload["text"] = text;
    for (const char* key : {"x", "y", "x1", "y1", "x2", "y2", "ms"}) {
      if (!args.is_object()) break;
      const auto it = args.find(key);
      if (it != args.end() && it->is_number()) payload[key] = it->get<int>();
    }
    return call_bridge("POST", "/phone/" + action, json_body(payload), 20000);
  }
  if (action == "apps") {
    const std::string filter = arg_string(args, "filter");
    return call_bridge("GET", "/phone/apps" + (filter.empty() ? "" : "?filter=" + filter),
                       std::string(), 15000);
  }
  return fail("unknown action '" + action + "'");
}

}  // namespace tea
