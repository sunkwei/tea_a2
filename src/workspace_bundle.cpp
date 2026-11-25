// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
#include "workspace_bundle.h"

#include <algorithm>
#include <cstring>
#include <map>
#include <set>

#include "gzip.h"
#include "log.h"
#include "state_lock.h"
#include "trajectory.h"
#include "util.h"
#include "version.h"

// miniz is the ZIP writer *and* the reader here: the same amalgamation that
// makes the archive download work reads a bundle back. Its zlib-compatible
// helpers are static inline, and this translation unit only uses the archive
// API, so GCC would report every other one as "defined but not used" - the
// warning is silenced around the include (the vendored header is not ours to
// annotate).
#if defined(__GNUC__)
#pragma GCC diagnostic push
#pragma GCC diagnostic ignored "-Wunused-function"
#endif
#include "miniz.h"
#if defined(__GNUC__)
#pragma GCC diagnostic pop
#endif

namespace tea {

namespace {

/// What a bundle is. The kind is checked on import: a zip that is not one of
/// ours is refused with a clear reason instead of half-restored.
const char* const kBundleKind = "tea_a2.workspace.bundle";
const int kBundleSchema = 1;

/// The names inside the bundle.
const char* const kManifestName = "manifest.json";
const char* const kSessionsDir = "sessions/";
const char* const kTrajectoriesDir = "trajectories/";
const char* const kImagesDir = "images/";
const char* const kMemoryName = "memory.json";
const char* const kAgentDocName = "agent.md";

/// Read a JSON file. A file that is missing or broken is not an error here:
/// the caller decides what an empty result means.
nlohmann::json read_json_file(const std::string& path, bool* ok) {
  if (ok != nullptr) *ok = false;
  if (!path_exists(path)) return nlohmann::json();
  std::string text;
  if (!read_file(path, &text)) return nlohmann::json();
  nlohmann::json parsed = nlohmann::json::parse(text, nullptr, false);
  if (parsed.is_discarded()) return nlohmann::json();
  if (ok != nullptr) *ok = true;
  return parsed;
}

/// The session id behind a session file name ("<id>.json" or "<id>.json.gz").
std::string session_id_from_file(const std::string& name) {
  if (name.size() > 8 && name.compare(name.size() - 8, 8, ".json.gz") == 0) {
    return name.substr(0, name.size() - 8);
  }
  if (path_extension(name) == ".json") return path_stem(name);
  return std::string();
}

/// The session id behind a trajectory file name ("<id>.jsonl" or
/// "<id>.jsonl.gz").
std::string trajectory_id_from_name(const std::string& name) {
  if (name.size() > 10 && name.compare(name.size() - 10, 10, ".jsonl.gz") == 0) {
    return name.substr(0, name.size() - 10);
  }
  if (name.size() > 6 && name.compare(name.size() - 6, 6, ".jsonl") == 0) {
    return name.substr(0, name.size() - 6);
  }
  return std::string();
}

/// The pictures a session refers to, as they are written in the file.
///
/// A message stores the *path* of the picture, not the bytes: the bytes live
/// once in the workspace's artifacts directory. The bundle therefore has to
/// carry those files separately, and the paths have to be rewritten when the
/// bundle lands on a device that keeps its runtime data somewhere else.
std::vector<std::string> session_image_paths(const nlohmann::json& session) {
  std::vector<std::string> out;
  auto msgs = session.find("messages");
  if (msgs == session.end() || !msgs->is_array()) return out;
  for (auto it = msgs->begin(); it != msgs->end(); ++it) {
    if (!it->is_object()) continue;
    auto imgs = it->find("images");
    if (imgs == it->end() || !imgs->is_array()) continue;
    for (auto im = imgs->begin(); im != imgs->end(); ++im) {
      if (!im->is_object()) continue;
      auto p = im->find("path");
      if (p == im->end() || !p->is_string()) continue;
      const std::string path = p->get<std::string>();
      if (path.empty()) continue;
      if (std::find(out.begin(), out.end(), path) == out.end()) out.push_back(path);
    }
  }
  return out;
}

/// Where the picture of a message actually is.
///
/// The message stores a workspace-relative path (".tea_agent_run/artifacts/
/// images/img_x.png"), but the runtime directory does not have to live inside
/// the workspace - then the path is absolute. Both are looked for, and the
/// artifacts directory is the last resort (the picture may have been moved
/// with the runtime directory).
std::string find_image_file(const Workspace& ws, const std::string& stored_path) {
  if (stored_path.empty()) return std::string();
  std::vector<std::string> candidates;
  if (path_is_absolute(stored_path)) {
    candidates.push_back(path_absolute(stored_path));
  } else {
    candidates.push_back(path_absolute(path_join(ws.root(), stored_path)));
  }
  const std::string base = path_filename(stored_path);
  if (!base.empty()) {
    candidates.push_back(path_join(path_join(ws.artifacts_dir(), "images"), base));
  }
  for (std::size_t i = 0; i < candidates.size(); ++i) {
    if (path_is_file(candidates[i])) return candidates[i];
  }
  return std::string();
}

/// Read one file of the workspace, transparently handling the compressed twin
/// the history cleaner leaves behind ("<id>.json.gz").
bool read_workspace_file(const std::string& path, std::string* out, std::size_t max_bytes) {
  std::string err;
  if (!read_file_maybe_gzip(path, out, &err, max_bytes)) return false;
  return true;
}

/// The writer of the bundle: one open archive plus the limits that keep it
/// inside the device's limits.
struct BundleWriter {
  mz_zip_archive* zip = nullptr;
  const BundleLimits* limits = nullptr;
  BundleExport* out = nullptr;
  bool full = false;
  unsigned long long total = 0;  // how much went in (uncompressed)

