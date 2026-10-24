#pragma once
#include <sys/types.h>

#define WNOHANG 1
#define WUNTRACED 2

#define WIFEXITED(status) (((status) & 0x7f) == 0)
#define WEXITSTATUS(status) (((status) & 0xff00) >> 8)
#define WIFSIGNALED(status) (((status) & 0x7f) != 0 && ((status) & 0x7f) != 0x7f)
#define WTERMSIG(status) ((status) & 0x7f)

#ifdef __cplusplus
extern "C" {
#endif

pid_t waitpid(pid_t pid, int* status, int options);

#ifdef __cplusplus
}
#endif
