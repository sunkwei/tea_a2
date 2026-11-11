// tunnel_paths.h -- where the tunnel programs keep their files.
//
// The broker and the sidecar both live in a home directory that may be given
// explicitly (TEA_AGENT_HOME), so a machine without a writable $HOME (a
// device with a read-only /root) can still be given a place to work.
//
// The paths here are the same ones the agent uses (~/.tea_agent/...), so the
// operator finds the token and the broker certificate where they expect them.
#pragma once

#include <cstdio>
#include <string>

#ifdef _WIN32
#include <direct.h>
#include <io.h>
#else
#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#endif

namespace tea_tunnel {

/// The home directory the tunnel programs work in.
inline std::string agent_home() {
  const char* env = std::getenv("TEA_AGENT_HOME");
  if (env && *env) return std::string(env);
#ifdef _WIN32
  const char* up = std::getenv("USERPROFILE");
  if (up && *up) return std::string(up);
#else
  const char* home = std::getenv("HOME");
  if (home && *home) return std::string(home);
#endif
  return std::string(".");
}

/// <home>/.tea_agent/tunnel - the directory the room token and the broker
/// certificate live in.
inline std::string tunnel_dir() {
  std::string path = agent_home();
  if (!path.empty() && path.back() != '/' && path.back() != '\\') path += '/';
  path += ".tea_agent/tunnel";
  return path;
}

/// Create a directory (and its parents). Returns true when it exists
/// afterwards.
inline bool ensure_directory(const std::string& path) {
  if (path.empty()) return false;
  std::string partial;
  partial.reserve(path.size());
  for (std::size_t i = 0; i <= path.size(); ++i) {
    if (i == path.size() || path[i] == '/' || path[i] == '\\') {
      if (!partial.empty() && partial != "." ) {
#ifdef _WIN32
        _mkdir(partial.c_str());
#else
        mkdir(partial.c_str(), 0700);
#endif
      }
    }
    if (i < path.size()) partial.push_back(path[i]);
  }
#ifdef _WIN32
  const bool ok = _access(path.c_str(), 0) == 0;
#else
  const bool ok = access(path.c_str(), F_OK) == 0;
#endif
  return ok;
}

/// Read a whole file. Returns false when it cannot be read.
inline bool read_whole_file(const std::string& path, std::string* out) {
  std::FILE* f = std::fopen(path.c_str(), "rb");
  if (!f) return false;
  out->clear();
  char buf[4096];
  size_t n;
  while ((n = std::fread(buf, 1, sizeof(buf), f)) > 0) out->append(buf, n);
  std::fclose(f);
  return true;
}

/// Write a file that only the owner can read (a private key is in it).
inline bool write_private_file(const std::string& path, const std::string& data) {
#ifdef _WIN32
  std::FILE* f = std::fopen(path.c_str(), "wb");
  if (!f) return false;
  const size_t n = std::fwrite(data.data(), 1, data.size(), f);
  std::fclose(f);
  return n == data.size();
#else
  const int fd = ::open(path.c_str(), O_WRONLY | O_CREAT | O_TRUNC, 0600);
  if (fd < 0) return false;
  size_t written = 0;
  while (written < data.size()) {
    const ssize_t n = ::write(fd, data.data() + written, data.size() - written);
    if (n <= 0) {
      ::close(fd);
      return false;
    }
    written += static_cast<size_t>(n);
  }
  ::close(fd);
  return true;
#endif
}

}  // namespace tea_tunnel
