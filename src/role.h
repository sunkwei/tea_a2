// role.h -- the workspace's stage profiles ("roles").
//
// A role is a named way of working: a persona, the stage's standing rules and
// the boundaries the stage runs under (tools, command fence, model, budget).
// It lives with the other per-workspace state:
//
//   <workspace>/.tea_agent_run/roles/<id>.yaml
//
// The *current* role is a pointer in the workspace's settings.yaml
// (`settings.role`). The pointer is read at the start of every turn, so a
// switch applies to the **next** turn: a turn that is already running keeps
// the role it started with.
//
// Only the user (the operator, or another a2 driving this one over the API)
// switches the role. The agent itself cannot: a stage that could promote
// itself would be a stage that could lift its own fences.
#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "config.h"

namespace tea {

/// One stage profile of a workspace.
struct Role {
  /// The id: the file's stem ("dev" for roles/dev.yaml). How the API and the
  /// settings.yaml pointer address the role.
  std::string id;
  /// What the UI shows ("开发"). Defaults to the id.
  std::string display_name;
  /// One line about the stage.
  std::string description;
  /// Replaces the persona block of the system prompt.
  std::string system_prompt;
  /// The stage's standing rules, appended after the persona.
  std::string checklist;
  /// The tools this stage may use. Empty => no extra restriction: the turn's
  /// own set applies.
  std::vector<std::string> tools;
  /// The command fence of this stage.
  ExecPolicy exec;
  /// The model this stage runs on (optional; the caller's choice wins).
  std::string provider;
  std::string model;
  /// The budget this stage may use (0 in a dimension => not set).
  TurnBudget limits;
  /// A read-only stage: the file tools may read and list, but not write,
  /// delete, move or create anything. The guarantee is in the tools, not in
  /// the prompt ("this stage is read-only" is a rule a model can talk itself
  /// out of).
  bool read_only = false;
  /// One of the roles that ship with the agent (see builtin_roles()). Such a
  /// role is a **template**: it lives in the program, not in the workspace, and
  /// the way to get your own version of it is to copy it.
  bool builtin = false;
  /// The file it was read from. Empty for a built-in that has not been copied
  /// into the workspace yet.
  std::string path;

  nlohmann::json to_json() const;
};

/// The roles of one workspace: every *.yaml / *.yml in <run_dir>/roles/.
///
/// A file that cannot be parsed is skipped and reported in errors(): one
/// broken role file must not take the other roles down with it.
class RoleSet {
 public:
  static RoleSet load(const std::string& roles_dir);

  const std::vector<Role>& all() const { return roles_; }
  const Role* find(const std::string& id) const;
  /// Add a role to the set. The workspace layer merges the built-in
  /// templates into the workspace's own files.
  void add(const Role& role);
  const std::vector<std::string>& errors() const { return errors_; }
  bool empty() const { return roles_.empty(); }

 private:
  std::vector<Role> roles_;
  std::vector<std::string> errors_;
};

/// Parse the YAML of one role. `id` is the id the role is addressed by (the
/// file's stem). The text must be a mapping; unknown keys are ignored, so a
/// role file may carry comments and notes for the human who wrote it.
bool parse_role_text(const std::string& text, const std::string& id, Role* out, std::string* err);

/// Parse a role file that may hold **one** role (a mapping) or **several**
/// (a sequence). A sequence entry names its own role with an `id:` key; an
/// entry without one is skipped and reported. This is the shape a human
/// writes when they think of "the roles of this project" as a list.
bool parse_roles_text(const std::string& text, const std::string& default_id,
                      std::vector<Role>* out, std::string* err);

/// Parse one role file (the same rules as parse_role_text).
bool parse_role_file(const std::string& path, const std::string& id, Role* out, std::string* err);

/// A role id is a plain file name: letters, digits, dash, underscore, dot.
/// No slashes and no spaces: the id becomes a file name.
bool valid_role_id(const std::string& id);

/// The roles that ship with the agent: 需求分析 / 项目设计 / 开发 / 测试 / 运维.
///
/// They are **templates in the program**, not files in the workspace: the
/// workspace holds only the roles the user made. To get your own version of a
/// built-in role, copy it (POST .../roles/{name}/copy) - the copy is a normal,
/// writable role on disk.
///
/// A workspace file with the same id **shadows** the built-in: it is the
/// user's own version of it, and deleting the file brings the template back.
const std::vector<std::pair<std::string, std::string>>& builtin_roles();

/// The built-in role with this id, parsed (empty id => none). The content is
/// the pristine template, whatever the workspace may have done to it.
bool builtin_role(const std::string& id, Role* out, std::string* err);

/// The raw YAML of a built-in role (empty when there is none). This is the
/// text a copy is made of.
std::string builtin_role_text(const std::string& id);

/// The text of a role copied under a new name: the same YAML with the
/// top-level `name:` replaced (a copy that kept the name would be
/// indistinguishable from its original in the list).
std::string copy_role_text(const std::string& text, const std::string& new_name);

/// The id of a role file ("dev.yaml" -> "dev"). Empty when the name is not a
/// role file.
std::string role_id_from_file(const std::string& file_name);

}  // namespace tea
