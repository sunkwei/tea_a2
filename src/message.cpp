#include "message.h"

#include <algorithm>
#include <map>
#include <set>

#include "util.h"

namespace tea {

nlohmann::json Message::to_wire(bool include_reasoning,
                               const std::map<std::string, std::string>* image_urls) const {
  nlohmann::json j = nlohmann::json::object();
  j["role"] = role;

  // Multimodal user turn: the OpenAI vision shape. Only attachments that
  // resolved to a data: URL are sent, so a deleted file degrades to a text-only
  // turn instead of a request the provider rejects.
  if (role == "user" && !images.empty() && image_urls != nullptr) {
    nlohmann::json parts = nlohmann::json::array();
    if (!content.empty()) {
      nlohmann::json text_part = nlohmann::json::object();
      text_part["type"] = "text";
      text_part["text"] = content;
      parts.push_back(text_part);
    }
    for (std::size_t i = 0; i < images.size(); ++i) {
      std::map<std::string, std::string>::const_iterator it = image_urls->find(images[i].path);
      if (it == image_urls->end() || it->second.empty()) continue;
      nlohmann::json part = nlohmann::json::object();
      part["type"] = "image_url";
      part["image_url"] = { {"url", it->second} };
      parts.push_back(part);
    }
    if (!parts.empty()) {
      j["content"] = parts;
      return j;
    }
  }

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
      fn["arguments"] = tc.raw_arguments.empty() ? dump_json(tc.arguments) : tc.raw_arguments;
      call["function"] = fn;
      arr.push_back(call);
    }
    j["tool_calls"] = arr;
    return j;
  }

  j["content"] = content;
  return j;
}

// ---------------------------------------------------------------------------
// The tool-call structure of a request
// ---------------------------------------------------------------------------
namespace {

/// The string field `key` of `j`, or an empty string.
std::string field(const nlohmann::json& j, const char* key) {
  auto it = j.find(key);
  if (it == j.end() || !it->is_string()) return std::string();
  return it->get<std::string>();
}

}  // namespace

bool ToolCallRepair::changed() const {
  return orphan_results || duplicate_results || duplicate_calls || unanswered_calls ||
         nameless_calls || idless_calls;
}

std::string ToolCallRepair::summary() const {
  std::string out;
  struct Item {
    const char* label;
    std::size_t count;
  };
  const Item items[] = {
      {"orphan tool results", orphan_results}, {"duplicate tool results", duplicate_results},
      {"duplicate tool calls", duplicate_calls}, {"unanswered tool calls", unanswered_calls},
      {"tool calls without a name", nameless_calls}, {"tool calls without an id", idless_calls},
  };
  for (std::size_t i = 0; i < sizeof(items) / sizeof(items[0]); ++i) {
    if (!items[i].count) continue;
    if (!out.empty()) out += ", ";
    out += std::to_string(items[i].count) + " " + items[i].label;
  }
  return out;
}

