// env.h -- probe the machine the agent is actually running on.
//
// The system prompt must describe the real device, not a hard-coded guess: the
// whole point of tea_agent is to be deployed on embedded boards whose OS,
// architecture, time zone, resources and available shells differ from the build
// host. Everything here is probed once per process (and, for the workspace
// volume, once per workspace) so the rendered prompt stays byte-stable and does
// not destroy prompt-cache prefixes.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

namespace tea {

struct ShellInfo {
  std::string name;  // "cmd", "powershell", "pwsh", "bash", "sh"
  std::string path;  // absolute path when found
};

struct EnvironmentInfo {
  std::string os_name;     // "Windows", "Ubuntu 24.04", "macOS"
  std::string os_version;  // build number / kernel release
  std::string arch;        // "x86_64", "aarch64", "armv7l"
  std::string hostname;
  std::string timezone;  // "UTC+08:00"
  int cpu_count = 0;
  uint64_t memory_total_bytes = 0;
  std::vector<ShellInfo> shells;  // discovered shells, most preferred first
  std::string default_shell;      // name of the shell the tool will use
};

/// Machine-level facts, probed once and cached for the process lifetime.
const EnvironmentInfo& machine_environment();

/// The machine's usable IPv4 addresses (loopback excluded), most useful first:
/// the address that carries the default route comes first, which is the one a
/// browser on another device on the same LAN can actually open. Empty when the
/// device has no network at all. Probed once per process and cached, so every
/// URL derived from it stays consistent.
std::vector<std::string> local_ipv4_addresses();

/// Free bytes on the volume holding `path` (0 when unknown). Cheap enough to
/// call per workspace open; the value is then kept for the process lifetime.
uint64_t free_space_bytes(const std::string& path);

/// Human readable size, e.g. "63.8 GiB". util.h's human_size uses decimal SI
/// units; this one uses binary units, which is what OSes report.
std::string binary_size(uint64_t bytes);

}  // namespace tea
