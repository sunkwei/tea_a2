// shell_win32.cpp -- the Windows half of the interactive shell.
//
// Windows gets a ConPTY (a "pseudo console"): conhost runs the program on a
// terminal that lives in a pipe, so PowerShell behaves exactly as it does in
// a console window - PSReadLine, colours, Ctrl+C, programs that ask the
// console for its size.
//
// The ConPTY entry points are loaded at runtime (GetProcAddress): a binary
// that imports them by name refuses to start on a Windows older than 10
// 1809, and this one has to keep starting there - it just says that the
// terminal is not available.
//
// The POSIX half lives in src/shell_posix.cpp (pty + bash).
#ifdef _WIN32

#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>

#include <processthreadsapi.h>

#include <chrono>
#include <cstdlib>
#include <string>
#include <thread>
#include <vector>

#include "log.h"
#include "shell.h"
#include "util.h"

namespace tea {

namespace {

/// A file that can really be run. The Store's "app execution aliases"
/// (the pwsh.exe in WindowsApps) are reparse points that start a packaged app
/// in a world of its own: such a program never sees the pseudo console, so it
/// is not a shell we can drive.
bool is_runnable_file(const std::string& path) {
  const DWORD attrs = GetFileAttributesA(path.c_str());
  if (attrs == INVALID_FILE_ATTRIBUTES) return false;
  if (attrs & FILE_ATTRIBUTE_REPARSE_POINT) return false;
  return true;
}

/// Look for an executable in the PATH.
bool found_in_path(const std::string& name) {
  if (name.find('\\') != std::string::npos || name.find('/') != std::string::npos) {
    return is_runnable_file(name);
  }
  const char* path = std::getenv("PATH");
  if (!path) return false;
  std::string all(path);
  std::size_t start = 0;
  while (start <= all.size()) {
    const std::size_t end = all.find(';', start);
    const std::string dir = all.substr(start, end == std::string::npos ? std::string::npos
                                                                        : end - start);
    if (!dir.empty()) {
      const std::string full = dir + "\\" + name;
      if (is_runnable_file(full)) return true;
    }
    if (end == std::string::npos) break;
    start = end + 1;
  }
  return false;
}

/// The base name of a path, lower-cased.
std::string base_name(const std::string& path) {
  const std::size_t slash = path.find_last_of("/\\");
  const std::string name = slash == std::string::npos ? path : path.substr(slash + 1);
  return to_lower(name);
}

}  // namespace

namespace shell_detail {

const char* platform_name() { return "windows"; }

const char* backend_name() { return "conpty"; }

std::string default_shell() {
  // The operator can pick the program; otherwise PowerShell: PowerShell 7
  // (pwsh) when it is installed, Windows PowerShell 5.1 otherwise.
  const char* env = std::getenv("TEA_AGENT_SHELL_BIN");
  if (env && *env) return env;
  if (found_in_path("pwsh.exe")) return "pwsh.exe";
  return "powershell.exe";
}

}  // namespace shell_detail

namespace {

// The ConPTY entry points, loaded at runtime.
typedef HRESULT(WINAPI* CreatePseudoConsoleFn)(COORD, HANDLE, HANDLE, DWORD, void**);
typedef HRESULT(WINAPI* ResizePseudoConsoleFn)(void*, COORD);
typedef void(WINAPI* ClosePseudoConsoleFn)(void*);

struct ConPtyApi {
  CreatePseudoConsoleFn create = nullptr;
  ResizePseudoConsoleFn resize = nullptr;
  ClosePseudoConsoleFn close = nullptr;
  bool ok() const { return create && resize && close; }
};

const ConPtyApi& conpty_api() {
  static ConPtyApi api;
  static bool loaded = false;
  if (!loaded) {
    loaded = true;
    HMODULE kernel = GetModuleHandleW(L"kernel32.dll");
    if (kernel) {
      // The cast goes through void*: GetProcAddress returns a generic
      // function pointer, and every compiler complains about casting that
      // straight into a typed one.
      api.create = reinterpret_cast<CreatePseudoConsoleFn>(
          reinterpret_cast<void*>(GetProcAddress(kernel, "CreatePseudoConsole")));
      api.resize = reinterpret_cast<ResizePseudoConsoleFn>(
          reinterpret_cast<void*>(GetProcAddress(kernel, "ResizePseudoConsole")));
      api.close = reinterpret_cast<ClosePseudoConsoleFn>(
          reinterpret_cast<void*>(GetProcAddress(kernel, "ClosePseudoConsole")));
    }
  }
  return api;
}

/// The value of PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE. The SDK that ships with
/// this project predates the ConPTY, so the constant is spelled out here:
/// ProcThreadAttributeValue(22, FALSE, TRUE, FALSE) - number 22 with the
/// "input" flag.
const DWORD kPseudoConsoleAttribute = 0x20016;

/// A shell on a ConPTY.
class ConPtySession : public ShellSession {
 public:
  ~ConPtySession() override { close(); }

