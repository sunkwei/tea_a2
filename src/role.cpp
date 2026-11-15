// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
#include "role.h"

#include <yaml-cpp/yaml.h>

#include <algorithm>
#include <cctype>

#include "util.h"

namespace tea {
namespace {

std::string ystr(const YAML::Node& node, const char* key, const std::string& def = "") {
  if (!node || !node.IsMap()) return def;
  const YAML::Node v = node[key];
  if (!v || !v.IsScalar()) return def;
  try {
    return trim(v.as<std::string>());
  } catch (const std::exception&) {
    return def;
  }
}

bool yhas(const YAML::Node& node, const char* key) {
  if (!node || !node.IsMap()) return false;
  const YAML::Node v = node[key];
  return static_cast<bool>(v) && v.IsDefined() && !v.IsNull();
}

int64_t yint(const YAML::Node& node, const char* key, int64_t def) {
  if (!yhas(node, key)) return def;
  const YAML::Node v = node[key];
  if (!v.IsScalar()) return def;
  try {
    return v.as<int64_t>();
  } catch (const std::exception&) {
    try {
      return static_cast<int64_t>(v.as<double>());
    } catch (const std::exception&) {
      return def;
    }
  }
}

/// Dump a YAML node back to text, so that one parser can serve both shapes
/// (a file with one role and a file with a list of them).
std::string dump_yaml(const YAML::Node& node) {
  YAML::Emitter out;
  out << node;
  return out.c_str() ? out.c_str() : std::string();
}

/// A list of strings: a sequence, or a single scalar ("tools: toolkit_exec").
bool string_list(const YAML::Node& node, std::vector<std::string>* out, std::string* err,
                 const char* what) {
  if (!node || !node.IsDefined() || node.IsNull()) return true;
  if (node.IsScalar()) {
    const std::string one = trim(node.as<std::string>());
    if (!one.empty()) out->push_back(one);
    return true;
  }
  if (!node.IsSequence()) {
    if (err) *err = std::string(what) + " must be a list of strings";
    return false;
  }
  for (auto it = node.begin(); it != node.end(); ++it) {
    if (!it->IsScalar()) {
      if (err) *err = std::string(what) + " must be a list of strings";
      return false;
    }
    std::string one;
    try {
      one = trim(it->as<std::string>());
    } catch (const std::exception&) {
      if (err) *err = std::string(what) + " must be a list of strings";
      return false;
    }
    if (one.empty()) continue;
    if (std::find(out->begin(), out->end(), one) == out->end()) out->push_back(one);
  }
  return true;
}

}  // namespace

std::string role_id_from_file(const std::string& file_name) {
  if (!ends_with(file_name, ".yaml") && !ends_with(file_name, ".yml")) return std::string();
  const std::size_t dot = file_name.rfind('.');
  if (dot == std::string::npos || dot == 0) return std::string();
  return file_name.substr(0, dot);
}

bool valid_role_id(const std::string& id) {
  if (id.empty() || id.size() > 64) return false;
  if (id == "." || id == "..") return false;
  for (std::size_t i = 0; i < id.size(); ++i) {
    const unsigned char c = static_cast<unsigned char>(id[i]);
    if (std::isalnum(c)) continue;
    if (c == '-' || c == '_' || c == '.') continue;
    return false;
  }
  return true;
}

bool parse_role_text(const std::string& text, const std::string& id, Role* out, std::string* err) {
  if (err) err->clear();
  YAML::Node root;
  try {
    root = YAML::Load(text);
  } catch (const std::exception& e) {
    if (err) *err = std::string("cannot parse the role: ") + e.what();
    return false;
  }
  if (!root || root.IsNull()) {
    if (err) *err = "a role must be a YAML mapping";
    return false;
  }
  if (!root.IsMap()) {
    if (err) *err = "a role must be a YAML mapping";
    return false;
  }

  Role role;
  role.id = id;
  role.display_name = ystr(root, "name", id);
  role.description = ystr(root, "description");
  role.system_prompt = ystr(root, "system_prompt");
  role.checklist = ystr(root, "checklist");

  if (yhas(root, "tools")) {
    if (!string_list(root["tools"], &role.tools, err, "tools")) return false;
  }

  const YAML::Node exec = root["exec"];
  if (exec && exec.IsDefined() && !exec.IsNull()) {
    if (!exec.IsMap()) {
      if (err) *err = "exec must be a mapping with 'allow' and/or 'deny'";
      return false;
    }
    if (!string_list(exec["allow"], &role.exec.allow, err, "exec.allow")) return false;
    if (!string_list(exec["deny"], &role.exec.deny, err, "exec.deny")) return false;
  }

  const YAML::Node model = root["model"];
  if (model && model.IsDefined() && !model.IsNull()) {
    if (model.IsMap()) {
      role.provider = ystr(model, "provider");
      role.model = ystr(model, "model");
    } else if (model.IsScalar()) {
      // "model: deepseek-chat" - the provider stays as it is.
      role.model = trim(model.as<std::string>());
    } else {
      if (err) *err = "model must be a mapping (provider/model) or a model name";
      return false;
    }
  }

  const YAML::Node read_only = root["read_only"];
  if (read_only && read_only.IsScalar()) {
    const std::string v = to_lower(trim(read_only.as<std::string>()));
    role.read_only = (v == "true" || v == "yes" || v == "on" || v == "1");
  }

  const YAML::Node limits = root["limits"];
  if (limits && limits.IsDefined() && !limits.IsNull()) {
    if (!limits.IsMap()) {
      if (err) *err = "limits must be a mapping";
      return false;
    }
    role.limits.max_iterations = static_cast<int>(yint(limits, "max_iterations", 0));
    role.limits.max_wall_ms = static_cast<int>(yint(limits, "max_wall_ms", 0));
    role.limits.max_tool_calls = static_cast<int>(yint(limits, "max_tool_calls", 0));
    role.limits.max_total_tokens = yint(limits, "max_total_tokens", 0);
  }

  if (out) *out = role;
  return true;
}

bool parse_role_file(const std::string& path, const std::string& id, Role* out, std::string* err) {
  std::string text;
  if (!read_file(path, &text, 256u * 1024u)) {
    if (err) *err = "cannot read " + path;
    return false;
  }
  if (!parse_role_text(text, id, out, err)) {
    if (err) *err = path + ": " + *err;
    return false;
  }
  if (out) out->path = path;
  return true;
}

bool parse_roles_text(const std::string& text, const std::string& default_id,
                      std::vector<Role>* out, std::string* err) {
  if (err) err->clear();
  YAML::Node root;
  try {
    root = YAML::Load(text);
  } catch (const std::exception& e) {
    if (err) *err = std::string("cannot parse the role file: ") + e.what();
    return false;
  }
  if (!root || root.IsNull()) {
    if (err) *err = "a role file must be a mapping (one role) or a list (several roles)";
    return false;
  }
  if (root.IsMap()) {
    Role role;
    if (!parse_role_text(text, default_id, &role, err)) return false;
    if (out) out->push_back(role);
    return true;
  }
  if (!root.IsSequence()) {
    if (err) *err = "a role file must be a mapping (one role) or a list (several roles)";
    return false;
  }
  // A list of roles. Each entry names itself with `id:` - the file name
  // cannot name them all.
  std::size_t index = 0;
  for (auto it = root.begin(); it != root.end(); ++it, ++index) {
    if (!it->IsMap()) {
      if (err) {
        *err = "entry " + std::to_string(index + 1) + " of the role list is not a mapping";
      }
      return false;
    }
    const std::string id = ystr(*it, "id");
    if (id.empty() || !valid_role_id(id)) {
      if (err) {
        *err = "entry " + std::to_string(index + 1) +
               " of the role list has no usable 'id' (letters, digits, dash, underscore, dot)";
      }
      return false;
    }
    Role role;
    std::string one_err;
    if (!parse_role_text(dump_yaml(*it), id, &role, &one_err)) {
      if (err) *err = "role '" + id + "': " + one_err;
      return false;
    }
    if (out) out->push_back(role);
  }
  return true;
}

bool builtin_role(const std::string& id, Role* out, std::string* err) {
  const std::vector<std::pair<std::string, std::string>>& roles = builtin_roles();
  for (std::size_t i = 0; i < roles.size(); ++i) {
    if (roles[i].first != id) continue;
    Role role;
    std::string one_err;
    if (!parse_role_text(roles[i].second, id, &role, &one_err)) {
      if (err) *err = "the built-in role '" + id + "' does not load: " + one_err;
      return false;
    }
    role.builtin = true;
    role.path.clear();   // it is not a file yet
    if (out) *out = role;
    return true;
  }
  if (err) *err = "no built-in role named '" + id + "'";
  return false;
}

std::string builtin_role_text(const std::string& id) {
  const std::vector<std::pair<std::string, std::string>>& roles = builtin_roles();
  for (std::size_t i = 0; i < roles.size(); ++i) {
    if (roles[i].first == id) return roles[i].second;
  }
  return std::string();
}

std::string copy_role_text(const std::string& text, const std::string& new_name) {
  // The copy gets its own display name: two roles called 需求分析 are hard to
  // tell apart in the list. Only a top-level `name:` is replaced - a role file
  // that holds a list of roles keeps its own names (the copy is then a copy
  // of the whole list).
  const std::string line = "name: " + new_name;
  std::vector<std::string> lines;
  {
    std::string cur;
    for (std::size_t i = 0; i <= text.size(); ++i) {
      const char c = i < text.size() ? text[i] : '\n';
      if (c == '\n') {
        lines.push_back(cur);
        cur.clear();
      } else {
        cur += c;
      }
    }
    if (!cur.empty()) lines.push_back(cur);
  }
  for (std::size_t i = 0; i < lines.size(); ++i) {
    if (lines[i].compare(0, 5, "name:") != 0) continue;
    if (!lines[i].empty() && lines[i][5] != ' ' && lines[i][5] != '\t') continue;
    lines[i] = line;
    std::string out;
    for (std::size_t k = 0; k < lines.size(); ++k) {
      out += lines[k];
      out += "\n";
    }
    return out;
  }
  // No top-level name: put one in front.
  std::string out = line + "\n";
  for (std::size_t i = 0; i < lines.size(); ++i) {
    out += lines[i];
    out += "\n";
  }
  return out;
}

void RoleSet::add(const Role& role) {
  for (std::size_t i = 0; i < roles_.size(); ++i) {
    if (roles_[i].id == role.id) {
      roles_[i] = role;   // the newer one wins
      return;
    }
  }
  roles_.push_back(role);
}

const Role* RoleSet::find(const std::string& id) const {
  for (std::size_t i = 0; i < roles_.size(); ++i) {
    if (roles_[i].id == id) return &roles_[i];
  }
  return nullptr;
}

RoleSet RoleSet::load(const std::string& roles_dir) {
  RoleSet set;
  std::vector<DirEntryInfo> entries;
  std::string err;
  if (!list_directory(roles_dir, &entries, &err)) return set;  // no roles yet

  for (std::size_t i = 0; i < entries.size(); ++i) {
    if (entries[i].is_dir) continue;
    const std::string id = role_id_from_file(entries[i].name);
    if (id.empty()) continue;
    if (!valid_role_id(id)) {
      set.errors_.push_back(entries[i].name + ": not a usable role name");
      continue;
    }
    std::string text;
    if (!read_file(entries[i].path, &text, 256u * 1024u)) {
      set.errors_.push_back(entries[i].name + ": cannot read the file");
      continue;
    }
    std::vector<Role> roles;
    std::string perr;
    if (!parse_roles_text(text, id, &roles, &perr)) {
      set.errors_.push_back(entries[i].name + ": " + perr);
      continue;
    }
    for (std::size_t k = 0; k < roles.size(); ++k) {
      roles[k].path = entries[i].path;
      set.roles_.push_back(roles[k]);
    }
  }
  std::sort(set.roles_.begin(), set.roles_.end(),
            [](const Role& a, const Role& b) { return a.id < b.id; });
  return set;
}

nlohmann::json Role::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["id"] = id;
  j["name"] = display_name;
  j["description"] = description;
  j["system_prompt"] = system_prompt;
  j["checklist"] = checklist;
  j["tools"] = tools;
  nlohmann::json ex = nlohmann::json::object();
  ex["allow"] = exec.allow;
  ex["deny"] = exec.deny;
  j["exec"] = ex;
  j["provider"] = provider;
  j["model"] = model;
  j["read_only"] = read_only;
  j["builtin"] = builtin;
  nlohmann::json lim = nlohmann::json::object();
  lim["max_iterations"] = limits.max_iterations;
  lim["max_wall_ms"] = limits.max_wall_ms;
  lim["max_tool_calls"] = limits.max_tool_calls;
  lim["max_total_tokens"] = limits.max_total_tokens;
  j["limits"] = lim;
  j["path"] = path;
  return j;
}

}  // namespace tea
