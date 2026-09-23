#include "fast_trace.h"
#include "trace_common.h"

typedef struct site_state_t {
    app_pc callsite;
    volatile int covered;
    struct site_state_t *next;
} site_state_t;

/*
 * This lock is used only while DynamoRIO is building/instrumenting code and
 * while maintaining the linked list of site metadata.  It is NOT taken on
 * every indirect-call execution.
 */
static void *site_list_lock;
static site_state_t *site_list;


/*
 * Called from the instrumented application.
 *
 * Hot-path work is intentionally tiny: one atomic store.
 * Repeated executions of the same callsite simply write 1 again.
 */
static void
mark_covered(site_state_t *site)
{
    dr_atomic_store32(&site->covered, 1);
}


static site_state_t *
create_site_state(app_pc callsite)
{
    site_state_t *site;

    site = (site_state_t *)dr_global_alloc(sizeof(site_state_t));
    DR_ASSERT(site != NULL);

    site->callsite = callsite;
    site->covered = 0;

    dr_mutex_lock(site_list_lock);

    site->next = site_list;
    site_list = site;

    dr_mutex_unlock(site_list_lock);

    return site;
}


void
fast_trace_insert(void *drcontext, instrlist_t *bb, instr_t *instr)
{
    /* Metadata is per instrumentation instance, as in the original tracer.
     * Duplicate rows are harmless: consumers treat callsites as a set. */
    site_state_t *site = create_site_state(instr_get_app_pc(instr));

    /* No runtime target computation or lookup on this hot path. */
    dr_insert_clean_call(drcontext, bb, instr, (void *)mark_covered, false, 1,
                         OPND_CREATE_INTPTR(site));
}


static void
flush_covered_sites(void)
{
    site_state_t *site;
    file_t log_file = trace_output_file();

    if (log_file == INVALID_FILE)
        return;

    for (site = site_list; site != NULL; site = site->next) {
        if (dr_atomic_load32(&site->covered) == 0)
            continue;

        trace_print_module_offset(log_file, site->callsite);

        /*
         * Preserve the current CSV schema so run_suite.py/report.py can use
         * this fast tracer without changes.
         *
         * Fast mode measures callsite coverage only.
         */
        dr_fprintf(
            log_file,
            ",<not-recorded>,0x0\n"
        );
    }
}


static void
free_site_list(void)
{
    site_state_t *site = site_list;

    while (site != NULL) {
        site_state_t *next = site->next;
        dr_global_free(site, sizeof(site_state_t));
        site = next;
    }

    site_list = NULL;
}


void
fast_trace_init(void)
{
    site_list_lock = dr_mutex_create();
    site_list = NULL;
}

void
fast_trace_fork(void)
{
    /* Cached instrumentation in the child still points to these records.
     * Keep the records, but discard coverage inherited from the parent. */
    for (site_state_t *site = site_list; site != NULL; site = site->next)
        dr_atomic_store32(&site->covered, 0);
}

void
fast_trace_exit(void)
{
    flush_covered_sites();
    free_site_list();
    dr_mutex_destroy(site_list_lock);
    site_list_lock = NULL;
}
