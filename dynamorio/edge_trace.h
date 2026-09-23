#ifndef ICALLCOV_EDGE_TRACE_H
#define ICALLCOV_EDGE_TRACE_H

#include "dr_api.h"

void edge_trace_init(void);
void edge_trace_insert(void *drcontext, instrlist_t *bb, instr_t *instr);
void edge_trace_exit(void);

#endif