ToolCallRepair repair_tool_calls(nlohmann::json* messages) {
  ToolCallRepair report;
  if (!messages || !messages->is_array()) return report;
  const std::size_t count = messages->size();
  if (count == 0) return report;

  // Per message: which of its tool calls have to go, and whether the message
  // itself (a tool result) has to go.
  std::vector<std::vector<bool> > drop_call(count);
  std::vector<bool> drop_message(count, false);
  // The calls that are still waiting for an answer, and the ones that got one.
  std::map<std::string, std::size_t> declared;  // id -> message index
  std::set<std::string> answered;

  for (std::size_t i = 0; i < count; ++i) {
    nlohmann::json& m = (*messages)[i];
    if (!m.is_object()) continue;
    const std::string role = field(m, "role");
    if (role == "assistant") {
      auto calls = m.find("tool_calls");
      if (calls == m.end() || !calls->is_array()) continue;
      drop_call[i].assign(calls->size(), false);
      for (std::size_t k = 0; k < calls->size(); ++k) {
        nlohmann::json& call = (*calls)[k];
        if (!call.is_object()) {
          // Not a call at all - nothing can answer it.
          drop_call[i][k] = true;
          ++report.idless_calls;
          continue;
        }
        const std::string id = field(call, "id");
        std::string name;
        auto fn = call.find("function");
        if (fn != call.end() && fn->is_object()) name = field(*fn, "name");
        if (id.empty()) {
          // Nothing can answer a call without an id.
          drop_call[i][k] = true;
          ++report.idless_calls;
          continue;
        }
        if (name.empty()) {
          // A call without a function name is not a call at all; providers
          // reject the whole request for it ("invalid params, 400 (2013)" on
          // MiniMax).
          drop_call[i][k] = true;
          ++report.nameless_calls;
          continue;
        }
        if (declared.find(id) != declared.end()) {
          // The same id twice: only the first declaration can be answered.
          drop_call[i][k] = true;
          ++report.duplicate_calls;
          continue;
        }
        declared[id] = i;
      }
      continue;
    }
    if (role == "tool") {
      const std::string id = field(m, "tool_call_id");
      if (id.empty() || declared.find(id) == declared.end()) {
        // No waiting call with that id: the result answers nothing.
        drop_message[i] = true;
        ++report.orphan_results;
        continue;
      }
      if (answered.find(id) != answered.end()) {
        // A second answer to the same call. MiniMax rejects the whole request
        // for this ("invalid params, 400 (2013)").
        drop_message[i] = true;
        ++report.duplicate_results;
        continue;
      }
      answered.insert(id);
    }
  }

  // A call that never got an answer would make the provider reject the
  // request ("tool call and result not match" on MiniMax). It happens when a
  // turn was interrupted between the call and its result.
  for (std::size_t i = 0; i < count; ++i) {
    nlohmann::json& m = (*messages)[i];
    if (!m.is_object()) continue;
    if (field(m, "role") != "assistant") continue;
    auto calls = m.find("tool_calls");
    if (calls == m.end() || !calls->is_array()) continue;
    for (std::size_t k = 0; k < calls->size(); ++k) {
      if (k < drop_call[i].size() && drop_call[i][k]) continue;
      nlohmann::json& call = (*calls)[k];
      if (!call.is_object()) continue;
      const std::string id = field(call, "id");
      if (id.empty() || answered.find(id) == answered.end()) {
        drop_call[i][k] = true;
        ++report.unanswered_calls;
      }
    }
  }

  // Rebuild: drop the broken calls, then the broken messages.
  for (std::size_t i = 0; i < count; ++i) {
    nlohmann::json& m = (*messages)[i];
    if (!m.is_object()) continue;
    auto calls = m.find("tool_calls");
    if (calls == m.end() || !calls->is_array()) continue;
    if (i >= drop_call.size() || drop_call[i].empty()) continue;
    nlohmann::json kept = nlohmann::json::array();
    for (std::size_t k = 0; k < calls->size(); ++k) {
      if (k < drop_call[i].size() && drop_call[i][k]) continue;
      kept.push_back((*calls)[k]);
    }
    if (kept.empty()) m.erase("tool_calls");
    else *calls = kept;
  }
  nlohmann::json kept_messages = nlohmann::json::array();
  for (std::size_t i = 0; i < count; ++i) {
    if (i < drop_message.size() && drop_message[i]) continue;
    kept_messages.push_back((*messages)[i]);
  }
  *messages = kept_messages;
  return report;
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
  if (!images.empty()) {
    nlohmann::json arr = nlohmann::json::array();
    for (std::size_t i = 0; i < images.size(); ++i) {
      nlohmann::json img = nlohmann::json::object();
      img["path"] = images[i].path;
      img["mime"] = images[i].mime;
      arr.push_back(img);
    }
    j["images"] = arr;
  }
  return j;
}

