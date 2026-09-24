#include "trace_common.h"

#include <stddef.h>

static file_t log_file = INVALID_FILE;

bool
trace_output_open(void)
{
    char path[MAXIMUM_PATH];
    dr_snprintf(path, sizeof(path), "dynamic.%d.csv", (int)dr_get_process_id());
    path[sizeof(path) - 1] = '\0';

    log_file = dr_open_file(path, DR_FILE_WRITE_OVERWRITE | DR_FILE_ALLOW_LARGE);
    if (log_file == INVALID_FILE)
        return false;

    dr_fprintf(log_file,
               "caller_module,caller_offset,target_module,target_offset\n");
    return true;
}

void
trace_output_close(void)
{
    if (log_file != INVALID_FILE) {
        dr_close_file(log_file);
        log_file = INVALID_FILE;
    }
}

file_t
trace_output_file(void)
{
    return log_file;
}

void
trace_print_module_offset(file_t file, app_pc pc)
{
    module_data_t *mod = dr_lookup_module(pc);
    if (mod != NULL) {
        const char *name = dr_module_preferred_name(mod);
        /* Canonical coordinate: offset from the module mapping start.
         * scan.py subtracts the page-aligned lowest ELF PT_LOAD virtual
         * address, giving the same offset for PIE and non-PIE binaries.
         * report.py adds that ELF base back only for symbolization. */
        size_t offset = (size_t)(pc - mod->start);
        dr_fprintf(file, "%s,0x%zx", name != NULL ? name : "<unknown>", offset);
        dr_free_module_data(mod);
    } else {
        /* Preserve the existing fallback for addresses outside known modules. */
        dr_fprintf(file, "<unknown>,0x%zx", (size_t)(ptr_uint_t)pc);
    }
}