  bool add(const std::string& name, const std::string& bytes, bool compress) {
    if (full) {
      ++out->skipped;
      return false;
    }
    if (total + bytes.size() > limits->max_total_bytes) {
      full = true;
      ++out->skipped;
      LOG_WARN("bundle: the archive would grow past the limit; ", name, " is left out");
      return false;
    }
    const mz_uint level = compress ? MZ_DEFAULT_COMPRESSION : MZ_NO_COMPRESSION;
    if (!mz_zip_writer_add_mem(zip, name.c_str(), bytes.data(), bytes.size(), level)) {
      ++out->skipped;
      LOG_WARN("bundle: cannot store ", name, ": ",
               mz_zip_get_error_string(mz_zip_get_last_error(zip)));
      return false;
    }
    total += bytes.size();
    return true;
  }
};

/// Read one entry of the bundle into memory.
bool zip_read(mz_zip_archive* zip, const std::string& name, std::string* out) {
  // This miniz has no MZ_INVALID_INDEX: locate_file answers -1 when the
  // entry is not in the archive.
  const int index = mz_zip_reader_locate_file(zip, name.c_str(), nullptr, 0);
  if (index < 0) return false;
  std::size_t size = 0;
  void* buffer = mz_zip_reader_extract_to_heap(zip, static_cast<mz_uint>(index), &size, 0);
  if (buffer == nullptr) return false;
  out->assign(static_cast<const char*>(buffer), size);
  mz_free(buffer);
  return true;
}

/// Every entry name of the bundle.
std::vector<std::string> zip_names(mz_zip_archive* zip) {
  std::vector<std::string> out;
  const mz_uint count = mz_zip_reader_get_num_files(zip);
  for (mz_uint i = 0; i < count; ++i) {
    char name[1024];
    if (!mz_zip_reader_get_filename(zip, i, name, sizeof(name))) continue;
    if (mz_zip_reader_is_file_a_directory(zip, i)) continue;
    out.push_back(name);
  }
  return out;
}

/// One topic of the bundle: the session file, its trajectory and its pictures.
struct BundleTopic {
  std::string id;
  std::string title;
  std::string created_at;
  std::string updated_at;
  int messages = 0;
  int todo_total = 0;
  int todo_done = 0;
  std::string forked_from;
  int fork_turn = 0;
  std::string file;       // the zip entry of the session
  std::string trajectory; // the zip entry of the trajectory ("" = none)
  std::vector<std::pair<std::string, std::string> > images;  // (stored path, zip entry)
};

/// The task list of a session file: how many steps, how many done.
void todo_counts_of(const nlohmann::json& session, int* total, int* done) {
  int t = 0;
  int d = 0;
  auto todos = session.find("todos");
  if (todos != session.end() && todos->is_array()) {
    for (auto it = todos->begin(); it != todos->end(); ++it) {
      ++t;
      if (!it->is_object()) continue;
      auto status = it->find("status");
      if (status != it->end() && status->is_string() &&
          status->get<std::string>() == "done") {
        ++d;
      }
    }
  }
  if (total) *total = t;
  if (done) *done = d;
}

/// The index entry of a restored topic.
SessionInfo topic_info(const BundleTopic& topic) {
  SessionInfo info;
  info.id = topic.id;
  info.title = topic.title;
  info.created_at = topic.created_at;
  info.updated_at = topic.updated_at;
  info.message_count = topic.messages;
  info.todo_total = topic.todo_total;
  info.todo_done = topic.todo_done;
  info.forked_from = topic.forked_from;
  info.fork_turn = topic.fork_turn;
  return info;
}

/// Put the restored topics into the session index, so the sidebar shows them.
///
/// The index belongs to whoever changes it: a turn that finishes right now
/// writes its own entry, and this must not drop it. Read - change - write
/// under the process-wide lock, the same way the session save does it.
bool update_session_index(const Workspace& ws, const std::vector<SessionInfo>& infos,
                          std::string* err) {
  if (infos.empty()) return true;
  StateLock lock(state_mutex());
  const std::string index_path = path_join(ws.sessions_dir(), "index.json");
  bool ok = false;
  nlohmann::json index = read_json_file(index_path, &ok);
  nlohmann::json list = nlohmann::json::array();
  if (ok && index.is_array()) list = index;
  for (std::size_t i = 0; i < infos.size(); ++i) {
    bool replaced = false;
    for (auto it = list.begin(); it != list.end(); ++it) {
      if (!it->is_object()) continue;
      auto id_it = it->find("id");
      if (id_it != it->end() && id_it->is_string() &&
          id_it->get<std::string>() == infos[i].id) {
        *it = infos[i].to_json();
        replaced = true;
        break;
      }
    }
    if (!replaced) list.push_back(infos[i].to_json());
  }
  if (!make_dirs(ws.sessions_dir(), err)) return false;
  return write_file_atomic(index_path, dump_json(list, 2) + "\n", err);
}

/// Does this device already have the topic?
bool local_session_exists(const Workspace& ws, const std::string& id) {
  const std::string path = path_join(ws.sessions_dir(), id + ".json");
  if (path_exists(path)) return true;
  return path_exists(path + ".gz");
}

/// Does this device already have the trajectory of that topic?
bool local_trajectory_exists(const Workspace& ws, const std::string& id) {
  const std::string path = trajectory_file_path(ws.run_dir(), id);
  if (path_exists(path)) return true;
  return path_exists(path + ".gz");
}

/// A file name that is not taken yet: "<stem>_<hex><ext>".
std::string unique_image_name(const std::string& base) {
  const std::string stem = path_stem(base);
  const std::string ext = path_extension(base);
  return stem + "_" + random_hex(3) + ext;
}

}  // namespace

nlohmann::json BundleImport::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["sessions"] = sessions;
  j["sessions_conflict"] = sessions_conflict;
  j["trajectories"] = trajectories;
  j["trajectories_conflict"] = trajectories_conflict;
  j["images"] = images;
  j["notes"] = notes;
  j["notes_conflict"] = notes_conflict;
  j["agent_doc"] = agent_doc;
  j["agent_doc_conflict"] = agent_doc_conflict;
  nlohmann::json c = nlohmann::json::array();
  for (std::size_t i = 0; i < conflicts.size(); ++i) c.push_back(conflicts[i]);
  j["conflicts"] = c;
  nlohmann::json w = nlohmann::json::array();
  for (std::size_t i = 0; i < warnings.size(); ++i) w.push_back(warnings[i]);
  j["warnings"] = w;
  return j;
}

