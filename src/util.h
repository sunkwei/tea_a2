// util.h -- small cross-platform helpers shared by every module.
//
// Paths and file I/O are implemented directly on top of the platform APIs
// (Win32 wide-character calls on Windows, POSIX calls elsewhere) rather than
// std::filesystem:
//   * every std::string in this program is UTF-8, including paths, which is not
//     what std::filesystem's narrow conversions give you on Windows;
//   * it keeps the code buildable with GCC 6.5, which only has
//     std::experimental::filesystem.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace tea {

// --------------------------------------------------------------------------
// time
// --------------------------------------------------------------------------
std::string now_iso8601();       // UTC, second precision: 2026-01-01T00:00:00Z
std::string now_iso8601_ms();    // UTC, millisecond precision
std::string iso8601_from_unix(int64_t unix_seconds);
int64_t now_ms();
std::string format_duration_ms(int64_t ms);

// --------------------------------------------------------------------------
// strings
// --------------------------------------------------------------------------
std::string to_lower(std::string s);
std::string trim(const std::string& s);
bool starts_with(const std::string& s, const std::string& prefix);
bool ends_with(const std::string& s, const std::string& suffix);
bool contains(const std::string& s, const std::string& needle);
std::string replace_all(std::string s, const std::string& from, const std::string& to);
std::vector<std::string> split(const std::string& s, char delim);
std::vector<std::string> split_lines(const std::string& s);
std::string join(const std::vector<std::string>& parts, const std::string& sep);

/// Truncate to at most `max_bytes` without splitting a UTF-8 sequence.
std::string truncate_utf8(const std::string& s, size_t max_bytes, bool* truncated = nullptr);
/// Drop a trailing incomplete UTF-8 sequence (and, with `head`, a leading one).
/// A file cut in the middle of a multi-byte character would make the whole
/// tool result invalid UTF-8 - and the JSON layer refuses to serialise that,
/// which crashed the agent on a file whose size happened to land mid-character.
void trim_utf8_edges(std::string* s, bool head = false);
size_t utf8_length(const std::string& s);
bool is_valid_utf8(const std::string& s);
/// Replace every byte sequence that is not valid UTF-8 with U+FFFD.
///
/// The world outside is not UTF-8: a binary file, a command that prints raw
/// bytes, a file name in a Latin-1 directory, a GBK-encoded source file. The
/// JSON parser accepts such bytes inside a string, but the serialiser refuses
/// them ("invalid UTF-8 byte at index N"), and that used to kill the whole
/// turn. Sanitising at the source keeps the model request, the session file
/// and the event stream valid UTF-8 instead of merely survivable.
std::string sanitize_utf8(const std::string& s);
std::string human_size(uint64_t bytes);

/// Show only enough of a secret to identify it: "sk-94e...a84b".
std::string mask_secret(const std::string& secret);

/// Normalize a base URL: trimmed, without the trailing '/', and only
/// http:// or https://. Returns "" when the URL cannot be used as a base
/// (empty, or a scheme this build cannot talk to).
std::string normalize_base_url(const std::string& url);

// --------------------------------------------------------------------------
// images
// --------------------------------------------------------------------------
/// Identify an image by its magic bytes rather than by its name: the value ends
/// up in a data: URL the provider parses, and the extension decides how /raw
/// serves it back. Returns nullptr for anything that is not PNG/JPEG/GIF/WebP.
const char* sniff_image_mime(const std::string& bytes);
/// ".png" / ".jpg" / ".gif" / ".webp" for a sniffed MIME type.
std::string image_extension_for(const std::string& mime);

// --------------------------------------------------------------------------
// base64 (used for data: URLs carrying pasted images)
// --------------------------------------------------------------------------
std::string base64_encode(const std::string& bytes);
/// Strict decode: ignores ASCII whitespace and newlines, rejects anything else
/// that is not in the base64 alphabet. Returns false and leaves `out` empty on
/// invalid input, so a malformed data: URL cannot become a truncated image.
bool base64_decode(const std::string& text, std::string* out);

// --------------------------------------------------------------------------
// ids
// --------------------------------------------------------------------------
std::string uuid4();
std::string random_hex(size_t nbytes);
std::string make_id(const std::string& prefix);

