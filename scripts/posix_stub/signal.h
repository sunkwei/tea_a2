#pragma once
#include_next <signal.h>

#include <sys/types.h>

#ifndef SIGHUP
#define SIGHUP 1
#endif
#ifndef SIGKILL
#define SIGKILL 9
#endif
#ifndef SIGTERM
#define SIGTERM 15
#endif

#ifdef __cplusplus
extern "C" {
#endif

int kill(pid_t pid, int sig);

#ifdef __cplusplus
}
#endif
