#include "dr_api.h"
#include "drmgr.h"

#include <stddef.h>

/*
 * Fast callsite-coverage tracer.
 *
 * Goal:
 *   Record whether each indirect callsite executed at least once.
 *
 * This intentionally does NOT collect runtime call targets.  Keeping target
 * tracing out of the hot path makes this mode suitable for running large test
 * suites and timing-sensitive tests.
 *
 * The CSV keeps the existing four-column schema for compatibility with the
 * current run_suite.py/report.py pipeline.  target_module is written as
 * "<not-recorded>" and target_offset as 0x0.
 */

typedef struct site_state_t {
    app_pc callsite;
    volatile int covered;
    struct site_state_t *next;
} site_state_t;

static file_t log_file = INVALID_FILE;

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


static void
print_module_offset(app_pc pc)
{
    module_data_t *mod = dr_lookup_module(pc);

    if (mod != NULL) {
        const char *name = dr_module_preferred_name(mod);
        size_t offset = (size_t)(pc - mod->start);

        dr_fprintf(log_file,
                   "%s,0x%zx",
                   name != NULL ? name : "<unknown>",
                   offset);

        dr_free_module_data(mod);
    } else {
        dr_fprintf(log_file,
                   "<unknown>,0x%zx",
                   (size_t)(ptr_uint_t)pc);
    }
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


static dr_emit_flags_t
event_app_instruction(void *drcontext,
                      void *tag,
                      instrlist_t *bb,
                      instr_t *instr,
                      bool for_trace,
                      bool translating,
                      void *user_data)
{
    app_pc callsite;
    site_state_t *site;

    if (!instr_is_call_indirect(instr))
        return DR_EMIT_DEFAULT;

    callsite = instr_get_app_pc(instr);

    if (callsite == NULL)
        return DR_EMIT_DEFAULT;

    /*
     * A site_state_t is tied to this instrumentation instance.
     *
     * DynamoRIO may translate the same application address more than once,
     * so duplicate metadata entries are possible.  That is harmless for V1:
     * the CSV consumer already treats callsites as a set.
     */
    site = create_site_state(callsite);

    /*
     * Unlike dr_insert_mbr_instrumentation(), this does not compute or pass
     * the runtime branch target.  We only pass a pointer to this callsite's
     * metadata.
     */
    dr_insert_clean_call(
        drcontext,
        bb,
        instr,
        (void *)mark_covered,
        false,
        1,
        OPND_CREATE_INTPTR(site)
    );

    return DR_EMIT_DEFAULT;
}


static void
flush_covered_sites(void)
{
    site_state_t *site;

    if (log_file == INVALID_FILE)
        return;

    dr_fprintf(
        log_file,
        "caller_module,caller_offset,"
        "target_module,target_offset\n"
    );

    for (site = site_list; site != NULL; site = site->next) {
        if (dr_atomic_load32(&site->covered) == 0)
            continue;

        print_module_offset(site->callsite);

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


static void
event_exit(void)
{
    flush_covered_sites();

    if (log_file != INVALID_FILE) {
        dr_close_file(log_file);
        log_file = INVALID_FILE;
    }

    free_site_list();

    if (site_list_lock != NULL) {
        dr_mutex_destroy(site_list_lock);
        site_list_lock = NULL;
    }

    drmgr_exit();
}


DR_EXPORT void
dr_client_main(client_id_t id, int argc, const char *argv[])
{
    char log_path[MAXIMUM_PATH];
    process_id_t pid;

    dr_set_client_name(
        "icallcov fast indirect-callsite tracer",
        "https://github.com/yibeizzZZ/icallcov"
    );

    if (!drmgr_init())
        DR_ASSERT(false);

    site_list_lock = dr_mutex_create();
    site_list = NULL;

    /*
     * One output file per process avoids parent/helper/child processes
     * overwriting each other's traces.
     */
    pid = dr_get_process_id();

    dr_snprintf(
        log_path,
        sizeof(log_path),
        "dynamic.%d.csv",
        (int)pid
    );

    log_path[sizeof(log_path) - 1] = '\0';

    log_file = dr_open_file(
        log_path,
        DR_FILE_WRITE_OVERWRITE |
        DR_FILE_ALLOW_LARGE
    );

    DR_ASSERT(log_file != INVALID_FILE);

    drmgr_register_exit_event(event_exit);

    drmgr_register_bb_instrumentation_event(
        NULL,
        event_app_instruction,
        NULL
    );
}
