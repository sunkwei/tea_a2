// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// errors.cpp -- the catalog itself.
//
// One entry per (code, status) pair. The wording is for a **caller**: what
// happened, and what to do about it. The code is the stable part; the message
// may be reworded.

#include "errors.h"

#include <algorithm>

namespace tea {

namespace {

std::vector<ErrorInfo> build_catalog() {
  std::vector<ErrorInfo> entries;
  entries.push_back({"asr_not_configured", 400, false, 0,
                      "there is no speech recognition configured on this machine",
                      "add an `asr:` section to provider.yaml (see docs/api.md)"});
  entries.push_back({"bad_export", 400, false, 0,
                      "the export cannot be built from this request",
                      "check the format and the ids in the request"});
  entries.push_back({"bad_image", 400, false, 0,
                      "the image could not be read (it is not a PNG, JPEG, GIF or WebP)",
                      "send the image as a data URL with the real bytes"});
  entries.push_back({"bad_json", 400, false, 0,
                      "the request body is not a JSON object",
                      "send a JSON object as the body"});
  entries.push_back({"bad_key", 400, false, 0,
                      "the key is not one this service knows",
                      "use a key you got from POST /api/projects"});
  entries.push_back({"bad_path", 400, false, 0,
                      "the path is not usable (it is relative, or it escapes the workspace)",
                      "send a path inside the workspace"});
  entries.push_back({"bad_request", 400, false, 0,
                      "a parameter is missing or has the wrong shape",
                      "read the message: it names the parameter"});
  entries.push_back({"fork_failed", 400, false, 0,
                      "the session could not be forked from that point",
                      "the message index must point at a user message of the session"});
  entries.push_back({"images_not_queued", 400, false, 0,
                      "a queued prompt cannot carry images",
                      "wait for the running turn to finish, then send the images"});
  entries.push_back({"invalid_config", 400, false, 0,
                      "the configuration was not accepted (the old one is still in use)",
                      "the message says what is wrong with the file"});
  entries.push_back({"invalid_mcp_config", 400, false, 0,
                      "the MCP configuration was not accepted (the running servers are untouched)",
                      "the message says what is wrong with mcp.yaml"});
  entries.push_back({"list_failed", 400, false, 0,
                      "the list could not be built",
                      "check the filters in the request"});
  entries.push_back({"mcp_not_configured", 400, false, 0,
                      "no MCP server is configured on this machine",
                      "add an `mcp:` section to provider.yaml"});
  entries.push_back({"missing_key", 400, false, 0,
                      "the request needs a key but none was sent",
                      "send `Authorization: Bearer <key>` (or X-Api-Key)"});
  entries.push_back({"not_a_directory", 400, false, 0,
                      "the path is not a directory",
                      "point at a directory, not at a file"});
  entries.push_back({"not_confirmed", 400, false, 0,
                      "this action needs a confirmation that was not given",
                      "confirm the action first, then do it"});
  entries.push_back({"no_web_tools", 400, false, 0,
                      "this provider offers no web tools, so there is nothing to call",
                      "use a provider that declares supports_search or supports_fetch"});
  entries.push_back({"patch_failed", 400, false, 0,
                      "the change could not be applied",
                      "the message says what went wrong"});
  entries.push_back({"read_failed", 400, false, 0,
                      "the file could not be read",
                      "check the path and the permissions"});
  entries.push_back({"session_error", 400, false, 0,
                      "the session could not be created or changed",
                      "the message says what went wrong"});
  entries.push_back({"settings_error", 400, false, 0,
                      "the settings could not be applied",
                      "the message says what is wrong with the values"});
  entries.push_back({"shell_unavailable", 400, false, 0,
                      "the web terminal is not available on this machine",
                      "the shell needs a host with a pty"});
  entries.push_back({"too_many_images", 400, false, 0,
                      "too many images in one message",
                      "at most four images per message"});
  entries.push_back({"too_many_models", 400, false, 0,
                      "too many models were sent in one request",
                      "the limit is in the message"});
  entries.push_back({"trajectory_error", 400, false, 0,
                      "the trajectory could not be read",
                      "check the turn id and the workspace"});
  entries.push_back({"gc_failed", 500, false, 0,
                      "the history cleaner could not do its work",
                      "the message says what went wrong; the history is untouched"});
  entries.push_back({"unknown_provider", 400, false, 0,
                      "no provider with that name is configured",
                      "ask GET /api/providers for the names"});
  entries.push_back({"unsupported_provider", 400, false, 0,
                      "this provider cannot be used for that",
                      "the message says what it cannot do"});
  entries.push_back({"usage_not_supported", 400, false, 0,
                      "this provider does not report a quota",
                      "the provider has to declare supports_usage"});
  entries.push_back({"vision_unsupported", 400, false, 0,
                      "the model cannot see images",
                      "pick a model that declares supports_vision"});
  entries.push_back({"workspace_error", 400, false, 0,
                      "the workspace could not be registered",
                      "the message says why (path, permissions, index)"});
  entries.push_back({"write_failed", 400, false, 0,
                      "the file could not be written",
                      "check the path and the permissions"});
  entries.push_back({"unauthorized", 401, false, 0,
                      "the request needs a valid key",
                      "send `Authorization: Bearer <key>`"});
  entries.push_back({"forbidden", 403, false, 0,
                      "this key does not reach that project",
                      "ask for a key of that project"});
  entries.push_back({"config_missing", 404, false, 0,
                      "there is no provider.yaml on this machine",
                      "configure one in the web UI or upload it"});
  entries.push_back({"no_active_turn", 404, false, 0,
                      "this session has no turn to talk to",
                      "the turn has finished; start a new one"});
  entries.push_back({"not_found", 404, false, 0,
                      "there is nothing at that address",
                      "the message names what was looked for"});
  entries.push_back({"unknown_turn", 404, false, 0,
                      "no turn with this id is known here",
                      "the id may be from another machine, or the handle was dropped"});
  entries.push_back({"config_missing", 409, false, 0,
                      "there is no provider.yaml, so there is nothing to replace",
                      "configure one first"});
  entries.push_back({"fixed_by_cli", 409, false, 0,
                      "this is set on the command line and cannot be changed here",
                      "change it in the service unit and restart"});
  entries.push_back({"idempotency_conflict", 409, false, 0,
                      "this idempotency key was already used for a different request",
                      "use a new key, or send exactly the request the key was used for"});
  entries.push_back({"images_not_queued", 409, false, 0,
                      "a queued prompt cannot carry images",
                      "wait for the running turn to finish, then send the images"});
  entries.push_back({"no_active_turn", 409, false, 0,
                      "there is no running turn to queue this prompt for",
                      "the turn has finished; send the prompt again as a new turn"});
  entries.push_back({"no_api_key", 409, false, 0,
                      "there is no API key for this provider yet",
                      "set the key in the web UI (or in provider.yaml)"});
  entries.push_back({"resize_failed", 409, false, 0,
                      "the terminal could not be resized",
                      "the message says why"});
  entries.push_back({"turn_running", 409, true, 5,
                      "a turn is already running for this session",
                      "wait for it to finish, or send {\"interrupt\": true}"});
  entries.push_back({"turn_still_running", 409, true, 5,
                      "the running turn did not stop within the time allowed",
                      "it is probably inside a long tool call; stop it and retry"});
  entries.push_back({"write_failed", 409, false, 0,
                      "the file could not be written (something else holds it)",
                      "the message says why; retry after the other writer is done"});
  entries.push_back({"image_too_large", 413, false, 0,
                      "the image is larger than the limit",
                      "at most 8 MB per image"});
  entries.push_back({"too_large", 413, false, 0,
                      "the request is larger than the limit",
                      "the limit is in the message"});
  entries.push_back({"too_many_subscribers", 429, true, 5,
                      "too many clients are following this turn",
                      "close one of them and try again"});
  entries.push_back({"too_many_turns", 429, true, 5,
                      "this server is already running as many turns as it allows",
                      "wait for one to finish, then try again"});
  entries.push_back({"archive_failed", 500, false, 0,
                      "the archive could not be written",
                      "check the path and the free space"});
  entries.push_back({"internal_error", 500, true, 0,
                      "something went wrong that was not supposed to",
                      "try again; if it keeps happening, the log has the details"});
  entries.push_back({"io_error", 500, false, 0,
                      "the file system refused the operation",
                      "the message carries the operating system's own words"});
  entries.push_back({"no_assets", 500, false, 0,
                      "the embedded files are missing from this binary",
                      "this build is broken - build it again"});
  entries.push_back({"reload_failed", 500, false, 0,
                      "the new configuration could not be loaded (the old one is still in use)",
                      "the message says what is wrong with the file"});
  entries.push_back({"session_error", 500, false, 0,
                      "the session could not be written",
                      "the message carries the file system's own words"});
  entries.push_back({"asr_failed", 502, true, 10,
                      "the speech recognition service did not answer",
                      "try again, or check the asr: section"});
  entries.push_back({"mcp_call_failed", 502, true, 5,
                      "the MCP server did not answer the call",
                      "the message says what the server said"});
  entries.push_back({"models_failed", 502, true, 5,
                      "the provider did not answer the model list",
                      "try again, or look the model up by hand"});
  entries.push_back({"upstream_error", 502, true, 5,
                      "the provider answered with an error",
                      "the message carries the provider's own answer"});
  entries.push_back({"usage_failed", 502, true, 5,
                      "the provider's quota endpoint did not answer",
                      "the message carries what the provider said; the quota is unknown"});
  // Sorted by status, then by code: the reader can find a code by scrolling,
  // and the answer of GET /api/errors is stable.
  std::sort(entries.begin(), entries.end(), [](const ErrorInfo& a, const ErrorInfo& b) {
    if (a.http != b.http) return a.http < b.http;
    return a.code < b.code;
  });
  return entries;
}

}  // namespace

const std::vector<ErrorInfo>& error_catalog() {
  static const std::vector<ErrorInfo>* catalog = new std::vector<ErrorInfo>(build_catalog());
  return *catalog;
}

const ErrorInfo* error_info(const std::string& code, int http) {
  const std::vector<ErrorInfo>& all = error_catalog();
  for (std::size_t i = 0; i < all.size(); ++i) {
    if (all[i].code == code && all[i].http == http) return &all[i];
  }
  return nullptr;
}

}  // namespace tea
