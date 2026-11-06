// history_gc.cpp -- the report the cleaner hands back, and the disk accounting
// it needs to decide whether the run_dir is over its budget.
#include "history_gc.h"

#include <algorithm>

#include "util.h"

namespace tea {

nlohmann::json HistoryGcReport::to_json() const {
  nlohmann::json j = nlohmann::json::object();
  j["dry_run"] = dry_run;
  j["bytes_before"] = bytes_before;
  j["bytes_after"] = bytes_after;
  j["freed_bytes"] = freed_bytes;
  j["compressed_saved"] = compressed_saved;
  j["sessions_removed"] = sessions_removed;
  j["trajectories_removed"] = trajectories_removed;
  j["sessions_compressed"] = sessions_compressed;
  j["trajectories_compressed"] = trajectories_compressed;
  j["removed_sessions"] = removed_sessions;
  j["compressed_sessions"] = compressed_sessions;
  j["notes"] = notes;
  return j;
}

int64_t tree_size(const std::string& dir) {
  int64_t total = 0;
  std::vector<DirEntryInfo> entries;
  std::string err;
  if (!list_directory(dir, &entries, &err)) return 0;
  for (std::size_t i = 0; i < entries.size(); ++i) {
    if (entries[i].is_dir) {
      total += tree_size(entries[i].path);
    } else {
      total += static_cast<int64_t>(entries[i].size);
    }
  }
  return total;
}

}  // namespace tea
