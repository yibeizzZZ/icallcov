#ifndef ICALLCOV_FAST_TRACE_H
#define ICALLCOV_FAST_TRACE_H

#include "dr_api.h"

void fast_trace_init(void);
void fast_trace_insert(void *drcontext, instrlist_t *bb, instr_t *instr);
void fast_trace_fork(void);
void fast_trace_exit(void);

#endif
