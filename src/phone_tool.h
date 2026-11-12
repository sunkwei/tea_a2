// phone_tool.h -- the phone bridge tool (Android builds only).
#pragma once

#include <nlohmann/json.hpp>

#include "tools.h"

namespace tea {

/// Ask the phone what only the phone can do: the notifications that came in,
/// where it is, what is on the clipboard, a screenshot, opening an app.
///
/// The bridge is the Android app's own HTTP API on the loopback interface
/// (the app and the agent share one process). This tool is a thin client for
/// it; the Android side owns every permission and every Android API.
ToolResult tool_phone(const nlohmann::json& args, const ToolContext& ctx);

}  // namespace tea
