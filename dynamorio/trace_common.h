#ifndef ICALLCOV_TRACE_COMMON_H
#define ICALLCOV_TRACE_COMMON_H

#include "dr_api.h"

/* Owned by the entry point; modes finish writing before output is closed. */
bool trace_output_open(void);
void trace_output_close(void);
file_t trace_output_file(void);

/* Writes two CSV fields; the caller serializes writes when necessary. */
void trace_print_module_offset(file_t file, app_pc pc);

#endif
