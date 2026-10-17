#include "env.h"

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <thread>

#include "log.h"
#include "util.h"

#ifdef _WIN32
#include <winsock2.h>
#include <windows.h>
#else
#include <sys/statvfs.h>
#include <sys/utsname.h>
#include <unistd.h>
#endif

namespace tea {
namespace {

#ifdef _WIN32

// RtlGetVersion is the only reliable way to read the real OS build (the
// documented GetVersionEx lies for manifests targeting older Windows).
struct TeaOsVersionInfo {
  ULONG size;
  ULONG major;
  ULONG minor;
  ULONG build;
  ULONG platform_id;
  wchar_t csd[128];
};
using RtlGetVersionFn = LONG(WINAPI*)(TeaOsVersionInfo*);

std::string windows_version() {
  TeaOsVersionInfo info;
  std::memset(&info, 0, sizeof(info));
  info.size = sizeof(info);
  HMODULE ntdll = GetModuleHandleW(L"ntdll.dll");
  if (ntdll) {
    RtlGetVersionFn fn = reinterpret_cast<RtlGetVersionFn>(
        reinterpret_cast<void*>(GetProcAddress(ntdll, "RtlGetVersion")));
    if (fn && fn(&info) == 0) {
      char buf[64];
      std::snprintf(buf, sizeof(buf), "%lu.%lu.%lu", info.major, info.minor, info.build);
      return buf;
    }
  }
  return std::string();
}

std::string windows_arch() {
  SYSTEM_INFO si;
  std::memset(&si, 0, sizeof(si));
  GetNativeSystemInfo(&si);
  switch (si.wProcessorArchitecture) {
    case PROCESSOR_ARCHITECTURE_AMD64: return "x86_64";
    case PROCESSOR_ARCHITECTURE_ARM64: return "aarch64";
    case PROCESSOR_ARCHITECTURE_ARM: return "arm";
    case PROCESSOR_ARCHITECTURE_INTEL: return "x86";
    default: return "unknown";
  }
}

std::string windows_hostname() {
  wchar_t buf[256];
  DWORD size = static_cast<DWORD>(sizeof(buf) / sizeof(buf[0]));
  if (GetComputerNameW(buf, &size)) return wide_to_utf8(std::wstring(buf, size));
  return std::string();
}

std::string windows_timezone() {
  TIME_ZONE_INFORMATION tzi;
  std::memset(&tzi, 0, sizeof(tzi));
  const DWORD rc = GetTimeZoneInformation(&tzi);
  long bias = tzi.Bias;
  if (rc == TIME_ZONE_ID_DAYLIGHT) {
    bias += tzi.DaylightBias;
  } else {
    bias += tzi.StandardBias;
  }
  // Windows Bias is "UTC = local + Bias", so the UTC offset is its negation.
  const long offset_minutes = -bias;
  const char sign = offset_minutes < 0 ? '-' : '+';
  const long abs_minutes = offset_minutes < 0 ? -offset_minutes : offset_minutes;
  char buf[16];
  std::snprintf(buf, sizeof(buf), "UTC%c%02ld:%02ld", sign, abs_minutes / 60, abs_minutes % 60);
  return buf;
}

uint64_t windows_memory_total() {
  MEMORYSTATUSEX status;
  std::memset(&status, 0, sizeof(status));
  status.dwLength = sizeof(status);
  if (GlobalMemoryStatusEx(&status)) return static_cast<uint64_t>(status.ullTotalPhys);
  return 0;
}

std::vector<std::string> path_entries() {
  std::vector<std::string> out;
  const char* path = std::getenv("PATH");
  if (!path) return out;
  out = split(path, ';');
  return out;
}

bool file_exists(const std::string& p) { return path_is_file(p); }

#else  // ---------------------------------------------------------------------

std::string posix_os_name() {
  // Prefer the distro description so an embedded board reads e.g. "Buildroot".
  std::string text;
  if (path_is_file("/etc/os-release") && read_file("/etc/os-release", &text, 8192)) {
    for (const std::string& line : split_lines(text)) {
      if (starts_with(line, "PRETTY_NAME=")) {
        std::string v = line.substr(std::strlen("PRETTY_NAME="));
        if (v.size() >= 2 && v.front() == '"' && v.back() == '"') {
          v = v.substr(1, v.size() - 2);
        }
        if (!v.empty()) return v;
      }
    }
  }
  struct utsname u;
  if (uname(&u) == 0) return std::string(u.sysname);
  return "Unix";
}

std::string posix_kernel() {
  struct utsname u;
  if (uname(&u) == 0) return std::string(u.release);
  return std::string();
}

std::string posix_arch() {
  struct utsname u;
  if (uname(&u) == 0) return std::string(u.machine);
  return "unknown";
}

std::string posix_hostname() {
  char buf[256];
  if (gethostname(buf, sizeof(buf)) == 0) {
    buf[sizeof(buf) - 1] = '\0';
    return std::string(buf);
  }
  return std::string();
}

std::string posix_timezone() {
  const std::time_t now = std::time(nullptr);
  std::tm tmv{};
  if (localtime_r(&now, &tmv) == nullptr) return std::string();
  char buf[16];
  if (std::strftime(buf, sizeof(buf), "%z", &tmv) == 0) return std::string();
  // %z gives "+0800"; render it as "UTC+08:00".
  const std::string raw(buf);
  if (raw.size() < 5) return raw;
  return "UTC" + raw.substr(0, 3) + ":" + raw.substr(3, 2);
}

uint64_t posix_memory_total() {
  const long pages = sysconf(_SC_PHYS_PAGES);
  const long page_size = sysconf(_SC_PAGESIZE);
  if (pages <= 0 || page_size <= 0) return 0;
  return static_cast<uint64_t>(pages) * static_cast<uint64_t>(page_size);
}

std::vector<std::string> path_entries() {
  std::vector<std::string> out;
  const char* path = std::getenv("PATH");
  if (!path) return out;
  out = split(path, ':');
  return out;
}

bool file_exists(const std::string& p) { return path_is_file(p); }

#endif

const char* const kShellNames[] = {"bash", "sh", "zsh", "powershell", "pwsh", "cmd"};

ShellInfo find_shell(const std::string& name, const std::vector<std::string>& dirs) {
  ShellInfo info;
  info.name = name;
#ifdef _WIN32
  const std::string exe = name + ".exe";
#else
  const std::string exe = name;
#endif
  for (const std::string& dir : dirs) {
    if (dir.empty()) continue;
    const std::string candidate = path_join(dir, exe);
    if (file_exists(candidate)) {
      info.path = candidate;
      return info;
    }
  }
  return info;
}

EnvironmentInfo probe_machine() {
  EnvironmentInfo env;
  const std::vector<std::string> dirs = path_entries();

#ifdef _WIN32
  env.os_name = "Windows";
  env.os_version = windows_version();
  env.arch = windows_arch();
  env.hostname = windows_hostname();
  env.timezone = windows_timezone();
  env.memory_total_bytes = windows_memory_total();
  env.default_shell = "cmd";
#else
  env.os_name = posix_os_name();
  env.os_version = posix_kernel();
  env.arch = posix_arch();
  env.hostname = posix_hostname();
  env.timezone = posix_timezone();
  env.memory_total_bytes = posix_memory_total();
  env.default_shell = "sh";
#endif

  env.cpu_count = static_cast<int>(std::thread::hardware_concurrency());

  for (const char* name : kShellNames) {
    ShellInfo found = find_shell(name, dirs);
    if (found.path.empty()) continue;
    bool duplicate = false;
    for (const ShellInfo& existing : env.shells) {
      if (existing.name == found.name) duplicate = true;
    }
    if (!duplicate) env.shells.push_back(found);
  }

  LOG_INFO("env: ", env.os_name, " ", env.os_version, " (", env.arch, ") host=", env.hostname,
           " tz=", env.timezone, " cpus=", env.cpu_count, " mem=",
           env.memory_total_bytes ? binary_size(env.memory_total_bytes) : "?",
           " shells=", env.shells.size());
  return env;
}

}  // namespace

const EnvironmentInfo& machine_environment() {
  static const EnvironmentInfo env = probe_machine();
  return env;
}

uint64_t free_space_bytes(const std::string& path) {
  if (path.empty()) return 0;
#ifdef _WIN32
  ULARGE_INTEGER available;
  available.QuadPart = 0;
  if (!GetDiskFreeSpaceExW(utf8_to_wide(path).c_str(), &available, nullptr, nullptr)) return 0;
  return static_cast<uint64_t>(available.QuadPart);
#else
  struct statvfs vfs;
  if (statvfs(path.c_str(), &vfs) != 0) return 0;
  return static_cast<uint64_t>(vfs.f_bavail) * static_cast<uint64_t>(vfs.f_frsize);
#endif
}

std::string binary_size(uint64_t bytes) {
  static const char* units[] = {"B", "KiB", "MiB", "GiB", "TiB"};
  double v = static_cast<double>(bytes);
  int u = 0;
  while (v >= 1024.0 && u < 4) {
    v /= 1024.0;
    ++u;
  }
  char buf[48];
  if (u == 0) {
    std::snprintf(buf, sizeof(buf), "%llu B", static_cast<unsigned long long>(bytes));
  } else {
    std::snprintf(buf, sizeof(buf), "%.1f %s", v, units[u]);
  }
  return buf;
}

}  // namespace tea
