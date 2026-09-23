#include "dr_api.h"
#include "drmgr.h"

#include <stddef.h>

static file_t log_file = INVALID_FILE;
static void *log_lock;

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

static void
at_indirect_call(app_pc callsite, app_pc target)
{
    dr_mutex_lock(log_lock);

    print_module_offset(callsite);
    dr_fprintf(log_file, ",");
    print_module_offset(target);
    dr_fprintf(log_file, "\n");

    dr_mutex_unlock(log_lock);
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
    if (instr_is_call_indirect(instr)) {
        /*
         * DynamoRIO passes two arguments to at_indirect_call:
         *
         *   1. address of the indirect-call instruction
         *   2. runtime target address
         */
        dr_insert_mbr_instrumentation(drcontext,
                                      bb,
                                      instr,
                                      (app_pc)at_indirect_call,
                                      SPILL_SLOT_1);
    }

    return DR_EMIT_DEFAULT;
}

static void
event_exit(void)
{
    if (log_file != INVALID_FILE) {
        dr_close_file(log_file);
        log_file = INVALID_FILE;
    }

    if (log_lock != NULL) {
        dr_mutex_destroy(log_lock);
        log_lock = NULL;
    }

    drmgr_exit();
}

DR_EXPORT void
dr_client_main(client_id_t id, int argc, const char *argv[])
{
    dr_set_client_name("icallcov indirect-call tracer",
                       "https://github.com/");

    if (!drmgr_init())
        DR_ASSERT(false);

    log_lock = dr_mutex_create();

    /*
     * For V1, always write dynamic.csv into the process working directory.
     * Later run.py can pass an output path as a client argument.
     */
    log_file = dr_open_file("dynamic.csv",
                            DR_FILE_WRITE_OVERWRITE |
                            DR_FILE_ALLOW_LARGE);

    DR_ASSERT(log_file != INVALID_FILE);

    dr_fprintf(log_file,
               "caller_module,caller_offset,"
               "target_module,target_offset\n");

    drmgr_register_exit_event(event_exit);

    drmgr_register_bb_instrumentation_event(
        NULL,
        event_app_instruction,
        NULL
    );
}
