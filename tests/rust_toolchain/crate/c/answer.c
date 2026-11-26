#include <stdint.h>
#include <string.h>

/* Six times seven, with one factor taken from the C library so the object needs its headers and symbols. */
int32_t answer(const char *text) { return (int32_t)(strlen(text) * 7); }
