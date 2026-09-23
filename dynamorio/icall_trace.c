#include "dr_api.h"
#include "drmgr.h"

#include "edge_trace.h"
#include "fast_trace.h"
#include "trace_common.h"

#include <string.h>

typedef enum { TRACE_FAST, TRACE_EDGE } trace_mode_t;
static trace_mode_t mode = TRACE_FAST;

static void
fail(const char *message)
{
    dr_fprintf(STDERR, "icallcov: %s\n", message);
    /* Full DR exit is unsafe while dr_client_main is still initializing. */
    dr_abort_with_code(1);
}

static void
parse_arguments(int argc, const char *argv[])
{
    /* argv[0] is the client library; no options means fast mode. */
    if (argc == 1)
        return;
    if (argc == 3 && strcmp(argv[1], "-mode") == 0) {
        if (strcmp(argv[2], "fast") == 0) {
            mode = TRACE_FAST;
            return;
        }
        if (strcmp(argv[2], "edge") == 0) {
            mode = TRACE_EDGE;
            return;
        }
    }
    fail("Usage: drrun -c libicall_trace.so [-mode fast|edge] -- program [args]");
}

static dr_emit_flags_t
event_app_instruction(void *drcontext, void *tag, instrlist_t *bb, instr_t *instr,
                      bool for_trace, bool translating, void *user_data)
{
    if (!instr_is_app(instr) || !instr_is_call_indirect(instr) ||
        instr_get_app_pc(instr) == NULL)
        return DR_EMIT_DEFAULT;

    /* Mode dispatch happens when instrumenting, not on every execution. */
    if (mode == TRACE_FAST)
        fast_trace_insert(drcontext, bb, instr);
    else
        edge_trace_insert(drcontext, bb, instr);
    return DR_EMIT_DEFAULT;
}

static void
event_fork(void *drcontext)
{
    /* A fork inherits the parent's descriptor; exec instead reruns client init.
     * Closing our copy leaves the parent's descriptor and trace untouched. */
    trace_output_close();
    if (!trace_output_open())
        fail("could not create child trace file");
    if (mode == TRACE_FAST)
        fast_trace_fork();
}

static void
event_exit(void)
{
    drmgr_unregister_bb_insertion_event(event_app_instruction);
    dr_unregister_fork_init_event(event_fork);
    if (mode == TRACE_FAST)
        fast_trace_exit();
    else
        edge_trace_exit();
    trace_output_close();
    drmgr_exit();
}

DR_EXPORT void
dr_client_main(client_id_t id, int argc, const char *argv[])
{
    dr_set_client_name("icallcov indirect-call tracer",
                       "https://github.com/yibeizzZZ/icallcov");
    parse_arguments(argc, argv);
    if (!drmgr_init())
        fail("could not initialize drmgr");
    if (!trace_output_open()) {
        drmgr_exit();
        fail("could not create trace file");
    }

    if (mode == TRACE_FAST)
        fast_trace_init();
    else
        edge_trace_init();

    if (!drmgr_register_exit_event(event_exit)) {
        event_exit();
        fail("could not register exit callback");
    }
    dr_register_fork_init_event(event_fork);
    if (!drmgr_register_bb_instrumentation_event(NULL, event_app_instruction, NULL)) {
        event_exit();
        fail("could not register instrumentation callback");
    }
}
