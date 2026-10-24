#pragma once
#include <sys/types.h>

#ifdef __cplusplus
extern "C" {
#endif

/* MinGW has no setenv(); on Linux it comes from <stdlib.h>. */
int setenv(const char* name, const char* value, int overwrite);

#ifdef __cplusplus
}
#endif