BundleExport export_workspace_bundle(const Workspace& ws, const BundleLimits& limits) {
  BundleExport out;
  mz_zip_archive zip;
  std::memset(&zip, 0, sizeof(zip));
  if (!mz_zip_writer_init_heap(&zip, 0, 64 * 1024)) {
    out.error = std::string("cannot start the archive (") +
                mz_zip_get_error_string(mz_zip_get_last_error(&zip)) + ")";
    return out;
  }

  BundleWriter writer;
  writer.zip = &zip;
  writer.limits = &limits;
  writer.out = &out;

  nlohmann::json manifest = nlohmann::json::object();
  manifest["schema"] = kBundleSchema;
  manifest["kind"] = kBundleKind;
  manifest["version"] = std::string(tea::version_string());
  manifest["exported_at"] = now_iso8601();
  nlohmann::json workspace = nlohmann::json::object();
  workspace["id"] = ws.id();
  workspace["name"] = ws.entry().name;
  workspace["path"] = ws.root();
  manifest["workspace"] = workspace;
  nlohmann::json topics = nlohmann::json::array();

  // ---- the topics ------------------------------------------------------
  std::vector<DirEntryInfo> files;
  std::string err;
  if (path_is_dir(ws.sessions_dir()) &&
      !list_directory(ws.sessions_dir(), &files, &err)) {
    LOG_WARN("bundle: cannot read the sessions directory: ", err);
  }
  for (std::size_t i = 0; i < files.size(); ++i) {
    if (files[i].is_dir) continue;
    const std::string id = session_id_from_file(files[i].name);
    if (id.empty()) continue;  // index.json and whatever else lives there
    if (files[i].size > limits.max_file_bytes) {
      ++out.skipped;
      LOG_WARN("bundle: ", files[i].name, " is too big (", files[i].size,
               " B); it is left out");
      continue;
    }
    std::string bytes;
    if (!read_workspace_file(files[i].path, &bytes, limits.max_file_bytes)) {
      ++out.skipped;
      LOG_WARN("bundle: cannot read ", files[i].name);
      continue;
    }
    // A file that hit the cap may have been cut in half - a half a session is
    // worse than no session, so it is left out (the compressed twin of a big
    // file can be small on disk and big once it is open).
    if (bytes.size() >= limits.max_file_bytes) {
      ++out.skipped;
      LOG_WARN("bundle: ", files[i].name, " is bigger than the limit; it is left out");
      continue;
    }
    nlohmann::json session = nlohmann::json::parse(bytes, nullptr, false);
    if (!session.is_object()) {
      ++out.skipped;
      LOG_WARN("bundle: ", files[i].name, " is not a session file");
      continue;
    }
    BundleTopic topic;
    topic.id = id;
    topic.file = std::string(kSessionsDir) + files[i].name;
    auto title_it = session.find("title");
    if (title_it != session.end() && title_it->is_string()) {
      topic.title = title_it->get<std::string>();
    }
    auto created_it = session.find("created_at");
    if (created_it != session.end() && created_it->is_string()) {
      topic.created_at = created_it->get<std::string>();
    }
    auto updated_it = session.find("updated_at");
    if (updated_it != session.end() && updated_it->is_string()) {
      topic.updated_at = updated_it->get<std::string>();
    }
    auto from_it = session.find("forked_from");
    if (from_it != session.end() && from_it->is_string()) {
      topic.forked_from = from_it->get<std::string>();
    }
    auto turn_it = session.find("fork_turn");
    if (turn_it != session.end() && turn_it->is_number()) {
      topic.fork_turn = turn_it->get<int>();
    }
    auto msgs = session.find("messages");
    if (msgs != session.end() && msgs->is_array()) {
      topic.messages = static_cast<int>(msgs->size());
    }
    todo_counts_of(session, &topic.todo_total, &topic.todo_done);

    // The pictures the messages refer to.
    const std::vector<std::string> wanted = session_image_paths(session);
    for (std::size_t w = 0; w < wanted.size(); ++w) {
      const std::string file = find_image_file(ws, wanted[w]);
      const std::string base = path_filename(wanted[w]);
      if (file.empty() || base.empty()) {
        LOG_WARN("bundle: the picture ", wanted[w], " is not on this device; it is left out");
        continue;
      }
      std::string image;
      if (!read_file_binary(file, &image, limits.max_file_bytes)) {
        ++out.skipped;
        LOG_WARN("bundle: cannot read the picture ", file);
        continue;
      }
      const std::string entry = std::string(kImagesDir) + id + "/" + base;
      if (!writer.add(entry, image, false)) continue;
      topic.images.push_back(std::make_pair(wanted[w], entry));
      ++out.images;
    }

    if (!writer.add(topic.file, bytes, true)) continue;
    ++out.sessions;

    // The trajectory of that topic: the audit trail of what the agent did.
    const std::string traj_path = trajectory_file_path(ws.run_dir(), id);
    std::string traj;
    bool have_traj = read_workspace_file(traj_path, &traj, limits.max_file_bytes);
    if (!have_traj && path_exists(traj_path + ".gz")) {
      have_traj = read_workspace_file(traj_path + ".gz", &traj, limits.max_file_bytes);
    }
    if (have_traj && traj.size() < limits.max_file_bytes) {
      topic.trajectory = std::string(kTrajectoriesDir) + id + ".jsonl";
      if (writer.add(topic.trajectory, traj, true)) {
        ++out.trajectories;
      } else {
        topic.trajectory.clear();
      }
    } else if (have_traj) {
      ++out.skipped;
      LOG_WARN("bundle: the trajectory of ", id, " is bigger than the limit; it is left out");
    }

    nlohmann::json one = nlohmann::json::object();
    one["id"] = topic.id;
    one["title"] = topic.title;
    one["created_at"] = topic.created_at;
    one["updated_at"] = topic.updated_at;
    one["messages"] = topic.messages;
    nlohmann::json todos = nlohmann::json::object();
    todos["total"] = topic.todo_total;
    todos["done"] = topic.todo_done;
    one["todos"] = todos;
    if (!topic.forked_from.empty()) one["forked_from"] = topic.forked_from;
    if (topic.fork_turn > 0) one["fork_turn"] = topic.fork_turn;
    one["file"] = topic.file;
    if (!topic.trajectory.empty()) one["trajectory"] = topic.trajectory;
    nlohmann::json images = nlohmann::json::array();
    for (std::size_t im = 0; im < topic.images.size(); ++im) {
      nlohmann::json pic = nlohmann::json::object();
      pic["path"] = topic.images[im].first;
      pic["file"] = topic.images[im].second;
      images.push_back(pic);
    }
    one["images"] = images;
    topics.push_back(one);
  }
  manifest["sessions"] = topics;

  // ---- the memory and the workspace's own notes ------------------------
  int notes = 0;
  {
    bool ok = false;
    const nlohmann::json memory = read_json_file(ws.memory_file(), &ok);
    if (ok && memory.is_object()) {
      nlohmann::json mem = nlohmann::json::object();
      auto list = memory.find("notes");
      mem["notes"] = (list != memory.end() && list->is_array()) ? *list
                                                                : nlohmann::json::array();
      notes = static_cast<int>(mem["notes"].size());
      writer.add(kMemoryName, dump_json(mem, 2) + "\n", true);
    }
  }
  manifest["notes"] = notes;

  bool has_agent_doc = false;
  {
    std::string doc;
    if (read_file(ws.agent_doc_file(), &doc) && !doc.empty()) {
      has_agent_doc = writer.add(kAgentDocName, doc, true);
    }
  }
  manifest["agent_doc"] = has_agent_doc;

  nlohmann::json counts = nlohmann::json::object();
  counts["sessions"] = out.sessions;
  counts["trajectories"] = out.trajectories;
  counts["images"] = out.images;
  counts["notes"] = notes;
  manifest["counts"] = counts;

  writer.add(kManifestName, dump_json(manifest, 2) + "\n", true);

  void* buffer = nullptr;
  std::size_t size = 0;
  if (!mz_zip_writer_finalize_heap_archive(&zip, &buffer, &size)) {
    out.error = std::string("cannot finish the archive (") +
                mz_zip_get_error_string(mz_zip_get_last_error(&zip)) + ")";
    mz_zip_writer_end(&zip);
    return out;
  }
  if (buffer != nullptr && size > 0) out.bytes.assign(static_cast<const char*>(buffer), size);
  if (buffer != nullptr) mz_free(buffer);
  mz_zip_writer_end(&zip);

  out.ok = true;
  LOG_INFO("bundle: ", ws.id(), " -> ", out.sessions, " topic(s), ", out.trajectories,
           " trajector(ies), ", out.images, " image(s), ", notes, " note(s), ",
           out.bytes.size(), " B, ", out.skipped, " skipped");
  return out;
}