// --------------------------------------------------------------------------
// paths -- all strings are UTF-8
// --------------------------------------------------------------------------
std::string home_dir();
std::string tea_home_dir();                 // $HOME/.tea_agent
std::string executable_path();              // absolute path of this binary
std::string executable_dir();               // directory containing this binary
std::string expand_user(const std::string& p);
std::string path_join(const std::string& a, const std::string& b);
std::string path_filename(const std::string& p);
std::string path_parent(const std::string& p);
std::string path_extension(const std::string& p);   // lower-case, includes the dot
std::string path_stem(const std::string& p);
std::string path_absolute(const std::string& p);    // resolve against the cwd, normalise
std::string path_canonical(const std::string& p);   // path_absolute + symlink resolution
std::string path_generic(const std::string& p);     // backslashes -> forward slashes
bool path_is_absolute(const std::string& p);
bool path_exists(const std::string& p);
bool path_is_dir(const std::string& p);
bool path_is_file(const std::string& p);
bool make_dirs(const std::string& p);
bool path_is_within(const std::string& child, const std::string& parent);
uint64_t file_size(const std::string& p);
std::string file_mtime_iso(const std::string& p);

struct DirEntryInfo {
  std::string name;
  std::string path;  // absolute
  bool is_dir = false;
  uint64_t size = 0;
  std::string mtime;  // ISO-8601 UTC
};

/// List one directory level (hidden entries included, "." and ".." excluded).
/// Returns false when the directory cannot be read (err is set).
bool list_directory(const std::string& dir, std::vector<DirEntryInfo>* out, std::string* err);

// --------------------------------------------------------------------------
// files
// --------------------------------------------------------------------------
bool read_file(const std::string& path, std::string* out, size_t max_bytes = 0,
               bool* truncated = nullptr);
/// Read at most the last `max_bytes` of a file (used for log tails).
bool read_file_tail(const std::string& path, size_t max_bytes, std::string* out);
bool write_file(const std::string& path, const std::string& data, bool append = false,
                std::string* err = nullptr);
/// Write `data` to `path` so that a concurrent reader never sees a partial file:
/// the bytes go to `<path>.tmp-<pid>-<n>` next to the target and are renamed over
/// it. Use this for state a running process rewrites (session transcripts, the
/// session index, memory.json, settings.yaml) - a truncate-then-write there can be
/// observed half-written, and a crash in the middle loses the whole file.
bool write_file_atomic(const std::string& path, const std::string& data,
                       std::string* err = nullptr);
/// Overwrite the file from `offset` with `data` and truncate whatever followed.
/// Lets an append-only log refresh its newest line in place (the trajectory
/// writer does exactly that) instead of rewriting the whole file every step.
bool write_file_at(const std::string& path, uint64_t offset, const std::string& data);
bool remove_path(const std::string& path, bool recursive, std::string* err = nullptr);
bool move_path(const std::string& from, const std::string& to, std::string* err = nullptr);

/// Append-only file handle. Uses the platform API so non-ASCII paths work.
class AppendFile {
 public:
  AppendFile() {}
  ~AppendFile();
  AppendFile(const AppendFile&) = delete;
  AppendFile& operator=(const AppendFile&) = delete;
  AppendFile(AppendFile&& other) noexcept;
  AppendFile& operator=(AppendFile&& other) noexcept;

  bool open(const std::string& path, bool truncate = false);
  bool is_open() const { return open_; }
  bool write(const std::string& data);
  void close();
  uint64_t size() const;
  const std::string& path() const { return path_; }

 private:
  std::intptr_t handle_ = -1;
  bool open_ = false;
  std::string path_;
  uint64_t base_size_ = 0;  // size when the file was opened
  uint64_t written_ = 0;    // bytes appended by this handle
};

// --------------------------------------------------------------------------
// platform / encoding
// --------------------------------------------------------------------------
std::wstring utf8_to_wide(const std::string& s);
std::string wide_to_utf8(const std::wstring& s);
std::string acp_to_utf8(const std::string& s);        // Windows ANSI/OEM page -> UTF-8
std::string native_to_utf8(const std::string& s);     // UTF-8 passthrough, else acp_to_utf8
std::string json_escape(const std::string& s);

/// Serialise JSON without ever throwing on a byte that is not valid UTF-8.
///
/// nlohmann's default is `strict`: one byte that is not valid UTF-8 anywhere
/// in the document aborts the whole serialisation with a type_error. That
/// turned "a file contains one odd byte" into "the agent crashed". `replace`
/// writes U+FFFD instead, so a document that carries text from the outside
/// world can always be serialised.
std::string dump_json(const nlohmann::json& j, int indent = -1);

/// Escape a string so a terminal does not interpret control sequences.
std::string sanitize_for_terminal(const std::string& s);

/// Describe the last OS-level failure: "Windows error 5: Access is denied" on
/// Windows, strerror(errno) elsewhere. Call it immediately after the failing
/// API call.
std::string last_os_error();

}  // namespace tea
