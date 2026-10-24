#pragma once
#include_next <unistd.h>

#include <stddef.h>
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

pid_t fork(void);
pid_t setsid(void);
int ptsname_r(int fd, char* buf, size_t buflen);
void _exit(int code);

#ifdef __cplusplus
}
#endif
