#include "message.h"

#include <algorithm>

#include "util.h"

namespace tea {

nlohmann::json Message::to_wire(bool include_reasoning) const {
  nlohmann::json j = nlohmann::json::object();
  j["role"] = role;

  if (role == "tool") {
    j["tool_call_id"] = tool_call_id;
    j["content"] = content;
    if (!name.empty()) j["name"] = name;
    return j;
  }

  if (role == "assistant") {
    // Some providers reject a missing "content" key even when tool calls are
    // present, so keep it (possibly empty).
    j["content"] = content;
    // "Thinking mode" gateways insist on receiving the previous assistant
    // reasoning back verbatim; api.deepseek.com rejects it instead. The callers
    // decide via include_reasoning (see resolve_reasoning_echo in agent.cpp).
    if (include_reasoning && !reasoning.empty()) {
      j["reasoning_content"] = reasoning;
    }
    if (tool_calls.empty()) return j;
    nlohmann::json arr = nlohmann::json::array();
    for (std::size_t i = 0; i < tool_calls.size(); ++i) {
      const ToolCall& tc = tool_calls[i];
      nlohmann::json call = nlohmann::json::object();
      call["id"] = tc.id;
      call["type"] = "function";
      nlohmann::json fn = nlohmann::json::object();
      fn["name"] = tc.name;
      fn["arguments"] = tc.raw_arguments.empty() ? tc.arguments.dump() : tc.raw_arguments;
      call["function"] = fn;
      arr.push_back(call);
    }
    j["tool_calls"] = arr;
    return j;
  }

  j["content"] = content;
  return j;
}

nlohmann::json Message::to_store() const {
  nlohmann::json j = nlohmann::json::object();
  j["role"] = role;
  j["content"] = content;
  if (!reasoning.empty()) j["reasoning"] = reasoning;
  if (!tool_call_id.empty()) j["tool_call_id"] = tool_call_id;
  if (!name.empty()) j["name"] = name;
  if (!created_at.empty()) j["created_at"] = created_at;
  if (role == "tool") {
    j["ok"] = ok;
    j["elapsed_ms"] = elapsed_ms;
    if (truncated) j["truncated"] = true;
  }
  if (!tool_calls.empty()) {
    nlohmann::json arr = nlohmann::json::array();
    for (std::size_t i = 0; i < tool_calls.size(); ++i) {
      const ToolCall& tc = tool_calls[i];
      nlohmann::json call = nlohmann::json::object();
      call["id"] = tc.id;
      call["name"] = tc.name;
      call["arguments"] = tc.arguments;
      if (!tc.raw_arguments.empty()) call["raw_arguments"] = tc.raw_arguments;
      arr.push_back(call);
    }
    j["tool_calls"] = arr;
  }
  return j;
}

Message Message::from_store(const nlohmann::json& j) {
  Message m;
  if (!j.is_object()) return m;
  auto get_str = [&](const char* key) -> std::string {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return std::string();
    return it->get<std::string>();
  };
  m.role = get_str("role");
  m.content = get_str("content");
  m.reasoning = get_str("reasoning");
  m.tool_call_id = get_str("tool_call_id");
  m.name = get_str("name");
  m.created_at = get_str("created_at");
  auto ok_it = j.find("ok");
  if (ok_it != j.end() && ok_it->is_boolean()) m.ok = ok_it->get<bool>();
  auto el_it = j.find("elapsed_ms");
  if (el_it != j.end() && el_it->is_number()) m.elapsed_ms = el_it->get<long long>();
  auto tr_it = j.find("truncated");
  if (tr_it != j.end() && tr_it->is_boolean()) m.truncated = tr_it->get<bool>();

  auto calls = j.find("tool_calls");
  if (calls != j.end() && calls->is_array()) {
    for (auto it = calls->begin(); it != calls->end(); ++it) {
      if (!it->is_object()) continue;
      ToolCall tc;
      auto id_it = it->find("id");
      if (id_it != it->end() && id_it->is_string()) tc.id = id_it->get<std::string>();
      auto name_it = it->find("name");
      if (name_it != it->end() && name_it->is_string()) tc.name = name_it->get<std::string>();
      auto args_it = it->find("arguments");
      if (args_it != it->end()) {
        if (args_it->is_string()) {
          tc.raw_arguments = args_it->get<std::string>();
          try {
            tc.arguments = nlohmann::json::parse(tc.raw_arguments);
          } catch (const std::exception&) {
            tc.arguments = nlohmann::json::object();
          }
        } else {
          tc.arguments = *args_it;
          tc.raw_arguments = args_it->dump();
        }
      }
      auto raw_it = it->find("raw_arguments");
      if (raw_it != it->end() && raw_it->is_string()) tc.raw_arguments = raw_it->get<std::string>();
      m.tool_calls.push_back(tc);
    }
  }
  return m;
}

Message Message::make(const std::string& role, const std::string& content) {
  Message m;
  m.role = role;
  m.content = content;
  m.created_at = now_iso8601();
  return m;
}

nlohmann::json SessionInfo::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["id"] = id;
  j["title"] = title;
  j["created_at"] = created_at;
  j["updated_at"] = updated_at;
  j["message_count"] = message_count;
  return j;
}

SessionInfo SessionInfo::from_json(const nlohmann::json& j) {
  SessionInfo s;
  if (!j.is_object()) return s;
  auto str_of = [&](const char* key) -> std::string {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return std::string();
    return it->get<std::string>();
  };
  s.id = str_of("id");
  s.title = str_of("title");
  s.created_at = str_of("created_at");
  s.updated_at = str_of("updated_at");
  auto it = j.find("message_count");
  if (it != j.end() && it->is_number()) s.message_count = it->get<int>();
  return s;
}

}  // namespace tea
