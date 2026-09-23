#include "edge_trace.h"
#include "trace_common.h"

static void *log_lock;

static void
record_edge(app_pc callsite, app_pc target)
{
    /* Keep complete rows together across application threads. Resolve both
     * addresses now, while the modules involved in this call are loaded. */
    dr_mutex_lock(log_lock);
    file_t file = trace_output_file();
    trace_print_module_offset(file, callsite);
    dr_fprintf(file, ",");
    trace_print_module_offset(file, target);
    dr_fprintf(file, "\n");
    dr_mutex_unlock(log_lock);
}

void
edge_trace_init(void)
{
    log_lock = dr_mutex_create();
}

void
edge_trace_insert(void *drcontext, instrlist_t *bb, instr_t *instr)
{
    /* DynamoRIO passes the callsite and the actual runtime branch target. */
    dr_insert_mbr_instrumentation(drcontext, bb, instr, (void *)record_edge,
                                  SPILL_SLOT_1);
}

void
edge_trace_exit(void)
{
    dr_mutex_destroy(log_lock);
    log_lock = NULL;
}