bool import_workspace_bundle(Workspace& ws, const std::string& zip_bytes,
                             BundleImport* report, std::string* err) {
  BundleImport result;
  mz_zip_archive zip;
  std::memset(&zip, 0, sizeof(zip));
  if (zip_bytes.empty() || !mz_zip_reader_init_mem(&zip, zip_bytes.data(), zip_bytes.size(), 0)) {
    if (err) *err = "this is not a readable zip file";
    return false;
  }

  std::string manifest_text;
  if (!zip_read(&zip, kManifestName, &manifest_text)) {
    mz_zip_reader_end(&zip);
    if (err) *err = "there is no manifest.json in it - this is not a workspace bundle";
    return false;
  }
  nlohmann::json manifest = nlohmann::json::parse(manifest_text, nullptr, false);
  if (!manifest.is_object()) {
    mz_zip_reader_end(&zip);
    if (err) *err = "the manifest.json is not readable";
    return false;
  }
  auto kind_it = manifest.find("kind");
  if (kind_it != manifest.end() && kind_it->is_string() &&
      kind_it->get<std::string>() != kBundleKind) {
    mz_zip_reader_end(&zip);
    if (err) *err = "this zip is not a workspace bundle (kind: " +
                    kind_it->get<std::string>() + ")";
    return false;
  }
  auto schema_it = manifest.find("schema");
  if (schema_it != manifest.end() && schema_it->is_number() &&
      schema_it->get<int>() > kBundleSchema) {
    mz_zip_reader_end(&zip);
    if (err) *err = "this bundle was made by a newer version (schema " +
                    std::to_string(schema_it->get<int>()) + ")";
    return false;
  }

  const std::vector<std::string> names = zip_names(&zip);

  // ---- the pictures first ----------------------------------------------
  // They come first because the topics point at them: a session that refers to
  // a picture that is not there yet would look broken until the file lands.
  std::map<std::string, std::string> image_paths;  // file name -> local path
  {
    const std::string dir = path_join(ws.artifacts_dir(), "images");
    for (std::size_t i = 0; i < names.size(); ++i) {
      if (names[i].compare(0, std::strlen(kImagesDir), kImagesDir) != 0) continue;
      const std::string base = path_filename(names[i]);
      if (base.empty() || base == "." || base == "..") continue;
      std::string bytes;
      if (!zip_read(&zip, names[i], &bytes)) continue;
      if (!make_dirs(dir, err)) {
        mz_zip_reader_end(&zip);
        return false;
      }
      std::string target = path_join(dir, base);
      if (path_exists(target)) {
        std::string local;
        if (read_file_binary(target, &local) && local == bytes) {
          image_paths[base] = ws.display_path(target);  // the same picture
          continue;
        }
        // A different picture with the same name: keep the local one and put
        // the imported one next to it, so neither topic loses its picture.
        target = path_join(dir, unique_image_name(base));
        result.warnings.push_back("the picture " + base +
                                  " already existed here; the imported one was stored as " +
                                  path_filename(target));
      }
      if (!write_file_atomic(target, bytes, err)) {
        mz_zip_reader_end(&zip);
        return false;
      }
      image_paths[base] = ws.display_path(target);
      ++result.images;
    }
  }

  // ---- the topics -------------------------------------------------------
  std::vector<SessionInfo> restored;
  {
    // The manifest lists the topics; the archive itself is scanned as well,
    // so a bundle that lost its manifest still restores what it carries.
    std::vector<std::string> session_entries;
    auto listed = manifest.find("sessions");
    if (listed != manifest.end() && listed->is_array()) {
      for (auto it = listed->begin(); it != listed->end(); ++it) {
        if (!it->is_object()) continue;
        auto file_it = it->find("file");
        if (file_it == it->end() || !file_it->is_string()) continue;
        session_entries.push_back(file_it->get<std::string>());
      }
    }
    for (std::size_t i = 0; i < names.size(); ++i) {
      if (names[i].compare(0, std::strlen(kSessionsDir), kSessionsDir) != 0) continue;
      if (std::find(session_entries.begin(), session_entries.end(), names[i]) ==
          session_entries.end()) {
        session_entries.push_back(names[i]);
      }
    }

    for (std::size_t i = 0; i < session_entries.size(); ++i) {
      const std::string entry = session_entries[i];
      const std::string base = path_filename(entry);
      std::string id = session_id_from_file(base);
      if (id.empty() || !trajectory_id_is_safe(id)) {
        result.warnings.push_back("a topic with an unusable name was left out: " + entry);
        continue;
      }
      if (local_session_exists(ws, id)) {
        // The local topic wins: it is the one this device has been using.
        ++result.sessions_conflict;
        result.conflicts.push_back("topic " + id + " is already here; the local one was kept");
        continue;
      }
      std::string bytes;
      if (!zip_read(&zip, entry, &bytes)) {
        result.warnings.push_back("cannot read " + entry);
        continue;
      }
      nlohmann::json session = nlohmann::json::parse(bytes, nullptr, false);
      if (!session.is_object()) {
        result.warnings.push_back(entry + " is not a session file");
        continue;
      }
      // The id in the file wins over the name: that is what the sessions
      // themselves use.
      auto id_it = session.find("id");
      if (id_it != session.end() && id_it->is_string() && !id_it->get<std::string>().empty()) {
        const std::string inside = id_it->get<std::string>();
        if (!trajectory_id_is_safe(inside)) {
          result.warnings.push_back("a topic with an unusable id was left out: " + inside);
          continue;
        }
        if (inside != id && local_session_exists(ws, inside)) {
          ++result.sessions_conflict;
          result.conflicts.push_back("topic " + inside + " is already here; the local one was kept");
          continue;
        }
        id = inside;
      }

      BundleTopic topic;
      topic.id = id;
      auto title_it = session.find("title");
      if (title_it != session.end() && title_it->is_string()) {
        topic.title = title_it->get<std::string>();
      }
      auto created_it = session.find("created_at");
      if (created_it != session.end() && created_it->is_string()) {
        topic.created_at = created_it->get<std::string>();
      }
      auto updated_it = session.find("updated_at");
      if (updated_it != session.end() && updated_it->is_string()) {
        topic.updated_at = updated_it->get<std::string>();
      }
      auto from_it = session.find("forked_from");
      if (from_it != session.end() && from_it->is_string()) {
        topic.forked_from = from_it->get<std::string>();
      }
      auto turn_it = session.find("fork_turn");
      if (turn_it != session.end() && turn_it->is_number()) {
        topic.fork_turn = turn_it->get<int>();
      }
      auto msgs = session.find("messages");
      if (msgs != session.end() && msgs->is_array()) {
        topic.messages = static_cast<int>(msgs->size());
      }
      todo_counts_of(session, &topic.todo_total, &topic.todo_done);

      // The pictures of this topic point at where they live on *this*
      // device now. When nothing changes, the file is written byte for byte
      // as it came - a restore that changes nothing is the best restore.
      bool rewritten = false;
      if (!image_paths.empty() && msgs != session.end() && msgs->is_array()) {
        for (auto it = msgs->begin(); it != msgs->end(); ++it) {
          if (!it->is_object()) continue;
          auto imgs = it->find("images");
          if (imgs == it->end() || !imgs->is_array()) continue;
          for (auto im = imgs->begin(); im != imgs->end(); ++im) {
            if (!im->is_object()) continue;
            auto p = im->find("path");
            if (p == im->end() || !p->is_string()) continue;
            const std::string stored = p->get<std::string>();
            auto found = image_paths.find(path_filename(stored));
            if (found == image_paths.end()) continue;
            if (found->second == stored) continue;
            p->get_ref<std::string&>() = found->second;
            rewritten = true;
          }
        }
      }

      if (!make_dirs(ws.sessions_dir(), err)) {
        mz_zip_reader_end(&zip);
        return false;
      }
      const std::string path = path_join(ws.sessions_dir(), id + ".json");
      const std::string data = rewritten ? dump_json(session, 2) + "\n" : bytes;
      if (!write_file_atomic(path, data, err)) {
        mz_zip_reader_end(&zip);
        return false;
      }
      restored.push_back(topic_info(topic));
      ++result.sessions;
    }
  }

  // ---- the trajectories --------------------------------------------------
  for (std::size_t i = 0; i < names.size(); ++i) {
    if (names[i].compare(0, std::strlen(kTrajectoriesDir), kTrajectoriesDir) != 0) continue;
    const std::string id = trajectory_id_from_name(path_filename(names[i]));
    if (id.empty() || !trajectory_id_is_safe(id)) {
      result.warnings.push_back("a trajectory with an unusable name was left out: " + names[i]);
      continue;
    }
    if (local_trajectory_exists(ws, id)) {
      ++result.trajectories_conflict;
      result.conflicts.push_back("the trajectory of " + id +
                                 " is already here; the local one was kept");
      continue;
    }
    std::string bytes;
    if (!zip_read(&zip, names[i], &bytes)) {
      result.warnings.push_back("cannot read " + names[i]);
      continue;
    }
    const std::string dir = path_join(ws.run_dir(), "trajectories");
    if (!make_dirs(dir, err)) {
      mz_zip_reader_end(&zip);
      return false;
    }
    if (!write_file_atomic(trajectory_file_path(ws.run_dir(), id), bytes, err)) {
      mz_zip_reader_end(&zip);
      return false;
    }
    ++result.trajectories;
  }

  // ---- the memory --------------------------------------------------------
  {
    std::string text;
    if (zip_read(&zip, kMemoryName, &text)) {
      nlohmann::json memory = nlohmann::json::parse(text, nullptr, false);
      std::vector<nlohmann::json> notes;
      if (memory.is_object()) {
        auto list = memory.find("notes");
        if (list != memory.end() && list->is_array()) {
          for (auto it = list->begin(); it != list->end(); ++it) notes.push_back(*it);
        }
      }
      if (!notes.empty()) {
        std::vector<std::string> conflicts;
        std::string memory_err;
        const int added = ws.import_memory_notes(notes, &conflicts, &memory_err);
        result.notes = added;
        result.notes_conflict = static_cast<int>(conflicts.size());
        for (std::size_t i = 0; i < conflicts.size(); ++i) {
          result.conflicts.push_back("memory note " + conflicts[i] +
                                     " is already here; the local one was kept");
        }
        if (!memory_err.empty()) result.warnings.push_back(memory_err);
      }
    }
  }

  // ---- the workspace's own notes ----------------------------------------
  {
    std::string doc;
    if (zip_read(&zip, kAgentDocName, &doc) && !doc.empty()) {
      // A fresh workspace has an agent.md, but it is the empty template: it
      // says nothing, so replacing it loses nothing. A file with content of
      // its own is a real conflict - the local one wins.
      if (path_exists(ws.agent_doc_file()) && !ws.agent_doc_is_pristine()) {
        result.agent_doc_conflict = true;
        result.conflicts.push_back("agent.md is already here; the local one was kept");
      } else {
        if (!ws.save_agent_doc(doc, err)) {
          mz_zip_reader_end(&zip);
          return false;
        }
        result.agent_doc = true;
      }
    }
  }

  // The sidebar reads the index, not the files: the restored topics have to
  // be in it, or they exist but nobody sees them.
  std::string index_err;
  if (!update_session_index(ws, restored, &index_err) && !index_err.empty()) {
    result.warnings.push_back("the session index could not be updated: " + index_err);
  }

  mz_zip_reader_end(&zip);
  if (report) *report = result;
  LOG_INFO("bundle: imported into ", ws.id(), ": ", result.sessions, " topic(s), ",
           result.trajectories, " trajector(ies), ", result.images, " image(s), ",
           result.notes, " note(s); kept ", result.sessions_conflict, " local topic(s)");
  return true;
}

}  // namespace tea
