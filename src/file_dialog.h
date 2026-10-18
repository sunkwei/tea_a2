// file_dialog.h -- ask the *server's* desktop for a directory.
//
// The browser cannot hand back a real absolute path: `<input type="file"
// webkitdirectory>` only yields relative names, and `showDirectoryPicker()`
// returns a handle, not a path. Since tea_agent already runs on the machine the
// user is looking at, the honest way to pick a project directory is to open the
// OS's own dialog there and return the path it produced.
//
// Consequence worth knowing: the dialog appears on the machine running the
// server. If the UI is opened from another computer, the user will not see it -
// the UI therefore keeps manual path entry as a fallback.
#pragma once

#include <string>

namespace tea {

struct DirectoryPickResult {
  /// A picker ran and the user chose a directory.
  bool ok = false;
  /// A picker ran and the user dismissed it (not an error).
  bool cancelled = false;
  /// No usable picker on this machine (headless server, no zenity/kdialog/...).
  bool unavailable = false;
  std::string path;
  std::string error;
  /// Which backend answered, e.g. "zenity", "kdialog", "osascript", "$TEA_AGENT_DIR_PICKER".
  std::string tool;
};

/// Open the platform's directory chooser and block until it closes.
///
/// `TEA_AGENT_DIR_PICKER` overrides the backend entirely: the value is run as a
/// shell command and its stdout is taken as the chosen path. That exists so the
/// HTTP layer can be tested without a desktop, and so unusual environments can
/// plug in their own picker (e.g. a terminal file browser over SSH).
DirectoryPickResult pick_directory(const std::string& initial_dir, const std::string& title,
                                   int timeout_ms);

/// True when some picker could plausibly run here (used to hide the button).
bool directory_picker_available();

}  // namespace tea
