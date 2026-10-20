// html_export.h -- turn a session into one self-contained HTML file.
//
// The file has to outlive the machine it was exported from: no server, no
// network, no external CSS/JS/fonts, pictures inlined as data: URLs. Opening it
// from a USB stick years later must show the same conversation.
//
// It also carries the session as JSON in a <script type="application/json">
// block, so the conversation can be restored (POST /api/workspaces/:id/
// sessions/import) instead of only looked at.
#pragma once

#include <map>
#include <string>

#include <nlohmann/json.hpp>

#include "message.h"
#include "workspace.h"

namespace tea {

struct ExportLimits {
  /// Per tool result. A build log can be megabytes; the export is a record of
  /// the conversation, not a backup of every byte a command printed.
  std::size_t tool_output_bytes = 8u * 1024u;
  /// Per inlined image, and for all images together. Past these the picture is
  /// replaced by a caption, so one screenshot-heavy session cannot produce a
  /// gigabyte file.
  std::size_t image_bytes = 8u * 1024u * 1024u;
  std::size_t total_image_bytes = 40u * 1024u * 1024u;
  /// Extra base64 bytes the embedded JSON may carry so an import on another
  /// device gets the pictures too. It is a second copy of the same bytes; past
  /// this the payload records `images_omitted` instead of doubling the file.
  std::size_t json_image_bytes = 16u * 1024u * 1024u;
};

/// Render `session` as a standalone HTML document. `version` is stamped in the
/// header ("tea_agent 0.7.0") so a reader knows what wrote the file.
///
/// Images are inlined as data: URLs so the file renders with no sidecar assets
/// and no network. `image_data` (optional) receives the same data: URLs keyed by
/// workspace path, up to `limits.json_image_bytes` extra bytes, so the embedded
/// JSON block can restore the pictures into another workspace on import.
std::string export_session_html(const Workspace& workspace, const Session& session,
                                const std::string& version,
                                const ExportLimits& limits = ExportLimits(),
                                std::map<std::string, std::string>* image_data = nullptr);

/// The JSON payload embedded in the HTML (and accepted by the import route).
/// `image_data` may carry data: URLs for the attachments; `images_omitted` is
/// set when they did not fit the budget.
nlohmann::json export_session_json(const Workspace& workspace, const Session& session,
                                   const std::string& version,
                                   const std::map<std::string, std::string>* image_data = nullptr,
                                   bool images_omitted = false);

/// Pull the `tea-session-data` JSON back out of an exported HTML file.
/// Returns false when the marker is absent or the JSON does not parse.
bool extract_session_json_from_html(const std::string& html, nlohmann::json* out,
                                    std::string* err);

/// Restore a payload (as produced by export_session_json, or extracted from an
/// exported HTML file) into `session`. A fresh id and timestamps are generated;
/// image data URLs are written into the workspace's artifacts directory and the
/// message paths rewritten, so the imported conversation shows its pictures.
bool import_session_payload(const Workspace& workspace, const nlohmann::json& payload,
                            Session* out, std::string* err);

/// A download name that survives every browser, shell and filesystem:
/// ASCII only, no separators, plus a UTC stamp so two exports do not collide.
std::string export_file_name(const std::string& session_title);

/// Minimal Markdown -> HTML, mirroring what the built-in UI renders: fenced
/// code blocks (with language label), headings, lists, blockquotes, tables,
/// horizontal rules, inline code, links, bold/italic/strikethrough. Everything
/// else is escaped, never passed through.
std::string markdown_to_html(const std::string& source);

}  // namespace tea