Message Message::from_store(const nlohmann::json& j) {
  Message m;
  if (!j.is_object()) return m;
  auto get_str = [&](const char* key) -> std::string {
    auto it = j.find(key);
    if (it == j.end() || !it->is_string()) return std::string();
    // A session written by an older version can hold bytes that are not valid
    // UTF-8 (the JSON parser accepts them). Clean them on the way in, so the
    // message is well-formed everywhere it goes.
    return sanitize_utf8(it->get<std::string>());
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

  auto imgs = j.find("images");
  if (imgs != j.end() && imgs->is_array()) {
    for (auto it = imgs->begin(); it != imgs->end(); ++it) {
      if (!it->is_object()) continue;
      ImageAttachment img;
      auto pit = it->find("path");
      if (pit != it->end() && pit->is_string()) img.path = pit->get<std::string>();
      auto mit = it->find("mime");
      if (mit != it->end() && mit->is_string()) img.mime = mit->get<std::string>();
      if (!img.path.empty()) m.images.push_back(img);
    }
  }

  auto calls = j.find("tool_calls");
  if (calls != j.end() && calls->is_array()) {
    for (auto it = calls->begin(); it != calls->end(); ++it) {
      if (!it->is_object()) continue;
      ToolCall tc;
      auto id_it = it->find("id");
      if (id_it != it->end() && id_it->is_string()) tc.id = id_it->get<std::string>();
      // Session files store the flat shape {id, name, arguments}; the OpenAI
      // wire shape nests them under "function". Accept both: an imported export
      // or a hand-written fixture may well be in wire form, and silently losing
      // the call name renders the transcript as an empty "调用".
      const nlohmann::json* fn = nullptr;
      auto fn_it = it->find("function");
      if (fn_it != it->end() && fn_it->is_object()) fn = &(*fn_it);

      auto name_it = it->find("name");
      if (name_it != it->end() && name_it->is_string()) {
        tc.name = name_it->get<std::string>();
      } else if (fn) {
        auto n = fn->find("name");
        if (n != fn->end() && n->is_string()) tc.name = n->get<std::string>();
      }

      const nlohmann::json* args = nullptr;
      auto args_it = it->find("arguments");
      if (args_it != it->end()) {
        args = &(*args_it);
      } else if (fn) {
        auto a = fn->find("arguments");
        if (a != fn->end()) args = &(*a);
      }
      if (args) {
        if (args->is_string()) {
          tc.raw_arguments = args->get<std::string>();
          try {
            tc.arguments = nlohmann::json::parse(tc.raw_arguments);
          } catch (const std::exception&) {
            tc.arguments = nlohmann::json::object();
          }
        } else {
          tc.arguments = *args;
          tc.raw_arguments = args->dump();
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

bool TodoItem::valid_status(const std::string& s) {
  return s == "pending" || s == "active" || s == "done";
}

nlohmann::json TodoItem::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["id"] = id;
  j["text"] = text;
  j["status"] = status.empty() ? "pending" : status;
  return j;
}

TodoItem TodoItem::from_json(const nlohmann::json& j) {
  TodoItem t;
  if (!j.is_object()) return t;
  if (j.contains("id") && j["id"].is_string()) t.id = j["id"].get<std::string>();
  if (j.contains("text") && j["text"].is_string()) t.text = j["text"].get<std::string>();
  if (j.contains("status") && j["status"].is_string()) {
    t.status = j["status"].get<std::string>();
  }
  if (!valid_status(t.status)) t.status = "pending";
  return t;
}

nlohmann::json SessionArtifact::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["path"] = path;
  j["bytes"] = bytes;
  j["kind"] = kind.empty() ? "other" : kind;
  if (!tool.empty()) j["tool"] = tool;
  if (!created_at.empty()) j["created_at"] = created_at;
  if (turn > 0) j["turn"] = turn;
  return j;
}

SessionArtifact SessionArtifact::from_json(const nlohmann::json& j) {
  SessionArtifact a;
  if (!j.is_object()) return a;
  if (j.contains("path") && j["path"].is_string()) a.path = j["path"].get<std::string>();
  if (j.contains("bytes") && j["bytes"].is_number()) a.bytes = j["bytes"].get<long long>();
  if (j.contains("kind") && j["kind"].is_string()) a.kind = j["kind"].get<std::string>();
  if (j.contains("tool") && j["tool"].is_string()) a.tool = j["tool"].get<std::string>();
  if (j.contains("created_at") && j["created_at"].is_string()) {
    a.created_at = j["created_at"].get<std::string>();
  }
  if (j.contains("turn") && j["turn"].is_number()) a.turn = j["turn"].get<int>();
  return a;
}

std::string artifact_kind_for(const std::string& path) {
  const std::string ext = path_extension(path);
  if (ext == ".png" || ext == ".jpg" || ext == ".jpeg" || ext == ".gif" || ext == ".webp" ||
      ext == ".svg" || ext == ".bmp" || ext == ".ico") {
    return "image";
  }
  if (ext == ".mp4" || ext == ".webm" || ext == ".mov" || ext == ".mkv" || ext == ".avi" ||
      ext == ".m4v") {
    return "video";
  }
  if (ext == ".mp3" || ext == ".wav" || ext == ".m4a" || ext == ".ogg" || ext == ".flac") {
    return "audio";
  }
  if (ext == ".pdf" || ext == ".md" || ext == ".txt" || ext == ".html" || ext == ".htm" ||
      ext == ".doc" || ext == ".docx" || ext == ".xls" || ext == ".xlsx" || ext == ".ppt" ||
      ext == ".pptx" || ext == ".odt" || ext == ".rtf") {
    return "document";
  }
  if (ext == ".csv" || ext == ".json" || ext == ".jsonl" || ext == ".xml" || ext == ".yaml" ||
      ext == ".yml" || ext == ".tsv" || ext == ".sql" || ext == ".db") {
    return "data";
  }
  if (ext == ".zip" || ext == ".tar" || ext == ".gz" || ext == ".7z" || ext == ".xz") {
    return "archive";
  }
  return "other";
}

bool artifact_is_build_noise(const std::string& path) {
  const std::string ext = path_extension(path);
  // Compiler/linker output and dependency stamps: a build produces hundreds of
  // these and none of them is a result the user wants to download.
  static const char* const kNoise[] = {
      ".o",   ".obj",  ".a",   ".lib",  ".so",   ".dylib", ".dll", ".exe", ".pdb",
      ".d",   ".dep",  ".ilk",  ".exp",  ".map",  ".gcda",  ".gcno", ".pyc", ".pyo",
      ".class", ".jar", ".war", ".ear", ".lo", ".la", ".lai", ".loi", ".slo",
      ".cmake", ".moc", ".swp", ".swo", ".tmp", ".temp", ".bak", ".orig",
      ".rej", ".lock", ".pid", ".seed", ".log",
      // NB: ".ts" is NOT here - TypeScript sources are a real deliverable.
  };
  for (std::size_t i = 0; i < sizeof(kNoise) / sizeof(kNoise[0]); ++i) {
    if (ext == kNoise[i]) return true;
  }
  return false;
}

void todo_counts(const std::vector<TodoItem>& todos, int* total, int* done) {
  int t = 0, d = 0;
  for (std::size_t i = 0; i < todos.size(); ++i) {
    if (trim(todos[i].text).empty()) continue;
    ++t;
    if (todos[i].status == "done") ++d;
  }
  if (total) *total = t;
  if (done) *done = d;
}

bool todos_finished(const std::vector<TodoItem>& todos) {
  int total = 0, done = 0;
  todo_counts(todos, &total, &done);
  return total > 0 && done == total;
}

nlohmann::json SessionInfo::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["id"] = id;
  j["title"] = title;
  j["created_at"] = created_at;
  j["updated_at"] = updated_at;
  j["message_count"] = message_count;
  if (todo_total > 0) {
    j["todo_total"] = todo_total;
    j["todo_done"] = todo_done;
  }
  if (!forked_from.empty()) j["forked_from"] = forked_from;
  if (fork_turn > 0) j["fork_turn"] = fork_turn;
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
  auto tt = j.find("todo_total");
  if (tt != j.end() && tt->is_number()) s.todo_total = tt->get<int>();
  auto td = j.find("todo_done");
  if (td != j.end() && td->is_number()) s.todo_done = td->get<int>();
  s.forked_from = str_of("forked_from");
  auto turn_it = j.find("fork_turn");
  if (turn_it != j.end() && turn_it->is_number()) s.fork_turn = turn_it->get<int>();
  return s;
}

}  // namespace tea