 protected:
  bool spawn(const std::string& shell, const std::string& cwd, int cols, int rows,
             std::string* err) override;
  bool send_raw(const std::string& data, std::string* err) override;
  bool set_size_raw(int cols, int rows, std::string* err) override;
  void terminate_raw() override;

 private:
  void read_loop();
  void collect_exit_code();
  std::wstring command_line(const std::string& shell) const;

  void* hpc_ = nullptr;        ///< the pseudo console
  HANDLE in_write_ = nullptr;  ///< the browser writes here, the pty reads
  HANDLE out_read_ = nullptr;  ///< the pty writes here, the browser reads
  HANDLE process_ = nullptr;
  DWORD process_id_ = 0;
  std::thread reader_;
  std::atomic<bool> stop_{false};
  std::atomic<bool> exited_{false};
};

std::wstring ConPtySession::command_line(const std::string& shell) const {
  const std::string name = base_name(shell);
  std::wstring line = utf8_to_wide(shell);
  // PowerShell: no banner, and no profile scripts - the terminal should come
  // up the same way on every machine.
  if (name == "powershell.exe" || name == "powershell" || name == "pwsh.exe" ||
      name == "pwsh") {
    line += L" -NoLogo -NoProfile";
  }
  return line;
}

bool ConPtySession::spawn(const std::string& shell, const std::string& cwd, int cols, int rows,
                          std::string* err) {
  if (shell.empty()) {
    *err = "no shell program was found on this machine";
    return false;
  }
  const ConPtyApi& api = conpty_api();
  if (!api.ok()) {
    *err = "this Windows has no ConPTY (Windows 10 1809 or newer is needed for the terminal)";
    return false;
  }

  SECURITY_ATTRIBUTES sa;
  sa.nLength = sizeof(sa);
  sa.lpSecurityDescriptor = nullptr;
  sa.bInheritHandle = FALSE;

  HANDLE in_read = nullptr;
  HANDLE in_write = nullptr;
  HANDLE out_read = nullptr;
  HANDLE out_write = nullptr;
  if (!CreatePipe(&in_read, &in_write, &sa, 0)) {
    *err = "cannot create the terminal's input pipe";
    return false;
  }
  if (!CreatePipe(&out_read, &out_write, &sa, 0)) {
    *err = "cannot create the terminal's output pipe";
    CloseHandle(in_read);
    CloseHandle(in_write);
    return false;
  }

  COORD size;
  size.X = static_cast<SHORT>(cols);
  size.Y = static_cast<SHORT>(rows);
  void* pc = nullptr;
  const HRESULT hr = api.create(size, in_read, out_write, 0, &pc);
  if (FAILED(hr) || pc == nullptr) {
    *err = "cannot create the pseudo console (error " + std::to_string(static_cast<long>(hr)) + ")";
    CloseHandle(in_read);
    CloseHandle(in_write);
    CloseHandle(out_read);
    CloseHandle(out_write);
    return false;
  }
  hpc_ = pc;
  in_write_ = in_write;
  out_read_ = out_read;

  SIZE_T attr_size = 0;
  InitializeProcThreadAttributeList(nullptr, 1, 0, &attr_size);
  std::vector<char> attr_storage(attr_size ? attr_size : 1);
  LPPROC_THREAD_ATTRIBUTE_LIST attrs =
      reinterpret_cast<LPPROC_THREAD_ATTRIBUTE_LIST>(attr_storage.data());
  if (!InitializeProcThreadAttributeList(attrs, 1, 0, &attr_size)) {
    *err = "cannot prepare the process attributes for the pseudo console";
    terminate_raw();
    return false;
  }
  if (!UpdateProcThreadAttribute(attrs, 0, static_cast<DWORD_PTR>(kPseudoConsoleAttribute), pc,
                                 sizeof(pc), nullptr, nullptr)) {
    DeleteProcThreadAttributeList(attrs);
    *err = "cannot attach the pseudo console to the process";
    terminate_raw();
    return false;
  }

  STARTUPINFOEXW startup;
  ZeroMemory(&startup, sizeof(startup));
  startup.StartupInfo.cb = sizeof(STARTUPINFOEXW);
  startup.lpAttributeList = attrs;
  // The standard handles are set to "none" on purpose: without this the
  // child would inherit *our* console's handles (the server usually runs in
  // one) and would write to it instead of to the pseudo console - PowerShell
  // does exactly that and then never sees the terminal at all. With them
  // cleared, the console the child is attached to provides the handles, so
  // the echo, the Ctrl+C event and the window size all work.
  startup.StartupInfo.dwFlags = EXTENDED_STARTUPINFO_PRESENT | STARTF_USESTDHANDLES;
  startup.StartupInfo.hStdInput = nullptr;
  startup.StartupInfo.hStdOutput = nullptr;
  startup.StartupInfo.hStdError = nullptr;

  PROCESS_INFORMATION pi;
  ZeroMemory(&pi, sizeof(pi));
  std::wstring cmd = command_line(shell);
  std::wstring cwd_wide;
  const wchar_t* cwd_ptr = nullptr;
  if (!cwd.empty()) {
    cwd_wide = utf8_to_wide(cwd);
    cwd_ptr = cwd_wide.c_str();
  }
  const BOOL created =
      CreateProcessW(nullptr, &cmd[0], nullptr, nullptr, FALSE, EXTENDED_STARTUPINFO_PRESENT,
                     nullptr, cwd_ptr, &startup.StartupInfo, &pi);
  DeleteProcThreadAttributeList(attrs);
  if (!created) {
    *err = "cannot start the shell: " +
           std::to_string(static_cast<long>(GetLastError()));
    terminate_raw();
    return false;
  }
  process_ = pi.hProcess;
  process_id_ = pi.dwProcessId;
  set_pid(static_cast<long>(process_id_));
  CloseHandle(pi.hThread);

  // The pseudo console has its own copies of these ends now.
  CloseHandle(in_read);
  CloseHandle(out_write);

  reader_ = std::thread([this]() { read_loop(); });
  return true;
}

void ConPtySession::read_loop() {
  char buffer[8192];
  while (!stop_.load()) {
    // The shell can be gone while the pseudo console still holds the output
    // pipe open (conhost keeps it until the console itself is closed), so the
    // process is checked too: otherwise the session would never end.
    if (process_ != nullptr && WaitForSingleObject(process_, 0) == WAIT_OBJECT_0) {
      // Give conhost a moment to flush the last output, then stop.
      for (int i = 0; i < 10; i++) {
        DWORD avail = 0;
        if (!PeekNamedPipe(out_read_, nullptr, 0, nullptr, &avail, nullptr)) break;
        if (!avail) {
          std::this_thread::sleep_for(std::chrono::milliseconds(50));
          continue;
        }
        DWORD got = 0;
        if (!ReadFile(out_read_, buffer, static_cast<DWORD>(sizeof(buffer)), &got, nullptr) ||
            got == 0) {
          break;
        }
        push_output(std::string(buffer, static_cast<std::size_t>(got)));
      }
      break;
    }
    DWORD avail = 0;
    // PeekNamedPipe (instead of a blocking ReadFile) keeps the loop
    // responsive: closing the session must not wait for a read that will
    // never return.
    if (!PeekNamedPipe(out_read_, nullptr, 0, nullptr, &avail, nullptr)) break;
    if (avail == 0) {
      std::this_thread::sleep_for(std::chrono::milliseconds(40));
      continue;
    }
    DWORD got = 0;
    if (!ReadFile(out_read_, buffer, static_cast<DWORD>(sizeof(buffer)), &got, nullptr)) break;
    if (got == 0) break;
    push_output(std::string(buffer, static_cast<std::size_t>(got)));
  }
  collect_exit_code();
}

void ConPtySession::collect_exit_code() {
  if (process_ == nullptr) return;
  // The shell usually exits right after the terminal is closed; give it a
  // moment before asking for its exit code.
  for (int i = 0; i < 50; ++i) {
    if (WaitForSingleObject(process_, 0) == WAIT_OBJECT_0) {
      DWORD code = 0;
      if (GetExitCodeProcess(process_, &code)) set_exit_code(static_cast<int>(code));
      exited_.store(true);
      mark_ended();
      return;
    }
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
  }
}

bool ConPtySession::send_raw(const std::string& data, std::string* err) {
  if (in_write_ == nullptr) {
    *err = "the terminal is closed";
    return false;
  }
  std::size_t sent = 0;
  while (sent < data.size()) {
    DWORD written = 0;
    if (!WriteFile(in_write_, data.data() + sent, static_cast<DWORD>(data.size() - sent),
                   &written, nullptr)) {
      *err = "cannot write to the terminal (error " +
             std::to_string(static_cast<long>(GetLastError())) + ")";
      return false;
    }
    if (written == 0) {
      *err = "the terminal is not accepting input";
      return false;
    }
    sent += static_cast<std::size_t>(written);
  }
  return true;
}

bool ConPtySession::set_size_raw(int cols, int rows, std::string* err) {
  if (hpc_ == nullptr) {
    *err = "the terminal is closed";
    return false;
  }
  COORD size;
  size.X = static_cast<SHORT>(cols);
  size.Y = static_cast<SHORT>(rows);
  const HRESULT hr = conpty_api().resize(hpc_, size);
  if (FAILED(hr)) {
    *err = "cannot resize the terminal (error " + std::to_string(static_cast<long>(hr)) + ")";
    return false;
  }
  return true;
}

void ConPtySession::terminate_raw() {
  stop_.store(true);
  if (hpc_ != nullptr) {
    // Closing the pseudo console ends the shell: it sees the console go
    // away and exits.
    conpty_api().close(hpc_);
    hpc_ = nullptr;
  }
  if (reader_.joinable()) reader_.join();
  if (process_ != nullptr) {
    if (WaitForSingleObject(process_, 200) == WAIT_TIMEOUT) {
      TerminateProcess(process_, 1);
      WaitForSingleObject(process_, 500);
    }
    if (!exited_.load()) collect_exit_code();
    CloseHandle(process_);
    process_ = nullptr;
  }
  if (in_write_ != nullptr) {
    CloseHandle(in_write_);
    in_write_ = nullptr;
  }
  if (out_read_ != nullptr) {
    CloseHandle(out_read_);
    out_read_ = nullptr;
  }
}

}  // namespace

std::unique_ptr<ShellSession> make_shell_session(std::string* err) {
  (void)err;
  return std::unique_ptr<ShellSession>(new ConPtySession());
}

}  // namespace tea

#endif  // _WIN32
