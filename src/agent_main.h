// Copyright (c) 2026 sunkwei
// SPDX-License-Identifier: GPL-3.0-or-later
// agent_main.h -- the agent's entry point, callable without a `main()`.
//
// The CLI is `main()` in main.cpp. The Android build is the same code loaded
// as a shared library (liba2.so): the APK calls tea_agent_main() from JNI on
// a background thread and asks the agent to stop with agent_request_stop().
#pragma once

namespace tea {

/// Run the agent (this is what `main()` does). Blocks until the agent stops.
/// Returns the process exit code.
int tea_agent_main(int argc, char** argv);

/// Ask a running agent to stop. Safe to call from another thread.
void agent_request_stop();

}  // namespace tea
